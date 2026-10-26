from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from galet_memory import (EmbeddingProfile, MemoryConfigurationError, ResourceLocation,
                          NewEvent, ProceduralMemoryRequest, RootResourceResolver,
                          build_memory, configuration_schema, load_memory_config)
from galet_memory import assembly
from galet_memory.examples.configured_memory import OfflineSampleProvider, initialize_sample, main
from galet_memory.ports import EmbeddingCompatibilityError


@pytest.fixture
def sample(tmp_path):
    # The checked-in, packaged JSON is the test fixture: no parallel test shape.
    return initialize_sample(tmp_path / 'sample')


def inline_config(path):
    raw = json.loads(path.read_text())
    for key, filename in [('external_roots', 'roots.json'), ('embedding_profiles', 'embedding-profiles.json')]:
        raw.pop(key + '_file')
        raw[key] = json.loads((path.parent / filename).read_text())[key]
    return raw


def test_sample_loads_all_components_and_defaults_without_opening_storage(sample):
    config = load_memory_config(path=sample)
    assert config.semantic.max_chars == 32000
    assert config.semantic.max_keywords == 20
    assert config.episodic and config.procedural and config.working
    assert config.embedding_profiles['semantic_default'].space == ('openai', 'text-embedding-3-small', 1536)
    assert list((sample.parent / 'data').glob('*.sqlite')) == []
    with pytest.raises(TypeError):
        config.external_roots['other'] = Path('/other')


def test_selection_precedence_and_selected_bad_file_fails(sample, monkeypatch, tmp_path):
    monkeypatch.setenv('GALET_MEMORY_CONFIG_PATH', str(sample))
    assert load_memory_config().semantic
    with pytest.raises(MemoryConfigurationError, match='cannot load'):
        load_memory_config(path=tmp_path / 'missing.json')
    monkeypatch.setenv('GALET_MEMORY_CONFIG_PATH', '/missing/environment.json')
    assert load_memory_config(path=sample).semantic
    assert load_memory_config(inline_config(sample), path='/ignored.json').semantic
    monkeypatch.delenv('GALET_MEMORY_CONFIG_PATH')
    with pytest.raises(MemoryConfigurationError, match='supply config'):
        load_memory_config()


def test_shared_references_are_relative_to_config_not_cwd(sample, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert load_memory_config(path=sample).semantic
    raw = json.loads(sample.read_text())
    with pytest.raises(MemoryConfigurationError, match='base_dir'):
        load_memory_config(raw)
    assert load_memory_config(raw, base_dir=sample.parent).semantic


@pytest.mark.parametrize('mutate', [
    lambda c: c.update(config_version=2),
    lambda c: c.update(config_version=True),
    lambda c: c.update(misspelled_setting=True),
    lambda c: c['memory']['semantic'].update(max_chars=0),
    lambda c: c['memory']['semantic'].update(embedding_profile='missing'),
    lambda c: c['memory']['semantic']['database'].update(external_root='missing'),
    lambda c: c['memory']['semantic']['database'].update(relative_path='../escape.sqlite'),
    lambda c: c['memory']['semantic']['database'].update(relative_path='/absolute.sqlite'),
    lambda c: c['memory']['semantic']['database'].update(relative_path='C:\\escape.sqlite'),
    lambda c: c['memory']['semantic']['keywords'].update(extractor='unknown'),
    lambda c: c['embedding_profiles']['offline_demo'].update(dimensions=True),
    lambda c: c['embedding_profiles']['offline_demo'].update(dimensions=1024),
    lambda c: c['external_roots'].update(notes='relative/root'),
    lambda c: c.update(external_roots_file='roots.json'),
    lambda c: c.update(embedding_profiles_file='embedding-profiles.json'),
    lambda c: c.update(memory={}),
])
def test_invalid_config_fails_before_any_storage_or_provider(sample, mutate):
    raw = inline_config(sample)
    mutate(raw)
    with pytest.raises(MemoryConfigurationError):
        build_memory(raw, embedding_provider_factory=lambda p: pytest.fail('provider must not be called'))
    assert list((sample.parent / 'data').glob('*.sqlite')) == []


def test_duplicate_keys_and_recursive_shared_documents_rejected(sample):
    bad = sample.parent / 'bad.json'
    bad.write_text('{"config_version":1,"config_version":2}')
    with pytest.raises(MemoryConfigurationError, match='duplicate'):
        load_memory_config(path=bad)
    roots = sample.parent / 'roots.json'
    roots.write_text(json.dumps({'config_version': 1, 'external_roots_file': 'roots.json'}))
    with pytest.raises(MemoryConfigurationError):
        load_memory_config(path=sample)


def test_versioned_schema_is_the_validation_contract(sample):
    from jsonschema import Draft202012Validator
    schema = configuration_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(sample.read_text()))


