from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Optional, Protocol, Sequence, runtime_checkable
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
    action: Literal["digest", "archive"]
    session_id: str
    digest: str
    boundary_event: Optional[EpisodicEvent] = None
    idempotency_key: str = ""
    publication: Optional[PublishedDigest] = None


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
        digest = self._generate(session, max_chars=max_chars)
        result = CurationResult(action="digest", session_id=session.session_id, digest=digest)
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
            result = self._archive_result(session_id, existing, operation_key)
            return self._publish(result, account_name) if publish else result

        expected_tail = session.events[-1].event_id if session.events else None
        digest = self._generate(session, max_chars=max_chars)
        boundary = EpisodicEvent(
            role="system",
            actor="curation",
            kind="session_digest",
            content=digest,
            metadata={
                "visibility_boundary": True,
                "curation_version": 1,
                "idempotency_key": operation_key,
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
                result = self._archive_result(session_id, existing, operation_key)
                return self._publish(result, account_name) if publish else result
            raise CurationConflictError(str(exc)) from exc
        except Exception as exc:
            raise CurationStorageError(
                f"failed to append digest boundary for session {session_id}"
            ) from exc
        result = self._archive_result(session_id, stored, operation_key)
        return self._publish(result, account_name) if publish else result

    def _require_publisher(self, publish: bool) -> None:
        if publish and self.digest_publisher is None:
            raise CurationStorageError("digest publisher is not configured")

    def _publish(self, result: CurationResult, account_name: str) -> CurationResult:
        assert self.digest_publisher is not None
        try:
            publication = self.digest_publisher.publish(
                account_name=account_name, session_id=result.session_id, digest=result.digest
            )
        except Exception as exc:
            raise CurationStorageError(
                f"failed to publish digest for session {result.session_id}"
            ) from exc
        return replace(result, publication=publication)

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

    def _generate(self, session: EpisodicSession, *, max_chars: int) -> str:
        if max_chars <= 0:
            raise ValueError("max_chars must be greater than zero")
        request = DigestGenerationRequest(
            session_id=session.session_id,
            account_name=session.account_name,
            agent_name=session.agent_name,
            friendly_name=session.friendly_name or "",
            events=tuple(session.events),
            max_chars=max_chars,
        )
        try:
            digest = self.digest_generator.generate(request)
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
                event.kind == "session_digest"
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
            action="archive",
            session_id=session_id,
            digest=str(boundary.content),
            boundary_event=boundary,
            idempotency_key=idempotency_key,
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
]
