"""Typed records for account-owned sessions and temporal event streams."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar


class Unset(Enum):
    VALUE = "unset"


UNSET = Unset.VALUE


class EmptyTail(Enum):
    VALUE = "empty"


EMPTY_TAIL = EmptyTail.VALUE


class EpisodicConcurrencyError(RuntimeError):
    """A conditional write observed a different append tail."""


class EpisodicSessionNotFoundError(ValueError):
    """Missing session or account ownership mismatch."""


class EpisodicCorrelationInvalidatedError(ValueError):
    """An append tried to extend an invalidated exchange."""


class EpisodicCompatibilityError(ValueError):
    """Unsupported storage schema; start with fresh storage."""


@dataclass(frozen=True)
class Session:
    session_id: str
    account_name: str
    friendly_name: str | None = None
    context_name: str | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_event_id: str | None = None


@dataclass(frozen=True)
class SessionChanges:
    friendly_name: str | None | Unset = UNSET
    context_name: str | None | Unset = UNSET
    tags: tuple[str, ...] | Unset = UNSET
    metadata: dict[str, Any] | Unset = UNSET


@dataclass(frozen=True)
class NewEvent:
    role: str
    content: Any
    actor: str
    kind: str = ""
    created_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    correlation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Event(NewEvent):
    event_id: str = ""
    session_id: str = ""
    sequence: int = 0
    stored_at: datetime | None = None


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...] = ()
    next_cursor: str | None = None


@dataclass(frozen=True)
class EventPage:
    events: tuple[Event, ...] = ()
    next_cursor: str | None = None
    before_event_id: str | None = None
    last_event_id: str | None = None


@dataclass(frozen=True)
class Exchange:
    session_id: str
    correlation_id: str
    events: tuple[Event, ...]


@dataclass(frozen=True)
class HistorySnapshot:
    session: Session
    events: tuple[Event, ...]

    @property
    def session_id(self):
        return self.session.session_id

    @property
    def account_name(self):
        return self.session.account_name

    @property
    def friendly_name(self):
        return self.session.friendly_name

    @property
    def last_event_id(self):
        return self.session.last_event_id


@dataclass(frozen=True)
class ClearResult:
    session_id: str
    removed_event_count: int
    last_event_id: str | None


@dataclass(frozen=True)
class InvalidationResult:
    session_id: str
    correlation_id: str
    status: Literal["invalidated", "already_invalidated", "not_found"]
    event_ids: tuple[str, ...] = ()
    invalidated_digest_ids: tuple[str, ...] = ()
    marker_event_id: str | None = None
    unprovenanced_digests_invalidated: bool = False

    @property
    def event_count(self):
        return len(self.event_ids)


@dataclass(frozen=True)
class Digest:
    digest_id: str
    session_id: str
    text: str
    source_event_ids: tuple[str, ...] = ()
    created_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DigestMatch:
    session_id: str
    snippet: str
    score: float = 0.0
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DigestSearchRequest:
    account_name: str
    query: str
    session_id: str | None = None
    count: int = 3
