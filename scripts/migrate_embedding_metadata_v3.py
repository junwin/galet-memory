"""Offline, explicit migration to the review-candidate resource metadata schema.

No imports from galet-memory: this tool can inspect an old deployment without
opening it through an adapter that silently changes its schema.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import sys


DDL = Path(__file__).resolve().parents[1] / 'src' / 'galet_memory' / 'schema' / 'embedding_metadata_v3.sql'


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('relative_path must be a nonempty string')
    if '\\' in value or ':' in value or PureWindowsPath(value).is_absolute():
        raise ValueError('relative_path must use portable relative POSIX syntax')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or value == '.':
        raise ValueError('absolute paths and parent traversal are forbidden')
    return path.as_posix()


def resolve_resource(roots: dict, alias: str, path: str) -> Path:
    """Resolve ONLY host-authorized aliases, with realpath containment."""
    if alias not in roots:
        raise ValueError(f'unknown or unauthorized external_root: {alias!r}')
    configured = Path(roots[alias])
    if not configured.is_absolute():
        raise ValueError('configured roots must be absolute')
    root = configured.resolve(strict=True)
    resolved = (root / relative_path(path)).resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError('source escapes external_root (possibly through a symlink)')
    if not resolved.is_file():
        raise ValueError('source must be a file')
    return resolved


def keywords_from_text(text: str) -> list[str]:
    # Local Unicode token frequency: no network/provider required for backfill.
    # A deliberately small stop list; applications can replace extraction later.
    stop = set('the and for that this with from are was were has have not but you your'.split())
    words = [w for w in re.findall(r'[^\W_]{3,}', text.casefold()) if w not in stop]
    return [word for word, _ in Counter(words).most_common(20)]


def string_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f'{field} must be an array of nonempty strings')
    return value


def prepare(row: dict, config: dict, roots: dict) -> dict:
    metadata = json.loads(row.get('source_metadata') or '{}')
    if not isinstance(metadata, dict):
        raise ValueError('source_metadata must be an object')
    override = config.get('overrides', {}).get(row['id'], {})
    mapping = config.get('namespaces', {}).get(row.get('namespace', ''), {})
    location = {**mapping, **override}
    kind = override.get('resource_type') or metadata.get('resource_type')
    if not kind:
        kind = 'digest' if row.get('source_type') == 'digest' else 'file'
        if not metadata.get('path') and not location and isinstance(metadata.get('text'), str):
            kind = 'inline'
    text = metadata.get('text')
    if text is not None and not isinstance(text, str):
        raise ValueError('stored text must be a string')
    base = location.get('source_id_base')
    if base is not None and base not in ('external_root', 'namespace_prefix'):
        raise ValueError('source_id_base must be external_root or namespace_prefix')
    # An explicit migration rule outranks legacy locator fields. In particular,
    # stale absolute metadata.path must not prevent a configured journal prefix.
    if base is not None:
        alias = override.get('external_root') or mapping.get('external_root')
    else:
        alias = override.get('external_root') or metadata.get('external_root') or mapping.get('external_root')
    path = override.get('relative_path')
    if kind == 'inline':
        alias = path = None
        if not text or not text.strip():
            raise ValueError('inline source requires stored text')
    else:
        if path is None and base is not None:
            source = relative_path(row.get('source_id', ''))
            if base == 'external_root':
                path = source
            else:
                prefix = relative_path(location['prefix'])
                path = source if source == prefix or source.startswith(prefix + '/') else prefix + '/' + source
        if path is None:
            path = metadata.get('relative_path')
        # With no explicit source-ID rule, retain the previous metadata handling.
        legacy = metadata.get('path')
        if path is None and legacy:
            old = Path(legacy)
            if old.is_absolute():
                if alias not in roots:
                    raise ValueError('absolute legacy path requires an explicit authorized root mapping')
                path = old.resolve(strict=True).relative_to(Path(roots[alias]).resolve(strict=True)).as_posix()
            else:
                raise ValueError('legacy relative metadata.path needs an explicit relative_path override')
        if path is None:
            raise ValueError('specify source_id_base or a per-record relative_path; refusing to guess')
        path = relative_path(path)
        resolved = resolve_resource(roots, alias, path)
        if text is None and resolved.suffix.lower() in ('.md', '.txt'):
            text = resolved.read_text(encoding='utf-8')
    stored_keywords = row.get('keywords')
    keywords = json.loads(stored_keywords) if stored_keywords else metadata.get('keywords', [])
    keywords = string_list(keywords, 'keywords')
    if not keywords:
        if text is None:
            raise ValueError('keyword backfill requires stored text or a supported text source; PDF needs extracted text')
        keywords = keywords_from_text(text)
    event_ids = string_list(override.get('source_event_ids', metadata.get('source_event_ids', [])), 'source_event_ids')
    session = override.get('session_id', metadata.get('session_id'))
    digest = override.get('digest_id', metadata.get('digest_id'))
    if kind == 'digest' and (not session or not digest):
        raise ValueError('digest requires explicit session_id and digest_id')
    if kind == 'event_fragment' and (not session or not event_ids):
        raise ValueError('event_fragment requires session_id and ordered source_event_ids')
    return dict(
        id=row['id'], account_name=row['account_name'], namespace=row.get('namespace') or '',
        source_type=row.get('source_type') or '', source_id=row.get('source_id') or '',
        document_id=row.get('document_id') or '', resource_type=kind,
        external_root=alias, relative_path=path, session_id=session, digest_id=digest,
        source_event_ids=json.dumps(event_ids),
        representation=override.get('representation', 'digest' if kind == 'digest' else 'fragment' if kind == 'event_fragment' else 'original'),
        text=text, keywords=json.dumps(keywords, ensure_ascii=False),
        model=row.get('model') or metadata.get('embedding_model') or '',
        provider=row.get('provider') or metadata.get('embedding_provider') or '',
        dimensions=row.get('dimensions') or row.get('dims') or config.get('legacy_dimensions'),
        source_metadata=json.dumps(metadata, ensure_ascii=False), created_at=row['created_at'],
    )


def migrate(source: Path, destination: Path, config: dict, roots: dict, *, dry_run: bool = True) -> dict:
    if destination.exists():
        raise FileExistsError(f'refusing to overwrite {destination}')
    if not source.is_file():
        raise FileNotFoundError(source)
    conn = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    # Hold a consistent snapshot through validation and backup.
    conn.execute('BEGIN')
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'embedding_schema_version' in tables or 'embedding_metadata_legacy' in tables:
            raise ValueError('source is already migrated or contains a conflicting migration table')
        rows = [dict(r) for r in conn.execute('SELECT * FROM embedding_metadata ORDER BY id')]
        # Verify every prepared row against the actual DDL even in dry-run mode.
        check = sqlite3.connect(':memory:')
        check.executescript(DDL.read_text())
        prepared, errors = [], []
        try:
            for row in rows:
                try:
                    record = prepare(row, config, roots)
                    columns = ','.join(record)
                    check.execute(f'INSERT INTO embedding_metadata ({columns}) VALUES ({",".join("?" for _ in record)})', tuple(record.values()))
                    prepared.append(record)
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
                    errors.append({'id': row.get('id'), 'namespace': row.get('namespace'),
                                   'source_id': row.get('source_id'), 'reason': str(exc)})
        finally:
            check.close()
        report = {'schema_version': 3, 'dry_run': dry_run, 'total': len(rows),
                  'ready': len(prepared), 'unresolved': errors, 'written': False}
        if dry_run or errors:
            return report
        # Exclusive creation prevents accidental overwrite, including racing runs.
        with destination.open('xb'):
            pass
        output = None
        try:
            output = sqlite3.connect(destination)
            conn.backup(output)  # preserves vectors and their IDs byte-for-byte
            output.executescript('BEGIN IMMEDIATE; ALTER TABLE embedding_metadata RENAME TO embedding_metadata_legacy;\n' + DDL.read_text())
            for record in prepared:
                output.execute(f'INSERT INTO embedding_metadata ({",".join(record)}) VALUES ({",".join("?" for _ in record)})', tuple(record.values()))
            count = output.execute('SELECT count(*) FROM embedding_metadata').fetchone()[0]
            if count != len(rows):
                raise ValueError('metadata row count mismatch')
            output.commit()
            report['written'] = True
        except BaseException:
            if output is not None:
                output.close()
                output = None
            destination.unlink(missing_ok=True)
            raise
        finally:
            if output is not None:
                output.close()
        return report
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--mapping', required=True, type=Path)
    parser.add_argument('--roots', required=True, type=Path, help='JSON containing authorized external_roots')
    parser.add_argument('--apply', action='store_true', help='write a new DB; default is dry-run')
    args = parser.parse_args()
    try:
        config = json.loads(args.mapping.read_text())
        roots = json.loads(args.roots.read_text())['external_roots']
        report = migrate(args.source, args.destination, config, roots, dry_run=not args.apply)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 2 if report['unresolved'] else 0
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
