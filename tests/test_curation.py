import pytest

from galet_memory import (
    CurationConflictError,
    CurationService,
    CurationSessionNotFoundError,
    DigestGenerationError,
    EpisodicConcurrencyError,
    EpisodicEvent,
    SqliteEpisodicMemory,
)


class RecordingDigestGenerator:
    def __init__(self, text="A useful digest"):
        self.text = text
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if isinstance(self.text, BaseException):
            raise self.text
        return self.text


def _memory_with_events(tmp_path, contents=("one", "two")):
    memory = SqliteEpisodicMemory(tmp_path / "chat2.sqlite")
    memory.create_session(
        account_name="acct",
        agent_name="agent",
        session_id="session",
        friendly_name="A session",
    )
    memory.add_events(
        "session",
        [EpisodicEvent(role="user", content=value) for value in contents],
    )
    return memory


def _contents(memory, scope="active"):
    session = memory.get_session("session", event_scope=scope)
    assert session is not None
    return [event.content for event in session.events]


def test_scopes_without_a_boundary(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        assert _contents(memory, "active") == ["one", "two"]
        assert _contents(memory, "all") == ["one", "two"]
        assert _contents(memory, "archived") == []


def test_unknown_scope_is_rejected_even_without_loading_events(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        with pytest.raises(ValueError, match="unsupported event scope"):
            memory.get_session(
                "session", include_events=False, event_scope="future"
            )


def test_latest_boundary_defines_active_and_archived_segments(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        memory.append_event(
            "session",
            EpisodicEvent(
                role="system",
                actor="curation",
                kind="session_digest",
                content="digest one",
                metadata={"visibility_boundary": True},
            ),
        )
        memory.append_event("session", EpisodicEvent("user", "three"))
        memory.append_event(
            "session",
            EpisodicEvent(
                role="system",
                actor="curation",
                kind="session_digest",
                content="digest two",
                metadata={"visibility_boundary": True},
            ),
        )
        memory.append_event("session", EpisodicEvent("user", "four"))

        assert _contents(memory, "active") == ["digest two", "four"]
        assert _contents(memory, "archived") == [
            "one",
            "two",
            "digest one",
            "three",
        ]
        assert _contents(memory, "all") == [
            "one",
            "two",
            "digest one",
            "three",
            "digest two",
            "four",
        ]


def test_legacy_lucy_archive_summary_is_a_read_only_boundary(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        legacy = memory.append_event(
            "session",
            EpisodicEvent(
                role="system",
                actor="curation",
                kind="summary",
                content="legacy digest",
                metadata={"curation_mode": "archive"},
            ),
        )
        memory.append_event("session", EpisodicEvent("user", "new"))

        active = memory.get_session("session", event_scope="active")
        assert active is not None
        assert [event.event_id for event in active.events][0] == legacy.event_id
        assert [event.content for event in active.events] == [
            "legacy digest",
            "new",
        ]
        assert memory.get_session("session", event_scope="all").events[2].kind == "summary"


def test_conditional_append_is_atomic_on_tail_conflict(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        original = memory.get_session("session", event_scope="all")
        assert original is not None
        with pytest.raises(EpisodicConcurrencyError):
            memory.append_event_if_tail(
                "session",
                EpisodicEvent("system", "digest", kind="session_digest"),
                expected_last_event_id="not-the-tail",
            )
        current = memory.get_session("session", event_scope="all")
        assert current is not None
        assert [event.event_id for event in current.events] == [
            event.event_id for event in original.events
        ]


def test_produce_digest_is_read_only_and_uses_active_events(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        generator = RecordingDigestGenerator("  digest text  ")
        service = CurationService(memory, generator)

        before = memory.get_session("session", event_scope="all")
        result = service.produce_digest(
            account_name="acct", session_id="session", max_chars=1234
        )
        after = memory.get_session("session", event_scope="all")

        assert result.action == "digest"
        assert result.digest == "digest text"
        assert result.boundary_event is None
        assert generator.requests[0].max_chars == 1234
        assert [event.event_id for event in before.events] == [
            event.event_id for event in after.events
        ]


def test_archive_appends_one_boundary_and_preserves_history(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        generator = RecordingDigestGenerator("digest")
        result = CurationService(memory, generator).archive(
            account_name="acct",
            session_id="session",
            idempotency_key="operation-1",
        )

        assert result.action == "archive"
        assert result.boundary_event is not None
        assert result.boundary_event.kind == "session_digest"
        assert result.boundary_event.metadata == {
            "visibility_boundary": True,
            "curation_version": 1,
            "idempotency_key": "operation-1",
        }
        assert _contents(memory, "active") == ["digest"]
        assert _contents(memory, "archived") == ["one", "two"]
        assert _contents(memory, "all") == ["one", "two", "digest"]


def test_archive_can_establish_the_first_event_in_an_empty_session(tmp_path):
    memory = SqliteEpisodicMemory(tmp_path / "chat2.sqlite")
    with memory:
        memory.create_session(
            account_name="acct", agent_name="agent", session_id="session"
        )
        result = CurationService(
            memory, RecordingDigestGenerator("empty-session digest")
        ).archive(
            account_name="acct",
            session_id="session",
            idempotency_key="operation-empty",
        )
        assert result.boundary_event is not None
        assert _contents(memory, "active") == ["empty-session digest"]


def test_repeated_archive_summarizes_only_the_active_segment(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        generator = RecordingDigestGenerator("digest one")
        service = CurationService(memory, generator)
        service.archive(
            account_name="acct",
            session_id="session",
            idempotency_key="operation-1",
        )
        memory.append_event("session", EpisodicEvent("user", "three"))
        generator.text = "digest two"
        service.archive(
            account_name="acct",
            session_id="session",
            idempotency_key="operation-2",
        )

        assert [event.content for event in generator.requests[1].events] == [
            "digest one",
            "three",
        ]
        assert _contents(memory, "active") == ["digest two"]
        assert _contents(memory, "all") == [
            "one",
            "two",
            "digest one",
            "three",
            "digest two",
        ]


def test_archive_retry_with_same_key_returns_existing_boundary(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        generator = RecordingDigestGenerator("digest")
        service = CurationService(memory, generator)
        first = service.archive(
            account_name="acct",
            session_id="session",
            idempotency_key="operation-1",
        )
        second = service.archive(
            account_name="acct",
            session_id="session",
            idempotency_key="operation-1",
        )

        assert len(generator.requests) == 1
        assert second.boundary_event.event_id == first.boundary_event.event_id
        assert _contents(memory, "all") == ["one", "two", "digest"]


def test_account_mismatch_does_not_call_generator(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        generator = RecordingDigestGenerator()
        with pytest.raises(CurationSessionNotFoundError):
            CurationService(memory, generator).produce_digest(
                account_name="other", session_id="session"
            )
        assert generator.requests == []


@pytest.mark.parametrize("generated", ["", "   ", RuntimeError("offline")])
def test_digest_failure_does_not_change_history(tmp_path, generated):
    with _memory_with_events(tmp_path) as memory:
        generator = RecordingDigestGenerator(generated)
        before = _contents(memory, "all")
        with pytest.raises(DigestGenerationError):
            CurationService(memory, generator).archive(
                account_name="acct", session_id="session"
            )
        assert _contents(memory, "all") == before


def test_concurrent_event_causes_conflict_without_hiding_it(tmp_path):
    with _memory_with_events(tmp_path) as memory:
        class ConcurrentGenerator:
            def generate(self, request):
                memory.append_event(
                    "session", EpisodicEvent("user", "arrived concurrently")
                )
                return "stale digest"

        with pytest.raises(CurationConflictError):
            CurationService(memory, ConcurrentGenerator()).archive(
                account_name="acct",
                session_id="session",
                idempotency_key="operation-1",
            )

        assert _contents(memory, "active") == [
            "one",
            "two",
            "arrived concurrently",
        ]
        assert all(
            event.kind != "session_digest"
            for event in memory.get_session("session", event_scope="all").events
        )
