from multiprocessing import get_context

import pytest

from galet_memory import (
    CurationConflictError, CurationError, CurationService, CurationSessionNotFoundError,
    EpisodicConcurrencyError, EpisodicCorrelationInvalidatedError,
    EpisodicEvent, EpisodicMemoryRequest, EpisodicSessionNotFoundError,
    EpisodicSessionQuery, JsonlEpisodicMemory, SqliteEpisodicMemory,
)
from galet_memory.publication import EmbeddingDigestPublisher, FilesystemDigestStore
from galet_memory.episodic import EmbeddingDigestRecall
from galet_memory.ports import FileTextLoader
from galet_memory.ports.embeddings import EmbeddingMatch, EmbeddingRecord


@pytest.fixture(params=[SqliteEpisodicMemory, JsonlEpisodicMemory])
def memory(request, tmp_path):
    with request.param(tmp_path / 'memory', digests_root=tmp_path / 'digests') as store:
        store.create_session(account_name='acct', agent_name='lucy', session_id='s')
        yield store


def append_linked(store, content, correlation='c', session='s', **kwargs):
    event = store.append_event(session, EpisodicEvent('user', content, **kwargs))
    store.link_event(correlation, session, event.event_id)
    return event


def contents(store, scope='all'):
    return [e.content for e in store.get_session('s', event_scope=scope).events]


def invalidate(store, **kwargs):
    return store.invalidate_events(account_name='acct', session_id='s', correlation_id='c', **kwargs)


def test_all_event_types_scope_reopen_idempotency_and_raw_tail(memory):
    memory.create_session(account_name='other', agent_name='lucy', session_id='other')
    other = append_linked(memory, 'other account', session='other')
    memory.create_session(account_name='acct', agent_name='lucy', session_id='same-account')
    same_account = append_linked(memory, 'other session', session='same-account')
    events = [append_linked(memory, 'secret', kind=kind) for kind in
              ('user_message', 'assistant_message', 'tool_result', 'image', 'processing')]
    keep = append_linked(memory, 'keep', correlation='keep')
    original_tail = memory.get_session('s').last_event_id
    result = invalidate(memory, expected_last_event_id=original_tail)
    assert result.status == 'invalidated'
    assert result.event_ids == tuple(e.event_id for e in events)
    assert result.event_count == 5
    assert result.unprovenanced_digests_invalidated
    assert contents(memory) == ['keep']
    assert contents(memory, 'active') == ['keep']
    assert contents(memory, 'raw') == ['secret'] * 5 + ['keep', '']
    snapshot = memory.get_session('s')
    assert snapshot.last_event_id == result.marker_event_id
    assert snapshot.events[-1].event_id == keep.event_id
    repeated = invalidate(memory, expected_last_event_id=original_tail)
    assert repeated.status == 'already_invalidated'
    assert repeated.event_ids == result.event_ids
    assert repeated.marker_event_id == result.marker_event_id
    assert len(contents(memory, 'raw')) == 7
    assert memory.get_session('other').events == [other]
    assert memory.get_session('same-account').events == [same_account]
    path = memory.db_path if isinstance(memory, SqliteEpisodicMemory) else memory.root
    with type(memory)(path) as reopened:
        assert contents(reopened) == ['keep']
        assert invalidate(reopened).marker_event_id == result.marker_event_id
    if isinstance(memory, SqliteEpisodicMemory):
        assert memory.get_events_by_correlation('c') == [other, same_account]


def test_missing_ownership_and_conditional_conflict_do_not_write(memory):
    event = append_linked(memory, 'secret')
    assert memory.invalidate_events(account_name='acct', session_id='s', correlation_id='missing').status == 'not_found'
    for account, session in [('other', 's'), ('acct', 'missing')]:
        with pytest.raises(EpisodicSessionNotFoundError):
            memory.invalidate_events(account_name=account, session_id=session, correlation_id='c')
    with pytest.raises(ValueError, match='empty'):
        memory.invalidate_events(account_name='acct', session_id='s', correlation_id=' ')
    with pytest.raises(EpisodicConcurrencyError):
        invalidate(memory, expected_last_event_id='stale')
    assert contents(memory, 'raw') == ['secret']
    assert memory.get_session('s').last_event_id == event.event_id