def test_sample_round_trip_real_vec0_keywords_account_scope_and_reopen(sample):
    pytest.importorskip('sqlite_vec')
    config = load_memory_config(path=sample)
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as memory:
        assert memory.episodic and memory.working and memory.procedural
        memory.episodic.create_session(account_name='john', session_id='session-1')
        memory.episodic.append_event(account_name='john', session_id='session-1',
            event=NewEvent('user', 'Remember the orchard', 'john'))
        assert len(memory.episodic.get_recent_events(account_name='john', session_id='session-1', count=5).events) == 1
        memory.procedural.repository.save_context(account_name='john', context_name='garden', text='Keep garden notes')
        assert memory.procedural.recall(ProceduralMemoryRequest('john', 'garden')).text == 'Keep garden notes'
        memory.working.start_run('john', 'run-1')
        memory.working.put('john', 'run-1', 'plan', 'inspect orchard', expected_version=0)
        assert memory.working.get('john', 'run-1', 'plan').value == 'inspect orchard'
        added = memory.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        again = memory.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        assert added.status == 'added' and again.status == 'unchanged'
        assert memory.recall(account_name='john', query='orchard', score_threshold=0).documents
        assert memory.recall(account_name='arla', query='orchard').documents == []
        result = memory.recall(account_name='john', query='orchard', use_embeddings=False)
        assert result.metadata['backend'] == 'keyword'
        assert result.documents[0].metadata['external_root'] == 'notes'
        record = memory._index.list_records(account_name='john', namespace='external')[0]
        assert record.source_id == 'notes:orchard.md'
    with pytest.raises(RuntimeError, match='closed'):
        memory.recall(account_name='john', query='orchard')
    # All connection-owning components close, and the same contract reopens stores.
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as reopened:
        assert reopened.recall(account_name='john', query='orchard').documents
        assert reopened.episodic.get_session(account_name='john', session_id='session-1')
        assert reopened.working.get('john', 'run-1', 'plan').value == 'inspect orchard'
        assert reopened.procedural.recall(ProceduralMemoryRequest('john', 'garden')).text == 'Keep garden notes'
    with sqlite3.connect(sample.parent / 'data/semantic-offline.sqlite') as conn:
        provider, model, dimensions, keywords = conn.execute('SELECT provider,model,dimensions,keywords FROM embedding_metadata').fetchone()
        assert (provider, model, dimensions) == ('sample', 'offline-hash-v1', 1536)
        assert 'orchard' in json.loads(keywords)


def test_existing_model_mismatch_rejected_without_writes_or_provider_calls(sample):
    pytest.importorskip('sqlite_vec')
    raw = inline_config(sample)
    with build_memory(raw, embedding_provider_factory=OfflineSampleProvider) as memory:
        memory.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
    database = sample.parent / 'data/semantic-offline.sqlite'
    before = database.read_bytes()
    raw['embedding_profiles']['offline_demo']['model'] = 'different-model-same-dimensions'
    with pytest.raises(MemoryConfigurationError, match='provider/model/dimensions'):
        build_memory(raw, embedding_provider_factory=lambda p: pytest.fail('mismatch must precede provider construction'))
    assert database.read_bytes() == before


def test_no_semantic_component_needs_no_provider_and_outages_use_keywords(sample):
    raw = inline_config(sample)
    only_working = deepcopy(raw)
    only_working['memory'] = {'working': raw['memory']['working']}
    only_working.pop('embedding_profiles')
    with build_memory(only_working, embedding_provider_factory=lambda p: pytest.fail('no provider required')) as memory:
        assert memory.working is not None and memory.semantic is None
    pytest.importorskip('sqlite_vec')
    with build_memory(raw, embedding_provider_factory=OfflineSampleProvider) as memory:
        memory.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')

        class Unavailable(OfflineSampleProvider):
            def embed(self, texts, *, model):
                raise ConnectionError('offline')

        profile = load_memory_config(raw).embedding_profiles['offline_demo']
        memory.semantic.embeddings = assembly._CheckedEmbeddings(Unavailable(profile), profile)
        result = memory.recall(account_name='john', query='orchard')
        assert result.metadata['backend'] == 'keyword'
        assert result.metadata['reason'] == 'embedding_provider_unavailable'
        assert result.documents


