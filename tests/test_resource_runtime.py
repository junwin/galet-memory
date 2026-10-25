from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from galet_memory import build_memory, load_memory_config
from galet_memory.configuration import MemoryConfigurationError
from galet_memory.examples.configured_memory import OfflineSampleProvider, initialize_sample
from galet_memory.examples.integration_runner import run_checks, main
from galet_memory.ports import EmbeddingResource, StoredEmbedding, EmbeddingCompatibilityError, SqliteVecEmbeddingIndex
from galet_memory.ports.sqlite_resource import SqliteVecResourceIndex


@pytest.fixture
def config(tmp_path):
    path = initialize_sample(tmp_path / 'sample')
    raw = json.loads(path.read_text())
    raw['memory']['semantic']['metadata_version'] = 3
    raw['memory']['semantic']['database']['relative_path'] = 'v3.sqlite'
    path.write_text(json.dumps(raw))
    return load_memory_config(path=path)


def test_v3_round_trip_namespace_independence_and_reopen(config):
    class Counting(OfflineSampleProvider):
        count = 0
        def embed(self, texts, *, model):
            type(self).count += 1
            return super().embed(texts, model=model)
    with build_memory(config, embedding_provider_factory=Counting) as m:
        added = m.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md', namespace='books')
        before = m._index._conn.execute('SELECT embedding FROM vec_embeddings_v2').fetchone()[0]
        again = m.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md', namespace='garden')
        assert again.status == 'unchanged' and added.record_id == again.record_id
        assert Counting.count == 1
        assert m.recall(account_name='john', query='orchard', namespaces=['garden'], score_threshold=0).documents
        assert m._index._conn.execute('SELECT embedding FROM vec_embeddings_v2').fetchone()[0] == before
        assert m.recall(account_name='john', query='orchard', namespaces=['books'], use_embeddings=False).documents == []
        result = m.recall(account_name='john', query='orchard', use_embeddings=False)
        assert result.documents[0].metadata['relative_path'] == 'orchard.md'
        assert result.documents[0].path == str(config.external_roots['notes'] / 'orchard.md')
        m.ingestion.ingest_text(account_name='john', source_id='inline-1', text='Silver orchard trees', namespace='')
        assert not m.recall(account_name='arla', query='orchard').documents
    with build_memory(config, embedding_provider_factory=Counting) as m:
        result = m.recall(account_name='john', query='orchard', use_embeddings=False)
        assert len(result.documents) == 2
        assert any(d.source_id == 'inline-1' and d.path is None for d in result.documents)
        assert all('keywords' in r.metadata for r in m._index.list_records(account_name='john', namespace='garden'))


def test_legacy_json_cannot_override_locator_or_restore_missing_file(config):
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as m:
        added = m.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        m._index._conn.execute("UPDATE embedding_metadata SET source_metadata=? WHERE id=?",
            (json.dumps({'path': '/etc/passwd', 'text': 'cached secret', 'external_root': 'other',
                         'relative_path': 'bad', 'keywords': ['secret']}), added.record_id))
        result = m.recall(account_name='john', query='orchard', use_embeddings=False)
        assert 'apple trees' in result.documents[0].snippet
        (config.external_roots['notes'] / 'orchard.md').unlink()
        result = m.recall(account_name='john', query='orchard', use_embeddings=False)
        assert result.documents == []
        assert result.metadata['unavailable_resources'][0]['reason'] == 'FileNotFoundError'


def test_resolver_reauthorizes_exact_alias_and_symlink(config, tmp_path):
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as m:
        m.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        path = config.external_roots['notes'] / 'orchard.md'
        outside = tmp_path / 'outside.md'
        outside.write_text('secret')
        path.unlink()
        path.symlink_to(outside)
        assert not m.recall(account_name='john', query='orchard', use_embeddings=False).documents
        with pytest.raises(PermissionError):
            m.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        with pytest.raises(ValueError):
            m.ingest_file(account_name='john', external_root='notes', relative_path='../outside.md')


