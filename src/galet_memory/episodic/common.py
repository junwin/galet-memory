"""Shared serialization and validation for supported episodic backends."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .interface import EpisodicDigest, EpisodicMemoryRequest

DigestRecall = Callable[[EpisodicMemoryRequest], Sequence[EpisodicDigest]]


_SESSION_PATCH_FIELDS = frozenset(
    {
        "user_id",
        "account_name",
        "agent_name",
        "friendly_name",
        "context_name",
        "session_type",
        "participants",
        "links",
        "tags",
        "metadata",
    }
)


class EpisodicCompatibilityError(ValueError):
    """Raised when a database contains an incompatible episodic record."""


def _utc_now() -> datetime:
    # Stored timestamps use timezone-naive values representing UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _validate_segment(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str, got {type(value).__name__}")
    if not value or "/" in value or ".." in value:
        raise ValueError(f"{name} is not a valid storage key segment: {value!r}")
    return value

