from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class EpisodicMemoryRequest:
    """Request for conversational or experiential memory."""

    account_name: str
    agent_name: str
    conversation_id: str = ""
    query: str = ""
    max_events: int = 6
    token_budget: Optional[int] = None
    digest_top_k: int = 3
    digest_max_chars: int = 3000
    event_kinds: Optional[List[str]] = None
    include_session_metadata: bool = True
    include_recent_history: bool = True
    include_archived_digests: bool = True


@dataclass(frozen=True)
class EpisodicEvent:
    """Provider-neutral representation of a stored event."""

    role: str
    content: Any
    kind: str = ""
    actor: str = ""
    event_id: str = ""
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EpisodicDigest:
    session_id: str
    snippet: str
    score: float = 0.0
    truncated: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EpisodicMemoryResult:
    session_id: str = ""
    session_user_id: str = ""
    session_account_name: str = ""
    session_agent_name: str = ""
    session_context_name: str = ""
    session_type: str = ""
    session_participants: List[str] = field(default_factory=list)
    session_friendly_name: str = ""
    session_tags: List[str] = field(default_factory=list)
    session_updated_at: Optional[datetime] = None
    events: List[EpisodicEvent] = field(default_factory=list)
    digests: List[EpisodicDigest] = field(default_factory=list)
    dropped_event_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class EpisodicMemory(ABC):
    """Prompt-time access to current and archived episodic memory."""

    @abstractmethod
    def recall(self, request: EpisodicMemoryRequest) -> EpisodicMemoryResult:
        raise NotImplementedError

    @abstractmethod
    def save_overflow_digest(
        self,
        *,
        account_name: str,
        conversation_id: str,
        snippet: str,
    ) -> Optional[str]:
        raise NotImplementedError
