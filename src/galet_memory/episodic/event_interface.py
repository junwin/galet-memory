"""Explicit event and exchange access; counts are records, never tokens."""

from datetime import datetime
from typing import Protocol, Sequence
from .models import Event, EventPage, Exchange, InvalidationResult, NewEvent, EmptyTail


class EventStore(Protocol):
    def append_event(
        self,
        *,
        account_name: str,
        session_id: str,
        event: NewEvent,
        expected_last_event_id: str | EmptyTail | None = None,
    ) -> Event: ...
    def append_events(
        self,
        *,
        account_name: str,
        session_id: str,
        events: Sequence[NewEvent],
        expected_last_event_id: str | EmptyTail | None = None,
    ) -> list[Event]: ...
    def get_event(
        self, *, account_name: str, session_id: str, event_id: str
    ) -> Event | None: ...
    def get_recent_events(
        self,
        *,
        account_name: str,
        session_id: str,
        count: int = 10,
        event_kinds: Sequence[str] | None = None,
        actors: Sequence[str] | None = None,
        before_event_id: str | None = None,
    ) -> EventPage: ...
    def get_events_by_period(
        self,
        *,
        account_name: str,
        session_id: str,
        start: datetime,
        end: datetime,
        event_kinds: Sequence[str] | None = None,
        actors: Sequence[str] | None = None,
        count: int = 100,
        cursor: str | None = None,
    ) -> EventPage: ...
    def get_exchange(
        self, *, account_name: str, session_id: str, correlation_id: str
    ) -> Exchange | None: ...
    def invalidate_exchange(
        self,
        *,
        account_name: str,
        session_id: str,
        correlation_id: str,
        expected_last_event_id: str | EmptyTail | None = None,
    ) -> InvalidationResult: ...
