"""Shared append-only invalidation and visibility rules for episodic stores."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional, Sequence

from .common import _validate_segment

from .interface import EpisodicEvent
from .management import (
    EpisodicConcurrencyError, EpisodicCorrelationInvalidatedError,
    EpisodicInvalidationResult, EpisodicSessionNotFoundError, EventScope,
)

INVALIDATION_KIND = "events_invalidated"


def is_boundary(event: EpisodicEvent) -> bool:
    return (event.kind in ("session_digest", "session_reset")
            and event.metadata.get("visibility_boundary") is True)


def invalidated_ids(events: Sequence[EpisodicEvent]) -> set[str]:
    invalid = {eid for event in events if event.kind == INVALIDATION_KIND
               for eid in event.metadata["invalidated_event_ids"]}
    if not invalid:
        return invalid
    # Include transitive dependencies and any later stale digest writes.
    changed = True
    while changed:
        changed = False
        for event in events:
            if event.kind != "session_digest" or event.event_id in invalid:
                continue
            sources = event.metadata.get("source_event_ids")
            if not sources or invalid.intersection(sources):
                invalid.add(event.event_id)
                changed = True
    return invalid


def select_events(events: list[EpisodicEvent], scope: EventScope) -> list[EpisodicEvent]:
    if scope not in ("active", "all", "archived", "raw"):
        raise ValueError(f"unsupported event scope: {scope!r}")
    if scope == "raw":
        return list(events)
    selected = events
    if scope != "all":
        boundary = next((i for i in range(len(events) - 1, -1, -1)
                         if is_boundary(events[i])), None)
        if boundary is None:
            selected = events if scope == "active" else []
        elif scope == "active":
            offset = 1 if events[boundary].kind == "session_reset" else 0
            selected = events[boundary + offset:]
        else:
            selected = events[:boundary]
    invalid = invalidated_ids(events)
    return [event for event in selected
            if event.kind != INVALIDATION_KIND and event.event_id not in invalid]


class InvalidationSupport:
    """Called with the backend lock (and SQLite write transaction) held."""

    _select_event_scope = staticmethod(select_events)
    _is_visibility_boundary = staticmethod(is_boundary)

    def _check_correlation(self, correlation_id: str, session_id: str) -> None:
        if any(event.kind == INVALIDATION_KIND
               and event.metadata["correlation_id"] == correlation_id
               for event in self._load_events(session_id)):
            raise EpisodicCorrelationInvalidatedError(
                f"correlation is invalidated in session {session_id}: {correlation_id}")

    def _invalidate(self, *, account_name: str, session_id: str,
                    correlation_id: str, expected_last_event_id: Optional[str]) -> EpisodicInvalidationResult:
        if not correlation_id or not correlation_id.strip():
            raise ValueError("correlation_id must not be empty")
        session = self._load_session(session_id)
        if session is None or session.account_name != account_name:
            raise EpisodicSessionNotFoundError("session not found for account")
        if self.digests_root is not None:
            _validate_segment(account_name, name="account_name")
            _validate_segment(session_id, name="session_id")
        events = self._load_events(session_id)
        previous = next((event for event in events if event.kind == INVALIDATION_KIND
                         and event.metadata["correlation_id"] == correlation_id), None)
        if previous is not None:
            return self._invalidation_result(previous, session_id, "already_invalidated")
        actual_tail = events[-1].event_id if events else None
        if expected_last_event_id is not None and expected_last_event_id != actual_tail:
            raise EpisodicConcurrencyError("session event tail changed")
        linked = set(self._linked_event_ids(correlation_id, session_id))
        targets = [event.event_id for event in events if event.event_id in linked
                   and event.kind not in (INVALIDATION_KIND, "session_reset")]
        if not targets:
            return EpisodicInvalidationResult(session_id, correlation_id, "not_found")
        marker = EpisodicEvent("system", "", kind=INVALIDATION_KIND,
                              actor="memory", metadata={
                                  "invalidation_version": 1,
                                  "correlation_id": correlation_id,
                                  "event_ids": targets,
                                  "invalidated_event_ids": targets,
                              })
        invalid = invalidated_ids([*events, marker])
        digests = [event.event_id for event in events
                   if event.kind == "session_digest" and event.event_id in invalid]
        marker.metadata.update(invalidated_event_ids=[*targets, *[eid for eid in digests if eid not in targets]],
                               invalidated_digest_ids=digests)
        stored = self._append_invalidation(session_id, marker)
        # The marker is authoritative even if external cleanup is interrupted.
        return self._invalidation_result(stored, session_id, "invalidated")

    @staticmethod
    def _invalidation_result(marker, session_id, status) -> EpisodicInvalidationResult:
        return EpisodicInvalidationResult(
            session_id, marker.metadata["correlation_id"], status,
            tuple(marker.metadata["event_ids"]),
            tuple(marker.metadata["invalidated_digest_ids"]), marker.event_id, True)

    def is_digest_valid(self, *, account_name: str, session_id: str,
                        digest_id: Optional[str] = None,
                        source_event_ids: Optional[Sequence[str]] = None) -> bool:
        with self._lock:
            session = self._load_session(session_id)
            if session is None or session.account_name != account_name:
                return False
            events = self._load_events(session_id)
            invalid = invalidated_ids(events)
            if not invalid:
                return True
            if digest_id in invalid:
                return False
            stored = next((event for event in events if event.event_id == digest_id
                           and event.kind == "session_digest"), None)
            if stored is not None:
                return stored.event_id not in invalid
            # External artifacts with no provenance cannot prove they are clean.
            if not source_event_ids:
                return False
            valid_ids = {event.event_id for event in events
                         if event.event_id not in invalid and event.kind != INVALIDATION_KIND}
            return set(source_event_ids) <= valid_ids

    def _filter_digests(self, request, digests):
        return [digest for digest in digests if self.is_digest_valid(
            account_name=request.account_name, session_id=digest.session_id,
            digest_id=digest.metadata.get("digest_id"),
            source_event_ids=digest.metadata.get("source_event_ids"))]

    def _discard_overflow(self, account_name, session_id, result):
        if self.digests_root is not None and result.status == "invalidated":
            account = _validate_segment(account_name, name="account_name")
            session = _validate_segment(session_id, name="session_id")
            (self.digests_root / account / f"{session}_overflow.md").unlink(missing_ok=True)

    def save_overflow_digest(self, *, account_name: str, conversation_id: str,
                             snippet: str) -> Optional[str]:
        if self.digests_root is None:
            return None
        from ..publication import FilesystemDigestStore
        account = _validate_segment(account_name, name="account_name")
        session_id = _validate_segment(conversation_id, name="conversation_id")
        documents = FilesystemDigestStore(self.digests_root)
        path = documents.path_for(account, session_id, "overflow")
        state_path = path.with_suffix(".state.json")
        with self._lock:
            events = self._load_events(session_id)
            latest = next((event.event_id for event in reversed(events)
                           if event.kind == INVALIDATION_KIND), None)
            # Fail closed if deletion committed but file cleanup failed/crashed.
            saved_epoch = None
            if state_path.exists():
                try:
                    saved_epoch = json.loads(state_path.read_text(encoding="utf-8"))["invalidation_epoch"]
                except (ValueError, KeyError):
                    saved_epoch = "unreadable"
            existing = (path.read_text(encoding="utf-8").strip()
                        if path.exists() and saved_epoch == latest else "")
            combined = f"{existing}\n\n{snippet}".strip() if existing else snippet
            documents.write(account_name=account, session_id=session_id,
                            digest_id="overflow", digest=combined)
            temporary = None
            try:
                with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                        prefix=".overflow-state-", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump({"invalidation_epoch": latest}, handle)
                os.replace(temporary, state_path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return combined
