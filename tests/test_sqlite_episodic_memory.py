import json
import sqlite3
from datetime import datetime

import pytest

from galet_memory import (
    EpisodicEvent,
    EpisodicMemoryManager,
    EpisodicMemoryRequest,
    EpisodicSessionQuery,
    SqliteEpisodicMemory,
)


def test_sqlite_memory_implements_neutral_manager_and_schema(tmp_path):
    path = tmp_path / "chat2.sqlite"
    with SqliteEpisodicMemory(path) as memory:
        assert isinstance(memory, EpisodicMemoryManager)

    connection = sqlite3.connect(path)
    assert {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    } >= {"sessions", "events", "event_correlations"}
    connection.close()


def test_session_lifecycle_events_and_correlation_use_relational_layout(tmp_path):
    path = tmp_path / "chat2.sqlite"
    with SqliteEpisodicMemory(path) as memory:
        session = memory.create_session(
            account_name="acct",
            agent_name="lucy",
            session_id="session-1",
            friendly_name="First",
            context_name="project",
            tags=["test"],
            metadata={"origin": "test"},
        )
        events = memory.add_events(
            session.session_id,
            [
                EpisodicEvent(role="user", content="hello"),
                EpisodicEvent(
                    role="assistant",
                    content={"answer": "hi"},
                    kind="assistant_message",
                    actor="lucy",
                ),
            ],
        )
        memory.link_event("run-1", session.session_id, events[1].event_id)
        memory.update_session(session.session_id, {"friendly_name": None})

        loaded = memory.get_session(session.session_id)
        assert loaded is not None
        assert loaded.friendly_name is None
        assert [event.content for event in loaded.events] == [
            "hello",
            {"answer": "hi"},
        ]
        assert all(event.event_id for event in loaded.events)

    connection = sqlite3.connect(path)
    assert connection.execute("SELECT session_id FROM sessions").fetchall() == [("session-1",)]
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert connection.execute("SELECT correlation_id FROM event_correlations").fetchall() == [("run-1",)]
    connection.close()




def test_recall_and_list_sessions_are_backed_by_sqlite(tmp_path):
    with SqliteEpisodicMemory(tmp_path / "chat2.sqlite") as memory:
        memory.create_session(
            account_name="acct", agent_name="lucy", session_id="one"
        )
        memory.add_events(
            "one",
            [
                EpisodicEvent(role="user", content="alpha"),
                EpisodicEvent(role="assistant", content="beta"),
                EpisodicEvent(role="user", content="needle"),
            ],
        )

        matches = memory.list_sessions(
            EpisodicSessionQuery(account_name="acct", query="needle")
        )
        result = memory.recall(
            EpisodicMemoryRequest(
                account_name="acct",
                agent_name="lucy",
                conversation_id="one",
                max_events=2,
                include_archived_digests=False,
            )
        )

        assert [item.session_id for item in matches] == ["one"]
        assert [event.content for event in result.events] == ["beta", "needle"]
        assert result.dropped_event_count == 1

        memory.reset_session("one")
        assert memory.get_session("one").events == []
        memory.delete_session("one")
        assert not memory.session_exists("one")


def test_add_event_requires_an_existing_session(tmp_path):
    with SqliteEpisodicMemory(tmp_path / "chat2.sqlite") as memory:
        with pytest.raises(ValueError, match="Session not found"):
            memory.append_event("missing", EpisodicEvent("user", "hello"))


def test_update_rejects_fields_outside_the_neutral_contract(tmp_path):
    with SqliteEpisodicMemory(tmp_path / "chat2.sqlite") as memory:
        memory.create_session(
            account_name="acct", agent_name="lucy", session_id="one"
        )
        with pytest.raises(ValueError, match="unsupported session patch"):
            memory.update_session("one", {"lucy_only_field": True})


def test_delete_sessions_removes_selected_keys_in_one_transaction(tmp_path):
    path = tmp_path / "chat2.sqlite"
    with SqliteEpisodicMemory(path) as memory:
        for sid in ("one", "two", "keep"):
            memory.create_session(account_name="acct", agent_name="lucy", session_id=sid)
            memory.append_event(sid, EpisodicEvent("user", sid))
        assert memory.delete_sessions(["one", "missing", "one", "two"]) == ["one", "two"]
        assert memory.delete_sessions([]) == []
        assert memory.session_exists("keep")
        assert not memory.session_exists("one")
        assert not memory.session_exists("two")
    with sqlite3.connect(path) as connection:
        sessions = {row[0] for row in connection.execute("SELECT session_id FROM sessions")}
        events = {row[0] for row in connection.execute("SELECT session_id FROM events")}
    assert sessions == events == {"keep"}


def test_delete_sessions_validates_all_ids_before_removing_any(tmp_path):
    with SqliteEpisodicMemory(tmp_path / "chat2.sqlite") as memory:
        memory.create_session(account_name="acct", agent_name="lucy", session_id="one")
        with pytest.raises(ValueError, match="session_id"):
            memory.delete_sessions(["one", "../bad"])
        assert memory.session_exists("one")
