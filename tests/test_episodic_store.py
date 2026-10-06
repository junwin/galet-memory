from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
import pytest
from galet_memory import (
    EMPTY_TAIL,
    NewEvent,
    SessionChanges,
    SqliteEpisodicMemory,
    JsonlEpisodicMemory,
    EpisodicSessionNotFoundError,
    EpisodicConcurrencyError,
    CurationService,
)


@pytest.fixture(params=[SqliteEpisodicMemory, JsonlEpisodicMemory])
def store(request, tmp_path):
    with request.param(tmp_path / "memory") as s:
        s.create_session(account_name="a", session_id="s")
        yield s


def append(s, text="hello", **kw):
    return s.append_event(
        account_name="a", session_id="s", event=NewEvent("user", text, "a", **kw)
    )


def test_last_ten_user_messages_filter_before_limit_across_agents(store):
    users = []
    for i in range(12):
        users.append(append(store, f"user-{i}", correlation_ids=(f"c{i}",)))
        store.append_event(
            account_name="a",
            session_id="s",
            event=NewEvent(
                "assistant",
                {"text": f"answer-{i}"},
                "peace" if i % 2 else "belle",
                correlation_ids=(f"c{i}",),
            ),
        )
        store.append_event(
            account_name="a",
            session_id="s",
            event=NewEvent("system", {"total": 100}, "system", "prompt_report"),
        )
    page = store.get_recent_events(
        account_name="a", session_id="s", count=10, event_kinds=["user_message"]
    )
    assert list(page.events) == users[-10:]
    assert page.before_event_id == users[-10].event_id
    older = store.get_recent_events(
        account_name="a",
        session_id="s",
        count=10,
        event_kinds=["user_message"],
        before_event_id=page.before_event_id,
    )
    assert list(older.events) == users[:2] and older.before_event_id is None
    assert page.events[-1].correlation_ids == ("c11",)
    session = store.get_session(account_name="a", session_id="s")
    assert "events" not in asdict(session) and "agent_name" not in asdict(session)
    assert (
        len(
            store.get_recent_events(
                account_name="a", session_id="s", count=100, actors=["peace"]
            ).events
        )
        == 6
    )


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_count_and_filter_semantics(store, count):
    append(store)
    if count == 0 and type(count) is int:
        assert (
            store.get_recent_events(account_name="a", session_id="s", count=0).events
            == ()
        )
    else:
        with pytest.raises(ValueError):
            store.get_recent_events(account_name="a", session_id="s", count=count)
    assert (
        store.get_recent_events(account_name="a", session_id="s", event_kinds=[]).events
        == ()
    )
    assert (
        store.get_recent_events(account_name="a", session_id="s", actors=[]).events
        == ()
    )
    with pytest.raises(ValueError):
        store.get_recent_events(
            account_name="a", session_id="s", event_kinds="user_message"
        )


@pytest.mark.parametrize(
    "operation",
    [
        "recent",
        "period",
        "event",
        "exchange",
        "update",
        "append",
        "clear",
        "delete",
        "invalidate",
        "digest",
        "digests",
    ],
)
def test_every_operation_enforces_account_ownership(store, operation):
    event = append(store, correlation_ids=("c",))
    owned = {"account_name": "other", "session_id": "s"}
    assert store.get_session(**owned) is None
    assert store.list_sessions(account_name="other").items == ()
    call = {
        "recent": lambda: store.get_recent_events(**owned),
        "period": lambda: store.get_events_by_period(
            **owned, start=datetime.now(timezone.utc), end=datetime.now(timezone.utc)
        ),
        "event": lambda: store.get_event(**owned, event_id=event.event_id),
        "exchange": lambda: store.get_exchange(**owned, correlation_id="c"),
        "update": lambda: store.update_session(
            **owned, changes=SessionChanges(friendly_name="bad")
        ),
        "append": lambda: store.append_event(
            **owned, event=NewEvent("user", "bad", "other")
        ),
        "clear": lambda: store.clear_session_events(**owned),
        "delete": lambda: store.delete_session(**owned),
        "invalidate": lambda: store.invalidate_exchange(**owned, correlation_id="c"),
        "digest": lambda: store.get_digest(**owned, digest_id="d"),
        "digests": lambda: store.list_digests(**owned),
    }[operation]
    with pytest.raises(EpisodicSessionNotFoundError):
        call()
    assert (
        store.get_event(account_name="a", session_id="s", event_id=event.event_id)
        == event
    )


