from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Literal, Mapping, Optional, Protocol, Sequence, runtime_checkable
from uuid import uuid4

from .publication import DigestPublisher, PublishedDigest
from .episodic import EpisodicEvent, EpisodicMemoryManager, EpisodicSession
from .episodic.management import EpisodicConcurrencyError


class CurationError(RuntimeError):
    """Base class for neutral curation failures."""


class CurationSessionNotFoundError(CurationError):
    """The session is missing or is not owned by the requested account."""


class DigestGenerationError(CurationError):
    """The digest generator failed or returned no usable text."""


class CurationConflictError(CurationError):
    """The session changed after the digest snapshot was taken."""


class CurationStorageError(CurationError):
    """The episodic store failed outside a recognised conflict."""


class DigestPublicationError(CurationStorageError):
    """Archive committed, but publication failed; retry with boundary_event_id."""

    def __init__(self, session_id: str, boundary_event_id: str) -> None:
        self.session_id = session_id
        self.boundary_event_id = boundary_event_id
        super().__init__(f"digest archive committed; publication failed for boundary {boundary_event_id}")


@dataclass(frozen=True)
class DigestGenerationRequest:
    session_id: str
    account_name: str
    agent_name: str
    friendly_name: str
    events: Sequence[EpisodicEvent]
    max_chars: int = 32000


@runtime_checkable
class DigestGenerator(Protocol):
    def generate(self, request: DigestGenerationRequest) -> str:
        """Return digest text without changing storage."""
        ...


@dataclass(frozen=True)
class CurationResult:
    action: Literal["digest", "archive", "reset", "cumulative"]
    session_id: str
    digest: str
    boundary_event: Optional[EpisodicEvent] = None
    idempotency_key: str = ""
    publication: Optional[PublishedDigest] = None
    source_event_ids: tuple[str, ...] = ()
    provenance: Mapping[str, Any] | None = None


