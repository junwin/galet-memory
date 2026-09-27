from datetime import datetime, timedelta, timezone

import pytest

from galet_memory import SqliteWorkingMemory, WorkingMemoryConflict, WorkingRunUnavailable


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 27, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def test_run_and_task_handoff_is_scoped_and_versioned(tmp_path):
    path = tmp_path / "working.sqlite"
    with SqliteWorkingMemory(path) as first, SqliteWorkingMemory(path) as second:
        first.start_run("alice", "run-1")
        created = first.put("alice", "run-1", "plan", {"steps": ["inspect", "edit"]},
                            expected_version=0)
        assert second.get("alice", "run-1", "plan").value == created.value
        with pytest.raises(WorkingMemoryConflict):
            second.put("alice", "run-1", "plan", "stale", expected_version=0)
        revised = second.put("alice", "run-1", "plan", {"steps": ["done"]},
                             expected_version=created.version)
        assert revised.version == 2
        first.put("alice", "run-1", "result", "agent two output",
                  task_id="task-2", expected_version=0)
        assert second.get("alice", "run-1", "result", task_id="task-2").value == "agent two output"
        assert second.get("alice", "run-1", "result") is None
        assert first.clear_task("alice", "run-1", "task-2") == 1
        assert first.get("alice", "run-1", "plan").version == 2
        with pytest.raises(WorkingRunUnavailable):
            second.get("bob", "run-1", "plan")
        first.finish_run("alice", "run-1")
        assert first.purge_expired() == 0
        with pytest.raises(WorkingMemoryConflict):
            first.start_run("alice", "run-1")
        with pytest.raises(WorkingRunUnavailable):
            second.get("alice", "run-1", "plan")
        with pytest.raises(WorkingRunUnavailable):
            second.put("alice", "run-1", "plan", "resurrect", expected_version=0)


def test_ttl_is_enforced_without_cleanup_and_purge_removes_rows(tmp_path):
    clock = Clock()
    with SqliteWorkingMemory(tmp_path / "working.sqlite", clock=clock) as memory:
        memory.start_run("alice", "run-1", ttl_seconds=30)
        memory.put("alice", "run-1", "note", "temporary", expected_version=0)
        clock.value += timedelta(seconds=30)
        with pytest.raises(WorkingRunUnavailable):
            memory.get("alice", "run-1", "note")
        assert memory.purge_expired() == 1
        assert memory.purge_expired() == 0


def test_run_identity_and_json_bounds(tmp_path):
    with SqliteWorkingMemory(tmp_path / "working.sqlite", max_value_bytes=20) as memory:
        memory.start_run("alice", "run-1")
        with pytest.raises(WorkingMemoryConflict):
            memory.start_run("alice", "run-1")
        with pytest.raises(ValueError, match="max_value_bytes"):
            memory.put("alice", "run-1", "large", "x" * 30, expected_version=0)
        assert memory.get("alice", "run-1", "large") is None
        with pytest.raises(ValueError):
            memory.put("alice", "run-1", "invalid", {"bad": object()}, expected_version=0)
