"""Relational SQLite implementation of the explicit episodic contracts."""

from __future__ import annotations
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .models import Event, Session, EpisodicCompatibilityError
from .store import EpisodicStore

_SCHEMA = """
CREATE TABLE store_info (version INTEGER NOT NULL, cursor_key TEXT NOT NULL);
CREATE TABLE sessions (
 session_id TEXT PRIMARY KEY, account_name TEXT NOT NULL,
 friendly_name TEXT, context_name TEXT, tags TEXT NOT NULL, metadata TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX sessions_account_updated ON sessions(account_name,updated_at DESC,session_id DESC);
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
 created_at TEXT NOT NULL, stored_at TEXT NOT NULL, role TEXT NOT NULL,
 actor TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, metadata TEXT NOT NULL
);
CREATE INDEX events_session_sequence ON events(session_id,sequence);
CREATE INDEX events_session_time ON events(session_id,created_at,sequence);
CREATE TABLE event_correlations (
 event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
 correlation_id TEXT NOT NULL, PRIMARY KEY(event_id,correlation_id)
);
CREATE INDEX correlations_id ON event_correlations(correlation_id,event_id);
"""


class SqliteEpisodicMemory(EpisodicStore):
    """Sessions and events only: no combined recall or token budgeting API."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        digests_root=None,
        digest_search=None,
        initialize_schema=True,
    ):
        self.db_path = str(db_path)
        self.digests_root = Path(digests_root) if digests_root is not None else None
        self.digest_search = digest_search
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            present = {
                r[0]
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not present and initialize_schema:
                self._conn.executescript(_SCHEMA)
                self._conn.execute(
                    "INSERT INTO store_info VALUES (?,?)", (2, secrets.token_hex(32))
                )
                self._conn.commit()
            info = self._conn.execute(
                "SELECT version,cursor_key FROM store_info"
            ).fetchall()
            if len(info) != 1 or info[0][0] != 2:
                raise ValueError("wrong schema version")
            expected = {
                "sessions": {
                    "session_id",
                    "account_name",
                    "friendly_name",
                    "context_name",
                    "tags",
                    "metadata",
                    "created_at",
                    "updated_at",
                },
                "events": {
                    "sequence",
                    "event_id",
                    "session_id",
                    "created_at",
                    "stored_at",
                    "role",
                    "actor",
                    "kind",
                    "payload",
                    "metadata",
                },
                "event_correlations": {"event_id", "correlation_id"},
            }
            for table, fields in expected.items():
                if {
                    r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")
                } != fields:
                    raise ValueError("unexpected columns")
            self._cursor_key = bytes.fromhex(info[0][1])
        except (sqlite3.Error, ValueError) as exc:
            self._conn.close()
            raise EpisodicCompatibilityError(
                "unsupported episodic database; use a fresh database (no migration)"
            ) from exc

    @contextmanager
    def _transaction(self, write=False):
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._depth += 1
            try:
                yield
                if outer:
                    self._conn.commit()
            except BaseException:
                if outer:
                    self._conn.rollback()
                raise
            finally:
                self._depth -= 1

    @staticmethod
    def _session_row(r):
        return Session(
            r["session_id"],
            r["account_name"],
            r["friendly_name"],
            r["context_name"],
            tuple(json.loads(r["tags"])),
            json.loads(r["metadata"]),
            datetime.fromisoformat(r["created_at"]),
            datetime.fromisoformat(r["updated_at"]),
        )

    def _load_session(self, sid):
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        return self._session_row(row) if row else None

    def _all_sessions(self):
        return [
            self._session_row(r) for r in self._conn.execute("SELECT * FROM sessions")
        ]

    def _write_session(self, s):
        self._conn.execute(
            """INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)
         ON CONFLICT(session_id) DO UPDATE SET friendly_name=excluded.friendly_name,
         context_name=excluded.context_name,tags=excluded.tags,metadata=excluded.metadata,
         updated_at=excluded.updated_at""",
            (
                s.session_id,
                s.account_name,
                s.friendly_name,
                s.context_name,
                json.dumps(s.tags),
                json.dumps(s.metadata),
                s.created_at.isoformat(),
                s.updated_at.isoformat(),
            ),
        )

    def _load_events(self, sid):
        links = {}
        for r in self._conn.execute(
            "SELECT c.event_id,c.correlation_id FROM event_correlations c JOIN events e ON e.event_id=c.event_id WHERE e.session_id=? ORDER BY c.correlation_id",
            (sid,),
        ):
            links.setdefault(r[0], []).append(r[1])
        return [
            Event(
                role=r["role"],
                content=json.loads(r["payload"]),
                actor=r["actor"],
                kind=r["kind"],
                created_at=datetime.fromisoformat(r["created_at"]),
                metadata=json.loads(r["metadata"]),
                correlation_ids=tuple(links.get(r["event_id"], ())),
                event_id=r["event_id"],
                session_id=r["session_id"],
                sequence=r["sequence"],
                stored_at=datetime.fromisoformat(r["stored_at"]),
            )
            for r in self._conn.execute(
                "SELECT * FROM events WHERE session_id=? ORDER BY sequence", (sid,)
            )
        ]

    def _insert_events(self, sid, events):
        result = []
        for e in events:
            cursor = self._conn.execute(
                "INSERT INTO events(event_id,session_id,created_at,stored_at,role,actor,kind,payload,metadata) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    e.event_id,
                    sid,
                    e.created_at.isoformat(),
                    e.stored_at.isoformat(),
                    e.role,
                    e.actor,
                    e.kind,
                    json.dumps(e.content),
                    json.dumps(e.metadata),
                ),
            )
            self._conn.executemany(
                "INSERT INTO event_correlations VALUES (?,?)",
                [(e.event_id, c) for c in e.correlation_ids],
            )
            result.append(replace(e, sequence=cursor.lastrowid))
        return result

    def _tail(self, sid):
        r = self._conn.execute(
            "SELECT event_id FROM events WHERE session_id=? ORDER BY sequence DESC LIMIT 1",
            (sid,),
        ).fetchone()
        return r[0] if r else None

    def _max_sequence(self):
        r = self._conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='events'"
        ).fetchone()
        return r[0] if r else 0

    def _delete_session(self, sid):
        self._conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))

    def _clear_events(self, sid):
        c = self._conn.execute(
            "DELETE FROM events WHERE session_id=? AND kind!='events_invalidated'",
            (sid,),
        )
        return c.rowcount

    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
