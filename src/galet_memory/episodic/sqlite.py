"""The single SQLite episodic store, using relational sessions and events."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import uuid4

from .invalidation import InvalidationSupport
from .interface import EpisodicEvent, EpisodicMemory, EpisodicMemoryRequest, EpisodicMemoryResult
from .management import EpisodicInvalidationResult, EpisodicConcurrencyError, EpisodicSession, EpisodicSessionQuery, EventScope, EpisodicMemoryManager
from .common import (
    DigestRecall,
    EpisodicCompatibilityError,
    _SESSION_PATCH_FIELDS,
    _json_text,
    _parse_datetime,
    _utc_now,
    _validate_segment,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    friendly_name TEXT,
    context_name TEXT,
    session_type TEXT NOT NULL,
    participants TEXT NOT NULL,
    links TEXT NOT NULL,
    tags TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_account_updated
    ON sessions(account_name, updated_at DESC);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    role TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session_sequence ON events(session_id, sequence);
CREATE TABLE IF NOT EXISTS event_correlations (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    linked_at TEXT NOT NULL,
    UNIQUE(correlation_id, event_id)
);
CREATE INDEX IF NOT EXISTS correlations_id_sequence
    ON event_correlations(correlation_id, sequence);
"""


class SqliteEpisodicMemory(InvalidationSupport, EpisodicMemory, EpisodicMemoryManager):
    """Inspectable, indexed SQLite storage with the existing neutral API.

    Only the relational schema is supported.
    JSON columns hold only flexible fields and event payloads.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        digests_root: str | Path | None = None,
        digest_recall: DigestRecall | None = None,
        initialize_schema: bool = True,
    ) -> None:
        self.db_path = str(db_path)
        self.digests_root = Path(digests_root) if digests_root is not None else None
        self.digest_recall = digest_recall
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            # SQLite's default rollback journal is sufficient for low-volume use.
            self._conn.execute("PRAGMA busy_timeout = 5000")
            present = {row[0] for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if present & {"kv", "logs"}:
                raise EpisodicCompatibilityError(
                    "unsupported episodic database: use a fresh database file; "
                    "only sessions/events/event_correlations are supported")
            if present:
                self._validate_schema()
            if initialize_schema:
                with self._lock, self._conn:
                    self._conn.executescript(_SCHEMA)
            self._validate_schema()
        except BaseException:
            self._conn.close()
            raise

    def _validate_schema(self) -> None:
        required = {
            "sessions": {"session_id", "user_id", "account_name", "agent_name",
                         "friendly_name", "context_name", "session_type", "participants",
                         "links", "tags", "metadata", "created_at", "updated_at"},
            "events": {"sequence", "event_id", "session_id", "created_at", "role",
                       "actor", "kind", "payload", "metadata"},
            "event_correlations": {"sequence", "correlation_id", "event_id", "linked_at"},
        }
        for table, columns in required.items():
            actual = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                raise EpisodicCompatibilityError(f"incompatible episodic table: {table}")

    @staticmethod
    def _session_row(row: sqlite3.Row) -> EpisodicSession:
        return EpisodicSession(
            session_id=row['session_id'], user_id=row['user_id'],
            account_name=row['account_name'], agent_name=row['agent_name'],
            friendly_name=row['friendly_name'], context_name=row['context_name'],
            session_type=row['session_type'],
            participants=json.loads(row['participants']), links=json.loads(row['links']),
            tags=json.loads(row['tags']), metadata=json.loads(row['metadata']),
            created_at=_parse_datetime(row['created_at']),
            updated_at=_parse_datetime(row['updated_at']),
        )

    def _load_session(self, session_id: str) -> Optional[EpisodicSession]:
        row = self._conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return self._session_row(row) if row else None

    def _write_session(self, session: EpisodicSession) -> None:
        self._conn.execute(
            """INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
               user_id=excluded.user_id, account_name=excluded.account_name,
               agent_name=excluded.agent_name, friendly_name=excluded.friendly_name,
               context_name=excluded.context_name, session_type=excluded.session_type,
               participants=excluded.participants, links=excluded.links,
               tags=excluded.tags, metadata=excluded.metadata,
               updated_at=excluded.updated_at""",
            (session.session_id, session.user_id, session.account_name, session.agent_name,
             session.friendly_name, session.context_name, session.session_type,
             _json_text(session.participants), _json_text(session.links),
             _json_text(session.tags), _json_text(session.metadata),
             session.created_at.isoformat(), session.updated_at.isoformat()),
        )

    @staticmethod
    def _event_row(row: sqlite3.Row) -> EpisodicEvent:
        return EpisodicEvent(
            role=row['role'], content=json.loads(row['payload']), kind=row['kind'],
            actor=row['actor'], event_id=row['event_id'],
            created_at=_parse_datetime(row['created_at']),
            metadata=json.loads(row['metadata']),
        )

    def _load_events(self, session_id: str) -> list[EpisodicEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY sequence", (session_id,)
        )
        return [self._event_row(row) for row in rows]

    def create_session(self, *, account_name: str, agent_name: str,
                       user_id: Optional[str] = None, session_id: Optional[str] = None,
                       friendly_name: Optional[str] = None, context_name: Optional[str] = None,
                       tags: Optional[list[str]] = None, session_type: str = 'user',
                       participants: Optional[list[str]] = None, links: Optional[dict[str, Any]] = None,
                       metadata: Optional[dict[str, Any]] = None) -> EpisodicSession:
        from uuid import uuid4
        now = _utc_now()
        session = EpisodicSession(
            session_id=session_id or str(uuid4()), account_name=account_name,
            agent_name=agent_name, user_id=user_id or account_name,
            friendly_name=friendly_name, context_name=context_name,
            tags=list(tags or []), session_type=session_type,
            participants=list(participants or []), links=dict(links or {}),
            metadata=dict(metadata or {}), created_at=now, updated_at=now,
        )
        with self._lock, self._conn:
            if self._load_session(session.session_id):
                raise ValueError(f"Session already exists: {session.session_id}")
            self._write_session(session)
        return session

    def get_session(self, session_id: str, *, include_events: bool = True,
                    event_scope: EventScope = 'active') -> Optional[EpisodicSession]:
        if event_scope not in ('active', 'all', 'archived', 'raw'):
            raise ValueError(f"unsupported event scope: {event_scope!r}")
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                return None
            raw = self._load_events(session_id)
            events = self._select_event_scope(raw, event_scope) if include_events else []
            return replace(session, events=events, last_event_id=raw[-1].event_id if raw else None)

    def list_sessions(self, query: EpisodicSessionQuery) -> list[EpisodicSession]:
        if query.limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE account_name=? AND (?='' OR agent_name=?) ORDER BY updated_at DESC",
                (query.account_name, query.agent_name, query.agent_name),
            )
            result = []
            needle = query.query.casefold().strip()
            for row in rows:
                session = self._session_row(row)
                if needle:
                    events = self._select_event_scope(self._load_events(session.session_id), "all")
                    if not any(needle in self._content_text(e.content).casefold() for e in events):
                        continue
                    session = replace(session, events=events)
                result.append(session)
                if len(result) >= query.limit:
                    break
            return result

    def session_exists(self, session_id: str) -> bool:
        with self._lock:
            return self._load_session(session_id) is not None

    def _insert_events(self, session_id: str, events: list[EpisodicEvent]) -> None:
        self._conn.executemany(
            "INSERT INTO events(event_id,session_id,created_at,role,actor,kind,payload,metadata) VALUES (?,?,?,?,?,?,?,?)",
            [(e.event_id, session_id, e.created_at.isoformat(), e.role, e.actor, e.kind,
              _json_text(e.content), _json_text(e.metadata)) for e in events],
        )
        session = self._load_session(session_id)
        self._write_session(replace(session, updated_at=_utc_now()))

    def add_events(self, session_id: str, events: list[EpisodicEvent]) -> list[EpisodicEvent]:
        stored = [self._stored_event(e) for e in events]
        if not stored:
            return []
        with self._lock, self._conn:
            if not self._load_session(session_id):
                raise ValueError(f"Session not found: {session_id}")
            self._insert_events(session_id, stored)
        return stored

    def append_event_if_tail(self, session_id: str, event: EpisodicEvent,
                             *, expected_last_event_id: Optional[str]) -> EpisodicEvent:
        stored = self._stored_event(event)
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                if not self._load_session(session_id):
                    raise ValueError(f"Session not found: {session_id}")
                row = self._conn.execute(
                    "SELECT event_id FROM events WHERE session_id=? ORDER BY sequence DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                actual = row[0] if row else None
                if actual != expected_last_event_id:
                    raise EpisodicConcurrencyError(f"session event tail changed: expected {expected_last_event_id!r}, found {actual!r}")
                self._insert_events(session_id, [stored])
                self._conn.execute('COMMIT')
            except BaseException:
                self._conn.execute('ROLLBACK')
                raise
        return stored

    def link_event(self, correlation_id: Optional[str], session_id: str, event_id: str) -> None:
        if not correlation_id:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._link_event(correlation_id, session_id, event_id)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _link_event(self, correlation_id: str, session_id: str, event_id: str) -> None:
        self._check_correlation(correlation_id, session_id)
        row = self._conn.execute("SELECT session_id FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None or row[0] != session_id:
            raise ValueError(f"Event not found in session {session_id}: {event_id}")
        self._conn.execute(
            "INSERT OR IGNORE INTO event_correlations(correlation_id,event_id,linked_at) VALUES (?,?,?)",
            (correlation_id, event_id, _utc_now().isoformat()),
        )

    def get_events_by_correlation(self, correlation_id: str) -> list[EpisodicEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.* FROM event_correlations c JOIN events e ON e.event_id=c.event_id "
                "WHERE c.correlation_id=? ORDER BY c.sequence", (correlation_id,)
            )
            rows = list(rows)
            visible = {e.event_id for sid in {row["session_id"] for row in rows}
                       for e in self._select_event_scope(self._load_events(sid), "all")}
            return [self._event_row(row) for row in rows if row["event_id"] in visible]

    def _linked_event_ids(self, correlation_id: str, session_id: str) -> list[str]:
        return [row[0] for row in self._conn.execute(
            "SELECT e.event_id FROM event_correlations c JOIN events e ON e.event_id=c.event_id "
            "WHERE c.correlation_id=? AND e.session_id=? ORDER BY e.sequence",
            (correlation_id, session_id))]

    def _append_invalidation(self, session_id: str, event: EpisodicEvent) -> EpisodicEvent:
        stored = self._stored_event(event)
        self._insert_events(session_id, [stored])
        return stored

    def invalidate_events(self, *, account_name: str, session_id: str, correlation_id: str,
                          expected_last_event_id: Optional[str] = None) -> EpisodicInvalidationResult:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._invalidate(
                    account_name=account_name, session_id=session_id, correlation_id=correlation_id,
                    expected_last_event_id=expected_last_event_id)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._discard_overflow(account_name, session_id, result)
            return result

    def update_session(self, session_id: str, patch: dict[str, Any]) -> EpisodicSession:
        unknown = set(patch) - _SESSION_PATCH_FIELDS
        if unknown:
            raise ValueError('unsupported session patch fields: ' + ', '.join(sorted(unknown)))
        with self._lock, self._conn:
            session = self._load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")
            values = dict(patch)
            for field in ('participants', 'tags'):
                if field in values:
                    values[field] = list(values[field] or [])
            for field in ('links', 'metadata'):
                if field in values:
                    values[field] = dict(values[field] or {})
            updated = replace(session, updated_at=_utc_now(), **values)
            self._write_session(updated)
            return updated

    def reset_session(self, session_id: str) -> None:
        with self._lock, self._conn:
            session = self._load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")
            # Keep deletion history so old external digests cannot become valid
            # again when the rest of a transcript is discarded.
            self._conn.execute("DELETE FROM events WHERE session_id=? AND kind != 'events_invalidated'", (session_id,))
            self._write_session(replace(session, updated_at=_utc_now()))

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    def delete_sessions(self, session_ids: Sequence[str]) -> list[str]:
        ids = list(dict.fromkeys(session_ids))
        for session_id in ids:
            _validate_segment(session_id, name='session_id')
        with self._lock, self._conn:
            found = [sid for sid in ids if self._load_session(sid)]
            self._conn.executemany("DELETE FROM sessions WHERE session_id=?", [(sid,) for sid in found])
            return found

    @staticmethod
    def _stored_event(event: EpisodicEvent) -> EpisodicEvent:
        role = event.role
        return replace(
            event,
            event_id=event.event_id or str(uuid4()),
            created_at=event.created_at or _utc_now(),
            actor=event.actor or role,
            kind=event.kind
            or ("user_message" if role == "user" else "assistant_message"),
            metadata=dict(event.metadata),
        )

    def append_event(self, session_id: str, event: EpisodicEvent) -> EpisodicEvent:
        return self.add_events(session_id, [event])[0]

    def recall(self, request: EpisodicMemoryRequest) -> EpisodicMemoryResult:
        session = (
            self.get_session(request.conversation_id)
            if request.conversation_id
            else None
        )
        if session is not None and session.account_name != request.account_name:
            session = None
        result = EpisodicMemoryResult()
        if session is not None and request.include_session_metadata:
            result.session_id = session.session_id
            result.session_user_id = session.user_id
            result.session_account_name = session.account_name
            result.session_agent_name = session.agent_name
            result.session_context_name = session.context_name or ""
            result.session_type = session.session_type
            result.session_participants = list(session.participants)
            result.session_friendly_name = session.friendly_name or ""
            result.session_tags = list(session.tags)
            result.session_updated_at = session.updated_at
            result.metadata.update(session.metadata)
        if session is not None and request.include_recent_history:
            events = list(session.events)
            if request.event_kinds:
                allowed = set(request.event_kinds)
                events = [event for event in events if event.kind in allowed]
            selected = events[-request.max_events :] if request.max_events > 0 else []
            if request.token_budget is not None:
                selected = self._apply_token_budget(selected, request.token_budget)
            result.events = selected
            result.dropped_event_count = max(0, len(events) - len(selected))
        if request.include_archived_digests:
            if self.digest_recall is None:
                result.metadata["archived_digests"] = "not_configured"
            else:
                result.digests = self._filter_digests(request, self.digest_recall(request))
        return result

    @classmethod
    def _apply_token_budget(
        cls, events: list[EpisodicEvent], token_budget: int
    ) -> list[EpisodicEvent]:
        remaining = max(0, token_budget)
        selected: list[EpisodicEvent] = []
        for event in reversed(events):
            estimate = max(1, len(cls._content_text(event.content)) // 4)
            if selected and estimate > remaining:
                break
            selected.insert(0, event)
            remaining = max(0, remaining - estimate)
        return selected

    @staticmethod
    def _content_text(content: Any) -> str:
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteEpisodicMemory":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["SqliteEpisodicMemory", "EpisodicCompatibilityError"]
