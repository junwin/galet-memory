import json

import pytest

from galet_memory import (
    EpisodicConcurrencyError,
    EpisodicEvent,
    EpisodicMemoryManager,
    EpisodicMemoryRequest,
    EpisodicSessionQuery,
    JsonlEpisodicMemory,
)


def test_jsonl_memory_implements_neutral_manager_and_lucy_layout(tmp_path):
    root = tmp_path / "chat2"
    with JsonlEpisodicMemory(root) as memory:
        assert isinstance(memory, EpisodicMemoryManager)
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

    assert (root / "sessions/session-1/meta.json").is_file()
    assert (root / "sessions/session-1/events.jsonl").is_file()
    assert (root / "correlations/run-1.jsonl").is_file()
    lines = (root / "sessions/session-1/events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert [json.loads(line)["payload"] for line in lines] == [
        "hello",
        {"answer": "hi"},
    ]


def test_jsonl_reads_existing_lucy_chat2_files(tmp_path):
    root = tmp_path / "chat2"
    session_dir = root / "sessions/existing-session"
    session_dir.mkdir(parents=True)
    (session_dir / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "existing-session",
                "user_id": "acct",
                "account_name": "acct",
                "agent_name": "lucy",
                "friendly_name": "Existing",
                "context_name": "project",
                "created_at": "2026-09-12T12:00:00",
                "updated_at": "2026-09-12T12:01:00",
                "tags": ["legacy"],
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "event_id": "event-from-lucy",
                "ts": "2026-09-12T12:01:00",
                "role": "user",
                "actor": "acct",
                "kind": "user_message",
                "payload": "stored by Lucy",
                "metadata": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with JsonlEpisodicMemory(root) as memory:
        session = memory.get_session("existing-session")

    assert session is not None
    assert session.tags == ["legacy"]
    assert session.events[0].content == "stored by Lucy"


def test_jsonl_lifecycle_recall_search_and_conditional_append(tmp_path):
    with JsonlEpisodicMemory(tmp_path / "chat2") as memory:
        memory.create_session(
            account_name="acct", agent_name="lucy", session_id="one"
        )
        stored = memory.add_events(
            "one",
            [
                EpisodicEvent(role="user", content="alpha"),
                EpisodicEvent(role="assistant", content="beta"),
            ],
        )
        final = memory.append_event_if_tail(
            "one",
            EpisodicEvent(role="user", content="needle"),
            expected_last_event_id=stored[-1].event_id,
        )

        with pytest.raises(EpisodicConcurrencyError):
            memory.append_event_if_tail(
                "one",
                EpisodicEvent(role="assistant", content="stale"),
                expected_last_event_id=stored[-1].event_id,
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
        memory.update_session("one", {"friendly_name": "Updated"})

        assert final.content == "needle"
        assert [item.session_id for item in matches] == ["one"]
        assert [event.content for event in result.events] == [
            "beta",
            "needle",
        ]
        assert result.dropped_event_count == 1
        assert memory.get_session("one").friendly_name == "Updated"

        memory.reset_session("one")
        assert memory.get_session("one").events == []
        memory.delete_session("one")
        assert not memory.session_exists("one")


def test_jsonl_event_visibility_scopes(tmp_path):
    with JsonlEpisodicMemory(tmp_path / "chat2") as memory:
        memory.create_session(
            account_name="acct", agent_name="lucy", session_id="one"
        )
        memory.add_events(
            "one",
            [
                EpisodicEvent("user", "old"),
                EpisodicEvent(
                    "system",
                    "digest",
                    kind="session_digest",
                    metadata={"visibility_boundary": True},
                ),
                EpisodicEvent("user", "new"),
            ],
        )

        active = memory.get_session("one", event_scope="active")
        archived = memory.get_session("one", event_scope="archived")
        all_events = memory.get_session("one", event_scope="all")

    assert [event.content for event in active.events] == ["digest", "new"]
    assert [event.content for event in archived.events] == ["old"]
    assert [event.content for event in all_events.events] == [
        "old",
        "digest",
        "new",
    ]