def test_search_recall_and_digest_inputs_exclude_deleted_data(memory):
    append_linked(memory, 'secret')
    append_linked(memory, 'safe', correlation='keep')
    invalidate(memory)
    assert memory.list_sessions(EpisodicSessionQuery('acct', query='secret')) == []
    assert [s.session_id for s in memory.list_sessions(EpisodicSessionQuery('acct', query='safe'))] == ['s']
    recalled = memory.recall(EpisodicMemoryRequest('acct', 'lucy', conversation_id='s'))
    assert [e.content for e in recalled.events] == ['safe']
    assert memory.recall(EpisodicMemoryRequest('other', 'lucy', conversation_id='s')).events == []
    generator = Generator()
    CurationService(memory, generator).produce_digest(account_name='acct', session_id='s')
    assert generator.inputs == [['safe']]


class Generator:
    def __init__(self):
        self.inputs = []

    def generate(self, request):
        self.inputs.append([e.content for e in request.events])
        return ' / '.join(str(e.content) for e in request.events)


def test_transitive_digests_are_hidden_without_revealing_archived_history(memory):
    delete = append_linked(memory, 'secret')
    append_linked(memory, 'archived safe', correlation='keep')
    service = CurationService(memory, Generator())
    first = service.archive(account_name='acct', session_id='s', idempotency_key='first')
    append_linked(memory, 'clean second interval', correlation='second')
    second = service.archive(account_name='acct', session_id='s')
    cumulative = memory.append_event('s', EpisodicEvent('system', 'stale cumulative', kind='session_digest',
        metadata={'source_event_ids': [first.boundary_event.event_id, second.boundary_event.event_id]}))
    unknown = memory.append_event('s', EpisodicEvent('system', 'no provenance', kind='session_digest'))
    result = invalidate(memory)
    assert set(result.invalidated_digest_ids) == {first.boundary_event.event_id, cumulative.event_id, unknown.event_id}
    assert contents(memory, 'active') == [second.digest]
    assert contents(memory, 'archived') == ['archived safe', 'clean second interval']
    combined = service.produce_cumulative_digest(account_name='acct', session_id='s')
    assert combined.source_event_ids == (second.boundary_event.event_id,)
    with pytest.raises(CurationConflictError, match='invalidated'):
        service.archive(account_name='acct', session_id='s', idempotency_key='first')
    assert delete in memory.get_session('s', event_scope='raw').events


def test_removed_latest_digest_and_reset_boundaries_remain_effective(memory):
    append_linked(memory, 'safe old', correlation='keep')
    secret = append_linked(memory, 'secret')
    service = CurationService(memory, Generator())
    archive = service.archive(account_name='acct', session_id='s')
    invalidate(memory)
    assert contents(memory, 'active') == []
    assert contents(memory, 'archived') == ['safe old']
    assert contents(memory) == ['safe old']
    # Fresh archive must use the hidden marker as its true conditional tail.
    append_linked(memory, 'fresh', correlation='fresh')
    fresh = service.archive(account_name='acct', session_id='s')
    assert fresh.source_event_ids != (secret.event_id,)
    assert service.produce_cumulative_digest(account_name='acct', session_id='s').source_event_ids == (fresh.boundary_event.event_id,)
    service.reset_context(account_name='acct', session_id='s')
    assert contents(memory, 'active') == []
    with pytest.raises(CurationError, match='no archived interval'):
        service.produce_cumulative_digest(account_name='acct', session_id='s')
    assert archive.boundary_event in memory.get_session('s', event_scope='raw').events


