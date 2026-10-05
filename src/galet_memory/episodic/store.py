"""Backend-independent session/event semantics behind explicit public operations."""

from __future__ import annotations
import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Sequence
from uuid import uuid4

from .models import (
    EMPTY_TAIL,
    UNSET,
    ClearResult,
    Digest,
    DigestSearchRequest,
    Event,
    EventPage,
    Exchange,
    HistorySnapshot,
    NewEvent,
    Page,
    Session,
    SessionChanges,
    EpisodicConcurrencyError,
    EpisodicSessionNotFoundError,
)
from .invalidation import InvalidationSupport, select_events, INVALIDATION_KIND

CONTROL_KINDS = frozenset({INVALIDATION_KIND, "session_reset", "session_digest"})


def utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


def count_value(count):
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("count must be a nonnegative integer")


def identity(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def string_filter(values):
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or any(not isinstance(v, str) for v in values):
        raise ValueError("filters must be collections of strings")
    return sorted(set(values))


class EpisodicStore(InvalidationSupport):
    """Concrete backends implement only transactional persistence primitives."""

    def _owned(self, account_name, session_id):
        identity(account_name, "account_name")
        identity(session_id, "session_id")
        session = self._load_session(session_id)
        if session is None or session.account_name != account_name:
            raise EpisodicSessionNotFoundError("session not found for account")
        return session

    def _with_tail(self, session):
        return replace(session, last_event_id=self._tail(session.session_id))

    def _guard(self, session_id, expected):
        if expected is None:
            return
        actual = self._tail(session_id)
        required = None if expected is EMPTY_TAIL else expected
        if not isinstance(required, (str, type(None))):
            raise ValueError("expected tail must be an event ID, EMPTY_TAIL or None")
        if actual != required:
            raise EpisodicConcurrencyError("session event tail changed")

    def _cursor(self, payload):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(self._cursor_key, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(sig + raw).decode()

    def _decode(self, cursor, binding):
        try:
            packed = base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
            sig, raw = packed[:32], packed[32:]
            if not hmac.compare_digest(
                sig, hmac.new(self._cursor_key, raw, hashlib.sha256).digest()
            ):
                raise ValueError()
            payload = json.loads(raw)
            if payload["binding"] != binding:
                raise ValueError()
            return payload
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("invalid cursor for this read") from exc

    def create_session(
        self,
        *,
        account_name,
        session_id=None,
        friendly_name=None,
        context_name=None,
        tags=(),
        metadata=None,
    ):
        identity(account_name, "account_name")
        sid = session_id if session_id is not None else str(uuid4())
        identity(sid, "session_id")
        changes = self._changes(
            SessionChanges(
                friendly_name, context_name, tags, {} if metadata is None else metadata
            )
        )
        stamp = now()
        session = Session(
            sid, account_name, created_at=stamp, updated_at=stamp, **changes
        )
        with self._transaction(write=True):
            if self._load_session(sid) is not None:
                raise ValueError("session ID already exists")
            self._write_session(session)
        return session

    def get_session(self, *, account_name, session_id):
        with self._transaction():
            try:
                return self._with_tail(self._owned(account_name, session_id))
            except EpisodicSessionNotFoundError:
                return None

    def list_sessions(self, *, account_name, count=20, cursor=None):
        identity(account_name, "account_name")
        count_value(count)
        binding = {"operation": "sessions", "account": account_name}
        with self._transaction():
            state = (
                self._decode(cursor, binding)
                if cursor
                else {
                    "binding": binding,
                    "cutoff": now().isoformat(),
                    "position": None,
                }
            )
            sessions = [
                s
                for s in self._all_sessions()
                if s.account_name == account_name
                and s.updated_at.isoformat() <= state["cutoff"]
            ]
            sessions.sort(
                key=lambda s: (s.updated_at.isoformat(), s.session_id), reverse=True
            )
            if state["position"] is not None:
                pos = tuple(state["position"])
                sessions = [
                    s
                    for s in sessions
                    if (s.updated_at.isoformat(), s.session_id) < pos
                ]
            chosen = sessions[:count]
            next_cursor = None
            if count and len(sessions) > count:
                last = chosen[-1]
                next_cursor = self._cursor(
                    {
                        **state,
                        "position": [last.updated_at.isoformat(), last.session_id],
                    }
                )
            return Page(tuple(self._with_tail(s) for s in chosen), next_cursor)

    @staticmethod
    def _changes(changes):
        if not isinstance(changes, SessionChanges):
            raise TypeError("changes must be SessionChanges")
        fields = {}
        for key in ("friendly_name", "context_name", "tags", "metadata"):
            value = getattr(changes, key)
            if value is UNSET:
                continue
            if key in ("friendly_name", "context_name"):
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{key} must be a string or None")
            elif key == "tags":
                if (
                    isinstance(value, (str, bytes))
                    or value is None
                    or any(not isinstance(v, str) for v in value)
                ):
                    raise ValueError("tags must be a collection of strings")
                value = tuple(value)
            else:
                if not isinstance(value, dict):
                    raise ValueError("metadata must be an object")
                value = json.loads(json.dumps(value))
            fields[key] = value
        return fields

    def update_session(self, *, account_name, session_id, changes):
        values = self._changes(changes)
        with self._transaction(write=True):
            session = self._owned(account_name, session_id)
            session = replace(session, updated_at=now(), **values)
            self._write_session(session)
            return self._with_tail(session)

    def delete_sessions(self, *, account_name, session_ids):
        if isinstance(session_ids, (str, bytes)):
            raise ValueError("session_ids must be a collection")
        identity(account_name, "account_name")
        ids = list(dict.fromkeys(session_ids))
        for sid in ids:
            identity(sid, "session_id")
        with self._transaction(write=True):
            found = []
            for sid in ids:
                s = self._load_session(sid)
                if s is not None:
                    self._owned(account_name, sid)
                    found.append(sid)
            for sid in found:
                self._delete_session(sid)
            return found

    def delete_session(self, *, account_name, session_id):
        return bool(
            self.delete_sessions(account_name=account_name, session_ids=[session_id])
        )

    def clear_session_events(
        self, *, account_name, session_id, expected_last_event_id=None
    ):
        with self._transaction(write=True):
            session = self._owned(account_name, session_id)
            self._guard(session_id, expected_last_event_id)
            removed = self._clear_events(session_id)
            self._write_session(replace(session, updated_at=now()))
            return ClearResult(session_id, removed, self._tail(session_id))

    def _new_events(self, session_id, inputs, *, controls=False):
        stored = []
        for event in inputs:
            if type(event) is not NewEvent:
                raise TypeError(
                    "append accepts NewEvent inputs, not stored Event records"
                )
            identity(event.role, "role")
            identity(event.actor, "actor")
            if event.role not in ("user", "assistant", "tool", "system"):
                raise ValueError("unsupported role")
            kinds = {"user": "user_message", "assistant": "assistant_message"}
            kind = event.kind or kinds.get(event.role)
            identity(kind, "kind")
            if kind in CONTROL_KINDS and not controls:
                raise ValueError(
                    "control records must be written through curation or invalidation"
                )
            if not isinstance(event.metadata, dict):
                raise ValueError("metadata must be an object")
            correlations = string_filter(event.correlation_ids)
            if correlations is None or any(not v.strip() for v in correlations):
                raise ValueError("correlation_ids must be nonempty strings")
            for correlation in correlations:
                self._check_correlation(correlation, session_id)
            stamp = now()
            stored.append(
                Event(
                    role=event.role,
                    content=json.loads(json.dumps(event.content)),
                    actor=event.actor,
                    kind=kind,
                    created_at=utc(event.created_at) if event.created_at else stamp,
                    metadata=json.loads(json.dumps(event.metadata)),
                    correlation_ids=tuple(correlations),
                    event_id=str(uuid4()),
                    session_id=session_id,
                    stored_at=stamp,
                )
            )
        return stored

    def append_events(
        self, *, account_name, session_id, events, expected_last_event_id=None
    ):
        with self._transaction(write=True):
            session = self._owned(account_name, session_id)
            self._guard(session_id, expected_last_event_id)
            stored = self._new_events(session_id, events)
            result = self._insert_events(session_id, stored)
            if stored:
                self._write_session(replace(session, updated_at=now()))
            return result

    def append_event(
        self, *, account_name, session_id, event, expected_last_event_id=None
    ):
        return self.append_events(
            account_name=account_name,
            session_id=session_id,
            events=[event],
            expected_last_event_id=expected_last_event_id,
        )[0]

    def append_boundary(
        self, *, account_name, session_id, event, expected_last_event_id
    ):
        if expected_last_event_id is None:
            raise ValueError("boundary append requires an event tail or EMPTY_TAIL")
        if (
            event.kind not in ("session_digest", "session_reset")
            or event.metadata.get("visibility_boundary") is not True
        ):
            raise ValueError("invalid curation boundary")
        with self._transaction(write=True):
            session = self._owned(account_name, session_id)
            self._guard(session_id, expected_last_event_id)
            stored = self._new_events(session_id, [event], controls=True)
            result = self._insert_events(session_id, stored)[0]
            self._write_session(replace(session, updated_at=now()))
            return result

    def _snapshot(self, account_name, session_id, scope):
        with self._transaction():
            session = self._with_tail(self._owned(account_name, session_id))
            return HistorySnapshot(
                session, tuple(select_events(self._load_events(session_id), scope))
            )

    def get_active_snapshot(self, *, account_name, session_id):
        return self._snapshot(account_name, session_id, "active")

    def get_transcript_snapshot(self, *, account_name, session_id):
        return self._snapshot(account_name, session_id, "all")

    def get_audit_snapshot(self, *, account_name, session_id):
        return self._snapshot(account_name, session_id, "raw")

    def _visible(self, session_id, scope="all"):
        return [
            e
            for e in select_events(self._load_events(session_id), scope)
            if e.kind not in CONTROL_KINDS
        ]

    @staticmethod
    def _filtered(events, kinds, actors):
        return [
            e
            for e in events
            if (kinds is None or e.kind in kinds)
            and (actors is None or e.actor in actors)
        ]

    def get_event(self, *, account_name, session_id, event_id):
        identity(event_id, "event_id")
        with self._transaction():
            self._owned(account_name, session_id)
            return next(
                (e for e in self._visible(session_id) if e.event_id == event_id), None
            )

    def get_recent_events(
        self,
        *,
        account_name,
        session_id,
        count=10,
        event_kinds=None,
        actors=None,
        before_event_id=None,
    ):
        count_value(count)
        kinds, actor_names = string_filter(event_kinds), string_filter(actors)
        with self._transaction():
            session = self._with_tail(self._owned(account_name, session_id))
            raw = self._load_events(session_id)
            anchor = None
            if before_event_id is not None:
                anchor = next(
                    (e.sequence for e in raw if e.event_id == before_event_id), None
                )
                if anchor is None:
                    raise ValueError("before_event_id is not in this session")
            events = self._filtered(
                self._visible(session_id, "active"), kinds, actor_names
            )
            if anchor is not None:
                events = [e for e in events if e.sequence < anchor]
            chosen = events[-count:] if count else []
            older = chosen[0].event_id if count and len(events) > count else None
            return EventPage(
                tuple(chosen),
                before_event_id=older,
                last_event_id=session.last_event_id,
            )

    def get_events_by_period(
        self,
        *,
        account_name,
        session_id,
        start,
        end,
        event_kinds=None,
        actors=None,
        count=100,
        cursor=None,
    ):
        count_value(count)
        start, end = utc(start), utc(end)
        if start > end:
            raise ValueError("start must not be after end")
        kinds, names = string_filter(event_kinds), string_filter(actors)
        binding = {
            "operation": "period",
            "account": account_name,
            "session": session_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "kinds": kinds,
            "actors": names,
        }
        with self._transaction():
            session = self._with_tail(self._owned(account_name, session_id))
            state = (
                self._decode(cursor, binding)
                if cursor
                else {
                    "binding": binding,
                    "high_water": self._max_sequence(),
                    "position": None,
                }
            )
            events = [
                e
                for e in self._filtered(self._visible(session_id), kinds, names)
                if start <= e.created_at < end and e.sequence <= state["high_water"]
            ]
            events.sort(key=lambda e: (e.created_at, e.sequence))
            if state["position"] is not None:
                stamp, seq = state["position"]
                events = [
                    e
                    for e in events
                    if (e.created_at, e.sequence) > (datetime.fromisoformat(stamp), seq)
                ]
            chosen = events[:count]
            next_cursor = (
                self._cursor(
                    {
                        **state,
                        "position": [
                            chosen[-1].created_at.isoformat(),
                            chosen[-1].sequence,
                        ],
                    }
                )
                if count and len(events) > count
                else None
            )
            return EventPage(
                tuple(chosen),
                next_cursor=next_cursor,
                last_event_id=session.last_event_id,
            )

    def get_exchange(self, *, account_name, session_id, correlation_id):
        identity(correlation_id, "correlation_id")
        with self._transaction():
            self._owned(account_name, session_id)
            events = tuple(
                e
                for e in self._visible(session_id)
                if correlation_id in e.correlation_ids
            )
            return Exchange(session_id, correlation_id, events) if events else None

    def _linked_event_ids(self, correlation_id, session_id):
        return [
            e.event_id
            for e in self._load_events(session_id)
            if correlation_id in e.correlation_ids
        ]

    def _append_invalidation(self, session_id, event):
        stored = self._new_events(session_id, [event], controls=True)
        return self._insert_events(session_id, stored)[0]

    def invalidate_exchange(
        self, *, account_name, session_id, correlation_id, expected_last_event_id=None
    ):
        with self._transaction(write=True):
            session = self._owned(account_name, session_id)
            # An idempotent retry takes precedence over an old expected tail.
            previous = any(
                e.kind == INVALIDATION_KIND
                and e.metadata["correlation_id"] == correlation_id
                for e in self._load_events(session_id)
            )
            if not previous:
                self._guard(session_id, expected_last_event_id)
            result = self._invalidate(
                account_name=account_name,
                session_id=session_id,
                correlation_id=correlation_id,
                expected_last_event_id=None,
            )
            if result.status == "invalidated":
                self._write_session(replace(session, updated_at=now()))
        self._discard_overflow(account_name, session_id, result)
        return result

    @staticmethod
    def _digest(event):
        return Digest(
            event.event_id,
            event.session_id,
            str(event.content),
            tuple(event.metadata.get("source_event_ids", ())),
            event.created_at,
            dict(event.metadata),
        )

    def get_digest(self, *, account_name, session_id, digest_id):
        with self._transaction():
            self._owned(account_name, session_id)
            event = next(
                (
                    e
                    for e in select_events(self._load_events(session_id), "all")
                    if e.event_id == digest_id and e.kind == "session_digest"
                ),
                None,
            )
            return self._digest(event) if event else None

    def list_digests(self, *, account_name, session_id, count=20, cursor=None):
        count_value(count)
        binding = {
            "operation": "digests",
            "account": account_name,
            "session": session_id,
        }
        with self._transaction():
            self._owned(account_name, session_id)
            state = (
                self._decode(cursor, binding)
                if cursor
                else {
                    "binding": binding,
                    "high_water": self._max_sequence(),
                    "position": 0,
                }
            )
            events = [
                e
                for e in select_events(self._load_events(session_id), "all")
                if e.kind == "session_digest"
                and state["position"] < e.sequence <= state["high_water"]
            ]
            chosen = events[:count]
            next_cursor = (
                self._cursor({**state, "position": chosen[-1].sequence})
                if count and len(events) > count
                else None
            )
            return Page(tuple(self._digest(e) for e in chosen), next_cursor)

    def search_digests(self, *, account_name, query, session_id=None, count=3):
        identity(account_name, "account_name")
        count_value(count)
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if session_id is not None:
            with self._transaction():
                self._owned(account_name, session_id)
        if not query.strip() or not count:
            return []
        if self.digest_search is None:
            raise ValueError("digest search is not configured")
        request = DigestSearchRequest(account_name, query, session_id, count)
        return self._filter_digests(request, self.digest_search(request))[:count]