def test_v3_metadata_is_rejected_without_explicit_v3_selection(sample):
    raw = inline_config(sample)
    db = sample.parent / 'data/semantic-offline.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE embedding_schema_version (version INTEGER)')
    with pytest.raises(MemoryConfigurationError, match='metadata version mismatch'):
        build_memory(raw, embedding_provider_factory=OfflineSampleProvider)


def test_default_creation_is_off_and_components_cannot_share_database(sample):
    raw = inline_config(sample)
    del raw['memory']['semantic']['database']['create']
    with pytest.raises(FileNotFoundError):
        build_memory(raw, embedding_provider_factory=OfflineSampleProvider)
    raw['memory']['semantic']['database']['create'] = True
    raw['memory']['working']['database'] = deepcopy(raw['memory']['semantic']['database'])
    with pytest.raises(MemoryConfigurationError, match='distinct database'):
        build_memory(raw, embedding_provider_factory=OfflineSampleProvider)


def test_factory_contract_and_vector_dimensions_cannot_silently_fallback(sample):
    raw = inline_config(sample)
    with pytest.raises(MemoryConfigurationError, match='undeclared profile'):
        build_memory(raw, embedding_provider_factory=lambda p: object())
    assert list((sample.parent / 'data').glob('*.sqlite')) == []

    class BadProvider(OfflineSampleProvider):
        def embed(self, texts, *, model):
            return [[1.0] for _ in texts]

    pytest.importorskip('sqlite_vec')
    with build_memory(raw, embedding_provider_factory=BadProvider) as memory:
        with pytest.raises(EmbeddingCompatibilityError, match='dimensions'):
            memory.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        # Store one valid record, then fail query embedding: this mismatch must
        # raise instead of getting disguised as provider-unavailable fallback.
        memory.ingestion.embeddings = assembly._CheckedEmbeddings(OfflineSampleProvider(
            raw_profile := load_memory_config(raw).embedding_profiles['offline_demo']), raw_profile)
        memory.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        with pytest.raises(EmbeddingCompatibilityError, match='dimensions'):
            memory.recall(account_name='john', query='orchard')


def test_resource_resolver_checks_traversal_symlinks_and_missing_parent(sample, tmp_path):
    config = load_memory_config(path=sample)
    resolver = RootResourceResolver(config.external_roots)
    outside = tmp_path / 'outside.md'
    outside.write_text('outside')
    (sample.parent / 'notes/link.md').symlink_to(outside)
    with pytest.raises(PermissionError, match='escapes'):
        resolver.resolve(ResourceLocation('notes', 'link.md'))
    with pytest.raises(MemoryConfigurationError, match='traversal'):
        resolver.resolve(ResourceLocation('notes', '../outside.md'))
    with pytest.raises(PermissionError, match='unauthorized'):
        resolver.resolve(ResourceLocation('unknown', 'file.md'))
    with pytest.raises(FileNotFoundError):
        resolver.resolve(ResourceLocation('notes', 'missing-parent/new.md'), must_exist=False)
    assert resolver.resolve(ResourceLocation('notes', 'new.md'), must_exist=False).name == 'new.md'


def test_injected_host_resolver_can_narrow_roots_and_recall_reauthorizes(sample, tmp_path):
    pytest.importorskip('sqlite_vec')
    config = load_memory_config(path=sample)
    restricted = RootResourceResolver({'memory_data': config.external_roots['memory_data']})
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider, resource_resolver=restricted) as memory:
        with pytest.raises(PermissionError):
            memory.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as memory:
        memory.ingest_file(account_name='john', external_root='notes', relative_path='orchard.md')
        outside = tmp_path / 'outside.md'
        outside.write_text('outside')
        memory._index._conn.execute('UPDATE embedding_metadata SET source_metadata=?',
                                   (json.dumps({'path': str(outside)}),))
        with pytest.raises(PermissionError, match='outside authorized'):
            memory.recall(account_name='john', query='orchard', score_threshold=0)


