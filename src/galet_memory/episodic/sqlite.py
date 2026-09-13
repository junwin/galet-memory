from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from uuid import uuid4

from .interface import (
    EpisodicDigest,
    EpisodicEvent,
    EpisodicMemory,
    EpisodicMemoryRequest,
    EpisodicMemoryResult,
)
from .management import (
    EpisodicMemoryManager,
    EpisodicSession,
    EpisodicSessionQuery,
)


DigestRecall = Callable[[EpisodicMemoryRequest], Sequence[EpisodicDigest]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    key  TEXT NOT NULL,
    seq  INTEGER NOT NULL,
    line TEXT NOT NULL,
    PRIMARY KEY (key, seq)
);
"""

_SESSION_PATCH_FIELDS = frozenset(
    {
        "user_id",
        "account_name",
        "agent_name",
        "friendly_name",
        "context_name",
        "session_type",
        "participants",
        "links",
        "tags",
        "metadata",
    }
)


class EpisodicCompatibilityError(ValueError):
    """Raised when a database contains an incompatible episodic record."""


def _utc_now() -> datetime:
    # Lucy's existing JSON records use timezone-naive values representing UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _validate_segment(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str, got {type(value).__name__}")
    if not value or "/" in value or ".." in value:
        raise ValueError(f"{name} is not a valid storage key segment: {value!r}")
    return value


class SqliteEpisodicMemory(EpisodicMemory, EpisodicMemoryManager):
    """Episodic session, event, and recall storage backed by SQLite.

    The physical ``kv``/``logs`` schema and logical key layout are compatible
    with Lucy's existing Chat2 SQLite databases.  The public API and stored
    values use only galet-memory's neutral models.
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
        self._lock = threading.RLock()
        try:
            self._configure()
            if initialize_schema:
                self._initialize_schema()
            self._validate_schema()
        except BaseException:
            self._conn.close()
            raise

    def _configure(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
            self._conn.execute("PRAGMA synchronous = NORMAL").fetchone()
            self._conn.execute("PRAGMA case_sensitive_like = ON").fetchone()

    def _initialize_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def _validate_schema(self) -> None:
        required = {
            "kv": {"key", "value", "updated_at"},
            "logs": {"key", "seq", "line"},
        }
        with self._lock:
            for table, columns in required.items():
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                present = {str(row[1]) for row in rows}
                missing = columns - present
                if missing:
                    raise EpisodicCompatibilityError(
                        f"{table} is missing columns: {', '.join(sorted(missing))}"
                    )

    @staticmethod
    def _meta_key(session_id: str) -> str:
        return f"sessions/{_validate_segment(session_id, name='session_id')}/meta.json"

    @staticmethod
    def _events_key(session_id: str) -> str:
        return f"sessions/{_validate_segment(session_id, name='session_id')}/events.jsonl"

    @staticmethod
    def _correlation_key(correlation_id: str) -> str:
        return f"correlations/{_validate_segment(correlation_id, name='correlation_id')}.jsonl"

    def _read_text(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else None

    def _write_text(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )

    def _read_lines(self, key: str) -> list[str]:
        return [
            str(row[0])
            for row in self._conn.execute(
                "SELECT line FROM logs WHERE key = ? ORDER BY seq", (key,)
            ).fetchall()
        ]

    def _append_lines(self, key: str, lines: Iterable[str]) -> None:
        items = list(lines)
        if not items:
            return
        start = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM logs WHERE key = ?", (key,)
        ).fetchone()[0]
        self._conn.executemany(
            "INSERT INTO logs(key, seq, line) VALUES (?, ?, ?)",
            [(key, start + index, line) for index, line in enumerate(items)],
        )

    @staticmethod
    def _session_payload(
        session: EpisodicSession,
    ) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "account_name": session.account_name,
            "agent_name": session.agent_name,
            "participants": list(session.participants),
            "session_type": session.session_type,
            "friendly_name": session.friendly_name,
            "context_name": session.context_name,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "tags": list(session.tags),
            "links": dict(session.links) or None,
            "metadata": dict(session.metadata),
        }

    @staticmethod
    def _session_from_payload(
        payload: dict[str, Any], events: Optional[list[EpisodicEvent]] = None
    ) -> EpisodicSession:
        try:
            return EpisodicSession(
                session_id=str(payload["session_id"]),
                user_id=str(payload.get("user_id") or payload["account_name"]),
                account_name=str(payload["account_name"]),
                agent_name=str(payload["agent_name"]),
                friendly_name=payload.get("friendly_name"),
                context_name=payload.get("context_name"),
                session_type=str(payload.get("session_type") or "user"),
                participants=[str(item) for item in payload.get("participants") or []],
                links=dict(payload.get("links") or {}),
                created_at=_parse_datetime(payload.get("created_at")),
                updated_at=_parse_datetime(payload.get("updated_at")),
                tags=[str(item) for item in payload.get("tags") or []],
                metadata=dict(payload.get("metadata") or {}),
                events=list(events or []),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EpisodicCompatibilityError("invalid episodic session record") from exc

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

    @staticmethod
    def _event_payload(event: EpisodicEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "ts": event.created_at,
            "role": event.role,
            "actor": event.actor,
            "kind": event.kind,
            "payload": event.content,
            "metadata": dict(event.metadata),
        }

    @staticmethod
    def _event_from_payload(payload: dict[str, Any]) -> EpisodicEvent:
        try:
            return EpisodicEvent(
                event_id=str(payload["event_id"]),
                created_at=_parse_datetime(payload.get("ts") or payload.get("created_at")),
                role=str(payload["role"]),
                actor=str(payload.get("actor") or payload["role"]),
                kind=str(payload.get("kind") or ""),
                content=payload.get("payload", payload.get("content")),
                metadata=dict(payload.get("metadata") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EpisodicCompatibilityError("invalid episodic event record") from exc

    def _load_session(self, session_id: str) -> Optional[EpisodicSession]:
        raw = self._read_text(self._meta_key(session_id))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EpisodicCompatibilityError("invalid episodic session JSON") from exc
        return self._session_from_payload(payload)

    def _load_events(self, session_id: str) -> list[EpisodicEvent]:
        events: list[EpisodicEvent] = []
        for line in self._read_lines(self._events_key(session_id)):
            if line.strip():
                try:
                    events.append(self._event_from_payload(json.loads(line)))
                except json.JSONDecodeError as exc:
                    raise EpisodicCompatibilityError("invalid episodic event JSON") from exc
        return events

    def create_session(
        self,
        *,
        account_name: str,
        agent_name: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        friendly_name: Optional[str] = None,
        context_name: Optional[str] = None,
        tags: Optional[list[str]] = None,
        session_type: str = "user",
        participants: Optional[list[str]] = None,
        links: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> EpisodicSession:
        sid = session_id or str(uuid4())
        now = _utc_now()
        session = EpisodicSession(
            session_id=sid,
            user_id=user_id or account_name,
            account_name=account_name,
            agent_name=agent_name,
            friendly_name=friendly_name,
            context_name=context_name,
            session_type=session_type,
            participants=list(participants or []),
            links=dict(links or {}),
            created_at=now,
            updated_at=now,
            tags=list(tags or []),
            metadata=dict(metadata or {}),
        )
        with self._lock, self._conn:
            self._write_text(self._meta_key(sid), _json_text(self._session_payload(session)))
            self._write_text(self._events_key(sid), "")
        return session

    def get_session(
        self, session_id: str, *, include_events: bool = True
    ) -> Optional[EpisodicSession]:
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                return None
            events = self._load_events(session_id) if include_events else []
        return replace(session, events=events)

    def list_sessions(self, query: EpisodicSessionQuery) -> list[EpisodicSession]:
        if query.limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT value FROM kv WHERE key LIKE 'sessions/%/meta.json'"
            ).fetchall()
            sessions: list[EpisodicSession] = []
            needle = query.query.casefold().strip()
            for (raw,) in rows:
                try:
                    session = self._session_from_payload(json.loads(raw))
                except (json.JSONDecodeError, EpisodicCompatibilityError):
                    continue
                if session.account_name != query.account_name:
                    continue
                if query.agent_name and session.agent_name != query.agent_name:
                    continue
                if needle:
                    events = self._load_events(session.session_id)
                    if not any(needle in self._content_text(event.content).casefold() for event in events):
                        continue
                    session = replace(session, events=events)
                sessions.append(session)
        sessions.sort(key=lambda item: item.updated_at or datetime.min, reverse=True)
        return sessions[: query.limit]

    def session_exists(self, session_id: str) -> bool:
        with self._lock:
            return self._read_text(self._meta_key(session_id)) is not None

    def append_event(self, session_id: str, event: EpisodicEvent) -> EpisodicEvent:
        return self.add_events(session_id, [event])[0]

    def add_events(
        self, session_id: str, events: list[EpisodicEvent]
    ) -> list[EpisodicEvent]:
        stored = [self._stored_event(event) for event in events]
        if not stored:
            return []
        with self._lock, self._conn:
            session = self._load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")
            self._append_lines(
                self._events_key(session_id),
                [_json_text(self._event_payload(event)) for event in stored],
            )
            updated = replace(session, updated_at=_utc_now())
            self._write_text(
                self._meta_key(session_id), _json_text(self._session_payload(updated))
            )
        return stored

    def link_event(
        self, correlation_id: Optional[str], session_id: str, event_id: str
    ) -> None:
        if not correlation_id:
            return
        line = _json_text(
            {
                "session_id": session_id,
                "event_id": event_id,
                "ts": datetime.now(timezone.utc),
            }
        )
        with self._lock, self._conn:
            self._append_lines(self._correlation_key(correlation_id), [line])

    def update_session(
        self, session_id: str, patch: dict[str, Any]
    ) -> EpisodicSession:
        unknown = set(patch) - _SESSION_PATCH_FIELDS
        if unknown:
            raise ValueError(
                "unsupported session patch fields: " + ", ".join(sorted(unknown))
            )
        with self._lock, self._conn:
            session = self._load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")
            values = {name: getattr(session, name) for name in _SESSION_PATCH_FIELDS}
            values.update(patch)
            values["participants"] = list(values["participants"] or [])
            values["links"] = dict(values["links"] or {})
            values["tags"] = list(values["tags"] or [])
            values["metadata"] = dict(values["metadata"] or {})
            updated = replace(session, updated_at=_utc_now(), **values)
            self._write_text(
                self._meta_key(session_id), _json_text(self._session_payload(updated))
            )
        return updated

    def reset_session(self, session_id: str) -> None:
        with self._lock, self._conn:
            session = self._load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")
            self._conn.execute("DELETE FROM logs WHERE key = ?", (self._events_key(session_id),))
            updated = replace(session, updated_at=_utc_now())
            self._write_text(
                self._meta_key(session_id), _json_text(self._session_payload(updated))
            )

    def delete_session(self, session_id: str) -> None:
        keys = (self._meta_key(session_id), self._events_key(session_id))
        with self._lock, self._conn:
            self._conn.executemany("DELETE FROM kv WHERE key = ?", [(key,) for key in keys])
            self._conn.executemany("DELETE FROM logs WHERE key = ?", [(key,) for key in keys])

    def recall(self, request: EpisodicMemoryRequest) -> EpisodicMemoryResult:
        session = (
            self.get_session(request.conversation_id)
            if request.conversation_id
            else None
        )
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
                result.digests = list(self.digest_recall(request))
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

    def save_overflow_digest(
        self, *, account_name: str, conversation_id: str, snippet: str
    ) -> Optional[str]:
        if self.digests_root is None:
            return None
        account = _validate_segment(account_name, name="account_name")
        conversation = _validate_segment(conversation_id, name="conversation_id")
        account_dir = self.digests_root / account
        account_dir.mkdir(parents=True, exist_ok=True)
        path = account_dir / f"{conversation}_overflow.md"
        existing = path.read_text(encoding="utf-8", errors="ignore").strip() if path.exists() else ""
        combined = f"{existing}\n\n{snippet}".strip() if existing else snippet
        path.write_text(combined, encoding="utf-8")
        return combined

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


__all__ = [
    "EpisodicCompatibilityError",
    "SqliteEpisodicMemory",
]