class CurationService:
    """Generate digests and establish non-destructive archive boundaries."""

    def __init__(
        self,
        episodic_store: EpisodicMemoryManager,
        digest_generator: DigestGenerator,
        digest_publisher: Optional[DigestPublisher] = None,
    ) -> None:
        self.episodic_store = episodic_store
        self.digest_generator = digest_generator
        self.digest_publisher = digest_publisher

    def produce_digest(
        self,
        *,
        account_name: str,
        session_id: str,
        max_chars: int = 32000,
        publish: bool = False,
    ) -> CurationResult:
        self._require_publisher(publish)
        session = self._load_owned_active_session(account_name, session_id)
        events = self._interval_events(session)
        digest = self._generate(session, events=events, max_chars=max_chars)
        result = CurationResult(action="digest", session_id=session.session_id, digest=digest,
                                source_event_ids=tuple(event.event_id for event in events),
                                provenance=self._provenance(events))
        return self._publish(result, account_name) if publish else result

    def produce_cumulative_digest(
        self, *, account_name: str, session_id: str,
        max_chars: int = 32000, since_reset: bool = True,
        publish: bool = False,
    ) -> CurationResult:
        """Rebuild from immutable intervals; optionally publish a derived view."""
        self._require_publisher(publish)
        session = self._load_owned_all_session(account_name, session_id)
        sources = []
        for event in session.events:
            if event.kind == "session_reset" and event.metadata.get("visibility_boundary") and since_reset:
                sources.clear()
            elif event.kind == "session_digest" and event.metadata.get("visibility_boundary"):
                sources.append(event)
        if not sources:
            raise CurationError("no archived interval digests to combine")
        digest = self._generate(session, events=sources, max_chars=max_chars)
        result = CurationResult("cumulative", session_id, digest,
                              source_event_ids=tuple(event.event_id for event in sources),
                              provenance=self._provenance(sources))
        return self._publish(result, account_name) if publish else result

    def archive(
        self,
        *,
        account_name: str,
        session_id: str,
        max_chars: int = 32000,
        idempotency_key: Optional[str] = None,
        publish: bool = False,
    ) -> CurationResult:
        self._require_publisher(publish)
        operation_key = idempotency_key or str(uuid4())
        session = self._load_owned_active_session(account_name, session_id)
        existing = self._find_idempotent_boundary(
            account_name, session_id, operation_key
        )
        if existing is not None:
            if existing.kind != "session_digest":
                raise CurationConflictError("idempotency key belongs to a reset")
            result = self._archive_result(session_id, existing, operation_key)
            return self._publish(result, account_name) if publish else result

        expected_tail = self._current_tail(session_id)
        events = self._interval_events(session)
        if not events:
            raise CurationError("cannot archive an empty interval")
        digest = self._generate(session, events=events, max_chars=max_chars)
        boundary = EpisodicEvent(
            role="system",
            actor="curation",
            kind="session_digest",
            content=digest,
            metadata={
                "visibility_boundary": True,
                "curation_version": 1,
                "idempotency_key": operation_key,
                "source_first_event_id": events[0].event_id,
                "source_last_event_id": events[-1].event_id,
                "source_event_count": len(events),
                "source_event_ids": [event.event_id for event in events],
                "source_first_created_at": events[0].created_at.isoformat() if events[0].created_at else None,
                "source_last_created_at": events[-1].created_at.isoformat() if events[-1].created_at else None,
                "previous_boundary_event_id": session.events[0].event_id if session.events and session.events[0].metadata.get("visibility_boundary") else None,
                "digest_sha256": sha256(digest.encode("utf-8")).hexdigest(),
                "generator": self._generator_info(),
            },
        )
        try:
            stored = self.episodic_store.append_event_if_tail(
                session_id,
                boundary,
                expected_last_event_id=expected_tail,
            )
        except EpisodicConcurrencyError as exc:
            existing = self._find_idempotent_boundary(
                account_name, session_id, operation_key
            )
            if existing is not None:
                if existing.kind != "session_digest":
                    raise CurationConflictError("idempotency key belongs to a reset")
                result = self._archive_result(session_id, existing, operation_key)
                return self._publish(result, account_name) if publish else result
            raise CurationConflictError(str(exc)) from exc
        except Exception as exc:
            raise CurationStorageError(
                f"failed to append digest boundary for session {session_id}"
            ) from exc
        result = self._archive_result(session_id, stored, operation_key)
        return self._publish(result, account_name) if publish else result

    def retry_publication(
        self, *, account_name: str, session_id: str, boundary_event_id: str,
    ) -> CurationResult:
        """Republish an existing committed archive without generating or appending."""
        self._require_publisher(True)
        session = self._load_owned_all_session(account_name, session_id)
        boundary = next((event for event in session.events
                         if event.event_id == boundary_event_id
                         and event.kind == "session_digest"
                         and event.metadata.get("visibility_boundary") is True), None)
        if boundary is None:
            raise CurationSessionNotFoundError("archive boundary not found in session")
        result = self._archive_result(session_id, boundary,
                                      str(boundary.metadata.get("idempotency_key", "")))
        return self._publish(result, account_name)

    def reset_context(
        self, *, account_name: str, session_id: str,
        idempotency_key: Optional[str] = None,
    ) -> CurationResult:
        """Start a new active interval without deleting or summarizing history."""
        operation_key = idempotency_key or str(uuid4())
        session = self._load_owned_active_session(account_name, session_id)
        existing = self._find_idempotent_boundary(account_name, session_id, operation_key)
        if existing is not None:
            if existing.kind != "session_reset":
                raise CurationConflictError("idempotency key belongs to an archive")
            return CurationResult("reset", session_id, "", existing, operation_key)
        expected_tail = self._current_tail(session_id)
        boundary = EpisodicEvent(
            role="system", actor="curation", kind="session_reset", content="",
            metadata={"visibility_boundary": True, "curation_version": 1,
                      "idempotency_key": operation_key},
        )
        try:
            stored = self.episodic_store.append_event_if_tail(
                session_id, boundary, expected_last_event_id=expected_tail
            )
        except EpisodicConcurrencyError as exc:
            existing = self._find_idempotent_boundary(account_name, session_id, operation_key)
            if existing is not None:
                if existing.kind != "session_reset":
                    raise CurationConflictError("idempotency key belongs to an archive")
                return CurationResult("reset", session_id, "", existing, operation_key)
            raise CurationConflictError(str(exc)) from exc
        except Exception as exc:
            raise CurationStorageError(f"failed to reset context for session {session_id}") from exc
        return CurationResult("reset", session_id, "", stored, operation_key)

    @staticmethod
    def _interval_events(session: EpisodicSession) -> tuple[EpisodicEvent, ...]:
        events = session.events
        if events and events[0].metadata.get("visibility_boundary") is True:
            return tuple(events[1:])
        return tuple(events)

    def _current_tail(self, session_id: str) -> Optional[str]:
        session = self.episodic_store.get_session(session_id, include_events=True, event_scope="all")
        return session.events[-1].event_id if session and session.events else None

    def _require_publisher(self, publish: bool) -> None:
        if publish and self.digest_publisher is None:
            raise CurationStorageError("digest publisher is not configured")

    def _publish(self, result: CurationResult, account_name: str) -> CurationResult:
        assert self.digest_publisher is not None
        try:
            publication = self.digest_publisher.publish(
                account_name=account_name, session_id=result.session_id,
                digest=result.digest,
                digest_id=(result.boundary_event.event_id if result.boundary_event else
                           "cumulative" if result.action == "cumulative" else None),
            )
        except Exception as exc:
            if result.boundary_event is not None:
                raise DigestPublicationError(result.session_id, result.boundary_event.event_id) from exc
            raise CurationStorageError(
                f"failed to publish digest for session {result.session_id}"
            ) from exc
        return replace(result, publication=publication)

    def _load_owned_all_session(self, account_name: str, session_id: str) -> EpisodicSession:
        session = self.episodic_store.get_session(session_id, include_events=True, event_scope="all")
        if session is None or session.account_name != account_name:
            raise CurationSessionNotFoundError(f"session not found for account: {session_id}")
        return session

    def _generator_info(self) -> dict[str, Any]:
        generator = self.digest_generator
        policy = getattr(generator, "policy", None)
        return {"name": type(generator).__name__, "model": getattr(policy, "model", None),
                "temperature": getattr(policy, "temperature", None),
                "include_tool_events": getattr(policy, "include_tool_events", None),
                "version": 1}

    def _provenance(self, events: Sequence[EpisodicEvent]) -> dict[str, Any]:
        return {"source_event_count": len(events),
                "source_event_ids": [event.event_id for event in events],
                "generator": self._generator_info()}

    def _load_owned_active_session(
        self, account_name: str, session_id: str
    ) -> EpisodicSession:
        try:
            session = self.episodic_store.get_session(
                session_id, include_events=True, event_scope="active"
            )
        except Exception as exc:
            raise CurationStorageError(
                f"failed to load session {session_id}"
            ) from exc
        if session is None or session.account_name != account_name:
            raise CurationSessionNotFoundError(
                f"session not found for account: {session_id}"
            )
        return session

    def _generate(self, session: EpisodicSession, *, events: Sequence[EpisodicEvent], max_chars: int) -> str:
        if max_chars <= 0:
            raise ValueError("max_chars must be greater than zero")
        request = DigestGenerationRequest(
            session_id=session.session_id,
            account_name=session.account_name,
            agent_name=session.agent_name,
            friendly_name=session.friendly_name or "",
            events=tuple(events),
            max_chars=max_chars,
        )
        try:
            digest = self.digest_generator.generate(request)
        except DigestGenerationError:
            raise
        except Exception as exc:
            raise DigestGenerationError("digest generation failed") from exc
        if not isinstance(digest, str) or not digest.strip():
            raise DigestGenerationError("digest generator returned empty text")
        return digest.strip()

    def _find_idempotent_boundary(
        self, account_name: str, session_id: str, idempotency_key: str
    ) -> Optional[EpisodicEvent]:
        try:
            session = self.episodic_store.get_session(
                session_id, include_events=True, event_scope="all"
            )
        except Exception as exc:
            raise CurationStorageError(
                f"failed to inspect session {session_id}"
            ) from exc
        if session is None or session.account_name != account_name:
            raise CurationSessionNotFoundError(
                f"session not found for account: {session_id}"
            )
        for event in reversed(session.events):
            if (
                event.kind in ("session_digest", "session_reset")
                and event.metadata.get("visibility_boundary") is True
                and event.metadata.get("idempotency_key") == idempotency_key
            ):
                return event
        return None

    @staticmethod
    def _archive_result(
        session_id: str,
        boundary: EpisodicEvent,
        idempotency_key: str,
    ) -> CurationResult:
        return CurationResult(
            action="reset" if boundary.kind == "session_reset" else "archive",
            session_id=session_id,
            digest=str(boundary.content),
            boundary_event=boundary,
            idempotency_key=idempotency_key,
            source_event_ids=tuple(boundary.metadata.get("source_event_ids", ())),
            provenance=dict(boundary.metadata),
        )


__all__ = [
    "CurationConflictError",
    "CurationError",
    "CurationResult",
    "CurationService",
    "CurationSessionNotFoundError",
    "CurationStorageError",
    "DigestGenerationError",
    "DigestGenerationRequest",
    "DigestGenerator",
    "DigestPublicationError",
]