def test_adapter_version_guards_do_not_alter_database(config):
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider):
        pass
    db = config.external_roots['memory_data'] / 'v3.sqlite'
    before = db.read_bytes()
    for initialize in (True, False):
        with pytest.raises(EmbeddingCompatibilityError, match='v3'):
            SqliteVecEmbeddingIndex(db, initialize_schema=initialize)
    assert db.read_bytes() == before
    old = db.parent / 'v2.sqlite'
    with SqliteVecEmbeddingIndex(old):
        pass
    before = old.read_bytes()
    with pytest.raises(EmbeddingCompatibilityError, match='migration'):
        SqliteVecResourceIndex(old)
    assert old.read_bytes() == before
    incompatible = replace(config, semantic=replace(config.semantic, metadata_version=2))
    with pytest.raises(MemoryConfigurationError, match='mismatch'):
        build_memory(incompatible, embedding_provider_factory=OfflineSampleProvider)


def test_v3_writes_require_keywords_and_explicit_resource_and_rollback(config):
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as m:
        e = StoredEmbedding(id='id', account_name='john', namespace='', vector=[1.0]+[0.0]*1535,
                            source_id='inline', resource=EmbeddingResource('inline', text='hello'), keywords=['hello'])
        with pytest.raises(ValueError, match='keywords'):
            m._index.upsert(replace(e, keywords=None))
        with pytest.raises(ValueError, match='EmbeddingResource'):
            m._index.upsert(replace(e, resource=None))
        m._index.upsert(e)
        with pytest.raises(sqlite3.IntegrityError):
            m._index.upsert(replace(e, resource=EmbeddingResource('inline', text=None)))
        assert m._index.get_record(account_name='john', namespace='', record_id='id').resource.text == 'hello'
        with pytest.raises(PermissionError):
            m._index.upsert(replace(e, account_name='arla'))
        m._index.store_keywords(account_name='john', namespace='', record_id='id', keywords=[])
        assert not m._index.query_by_keywords(account_name='john', namespaces=[''], keywords=['hello'], limit=3)


def test_unsupported_digests_fail_closed(config):
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as m:
        m._index.upsert(StoredEmbedding(id='digest', account_name='john', namespace='external',
            source_type='digest', source_id='session', vector=[1.0]+[0.0]*1535, keywords=['orchard'],
            resource=EmbeddingResource('digest', 'memory_data', 'episodic.sqlite',
                session_id='session', digest_id='digest', text='must not leak')))
        result = m.recall(account_name='john', query='orchard', use_embeddings=False)
        assert not result.documents
        assert result.metadata['unavailable_resources'][0]['reason'] == 'unsupported_resource_type'


