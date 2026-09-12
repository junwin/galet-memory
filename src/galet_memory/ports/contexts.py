from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class SkillSnapshot:
    name: str
    text: str
    mandatory_tools: Sequence[str] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextSnapshot:
    id: str
    account_name: str
    tag: Optional[str] = None
    text: str = ""
    resolved_text: str = ""
    imports: Sequence[str] = ()
    skills: Sequence[SkillSnapshot] = ()
    missing_imports: Sequence[str] = ()
    required_tools: Sequence[str] = ()
    search_namespaces: Sequence[str] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    updated_at: Optional[datetime] = None


class ContextRepository(Protocol):
    def get(
        self, account_name: str, context_name: str
    ) -> Optional[ContextSnapshot]: ...

    def get_or_create(
        self, account_name: str, context_name: str
    ) -> ContextSnapshot: ...
