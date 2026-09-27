"""Run a two-agent handoff and expiration check with disposable storage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from ..working import SqliteWorkingMemory, WorkingMemoryConflict, WorkingRunUnavailable


def main() -> int:
    current = [datetime.now(timezone.utc)]
    clock = lambda: current[0]
    with TemporaryDirectory(prefix="galet-working-") as directory:
        path = Path(directory) / "working.sqlite"
        with SqliteWorkingMemory(path, clock=clock) as first, SqliteWorkingMemory(path, clock=clock) as second:
            first.start_run("demo", "run-one", ttl_seconds=60)
            plan = first.put("demo", "run-one", "plan", {"next": "inspect"}, expected_version=0)
            received = second.get("demo", "run-one", "plan")
            assert received.value == plan.value
            second.put("demo", "run-one", "plan", {"next": "report"},
                       expected_version=received.version)
            try:
                first.put("demo", "run-one", "plan", "stale", expected_version=plan.version)
            except WorkingMemoryConflict:
                conflict_detected = True
            else:
                raise AssertionError("stale update was accepted")
            current[0] += timedelta(seconds=60)
            try:
                second.get("demo", "run-one", "plan")
            except WorkingRunUnavailable:
                expired = True
            else:
                raise AssertionError("expired run was readable")
            purged = first.purge_expired()
    print(json.dumps({"handoff": received.value, "conflict_detected": conflict_detected,
                      "expired": expired, "purged_runs": purged, "checks": "passed"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