def test_late_links_and_stale_conditional_writes_are_rejected(memory):
    original = append_linked(memory, 'secret')
    invalidate(memory)
    with pytest.raises(EpisodicCorrelationInvalidatedError):
        memory.link_event('c', 's', original.event_id)
    with pytest.raises(EpisodicConcurrencyError):
        memory.append_event_if_tail('s', EpisodicEvent('system', 'stale'), expected_last_event_id=original.event_id)
    tail = memory.get_session('s').last_event_id
    memory.append_event_if_tail('s', EpisodicEvent('user', 'safe'), expected_last_event_id=tail)
    assert contents(memory) == ['safe']


def test_invalidation_during_generation_conflicts_without_committing_stale_archive(memory):
    append_linked(memory, 'secret')
    class ConcurrentGenerator:
        def generate(self, request):
            invalidate(memory)
            return 'stale'
    with pytest.raises(CurationConflictError):
        CurationService(memory, ConcurrentGenerator()).archive(account_name='acct', session_id='s')
    assert contents(memory) == []
    assert not any(e.kind == 'session_digest' for e in memory.get_session('s', event_scope='raw').events)


def test_overflow_is_discarded_and_repeated_invalidation_preserves_fresh_overflow(memory):
    append_linked(memory, 'secret')
    assert memory.save_overflow_digest(account_name='acct', conversation_id='s', snippet='old') == 'old'
    path = memory.digests_root / 'acct/s_overflow.md'
    invalidate(memory)
    assert not path.exists()
    assert memory.save_overflow_digest(account_name='acct', conversation_id='s', snippet='safe') == 'safe'
    invalidate(memory)
    assert path.read_text() == 'safe'


class Embeddings:
    def embed(self, texts, *, model):
        return [[0.1, 0.2]]


class Index:
    def __init__(self):
        self.records = {}

    def upsert(self, record):
        self.records[record.id] = record

    def query(self, **kwargs):
        return [EmbeddingMatch(EmbeddingRecord(r.id, r.source_id, r.source_type, r.metadata), 0.9)
                for r in self.records.values() if r.account_name == kwargs['account_name']]


def test_published_interval_cumulative_preview_and_old_embeddings_are_filtered(memory, tmp_path):
    index = Index()
    publisher = EmbeddingDigestPublisher(FilesystemDigestStore(tmp_path / 'published'), Embeddings(), index)
    memory.digest_recall = EmbeddingDigestRecall(embeddings=Embeddings(), index=index, text_loader=FileTextLoader())
    append_linked(memory, 'secret')
    service = CurationService(memory, Generator(), publisher)
    preview = service.produce_digest(account_name='acct', session_id='s', publish=True)
    interval = service.archive(account_name='acct', session_id='s', publish=True)
    service.produce_cumulative_digest(account_name='acct', session_id='s', publish=True)
    # Also simulate an existing embedding with no event provenance.
    old_record = index.records[preview.publication.embedding_id]
    from dataclasses import replace
    index.upsert(replace(old_record, id='old', metadata={'path': preview.publication.path}))
    request = EpisodicMemoryRequest('acct', 'lucy', conversation_id='s', query='secret', digest_top_k=20)
    assert len(memory.recall(request).digests) == 4
    invalidate(memory)
    assert memory.recall(request).digests == []
    assert len(index.records) == 4  # retained but no longer recallable
    with pytest.raises(CurationSessionNotFoundError, match='not found'):
        service.retry_publication(account_name='acct', session_id='s', boundary_event_id=interval.boundary_event.event_id)
    append_linked(memory, 'safe', correlation='safe')
    service.produce_digest(account_name='acct', session_id='s', publish=True)
    assert [d.snippet for d in memory.recall(request).digests] == ['safe']


def test_stale_digest_written_after_invalidation_is_not_visible(memory):
    event = append_linked(memory, 'secret')
    invalidate(memory)
    memory.append_event('s', EpisodicEvent('system', 'stale digest', kind='session_digest',
                                         metadata={'source_event_ids': [event.event_id]}))
    assert contents(memory) == []


def _invalidate_in_process(backend_name, path):
    backend = SqliteEpisodicMemory if backend_name == 'sqlite' else JsonlEpisodicMemory
    with backend(path) as store:
        return invalidate(store).status


