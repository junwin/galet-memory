from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from uuid import uuid4

from .invalidation import InvalidationSupport
from .file_lock import StoreFileLock
from .interface import (
    EpisodicDigest,
    EpisodicEvent,
    EpisodicMemory,
    EpisodicMemoryRequest,
    EpisodicMemoryResult,
)
from .management import (
    EventScope,
    EpisodicConcurrencyError,
    EpisodicInvalidationResult,
    EpisodicMemoryManager,
    EpisodicSession,
    EpisodicSessionQuery,
)
from .common import (
    EpisodicCompatibilityError,
    _SESSION_PATCH_FIELDS,
    _json_text,
    _parse_datetime,
    _utc_now,
    _validate_segment,
)


DigestRecall = Callable[[EpisodicMemoryRequest], Sequence[EpisodicDigest]]


class JsonlEpisodicMemory(InvalidationSupport, EpisodicMemory, EpisodicMemoryManager):
    """Filesystem episodic memory using Lucy-compatible JSON and JSONL files.

    ``root`` contains ``sessions/<id>/meta.json``,
    ``sessions/<id>/events.jsonl``, and ``correlations/<id>.jsonl``. Writes to
    metadata files are atomic within a filesystem. Event appends and
    conditional appends and invalidations are serialized across instances and
    processes using a reentrant filesystem lock.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        digests_root: str | Path | None = None,
        digest_recall: DigestRecall | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.digests_root = (
            Path(digests_root) if digests_root is not None else None
        )
        self.digest_recall = digest_recall
        self._lock = StoreFileLock(self.root)

    def _path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"episodic key escapes storage root: {relative!r}")
        return path

    @staticmethod
    def _meta_key(session_id: str) -> str:
        session = _validate_segment(session_id, name="session_id")
        return f"sessions/{session}/meta.json"

    @staticmethod
    def _events_key(session_id: str) -> str:
        session = _validate_segment(session_id, name="session_id")
        return f"sessions/{session}/events.jsonl"

    @staticmethod
    def _correlation_key(correlation_id: str) -> str:
        correlation = _validate_segment(
            correlation_id, name="correlation_id"
        )
        return f"correlations/{correlation}.jsonl"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _append_lines(path: Path, lines: Sequence[str]) -> None:
        if not lines:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line)
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _session_payload(session: EpisodicSession) -> dict[str, Any]:
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
        payload: dict[str, Any],
        events: Optional[list[EpisodicEvent]] = None,
    ) -> EpisodicSession:
        try:
            return EpisodicSession(
                session_id=str(payload["session_id"]),
                user_id=str(
                    payload.get("user_id") or payload["account_name"]
                ),
                account_name=str(payload["account_name"]),
                agent_name=str(payload["agent_name"]),
                friendly_name=payload.get("friendly_name"),
                context_name=payload.get("context_name"),
                session_type=str(payload.get("session_type") or "user"),
                participants=[
                    str(item) for item in payload.get("participants") or []
                ],
                links=dict(payload.get("links") or {}),
                created_at=_parse_datetime(payload.get("created_at")),
                updated_at=_parse_datetime(payload.get("updated_at")),
                tags=[str(item) for item in payload.get("tags") or []],
                metadata=dict(payload.get("metadata") or {}),
                events=list(events or []),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EpisodicCompatibilityError(
                "invalid episodic session record"
            ) from exc

    @staticmethod
    def _stored_event(event: EpisodicEvent) -> EpisodicEvent:
        role = event.role
        return replace(
            event,
            event_id=event.event_id or str(uuid4()),
            created_at=event.created_at or _utc_now(),
            actor=event.actor or role,
            kind=event.kind
            or (
                "user_message"
                if role == "user"
                else "assistant_message"
            ),
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
                created_at=_parse_datetime(
                    payload.get("ts") or payload.get("created_at")
                ),
                role=str(payload["role"]),
                actor=str(payload.get("actor") or payload["role"]),
                kind=str(payload.get("kind") or ""),
                content=payload.get("payload", payload.get("content")),
                metadata=dict(payload.get("metadata") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EpisodicCompatibilityError(
                "invalid episodic event record"
            ) from exc

    def _load_session(self, session_id: str) -> Optional[EpisodicSession]:
        path = self._path(self._meta_key(session_id))
        if not path.exists():
            return None
        try:
            return self._session_from_payload(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except json.JSONDecodeError as exc:
            raise EpisodicCompatibilityError(
                "invalid episodic session JSON"
            ) from exc

    def _load_events(self, session_id: str) -> list[EpisodicEvent]:
        path = self._path(self._events_key(session_id))
        if not path.exists():
            return []
        events: list[EpisodicEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(self._event_from_payload(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise EpisodicCompatibilityError(
                    "invalid episodic event JSON"
                ) from exc
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
        with self._lock:
            if self._load_session(sid) is not None:
                raise ValueError(f"Session already exists: {sid}")
            self._atomic_write(
                self._path(self._meta_key(sid)),
                _json_text(self._session_payload(session)),
            )
            self._atomic_write(self._path(self._events_key(sid)), "")
        return session

    def get_session(
        self,
        session_id: str,
        *,
        include_events: bool = True,
        event_scope: EventScope = "active",
    ) -> Optional[EpisodicSession]:
        if event_scope not in ("active", "all", "archived", "raw"):
            raise ValueError(f"unsupported event scope: {event_scope!r}")
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                return None
            raw = self._load_events(session_id)
            events = (
                self._select_event_scope(
                    raw, event_scope
                )
                if include_events
                else []
            )
        return replace(session, events=events, last_event_id=raw[-1].event_id if raw else None)

    def list_sessions(self, query: EpisodicSessionQuery) -> list[EpisodicSession]:
        if query.limit <= 0:
            return []
        sessions: list[EpisodicSession] = []
        needle = query.query.casefold().strip()
        with self._lock:
            for path in self._path("sessions").glob("*/meta.json"):
                try:
                    session = self._session_from_payload(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                except (json.JSONDecodeError, EpisodicCompatibilityError):
                    continue
                if session.account_name != query.account_name:
                    continue
                if query.agent_name and session.agent_name != query.agent_name:
                    continue
                if needle:
                    events = self._select_event_scope(self._load_events(session.session_id), "all")
                    if not any(
                        needle in self._content_text(event.content).casefold()
                        for event in events
                    ):
                        continue
                    session = replace(session, events=events)
                sessions.append(session)
        sessions.sort(
            key=lambda item: item.updated_at or datetime.min, reverse=True
        )
        return sessions[: query.limit]

    def session_exists(self, session_id: str) -> bool:
        with self._lock:
            return self._path(self._meta_key(session_id)).is_file()

    def append_event(
        self, session_id: str, event: EpisodicEvent
    ) -> EpisodicEvent:
        return self.add_events(session_id, [event])[0]

    def append_event_if_tail(
        self,
        session_id: str,
        event: EpisodicEvent,
        *,
        expected_last_event_id: Optional[str],
    ) -> EpisodicEvent:
        with self._lock:
            events = self._load_events(session_id)
            actual = events[-1].event_id if events else None
            if actual != expected_last_event_id:
                raise EpisodicConcurrencyError(
                    "session event tail changed: "
                    f"expected {expected_last_event_id!r}, found {actual!r}"
                )
            return self.append_event(session_id, event)

    def add_events(
        self, session_id: str, events: list[EpisodicEvent]
    ) -> list[EpisodicEvent]:
        stored = [self._stored_event(event) for event in events]
        if not stored:
            return []
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")
            self._append_lines(
                self._path(self._events_key(session_id)),
                [_json_text(self._event_payload(event)) for event in stored],
            )
            updated = replace(session, updated_at=_utc_now())
            self._atomic_write(
                self._path(self._meta_key(session_id)),
                _json_text(self._session_payload(updated)),
            )
        return stored

    def link_event(
        self,
        correlation_id: Optional[str],
        session_id: str,
        event_id: str,
    ) -> None:
        if not correlation_id:
            return
        with self._lock:
            self._check_correlation(correlation_id, session_id)
            if not any(event.event_id == event_id for event in self._load_events(session_id)):
                raise ValueError(f"Event not found in session {session_id}: {event_id}")
            self._append_lines(
                self._path(self._correlation_key(correlation_id)),
                [
                    _json_text(
                        {
                            "session_id": session_id,
                            "event_id": event_id,
                            "ts": datetime.now(timezone.utc),
                        }
                    )
                ],
            )

    def _linked_event_ids(self, correlation_id: str, session_id: str) -> list[str]:
        path = self._path(self._correlation_key(correlation_id))
        if not path.exists():
            return []
        links = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [link["event_id"] for link in links if link["session_id"] == session_id]

    def _append_invalidation(self, session_id: str, event: EpisodicEvent) -> EpisodicEvent:
        return self.append_event(session_id, event)

    def invalidate_events(self, *, account_name: str, session_id: str, correlation_id: str,
                          expected_last_event_id: Optional[str] = None) -> EpisodicInvalidationResult:
        with self._lock:
            result = self._invalidate(
                account_name=account_name, session_id=session_id, correlation_id=correlation_id,
                expected_last_event_id=expected_last_event_id)
            self._discard_overflow(account_name, session_id, result)
            return result

    def update_session(
        self, session_id: str, patch: dict[str, Any]
    ) -> EpisodicSession:
        unknown = set(patch) - _SESSION_PATCH_FIELDS
        if unknown:
            raise ValueError(
                "unsupported session patch fields: "
                + ", ".join(sorted(unknown))
            )
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")
            values = {
                name: getattr(session, name) for name in _SESSION_PATCH_FIELDS
            }
            values.update(patch)
            values["participants"] = list(values["participants"] or [])
            values["links"] = dict(values["links"] or {})
            values["tags"] = list(values["tags"] or [])
            values["metadata"] = dict(values["metadata"] or {})
            updated = replace(session, updated_at=_utc_now(), **values)
            self._atomic_write(
                self._path(self._meta_key(session_id)),
                _json_text(self._session_payload(updated)),
            )
        return updated

    def reset_session(self, session_id: str) -> None:
        with self._lock:
            session = self._load_session(session_id)
            if session is None:
                raise ValueError(f"Session not found: {session_id}")
            markers = [event for event in self._load_events(session_id)
                       if event.kind == "events_invalidated"]
            text = "".join(_json_text(self._event_payload(event)) + "\n" for event in markers)
            self._atomic_write(self._path(self._events_key(session_id)), text)
            updated = replace(session, updated_at=_utc_now())
            self._atomic_write(
                self._path(self._meta_key(session_id)),
                _json_text(self._session_payload(updated)),
            )

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            for key in (self._meta_key(session_id), self._events_key(session_id)):
                path = self._path(key)
                if path.exists():
                    path.unlink()
            session_dir = self._path(
                f"sessions/{_validate_segment(session_id, name='session_id')}"
            )
            if session_dir.exists() and not any(session_dir.iterdir()):
                session_dir.rmdir()

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
            selected = (
                events[-request.max_events :] if request.max_events > 0 else []
            )
            if request.token_budget is not None:
                selected = self._apply_token_budget(
                    selected, request.token_budget
                )
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
        return (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False)
        )

    def close(self) -> None:
        return None

    def __enter__(self) -> "JsonlEpisodicMemory":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["JsonlEpisodicMemory"]
