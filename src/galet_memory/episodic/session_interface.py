"""Session metadata operations, always scoped to the owning account."""

from typing import Protocol, Sequence
from .models import Session, SessionChanges, Page, ClearResult, EmptyTail


class SessionStore(Protocol):
    def create_session(
        self,
        *,
        account_name: str,
        session_id: str | None = None,
        friendly_name: str | None = None,
        context_name: str | None = None,
        tags: Sequence[str] = (),
        metadata: dict | None = None,
    ) -> Session: ...
    def get_session(self, *, account_name: str, session_id: str) -> Session | None: ...
    def list_sessions(
        self, *, account_name: str, count: int = 20, cursor: str | None = None
    ) -> Page[Session]: ...
    def update_session(
        self, *, account_name: str, session_id: str, changes: SessionChanges
    ) -> Session: ...
    def clear_session_events(
        self,
        *,
        account_name: str,
        session_id: str,
        expected_last_event_id: str | EmptyTail | None = None,
    ) -> ClearResult: ...
    def delete_session(self, *, account_name: str, session_id: str) -> bool: ...
    def delete_sessions(
        self, *, account_name: str, session_ids: Sequence[str]
    ) -> list[str]: ...
