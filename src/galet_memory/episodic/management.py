from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Sequence

from .interface import EpisodicEvent


EventScope = Literal["active", "all", "archived", "raw"]


class EpisodicConcurrencyError(RuntimeError):
    """Raised when a conditional write observes a different event tail."""


class EpisodicSessionNotFoundError(ValueError):
    """The session is missing or not owned by the requested account."""


class EpisodicCorrelationInvalidatedError(ValueError):
    """An event cannot be linked to an invalidated correlation."""


@dataclass(frozen=True)
class EpisodicInvalidationResult:
    session_id: str
    correlation_id: str
    status: Literal["invalidated", "already_invalidated", "not_found"]
    event_ids: tuple[str, ...] = ()
    invalidated_digest_ids: tuple[str, ...] = ()
    marker_event_id: Optional[str] = None
    # External artifacts without trustworthy event provenance are suppressed
    # for this session, including cumulative and overflow digests.
    unprovenanced_digests_invalidated: bool = False

    @property
    def event_count(self) -> int:
        return len(self.event_ids)


@dataclass(frozen=True)
class EpisodicSessionQuery:
    account_name: str
    agent_name: str = ""
    query: str = ""
    limit: int = 20


@dataclass(frozen=True)
class EpisodicSession:
    session_id: str
    account_name: str
    agent_name: str
    user_id: str = ""
    friendly_name: Optional[str] = None
    context_name: Optional[str] = None
    session_type: str = "user"
    participants: List[str] = field(default_factory=list)
    links: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    events: List[EpisodicEvent] = field(default_factory=list)
    # The actual append tail, including hidden control and invalidated events.
    last_event_id: Optional[str] = None


@dataclass(frozen=True)
class EpisodicCurationRequest:
    account_name: str
    session_id: str = ""
    friendly_name: str = ""
    mode: str = "filter"
    preview: bool = True
    publish: bool = False
    template_name: str = "default"
    curation_rules: Dict[str, Any] = field(default_factory=dict)
    max_chars: int = 32000


@dataclass(frozen=True)
class EpisodicCurationResult:
    status: str
    session_id: str = ""
    note_text: str = ""
    output_path: str = ""
    summary: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


class EpisodicMemoryManager(ABC):
    """Provider-neutral episodic session and event store."""

    @abstractmethod
    def invalidate_events(
        self, *, account_name: str, session_id: str, correlation_id: str,
        expected_last_event_id: Optional[str] = None,
    ) -> EpisodicInvalidationResult:
        """Invalidate one correlation. A non-None expected tail guards the write.

        Missing correlations return not_found; missing/unowned sessions raise.
        Repeated calls return the original result without appending again.
        """
        raise NotImplementedError

    @abstractmethod
    def is_digest_valid(
        self, *, account_name: str, session_id: str,
        digest_id: Optional[str] = None,
        source_event_ids: Optional[Sequence[str]] = None,
    ) -> bool:
        """Check stored or external digest provenance against invalidations."""
        raise NotImplementedError

    @abstractmethod
    def create_session(
        self,
        *,
        account_name: str,
        agent_name: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        friendly_name: Optional[str] = None,
        context_name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        session_type: str = "user",
        participants: Optional[List[str]] = None,
        links: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EpisodicSession:
        raise NotImplementedError

    @abstractmethod
    def get_session(
        self,
        session_id: str,
        *,
        include_events: bool = True,
        event_scope: EventScope = "active",
    ) -> Optional[EpisodicSession]:
        """Read visible events; raw explicitly includes originals/control markers.

        last_event_id always describes the raw append tail from this snapshot.
        """
        raise NotImplementedError

    @abstractmethod
    def list_sessions(self, query: EpisodicSessionQuery) -> List[EpisodicSession]:
        raise NotImplementedError

    @abstractmethod
    def session_exists(self, session_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def append_event(
        self, session_id: str, event: EpisodicEvent
    ) -> EpisodicEvent:
        raise NotImplementedError

    @abstractmethod
    def append_event_if_tail(
        self,
        session_id: str,
        event: EpisodicEvent,
        *,
        expected_last_event_id: Optional[str],
    ) -> EpisodicEvent:
        """Append atomically only when the current event tail is expected."""
        raise NotImplementedError

    @abstractmethod
    def add_events(
        self, session_id: str, events: List[EpisodicEvent]
    ) -> List[EpisodicEvent]:
        raise NotImplementedError

    @abstractmethod
    def link_event(
        self,
        correlation_id: Optional[str],
        session_id: str,
        event_id: str,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def update_session(
        self, session_id: str, patch: Dict[str, Any]
    ) -> EpisodicSession:
        raise NotImplementedError

    @abstractmethod
    def reset_session(self, session_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def delete_session(self, session_id: str) -> None:
        raise NotImplementedError

    def delete_sessions(self, session_ids: Sequence[str]) -> List[str]:
        """Delete selected sessions; return the IDs that existed."""
        deleted: List[str] = []
        for session_id in dict.fromkeys(session_ids):
            if self.session_exists(session_id):
                self.delete_session(session_id)
                deleted.append(session_id)
        return deleted