def test_typed_changes_bulk_delete_and_idempotency(store):
    s = store.update_session(
        account_name="a",
        session_id="s",
        changes=SessionChanges(friendly_name="hello", tags=("x",), metadata={"x": 1}),
    )
    s = store.update_session(
        account_name="a", session_id="s", changes=SessionChanges(friendly_name=None)
    )
    assert s.friendly_name is None and s.tags == ("x",) and s.metadata == {"x": 1}
    with pytest.raises(TypeError):
        store.update_session(
            account_name="a", session_id="s", changes={"account_name": "other"}
        )
    store.create_session(account_name="other", session_id="other")
    with pytest.raises(EpisodicSessionNotFoundError):
        store.delete_sessions(account_name="a", session_ids=["s", "other"])
    assert store.get_session(account_name="a", session_id="s")
    assert store.delete_sessions(
        account_name="a", session_ids=["s", "missing", "s"]
    ) == ["s"]
    assert not store.delete_session(account_name="a", session_id="s")


def test_atomic_batch_and_empty_tail_guard(store):
    one = store.append_event(
        account_name="a",
        session_id="s",
        event=NewEvent("user", "one", "a"),
        expected_last_event_id=EMPTY_TAIL,
    )
    with pytest.raises(EpisodicConcurrencyError):
        store.append_event(
            account_name="a",
            session_id="s",
            event=NewEvent("user", "stale", "a"),
            expected_last_event_id=EMPTY_TAIL,
        )
    with pytest.raises(ValueError):
        store.append_events(
            account_name="a",
            session_id="s",
            events=[
                NewEvent("user", "good", "a", correlation_ids=("c",)),
                NewEvent("user", "bad", "a", correlation_ids=("",)),
            ],
        )
    assert (
        store.get_exchange(account_name="a", session_id="s", correlation_id="c") is None
    )
    assert store.get_recent_events(account_name="a", session_id="s").events == (one,)
    two = store.append_event(
        account_name="a",
        session_id="s",
        event=NewEvent("user", "two", "a"),
        expected_last_event_id=one.event_id,
    )
    with pytest.raises(EpisodicConcurrencyError):
        store.clear_session_events(
            account_name="a", session_id="s", expected_last_event_id=one.event_id
        )
    assert (
        store.get_session(account_name="a", session_id="s").last_event_id
        == two.event_id
    )


def test_half_open_period_occurrence_order_ties_and_cursor_high_water(store):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    late = append(store, "later", created_at=start + timedelta(hours=2))
    first = append(store, "first", created_at=start)
    tied = append(store, "tied", created_at=start)
    append(store, "exclusive end", created_at=end)
    page = store.get_events_by_period(
        account_name="a", session_id="s", start=start, end=end, count=1
    )
    assert page.events == (first,)
    append(store, "backdated new append", created_at=start)
    page2 = store.get_events_by_period(
        account_name="a",
        session_id="s",
        start=start,
        end=end,
        count=10,
        cursor=page.next_cursor,
    )
    assert page2.events == (tied, late)
    with pytest.raises(ValueError):
        store.get_events_by_period(
            account_name="a",
            session_id="s",
            start=start,
            end=end,
            event_kinds=["user_message"],
            cursor=page.next_cursor,
        )
    with pytest.raises(ValueError):
        store.get_events_by_period(
            account_name="a",
            session_id="s",
            start=start,
            end=end,
            cursor=page.next_cursor + "x",
        )
    with pytest.raises(ValueError):
        store.get_events_by_period(
            account_name="a", session_id="s", start=start.replace(tzinfo=None), end=end
        )
    with pytest.raises(ValueError):
        store.get_events_by_period(
            account_name="a", session_id="s", start=end, end=start
        )


