import sqlite3
import pytest
from galet_memory import (
    SqliteEpisodicMemory,
    JsonlEpisodicMemory,
    NewEvent,
    EpisodicCompatibilityError,
)


def test_inspectable_schema_atomic_links_and_cascade(tmp_path):
    path = tmp_path / "chat.sqlite"
    with SqliteEpisodicMemory(path) as store:
        store.create_session(account_name="a", session_id="s")
        event = store.append_event(
            account_name="a",
            session_id="s",
            event=NewEvent(
                "user", {"text": "hello"}, "a", correlation_ids=("one", "two")
            ),
        )
    with SqliteEpisodicMemory(path, initialize_schema=False) as store:
        assert (
            store.get_event(account_name="a", session_id="s", event_id=event.event_id)
            == event
        )
        with sqlite3.connect(path) as conn:
            assert "agent_name" not in {
                r[1] for r in conn.execute("PRAGMA table_info(sessions)")
            }
            assert conn.execute(
                "SELECT correlation_id FROM event_correlations ORDER BY correlation_id"
            ).fetchall() == [("one",), ("two",)]
        assert store.delete_session(account_name="a", session_id="s")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM event_correlations").fetchone()[0] == 0
        )


@pytest.mark.parametrize("table", ["kv", "logs", "sessions"])
@pytest.mark.parametrize("initialize", [True, False])
def test_old_sqlite_is_rejected_without_modification(tmp_path, table, initialize):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE {table}(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(EpisodicCompatibilityError, match="fresh database"):
        SqliteEpisodicMemory(path, initialize_schema=initialize)
    assert path.read_bytes() == before


def test_jsonl_rejects_old_layout(tmp_path):
    (tmp_path / "sessions").mkdir()
    with pytest.raises(EpisodicCompatibilityError, match="fresh directory"):
        JsonlEpisodicMemory(tmp_path)
