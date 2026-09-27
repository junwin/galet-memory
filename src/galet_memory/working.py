"""Temporary, run-scoped state for agent handoffs and task execution."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


class WorkingMemoryConflict(RuntimeError):
    """A version check failed or a run identifier is already in use."""


class WorkingRunUnavailable(RuntimeError):
    """The run is missing, finished, or expired."""


@dataclass(frozen=True)
class WorkingValue:
    key: str
    value: Any
    version: int
    task_id: str | None
    updated_at: datetime


class WorkingMemory(Protocol):
    def start_run(self, account_name: str, run_id: str, *, ttl_seconds: int = 3600) -> datetime: ...
    def get(self, account_name: str, run_id: str, key: str,
            *, task_id: str | None = None) -> WorkingValue | None: ...
    def put(self, account_name: str, run_id: str, key: str, value: Any,
            *, expected_version: int, task_id: str | None = None) -> WorkingValue: ...
    def clear_task(self, account_name: str, run_id: str, task_id: str) -> int: ...
    def finish_run(self, account_name: str, run_id: str) -> None: ...
    def purge_expired(self) -> int: ...


_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS working_runs (
    account_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finished INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_name, run_id)
);
CREATE TABLE IF NOT EXISTS working_values (
    account_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_name, run_id, task_id, key),
    FOREIGN KEY (account_name, run_id) REFERENCES working_runs(account_name, run_id) ON DELETE CASCADE
);
"""


class SqliteWorkingMemory:
    """Share temporary JSON state across processes using an explicit run lifetime.

    Reads reject expired and finished runs immediately; purge removes expired
    physical rows. Completing a run removes values and reserves its identifier
    until expiry.
    """

    def __init__(self, db_path: str | Path, *, clock: Callable[[], datetime] | None = None,
                 max_value_bytes: int = 65536) -> None:
        if max_value_bytes <= 0:
            raise ValueError("max_value_bytes must be positive")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_value_bytes = max_value_bytes
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.executescript(_SCHEMA)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetime")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _identity(account_name: str, run_id: str, key: str | None = None) -> None:
        if not account_name or not run_id or key == "":
            raise ValueError("account_name, run_id, and key must be nonempty")

    def _active(self, account_name: str, run_id: str, now: datetime) -> None:
        row = self._conn.execute(
            "SELECT expires_at, finished FROM working_runs WHERE account_name=? AND run_id=?",
            (account_name, run_id),
        ).fetchone()
        if row is None or row[1] or datetime.fromisoformat(row[0]) <= now:
            raise WorkingRunUnavailable(f"run is unavailable: {run_id}")

    def start_run(self, account_name: str, run_id: str, *, ttl_seconds: int = 3600) -> datetime:
        self._identity(account_name, run_id)
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        expires = self._now() + timedelta(seconds=ttl_seconds)
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO working_runs(account_name, run_id, expires_at) VALUES (?, ?, ?)",
                    (account_name, run_id, expires.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkingMemoryConflict("run identifier already exists") from exc
        return expires

    def get(self, account_name: str, run_id: str, key: str,
            *, task_id: str | None = None) -> WorkingValue | None:
        self._identity(account_name, run_id, key)
        now = self._now()
        with self._lock:
            self._active(account_name, run_id, now)
            row = self._conn.execute(
                "SELECT value_json, version, updated_at FROM working_values "
                "WHERE account_name=? AND run_id=? AND task_id=? AND key=?",
                (account_name, run_id, task_id or "", key),
            ).fetchone()
        return (WorkingValue(key, json.loads(row[0]), int(row[1]), task_id,
                             datetime.fromisoformat(row[2])) if row else None)

    def put(self, account_name: str, run_id: str, key: str, value: Any,
            *, expected_version: int, task_id: str | None = None) -> WorkingValue:
        self._identity(account_name, run_id, key)
        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        try:
            encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("working value must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > self.max_value_bytes:
            raise ValueError("working value exceeds max_value_bytes")
        now = self._now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._active(account_name, run_id, now)
                row = self._conn.execute(
                    "SELECT version FROM working_values WHERE account_name=? AND run_id=? AND task_id=? AND key=?",
                    (account_name, run_id, task_id or "", key),
                ).fetchone()
                current = int(row[0]) if row else 0
                if current != expected_version:
                    raise WorkingMemoryConflict(f"expected version {expected_version}, found {current}")
                version = current + 1
                self._conn.execute(
                    "INSERT INTO working_values VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_name, run_id, task_id, key) DO UPDATE SET "
                    "value_json=excluded.value_json, version=excluded.version, updated_at=excluded.updated_at",
                    (account_name, run_id, task_id or "", key, encoded, version, now.isoformat()),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return WorkingValue(key, value, version, task_id, now)

    def clear_task(self, account_name: str, run_id: str, task_id: str) -> int:
        self._identity(account_name, run_id, task_id)
        with self._lock, self._conn:
            self._active(account_name, run_id, self._now())
            cursor = self._conn.execute(
                "DELETE FROM working_values WHERE account_name=? AND run_id=? AND task_id=?",
                (account_name, run_id, task_id),
            )
            return cursor.rowcount

    def finish_run(self, account_name: str, run_id: str) -> None:
        self._identity(account_name, run_id)
        with self._lock, self._conn:
            self._conn.execute("UPDATE working_runs SET finished=1 WHERE account_name=? AND run_id=?",
                               (account_name, run_id))
            self._conn.execute("DELETE FROM working_values WHERE account_name=? AND run_id=?",
                               (account_name, run_id))

    def purge_expired(self) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM working_runs WHERE expires_at<=?",
                                        (self._now().isoformat(),))
            return cursor.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteWorkingMemory":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
