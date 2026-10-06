from multiprocessing import get_context
from pathlib import Path
import pytest
from galet_memory import (
    CurationService,
    CurationConflictError,
    CurationError,
    CurationSessionNotFoundError,
    SqliteEpisodicMemory,
    JsonlEpisodicMemory,
    NewEvent,
    EpisodicConcurrencyError,
    EpisodicCorrelationInvalidatedError,
    DigestSearchRequest,
    EmbeddingDigestRecall,
    VectorSemanticMemory,
    SemanticMemoryRequest,
)
from galet_memory.publication import EmbeddingDigestPublisher, FilesystemDigestStore
from galet_memory.ports import FileTextLoader, EmbeddingMatch, EmbeddingRecord


@pytest.fixture(params=[SqliteEpisodicMemory, JsonlEpisodicMemory])
def memory(request, tmp_path):
    with request.param(tmp_path / "memory", digests_root=tmp_path / "digests") as s:
        s.create_session(account_name="acct", session_id="s")
        yield s


def append(s, text, correlation="c", session="s", account="acct", kind="user_message"):
    return s.append_event(
        account_name=account,
        session_id=session,
        event=NewEvent("user", text, account, kind, correlation_ids=(correlation,)),
    )


def invalidate(s, **kw):
    return s.invalidate_exchange(
        account_name="acct", session_id="s", **{"correlation_id": "c", **kw}
    )


def contents(s):
    return [
        e.content
        for e in s.get_recent_events(
            account_name="acct", session_id="s", count=100
        ).events
    ]


class Generator:
    def __init__(self):
        self.inputs = []

    def generate(self, request):
        self.inputs.append([e.content for e in request.events])
        return " / ".join(str(e.content) for e in request.events)


def test_exchange_retrieval_invalidation_isolation_raw_tail_and_reopen(memory):
    memory.create_session(account_name="other", session_id="other")
    other = append(memory, "other", session="other", account="other")
    originals = [
        append(memory, "secret", kind=k)
        for k in (
            "user_message",
            "assistant_message",
            "tool_result",
            "image",
            "processing",
        )
    ]
    keep = append(memory, "keep", correlation="keep")
    exchange = memory.get_exchange(
        account_name="acct",
        session_id="s",
        correlation_id=originals[0].correlation_ids[0],
    )
    assert exchange.events == tuple(originals)
    tail = memory.get_session(account_name="acct", session_id="s").last_event_id
    result = invalidate(memory, expected_last_event_id=tail)
    assert result.event_ids == tuple(e.event_id for e in originals)
    assert result.event_count == 5 and result.unprovenanced_digests_invalidated
    assert contents(memory) == ["keep"]
    assert (
        memory.get_exchange(account_name="acct", session_id="s", correlation_id="c")
        is None
    )
    assert (
        memory.get_event(
            account_name="acct", session_id="s", event_id=originals[0].event_id
        )
        is None
    )
    assert memory.get_exchange(
        account_name="other", session_id="other", correlation_id="c"
    ).events == (other,)
    assert (
        len(memory.get_audit_snapshot(account_name="acct", session_id="s").events) == 7
    )
    assert (
        memory.get_session(account_name="acct", session_id="s").last_event_id
        == result.marker_event_id
    )
    repeated = invalidate(memory, expected_last_event_id=tail)
    assert (
        repeated.status == "already_invalidated"
        and repeated.marker_event_id == result.marker_event_id
    )
    path = memory.db_path if isinstance(memory, SqliteEpisodicMemory) else memory.root
    with type(memory)(path) as reopened:
        assert contents(reopened) == ["keep"]
        assert invalidate(reopened).marker_event_id == result.marker_event_id