def test_partial_assembly_closes_opened_stores(sample, monkeypatch):
    pytest.importorskip('sqlite_vec')
    closed = []
    real_close = assembly.SqliteVecEmbeddingIndex.close

    def close(index):
        closed.append(index)
        real_close(index)

    def fail(*args, **kwargs):
        raise RuntimeError('working initialization failed')

    monkeypatch.setattr(assembly.SqliteVecEmbeddingIndex, 'close', close)
    monkeypatch.setattr(assembly, 'SqliteWorkingMemory', fail)
    with pytest.raises(RuntimeError, match='working initialization'):
        build_memory(path=sample, embedding_provider_factory=OfflineSampleProvider)
    assert len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        closed[0]._conn.execute('SELECT 1')


def test_galet_factory_validates_returned_model_with_explicit_settings(monkeypatch):
    from galet.embedding_dto import EmbeddingResponse
    from galet.openai_embedding import OpenAIEmbeddingApi
    from galet.settings import Settings
    profile = EmbeddingProfile('openai', 'text-embedding-3-small', 1536)
    settings = Settings(credential_path='/explicit/test/credentials')
    received = []

    def init(self, **kwargs):
        received.append(kwargs['settings'])

    monkeypatch.setattr(OpenAIEmbeddingApi, '__init__', init)
    monkeypatch.setattr(OpenAIEmbeddingApi, 'embed', lambda *a, **kw: EmbeddingResponse(
        model='unexpected-model', embeddings=[[1.0] * 1536]))
    provider = assembly.galet_embedding_factory(settings)(profile)
    assert received == [settings]
    with pytest.raises(EmbeddingCompatibilityError, match='Galet returned'):
        provider.embed(['text'], model=profile.model)


def test_environment_sample_and_offline_provider_never_claims_openai(sample, monkeypatch, capsys):
    pytest.importorskip('sqlite_vec')
    monkeypatch.setenv('GALET_MEMORY_CONFIG_PATH', str(sample))
    assert main(['--offline']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['vector_matches'] == 1 and result['keyword_matches'] == 1
    with pytest.raises(MemoryConfigurationError, match='offline sample requires'):
        OfflineSampleProvider(EmbeddingProfile('openai', 'text-embedding-3-small', 1536))


def test_import_does_not_discover_configuration_or_read_resources(monkeypatch):
    monkeypatch.setenv('GALET_MEMORY_CONFIG_PATH', '/must-not-be-read.json')
    subprocess.run([sys.executable, '-c',
        "import sys\n"
        "def audit(event, args):\n"
        "    if event == 'open' and isinstance(args[0], str) and (args[0].endswith('configuration.schema.json') or 'must-not-be-read' in args[0] or args[0].endswith('.sqlite')):\n"
        "        raise RuntimeError('unexpected configuration/storage I/O')\n"
        "sys.addaudithook(audit)\n"
        "import galet_memory\n"],
        check=True, capture_output=True, text=True)


def test_configured_procedural_layout_preserves_existing_paths(sample):
    raw = inline_config(sample)
    raw['memory'] = {'procedural': raw['memory']['procedural']}
    raw['memory']['procedural']['context_resolution'] = 'most_specific'
    raw['memory']['procedural']['layout'] = {
        'global_contexts': 'contexts', 'account_contexts': 'contexts/{account}',
        'project_contexts': None, 'global_skills': 'skills',
        'account_skills': 'skills/{account}', 'project_skills': None,
    }
    with build_memory(raw) as runtime:
        repo = runtime.procedural.repository
        global_path = repo.save_context(account_name='john', context_name='dev', text='Global', scope='global')
        account_path = repo.save_context(account_name='john', context_name='dev', text='Account')
        assert global_path == sample.parent / 'data/contexts/dev.md'
        assert account_path == sample.parent / 'data/contexts/john/dev.md'
        assert runtime.procedural.recall(ProceduralMemoryRequest('john', 'dev')).text == 'Account'
    with build_memory(raw) as runtime:
        assert runtime.procedural.recall(ProceduralMemoryRequest('john', 'dev')).text == 'Account'


@pytest.mark.parametrize('layout', [{'unknown': 'contexts'}, {'global_contexts': 123}])
def test_procedural_layout_schema_rejects_bad_options(sample, layout):
    raw = inline_config(sample)
    raw['memory']['procedural']['layout'] = layout
    with pytest.raises(MemoryConfigurationError):
        load_memory_config(raw)
