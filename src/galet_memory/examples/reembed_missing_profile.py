"""Rescan scoped v3 files with missing profiles into a separate database copy."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from ..assembly import _CheckedEmbeddings, galet_embedding_factory
from ..configuration import ResourceLocation, load_memory_config
from ..ports import SimpleKeywordExtractor, StoredEmbedding
from ..ports.sqlite_vec import load_sqlite_vec
from ..ports.sqlite_resource import SqliteVecResourceIndex
from ..resources import ConfinedTextLoader, RootResourceResolver


def rescan(config, *, source, destination, account, namespace, expected_count=None,
           apply=False, provider_factory=None, credential_path=None):
    if config.semantic is None:
        raise ValueError('semantic configuration is required')
    if not account or not namespace:
        raise ValueError('account and namespace are required')
    if apply and (type(expected_count) is not int or expected_count < 1):
        raise ValueError('--apply requires a positive --expected-count')
    source, destination = Path(source).resolve(strict=True), Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f'refusing to overwrite {destination}')
    if not destination.parent.is_dir():
        raise ValueError('destination parent must exist')
    profile = config.embedding_profiles[config.semantic.embedding_profile]
    if profile.dimensions != 1536:
        raise ValueError('v3 requires 1536-dimensional embeddings')
    resolver = RootResourceResolver(config.external_roots)
    loader = ConfinedTextLoader(resolver)
    extractor = SimpleKeywordExtractor()
    report = {'dry_run': not apply, 'source': str(source), 'destination': str(destination),
              'account': account, 'namespace': namespace, 'target_profile': {
                  'provider': profile.provider, 'model': profile.model, 'dimensions': profile.dimensions},
              'selected': 0, 'unchanged': 0, 'updated': 0, 'written': False, 'errors': []}
    # Keep one read-only SQLite snapshot through planning and backup, including WAL.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
        load_sqlite_vec(original)
        original.row_factory = sqlite3.Row
        original.execute('BEGIN')
        version = original.execute("SELECT version FROM embedding_schema_version WHERE component='embedding_metadata'").fetchone()
        if version is None or version[0] != 3:
            raise ValueError('rescan requires a migrated v3 database')
        rows = original.execute('SELECT * FROM embedding_metadata ORDER BY id').fetchall()
        selected = [r for r in rows if r['account_name'] == account and r['namespace'] == namespace
                    and (not r['provider'] or not r['model'])]
        ids = {r['id'] for r in selected}
        report['selected'], report['unchanged'] = len(selected), len(rows) - len(selected)
        if expected_count is not None and len(selected) != expected_count:
            report['errors'].append(f'expected {expected_count} selected records, found {len(selected)}')
        for row in rows:
            if row['id'] not in ids and (row['provider'], row['model'], row['dimensions']) != profile.space:
                report['errors'].append(f"unselected record has an incompatible or unknown profile: {row['id']}")
        inputs = []
        for row in selected:
            try:
                if row['resource_type'] != 'file' or row['representation'] != 'original':
                    raise ValueError('only original file resources can be rescanned')
                path = resolver.resolve(ResourceLocation(row['external_root'], row['relative_path']))
                snippet = loader.load(path, max_chars=config.semantic.max_chars + 1)
                if snippet.truncated or len(snippet.text) > config.semantic.max_chars:
                    raise ValueError('file exceeds configured max_chars')
                if not snippet.text.strip():
                    raise ValueError('file is empty')
                keywords = list(extractor.extract(snippet.text, top_n=config.semantic.max_keywords))
                inputs.append((row, snippet.text, keywords))
            except (OSError, ValueError) as exc:
                report['errors'].append(f"{row['id']}: {exc}")
        if report['errors'] or not apply:
            return report
        if not selected:
            raise ValueError('no missing-profile records selected')
        # Nothing touches the requested destination until the entire copy validates.
        with TemporaryDirectory(prefix='.memory-rescan-', dir=destination.parent) as temporary:
            staged = Path(temporary) / 'rescanned.sqlite'
            with closing(sqlite3.connect(staged)) as target:
                original.backup(target)
            if provider_factory is None:
                from galet.settings import Settings
                provider_factory = galet_embedding_factory(Settings(credential_path=credential_path))
            embeddings = _CheckedEmbeddings(provider_factory(profile), profile)
            with SqliteVecResourceIndex(staged, initialize_schema=False) as index:
                for offset in range(0, len(inputs), 16):
                    batch = inputs[offset:offset + 16]
                    vectors = embeddings.embed([text for _, text, _ in batch], model=profile.model)
                    for (row, text, keywords), vector in zip(batch, vectors):
                        record = index.get_record(account_name=account, namespace=namespace, record_id=row['id'])
                        metadata = dict(record.metadata)
                        metadata.update(content_hash=hashlib.sha256(text.encode()).hexdigest(),
                                        embedding_model=profile.model)
                        index.upsert(StoredEmbedding(id=record.id, account_name=account, namespace=namespace,
                            vector=vector, source_type=record.source_type, source_id=record.source_id,
                            document_id=record.document_id, model=profile.model, provider=profile.provider,
                            metadata=metadata, created_at=record.created_at, keywords=keywords,
                            resource=replace(record.resource, text=text)))
                # Check IDs and every untouched vector byte-for-byte against the snapshot.
                old_vectors = {r['id']: r['embedding'] for r in original.execute('SELECT id,embedding FROM vec_embeddings_v2')}
                new_vectors = dict(index._conn.execute('SELECT id,embedding FROM vec_embeddings_v2'))
                if old_vectors.keys() != new_vectors.keys():
                    raise RuntimeError('rescan changed the embedding IDs')
                if any(old_vectors[k] != new_vectors[k] for k in old_vectors.keys() - ids):
                    raise RuntimeError('rescan changed an unselected vector')
                old_metadata = {r['id']: tuple(r) for r in rows if r['id'] not in ids}
                untouched = index._conn.execute('SELECT * FROM embedding_metadata ORDER BY id').fetchall()
                if any(tuple(r) != old_metadata[r[0]] for r in untouched if r[0] not in ids):
                    raise RuntimeError('rescan changed unselected metadata')
                spaces = index._conn.execute('SELECT DISTINCT provider,model,dimensions FROM embedding_metadata').fetchall()
                if spaces != [profile.space]:
                    raise RuntimeError('not all active records match the selected embedding profile')
                if index._conn.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise RuntimeError('rescanned copy failed integrity check')
            # Reopen to validate metadata/vector ID correspondence before publishing.
            with SqliteVecResourceIndex(staged, initialize_schema=False):
                pass
            os.link(staged, destination)  # atomic, exclusive creation on the same filesystem
            report.update(updated=len(selected), written=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='otherwise GALET_MEMORY_CONFIG_PATH')
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--account', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--expected-count', type=int)
    parser.add_argument('--credential-path')
    parser.add_argument('--apply', action='store_true', help='make paid embedding calls and publish a new copy')
    args = parser.parse_args(argv)
    try:
        report = rescan(load_memory_config(path=args.config), source=args.source,
            destination=args.destination, account=args.account, namespace=args.namespace,
            expected_count=args.expected_count, apply=args.apply, credential_path=args.credential_path)
        print(json.dumps(report, indent=2))
        return 1 if report['errors'] else 0
    except Exception as exc:
        print(json.dumps({'written': False, 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