def test_missing_conflict_and_invalidated_appends_do_not_write(memory):
    event = append(memory, "secret")
    assert (
        memory.invalidate_exchange(
            account_name="acct", session_id="s", correlation_id="missing"
        ).status
        == "not_found"
    )
    with pytest.raises(EpisodicConcurrencyError):
        invalidate(memory, expected_last_event_id="stale")
    assert contents(memory) == ["secret"]
    invalidate(memory)
    with pytest.raises(EpisodicCorrelationInvalidatedError):
        append(memory, "late")
    assert (
        len(memory.get_audit_snapshot(account_name="acct", session_id="s").events) == 2
    )
    with pytest.raises(ValueError):
        invalidate(memory, correlation_id=" ")


def test_invalidated_digest_does_not_reveal_archived_messages_and_cumulative_provenance(
    memory,
):
    append(memory, "secret")
    append(memory, "archived safe", correlation="keep")
    service = CurationService(memory, Generator())
    first = service.archive(
        account_name="acct", session_id="s", idempotency_key="first"
    )
    append(memory, "clean second interval", correlation="second")
    second = service.archive(account_name="acct", session_id="s")
    result = invalidate(memory)
    assert result.invalidated_digest_ids == (first.boundary_event.event_id,)
    assert contents(memory) == []
    assert (
        memory.get_digest(
            account_name="acct", session_id="s", digest_id=first.boundary_event.event_id
        )
        is None
    )
    assert [
        d.digest_id
        for d in memory.list_digests(account_name="acct", session_id="s").items
    ] == [second.boundary_event.event_id]
    combined = service.produce_cumulative_digest(account_name="acct", session_id="s")
    assert combined.source_event_ids == (second.boundary_event.event_id,)
    with pytest.raises(CurationConflictError, match="invalidated"):
        service.archive(account_name="acct", session_id="s", idempotency_key="first")
    service.reset_context(account_name="acct", session_id="s")
    with pytest.raises(CurationError):
        service.produce_cumulative_digest(account_name="acct", session_id="s")


def test_invalidation_during_digest_generation_refuses_stale_archive(memory):
    append(memory, "secret")

    class ConcurrentGenerator:
        def generate(self, request):
            invalidate(memory)
            return "stale"

    with pytest.raises(CurationConflictError):
        CurationService(memory, ConcurrentGenerator()).archive(
            account_name="acct", session_id="s"
        )
    assert memory.list_digests(account_name="acct", session_id="s").items == ()


def test_clear_preserves_exclusion_of_external_unprovenanced_digests(memory):
    event = append(memory, "secret")
    invalidate(memory)
    append(memory, "safe", correlation="keep")
    cleared = memory.clear_session_events(account_name="acct", session_id="s")
    assert cleared.removed_event_count == 2
    assert not memory.is_digest_valid(
        account_name="acct", session_id="s", source_event_ids=[event.event_id]
    )
    assert not memory.is_digest_valid(account_name="acct", session_id="s")
    assert invalidate(memory).status == "already_invalidated"


class Embeddings:
    def embed(self, texts, *, model):
        return [[0.1, 0.2]]


class Index:
    def __init__(self):
        self.records = {}

    def upsert(self, r):
        self.records[r.id] = r

    def query(self, **kw):
        return [
            EmbeddingMatch(
                EmbeddingRecord(r.id, r.source_id, r.source_type, r.metadata), 0.9
            )
            for r in self.records.values()
            if r.account_name == kw["account_name"]
        ]


