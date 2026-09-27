"""Reusable semantic source ingestion, independent of an agent application."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..ports import EmbeddingIndex, EmbeddingProvider, StoredEmbedding, TextLoader


@dataclass(frozen=True)
class SemanticIngestionResult:
    record_id: str
    source_id: str
    status: str  # added, updated, unchanged
    content_hash: str


class SemanticIngestionService:
    """Embed and refresh one source in an account-scoped namespace.

    The caller authorizes paths and chooses account, namespace and model. A
    source has a stable identity across edits; unchanged text avoids API calls.
    """

    def __init__(self, *, embeddings: EmbeddingProvider, index: EmbeddingIndex,
                 text_loader: TextLoader, model: str = "text-embedding-3-small",
                 max_chars: int = 32000) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self.embeddings = embeddings
        self.index = index
        self.text_loader = text_loader
        self.model = model
        self.max_chars = max_chars

    @staticmethod
    def record_id(account_name: str, namespace: str, source_id: str) -> str:
        if not all((account_name, namespace, source_id)):
            raise ValueError("account_name, namespace and source_id are required")
        key = "\x00".join((account_name, namespace, source_id))
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def ingest_file(self, *, account_name: str, namespace: str, path: str | Path,
                    source_id: str | None = None, title: str | None = None,
                    tags: tuple[str, ...] = ()) -> SemanticIngestionResult:
        resolved = Path(path).resolve(strict=True)
        snippet = self.text_loader.load(resolved, max_chars=self.max_chars + 1)
        if snippet.truncated or len(snippet.text) > self.max_chars:
            raise ValueError("source exceeds max_chars; split it before indexing")
        return self.ingest_text(
            account_name=account_name, namespace=namespace,
            source_id=source_id or str(resolved), text=snippet.text,
            source_type="file", title=title or resolved.name, tags=tags,
            metadata={"path": str(resolved)},
        )

    def ingest_text(self, *, account_name: str, namespace: str, source_id: str,
                    text: str, source_type: str = "text", title: str | None = None,
                    tags: tuple[str, ...] = (), metadata: Mapping[str, Any] | None = None,
                    ) -> SemanticIngestionResult:
        if not text.strip():
            raise ValueError("cannot index empty text")
        if len(text) > self.max_chars:
            raise ValueError("source exceeds max_chars; split it before indexing")
        record_id = self.record_id(account_name, namespace, source_id)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        details = dict(metadata or {})
        details.update({"content_hash": digest, "embedding_model": self.model,
                        "title": title or source_id, "tags": list(tags)})
        if "path" not in details:
            details["text"] = text
        previous = self.index.get_record(account_name=account_name, namespace=namespace,
                                         record_id=record_id)
        if previous and previous.metadata == details and previous.source_type == source_type:
            return SemanticIngestionResult(record_id, source_id, "unchanged", digest)
        vectors = self.embeddings.embed([text], model=self.model)
        if len(vectors) != 1 or not vectors[0]:
            raise ValueError("embedding provider returned no vector")
        self.index.upsert(StoredEmbedding(
            id=record_id, account_name=account_name, namespace=namespace,
            vector=vectors[0], source_type=source_type, source_id=source_id,
            document_id=source_id, model=self.model, metadata=details,
        ))
        return SemanticIngestionResult(record_id, source_id,
                                       "updated" if previous else "added", digest)

    def delete(self, *, account_name: str, namespace: str, source_id: str) -> bool:
        return self.index.delete_record(account_name=account_name, namespace=namespace,
                                        record_id=self.record_id(account_name, namespace, source_id))