def test_real_migration_recall_and_refresh_preserve_identity(config, tmp_path):
    old, new = tmp_path / 'old.sqlite', tmp_path / 'migrated.sqlite'
    profile = config.embedding_profiles[config.semantic.embedding_profile]
    vector = OfflineSampleProvider(profile).embed(['orchard'], model=profile.model)[0]
    with SqliteVecEmbeddingIndex(old) as index:
        index.upsert(StoredEmbedding(id='historical-id', account_name='john', namespace='books',
            source_type='notes', source_id='legacy-name', document_id='original-document',
            vector=vector, provider=profile.provider, model=profile.model,
            metadata={'path': str(config.external_roots['notes'] / 'orchard.md'), 'keywords': ['orchard']}))
    spec = importlib.util.spec_from_file_location('migration', Path(__file__).parents[1] / 'scripts/migrate_embedding_metadata_v3.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    roots = {k: str(v) for k,v in config.external_roots.items()}
    mapping = {'overrides': {'historical-id': {'external_root': 'notes', 'relative_path': 'orchard.md'}}}
    before = old.read_bytes()
    assert migration.migrate(old, new, mapping, roots, dry_run=True)['written'] is False
    assert not new.exists()
    assert migration.migrate(old, new, mapping, roots, dry_run=False)['written']
    assert old.read_bytes() == before
    from galet_memory.configuration import DatabaseSettings, ResourceLocation
    migrated_config = replace(config, external_roots={**config.external_roots, 'test': tmp_path},
        semantic=replace(config.semantic, database=DatabaseSettings(ResourceLocation('test', 'migrated.sqlite'))))
    with build_memory(migrated_config, embedding_provider_factory=OfflineSampleProvider) as m:
        assert m.recall(account_name='john', query='orchard', use_embeddings=False).documents
        record = m._index.get_record(account_name='john', namespace='books', record_id='historical-id')
        assert m._index._conn.execute('SELECT embedding FROM vec_embeddings_v2').fetchone()[0] == __import__('struct').pack('<1536f', *vector)
        refreshed = m.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md', namespace='garden')
        assert refreshed.record_id == 'historical-id'
        after = m._index.get_record(account_name='john', namespace='garden', record_id='historical-id')
        assert after.document_id == 'original-document' and after.created_at == record.created_at
        assert after.source_id == 'legacy-name'


def test_runner_uses_config_with_isolated_stores_and_sources(config, tmp_path):
    before = set(config.external_roots['memory_data'].rglob('*'))
    report = run_checks(config, work_dir=tmp_path / 'run', source_root='notes', source_file='orchard.md', query='orchard')
    assert report['ok'], report
    assert report['metadata_version'] == 3
    assert report['embedding_profile']['provider'] == 'sample'
    assert set(config.external_roots['memory_data'].rglob('*')) == before
    assert json.loads((tmp_path / 'run/report.json').read_text())['ok']
    with pytest.raises(FileExistsError):
        run_checks(config, work_dir=tmp_path / 'run')
    failed = run_checks(config, work_dir=tmp_path / 'bad', source_root='notes', source_file='missing.md', query='orchard')
    assert not failed['ok'] and failed['error']


def test_runner_cli_honors_environment_and_failure_exit(tmp_path, monkeypatch, capsys):
    path = initialize_sample(tmp_path / 'sample')
    monkeypatch.setenv('GALET_MEMORY_CONFIG_PATH', str(path))
    assert main(['--work-dir', str(tmp_path / 'run')]) == 0
    assert json.loads(capsys.readouterr().out)['ok']
    assert main(['--work-dir', str(tmp_path / 'run')]) == 2


def test_v3_open_rejects_orphaned_metadata(config):
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as m:
        m.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        m._index._conn.execute('DELETE FROM vec_embeddings_v2')
    with pytest.raises(EmbeddingCompatibilityError, match='IDs differ'):
        build_memory(config, embedding_provider_factory=OfflineSampleProvider)


def test_live_runner_keeps_the_configured_profile(config, tmp_path, monkeypatch):
    from galet_memory.examples import integration_runner
    profile = replace(config.embedding_profiles[config.semantic.embedding_profile],
                      provider='openai', model='text-embedding-3-small')
    profiles = {**config.embedding_profiles, 'real': profile}
    live_config = replace(config, embedding_profiles=profiles,
                          semantic=replace(config.semantic, embedding_profile='real'))
    seen = []
    class Provider:
        def __init__(self, selected):
            self.profile = selected
            seen.append(selected)
        def embed(self, texts, *, model):
            assert model == 'text-embedding-3-small'
            return [[1.0] + [0.0] * 1535 for _ in texts]
    monkeypatch.setattr(integration_runner, 'galet_embedding_factory', lambda settings: Provider)
    report = run_checks(live_config, work_dir=tmp_path / 'live', live=True)
    assert report['ok'], report
    assert seen and all(p == profile for p in seen)
    assert report['embedding_profile']['provider'] == 'openai'


def test_v3_uses_default_keywords_only_from_column(config):
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as m:
        added = m.ingestion.ingest_text(account_name='john', source_id='inline', text='orchard', namespace='')
        m._index._conn.execute('UPDATE embedding_metadata SET keywords=\'[]\',source_metadata=? WHERE id=?',
                              (json.dumps({'keywords': ['orchard'], 'text': 'wrong'}), added.record_id))
        assert not m.recall(account_name='john', query='orchard', use_embeddings=False).documents