def test_digest_search_and_semantic_search_cannot_bypass_invalidation(memory, tmp_path):
    index = Index()
    memory.digest_search = EmbeddingDigestRecall(
        embeddings=Embeddings(), index=index, text_loader=FileTextLoader()
    )
    publisher = EmbeddingDigestPublisher(
        FilesystemDigestStore(tmp_path / "published"), Embeddings(), index
    )
    append(memory, "secret")
    service = CurationService(memory, Generator(), publisher)
    preview = service.produce_digest(account_name="acct", session_id="s", publish=True)
    interval = service.archive(account_name="acct", session_id="s", publish=True)
    service.produce_cumulative_digest(account_name="acct", session_id="s", publish=True)
    assert (
        len(
            memory.search_digests(
                account_name="acct", query="secret", session_id="s", count=20
            )
        )
        == 3
    )
    semantic = VectorSemanticMemory(
        embeddings=Embeddings(),
        index=index,
        text_loader=FileTextLoader(),
        episodic_store=memory,
    )
    query = SemanticMemoryRequest("acct", query="secret", namespaces=["digests"])
    assert semantic.recall(query).documents
    invalidate(memory)
    assert memory.search_digests(account_name="acct", query="secret", count=20) == []
    assert semantic.recall(query).documents == []
    with pytest.raises(CurationSessionNotFoundError):
        service.retry_publication(
            account_name="acct",
            session_id="s",
            boundary_event_id=interval.boundary_event.event_id,
        )
    append(memory, "safe", correlation="safe")
    service.produce_digest(account_name="acct", session_id="s", publish=True)
    assert [
        d.snippet
        for d in memory.search_digests(account_name="acct", query="safe", count=20)
    ] == ["safe"]


def test_overflow_cleanup_failure_and_new_epoch(memory, monkeypatch):
    append(memory, "secret")
    assert (
        memory.save_overflow_digest(
            account_name="acct", session_id="s", snippet="secret overflow"
        )
        == "secret overflow"
    )
    path = memory.digests_root / "acct/s_overflow.md"
    original = Path.unlink

    def fail(self, *a, **kw):
        if self == path:
            raise OSError("cleanup interrupted")
        return original(self, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        with pytest.raises(OSError):
            invalidate(memory)
    assert contents(memory) == []
    assert (
        memory.save_overflow_digest(account_name="acct", session_id="s", snippet="safe")
        == "safe"
    )
    invalidate(memory)
    assert path.read_text() == "safe"


def _invalidate_process(backend, path):
    with (SqliteEpisodicMemory if backend == "sqlite" else JsonlEpisodicMemory)(
        path
    ) as store:
        return invalidate(store).status


@pytest.mark.parametrize("backend", [SqliteEpisodicMemory, JsonlEpisodicMemory])
def test_concurrent_processes_invalidate_only_once(tmp_path, backend):
    path = tmp_path / "memory"
    with backend(path) as s:
        s.create_session(account_name="acct", session_id="s")
        append(s, "secret")
    with get_context("spawn").Pool(2) as pool:
        statuses = pool.starmap(
            _invalidate_process,
            [("sqlite" if backend is SqliteEpisodicMemory else "jsonl", path)] * 2,
        )
    assert sorted(statuses) == ["already_invalidated", "invalidated"]
    with backend(path) as s:
        assert (
            len(s.get_audit_snapshot(account_name="acct", session_id="s").events) == 2
        )


def test_transitive_and_unprovenanced_digest_boundaries_are_invalidated(memory):
    event = append(memory, "secret")
    tail = event.event_id
    digests = []
    for sources in ([event.event_id], None, None):
        if sources is None and digests:
            sources = [digests[-1].event_id]
        digest = memory.append_boundary(
            account_name="acct",
            session_id="s",
            expected_last_event_id=tail,
            event=NewEvent(
                "system",
                "derived secret",
                "memory",
                "session_digest",
                metadata={"visibility_boundary": True, "source_event_ids": sources},
            ),
        )
        digests.append(digest)
        tail = digest.event_id
    unknown = memory.append_boundary(
        account_name="acct",
        session_id="s",
        expected_last_event_id=tail,
        event=NewEvent(
            "system",
            "unknown provenance",
            "memory",
            "session_digest",
            metadata={"visibility_boundary": True},
        ),
    )
    result = invalidate(memory)
    assert set(result.invalidated_digest_ids) == {
        d.event_id for d in [*digests, unknown]
    }
    assert memory.list_digests(account_name="acct", session_id="s").items == ()
    assert contents(memory) == []
