from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ProceduralMemoryRequest:
    """Request for named project context and imported skills."""

    account_name: str
    context_name: str
    create_if_missing: bool = True
    include_resolved_text: bool = True
    include_skills: bool = True
    include_required_tools: bool = True


@dataclass(frozen=True)
class ProceduralSkill:
    name: str
    text: str
    mandatory_tools: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProceduralMemoryResult:
    context_id: str = ""
    account_name: str = ""
    tag: Optional[str] = None
    text: str = ""
    resolved_text: str = ""
    imports: List[str] = field(default_factory=list)
    skills: List[ProceduralSkill] = field(default_factory=list)
    missing_imports: List[str] = field(default_factory=list)
    required_tools: List[str] = field(default_factory=list)
    search_namespaces: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProceduralMemory(ABC):
    @abstractmethod
    def recall(
        self, request: ProceduralMemoryRequest
    ) -> ProceduralMemoryResult:
        raise NotImplementedError