@pytest.mark.parametrize('backend', [SqliteEpisodicMemory, JsonlEpisodicMemory])
def test_concurrent_processes_commit_only_one_marker(tmp_path, backend):
    path = tmp_path / 'memory'
    with backend(path) as store:
        store.create_session(account_name='acct', agent_name='lucy', session_id='s')
        append_linked(store, 'secret')
    name = 'sqlite' if backend is SqliteEpisodicMemory else 'jsonl'
    with get_context('spawn').Pool(2) as pool:
        statuses = pool.starmap(_invalidate_in_process, [(name, path), (name, path)])
    assert sorted(statuses) == ['already_invalidated', 'invalidated']
    with backend(path) as store:
        assert contents(store) == []
        assert len(contents(store, 'raw')) == 2


def test_semantic_digest_search_cannot_bypass_invalidation(memory, tmp_path):
    from galet_memory import SemanticMemoryRequest, VectorSemanticMemory
    index = Index()
    publisher = EmbeddingDigestPublisher(FilesystemDigestStore(tmp_path / 'published'), Embeddings(), index)
    append_linked(memory, 'secret')
    CurationService(memory, Generator(), publisher).produce_digest(account_name='acct', session_id='s', publish=True)
    request = SemanticMemoryRequest('acct', query='secret', namespaces=['digests'])
    raw_semantic = VectorSemanticMemory(embeddings=Embeddings(), index=index, text_loader=FileTextLoader())
    assert raw_semantic.recall(request).documents == []  # no owning store configured
    semantic = VectorSemanticMemory(embeddings=Embeddings(), index=index,
                                    text_loader=FileTextLoader(), episodic_store=memory)
    assert [d.snippet for d in semantic.recall(request).documents] == ['secret']
    invalidate(memory)
    assert semantic.recall(request).documents == []


def test_overflow_cleanup_failure_does_not_reintroduce_stale_file(memory, monkeypatch):
    from pathlib import Path
    append_linked(memory, 'secret')
    memory.save_overflow_digest(account_name='acct', conversation_id='s', snippet='secret overflow')
    original_unlink = Path.unlink
    path = memory.digests_root / 'acct/s_overflow.md'
    def fail_cleanup(self, *args, **kwargs):
        if self == path:
            raise OSError('cleanup interrupted')
        return original_unlink(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_cleanup)
        with pytest.raises(OSError, match='interrupted'):
            invalidate(memory)
    assert contents(memory) == []
    assert memory.save_overflow_digest(account_name='acct', conversation_id='s', snippet='safe') == 'safe'
    assert path.read_text() == 'safe'


def test_invalidation_during_embedding_publication_cannot_recall_stale_digest(memory, tmp_path):
    index = Index()
    class ConcurrentEmbeddings(Embeddings):
        def embed(self, texts, *, model):
            invalidate(memory)
            return super().embed(texts, model=model)
    publisher = EmbeddingDigestPublisher(FilesystemDigestStore(tmp_path / 'published'), ConcurrentEmbeddings(), index)
    append_linked(memory, 'secret')
    CurationService(memory, Generator(), publisher).produce_digest(account_name='acct', session_id='s', publish=True)
    memory.digest_recall = EmbeddingDigestRecall(embeddings=Embeddings(), index=index, text_loader=FileTextLoader())
    assert memory.recall(EpisodicMemoryRequest('acct', 'lucy', query='secret')).digests == []
    assert contents(memory) == []


def test_discarding_transcript_does_not_restore_invalidated_external_digests(memory):
    event = append_linked(memory, 'secret')
    invalidate(memory)
    memory.reset_session('s')
    assert contents(memory) == []
    assert len(contents(memory, 'raw')) == 1
    assert not memory.is_digest_valid(account_name='acct', session_id='s', source_event_ids=[event.event_id])
    assert not memory.is_digest_valid(account_name='acct', session_id='s')
    assert invalidate(memory).status == 'already_invalidated'
    with pytest.raises(ValueError, match='already exists'):
        memory.create_session(account_name='acct', agent_name='lucy', session_id='s')
