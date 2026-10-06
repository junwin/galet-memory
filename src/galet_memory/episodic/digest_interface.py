"""Stored and searched digests, separate from transcript and prompt budgeting."""

from typing import Protocol, Sequence
from .models import (
    Digest,
    DigestMatch,
    Page,
    HistorySnapshot,
    NewEvent,
    Event,
    EmptyTail,
)


class DigestStore(Protocol):
    def get_digest(
        self, *, account_name: str, session_id: str, digest_id: str
    ) -> Digest | None: ...
    def list_digests(
        self,
        *,
        account_name: str,
        session_id: str,
        count: int = 20,
        cursor: str | None = None,
    ) -> Page[Digest]: ...
    def search_digests(
        self,
        *,
        account_name: str,
        query: str,
        session_id: str | None = None,
        count: int = 3,
    ) -> list[DigestMatch]: ...
    def save_overflow_digest(
        self, *, account_name: str, session_id: str, snippet: str
    ) -> str | None: ...
    def is_digest_valid(
        self,
        *,
        account_name: str,
        session_id: str,
        digest_id: str | None = None,
        source_event_ids: Sequence[str] | None = None,
    ) -> bool: ...


class CurationStore(DigestStore, Protocol):
    """Atomic snapshots/control writes for package-owned curation, not user tools."""

    def get_active_snapshot(
        self, *, account_name: str, session_id: str
    ) -> HistorySnapshot: ...
    def get_transcript_snapshot(
        self, *, account_name: str, session_id: str
    ) -> HistorySnapshot: ...
    def get_audit_snapshot(
        self, *, account_name: str, session_id: str
    ) -> HistorySnapshot: ...
    def append_boundary(
        self,
        *,
        account_name: str,
        session_id: str,
        event: NewEvent,
        expected_last_event_id: str | EmptyTail,
    ) -> Event: ...
