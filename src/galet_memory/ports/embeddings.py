from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence


class EmbeddingProvider(Protocol):
    def embed(
        self, texts: Sequence[str], *, model: str
    ) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True)
class EmbeddingRecord:
    id: str
    source_id: str
    source_type: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmbeddingMatch:
    record: EmbeddingRecord
    score: float


class EmbeddingIndex(Protocol):
    def list_namespaces(self, account_name: str) -> Sequence[str]: ...

    def query(
        self,
        *,
        account_name: str,
        namespaces: Sequence[str],
        vector: Sequence[float],
        limit: int,
        filters: Optional[Mapping[str, Any]] = None,
    ) -> Sequence[EmbeddingMatch]: ...
