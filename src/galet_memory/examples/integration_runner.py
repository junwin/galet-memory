"""Run high-level checks through a supplied config in a NEW isolated workspace."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path

from ..assembly import build_memory, galet_embedding_factory
from ..configuration import (DatabaseSettings, EmbeddingProfile, MemoryConfigurationError,
                             ResourceLocation, load_memory_config)
from ..episodic import NewEvent
from ..procedural import ProceduralMemoryRequest
from .configured_memory import OfflineSampleProvider


def run_checks(config, *, work_dir, account='integration', source_root=None,
               source_file=None, query=None, live=False, credential_path=None, metadata_version=None):
    """Retain source roots/profile/options, redirect ALL writable components.

    Never opens configured databases or writes into source/procedural roots.
    Explicit --live retains the real profile; offline vectors use their own
    declared sample identity, never a real provider's model name.
    """
    if config.semantic is None:
        raise MemoryConfigurationError('integration runner requires semantic memory')
    if metadata_version is not None:
        config = replace(config, semantic=replace(config.semantic, metadata_version=metadata_version))
    if bool(source_root) != bool(source_file):
        raise ValueError('source_root and source_file must be supplied together')
    if source_root is not None and (not query or not query.strip()):
        raise ValueError('a nonempty query is required when testing an existing source')
    output = Path(work_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name in ('data', 'digests', 'procedural', 'fixture'):
        (output / name).mkdir()
    roots = dict(config.external_roots)
    alias = '__integration__'
    while alias in roots:
        alias += '_'
    roots[alias] = output
    if source_root is None:
        source_root, source_file = alias, 'fixture/orchard.md'
        (output / source_file).write_text('Copper finches nest in the orchard garden.\n', encoding='utf-8')
        query = query or 'copper finches orchard'
    elif not query or not query.strip():
        raise ValueError('a nonempty query is required when testing an existing source')
    def db(name):
        return DatabaseSettings(ResourceLocation(alias, 'data/' + name + '.sqlite'), True)
    profiles = dict(config.embedding_profiles)
    profile_name = config.semantic.embedding_profile
    if not live:
        profile_name = '__integration_offline__'
        while profile_name in profiles:
            profile_name += '_'
        profiles[profile_name] = EmbeddingProfile('sample', 'offline-hash-v1', 1536)
    effective = replace(config, external_roots=roots, embedding_profiles=profiles,
        semantic=replace(config.semantic, database=db('semantic'), embedding_profile=profile_name),
        episodic=replace(config.episodic, database=db('episodic'),
                         digests_directory=ResourceLocation(alias, 'digests')) if config.episodic else None,
        procedural=replace(config.procedural, root=ResourceLocation(alias, 'procedural')) if config.procedural else None,
        working=replace(config.working, database=db('working')) if config.working else None)
    if live:
        from galet.settings import Settings
        factory = galet_embedding_factory(Settings(credential_path=credential_path))
    else:
        factory = OfflineSampleProvider
    report = {'ok': False, 'workspace': str(output), 'metadata_version': effective.semantic.metadata_version,
              'live': live, 'embedding_profile': asdict(profiles[profile_name]), 'checks': []}
    def check(name, condition):
        report['checks'].append({'name': name, 'passed': bool(condition)})
    def recall(memory, embeddings=True):
        return memory.recall(account_name=account, query=query, use_embeddings=embeddings,
                             score_threshold=0, top_k=10)
    try:
        with build_memory(effective, embedding_provider_factory=factory) as memory:
            added = memory.ingest_file(account_name=account, external_root=source_root,
                                       relative_path=source_file)
            again = memory.ingest_file(account_name=account, external_root=source_root,
                                       relative_path=source_file)
            check('ingestion_added', added.status == 'added')
            check('repeat_unchanged', again.status == 'unchanged' and added.record_id == again.record_id)
            vector, keyword = recall(memory), recall(memory, False)
            check('vector_recall', bool(vector.documents))
            check('keyword_recall', bool(keyword.documents) and keyword.metadata.get('backend') == 'keyword')
            check('account_isolation', not memory.recall(account_name=account + '-other', query=query).documents)
            report['sources'] = [{'source_id': d.source_id, 'path': d.path,
                'external_root': d.metadata.get('external_root'),
                'relative_path': d.metadata.get('relative_path')} for d in vector.documents]
            check('source_location', any(s['external_root'] == source_root and s['relative_path'] == source_file
                                         for s in report['sources']))
            if effective.semantic.metadata_version == 3:
                moved = memory.ingest_file(account_name=account, external_root=source_root,
                    relative_path=source_file, namespace='integration-moved')
                check('namespace_independent_identity', moved.record_id == added.record_id and moved.status == 'unchanged')
                memory.ingestion.ingest_text(account_name=account, source_id='integration-inline',
                    text='Inline copper orchard marker', namespace='inline-check')
                inline = memory.recall(account_name=account, query='copper orchard',
                    namespaces=['inline-check'], use_embeddings=False, score_threshold=0)
                check('inline_recall', bool(inline.documents) and inline.documents[0].path is None)
            if memory.episodic:
                memory.episodic.create_session(account_name=account, session_id='integration-session')
                memory.episodic.append_event(account_name=account, session_id='integration-session',
                                            event=NewEvent('user', 'integration marker', account))
            if memory.procedural:
                memory.procedural.repository.save_context(account_name=account, context_name='integration',
                                                          text='Integration context marker')
            if memory.working:
                memory.working.start_run(account, 'integration-run')
                memory.working.put(account, 'integration-run', 'marker', 'present', expected_version=0)
        with build_memory(effective, embedding_provider_factory=factory) as memory:
            check('semantic_reopen', bool(recall(memory).documents) and bool(recall(memory, False).documents))
            if memory.episodic:
                events = memory.episodic.get_recent_events(account_name=account,
                    session_id='integration-session', count=5).events
                check('episodic_reopen', len(events) == 1)
            if memory.procedural:
                check('procedural_reopen', memory.procedural.recall(
                    ProceduralMemoryRequest(account, 'integration')).text == 'Integration context marker')
            if memory.working:
                check('working_reopen', memory.working.get(account, 'integration-run', 'marker').value == 'present')
        report['ok'] = all(c['passed'] for c in report['checks'])
    except Exception as exc:
        report['error'] = str(exc)
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='otherwise GALET_MEMORY_CONFIG_PATH')
    parser.add_argument('--work-dir', required=True, type=Path, help='NEW isolated output directory')
    parser.add_argument('--metadata-version', type=int, choices=[2, 3], help='override only the isolated semantic store version')
    parser.add_argument('--account', default='integration')
    parser.add_argument('--source-root')
    parser.add_argument('--source-file')
    parser.add_argument('--query')
    parser.add_argument('--live', action='store_true', help='use configured provider; makes paid embedding calls')
    parser.add_argument('--credential-path')
    args = parser.parse_args(argv)
    try:
        config = load_memory_config(path=args.config)
        report = run_checks(config, work_dir=args.work_dir, account=args.account,
            source_root=args.source_root, source_file=args.source_file, query=args.query,
            live=args.live, credential_path=args.credential_path, metadata_version=args.metadata_version)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
