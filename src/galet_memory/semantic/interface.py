from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SemanticMemoryRequest:
    """Request for embedding-backed durable knowledge."""

    account_name: str
    query: str
    use_embeddings: bool = True
    namespaces: List[str] = field(default_factory=lambda: ["external"])
    top_k: int = 3
    max_chars: int = 9000
    score_threshold: float = 0.25
    embedding_model: str = "text-embedding-3-small"
    source_type: Optional[str] = None


@dataclass(frozen=True)
class SemanticDocument:
    source_id: str
    title: str
    snippet: str
    tags: List[str] = field(default_factory=list)
    score: Optional[float] = None
    truncated: bool = False
    path: Optional[str] = None
    source_type: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SemanticMemoryResult:
    documents: List[SemanticDocument] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SemanticMemory(ABC):
    @abstractmethod
    def recall(self, request: SemanticMemoryRequest) -> SemanticMemoryResult:
        raise NotImplementedError