def test_session_cursor_across_reopen_and_recent_anchor_is_session_local(store):
    store.create_session(account_name="a", session_id="two")
    store.create_session(account_name="a", session_id="three")
    page = store.list_sessions(account_name="a", count=1)
    assert page.items[0].session_id == "three"
    path = store.db_path if isinstance(store, SqliteEpisodicMemory) else store.root
    with type(store)(path) as other:
        more = other.list_sessions(account_name="a", cursor=page.next_cursor, count=10)
        assert [s.session_id for s in more.items] == ["two", "s"]
    e = store.append_event(
        account_name="a", session_id="two", event=NewEvent("user", "other", "a")
    )
    with pytest.raises(ValueError):
        store.get_recent_events(
            account_name="a", session_id="s", before_event_id=e.event_id
        )


def test_payload_round_trip_long_content_not_trimmed_and_no_old_dispatcher(store):
    data = {"text": "hello" * 10000, "items": [1, None, True]}
    event = append(store, data, correlation_ids=("c", "parent"))
    assert (
        store.get_event(
            account_name="a", session_id="s", event_id=event.event_id
        ).content
        == data
    )
    assert event.correlation_ids == ("c", "parent")
    with pytest.raises(TypeError):
        store.append_event(account_name="a", session_id="s", event=event)
    for name in (
        "recall",
        "link_event",
        "reset_session",
        "add_events",
        "get_events_by_correlation",
    ):
        assert not hasattr(store, name)


def test_active_window_excludes_controls_and_archived_messages(store):
    old = append(store, "old", correlation_ids=("old",))
    generator = type("Generator", (), {"generate": lambda self, request: "digest"})()
    service = CurationService(store, generator)
    service.archive(account_name="a", session_id="s")
    new = append(store, "new")
    assert store.get_recent_events(account_name="a", session_id="s").events == (new,)
    assert (
        store.get_event(account_name="a", session_id="s", event_id=old.event_id) == old
    )
    start = old.created_at - timedelta(days=1)
    end = new.created_at + timedelta(days=1)
    assert store.get_events_by_period(
        account_name="a", session_id="s", start=start, end=end
    ).events == (old, new)


def _race(backend, path, tail):
    with (SqliteEpisodicMemory if backend == "sqlite" else JsonlEpisodicMemory)(
        path
    ) as s:
        try:
            s.append_event(
                account_name="a",
                session_id="s",
                event=NewEvent("user", "race", "a"),
                expected_last_event_id=tail,
            )
            return "committed"
        except EpisodicConcurrencyError:
            return "conflict"


@pytest.mark.parametrize("backend", [SqliteEpisodicMemory, JsonlEpisodicMemory])
def test_concurrent_process_conditional_append(tmp_path, backend):
    path = tmp_path / "memory"
    with backend(path) as s:
        s.create_session(account_name="a", session_id="s")
        tail = append(s).event_id
    with get_context("spawn").Pool(2) as pool:
        result = pool.starmap(
            _race,
            [("sqlite" if backend is SqliteEpisodicMemory else "jsonl", path, tail)]
            * 2,
        )
    assert sorted(result) == ["committed", "conflict"]


def test_failed_persistence_rolls_back_events_correlations_and_tail(store, monkeypatch):
    original = store._insert_events

    def interrupted(session_id, events):
        original(session_id, events)
        raise OSError("interrupted write")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_insert_events", interrupted)
        with pytest.raises(OSError):
            append(store, correlation_ids=("failed",))
    assert store.get_recent_events(account_name="a", session_id="s").events == ()
    assert (
        store.get_exchange(account_name="a", session_id="s", correlation_id="failed")
        is None
    )
    assert store.get_session(account_name="a", session_id="s").last_event_id is None
    assert append(store).sequence == 1


def test_period_cursor_rechecks_invalidation(store):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    first = append(store, "first", created_at=start)
    append(store, "secret", created_at=start, correlation_ids=("secret",))
    safe = append(store, "safe", created_at=start)
    end = start + timedelta(days=1)
    page = store.get_events_by_period(
        account_name="a", session_id="s", start=start, end=end, count=1
    )
    assert page.events == (first,) and page.next_cursor
    store.invalidate_exchange(account_name="a", session_id="s", correlation_id="secret")
    more = store.get_events_by_period(
        account_name="a", session_id="s", start=start, end=end, cursor=page.next_cursor
    )
    assert more.events == (safe,)
