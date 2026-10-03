from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..episodic.management import EpisodicMemoryManager

from .interface import (
    SemanticDocument,
    SemanticMemory,
    SemanticMemoryRequest,
    SemanticMemoryResult,
)
from ..ports import EmbeddingIndex, EmbeddingProvider, TextLoader


class VectorSemanticMemory(SemanticMemory):
    def __init__(
        self,
        *,
        embeddings: EmbeddingProvider,
        index: EmbeddingIndex,
        text_loader: TextLoader,
        episodic_store: EpisodicMemoryManager | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.index = index
        self.text_loader = text_loader
        self.episodic_store = episodic_store

    def list_namespaces(self, account_name: str) -> list[str]:
        return list(self.index.list_namespaces(account_name))

    def recall(self, request: SemanticMemoryRequest) -> SemanticMemoryResult:
        if not request.query.strip():
            return SemanticMemoryResult(
                metadata={"backend": "vector", "reason": "empty_query"}
            )
        if not request.use_embeddings:
            return SemanticMemoryResult(
                metadata={"backend": "vector", "reason": "embedding_mode_disabled"}
            )

        namespaces = list(request.namespaces or ["external"])
        vector = self.embeddings.embed(
            [request.query], model=request.embedding_model
        )[0]
        filters = (
            {"source_type": request.source_type}
            if request.source_type
            else None
        )
        matches = list(
            self.index.query(
                account_name=request.account_name,
                namespaces=namespaces,
                vector=vector,
                limit=request.top_k,
                filters=filters,
            )
        )

        documents: list[SemanticDocument] = []
        skipped_below_threshold = 0
        skipped_without_path = 0
        skipped_empty_snippet = 0
        skipped_invalid_digest = 0
        for match in matches:
            if match.score < request.score_threshold:
                skipped_below_threshold += 1
                continue
            metadata = dict(match.record.metadata)
            if match.record.source_type == "digest":
                # A semantic namespace must not bypass episodic invalidation.
                if self.episodic_store is None or not self.episodic_store.is_digest_valid(
                    account_name=request.account_name, session_id=match.record.source_id,
                    digest_id=metadata.get("digest_id"),
                    source_event_ids=metadata.get("source_event_ids"),
                ):
                    skipped_invalid_digest += 1
                    continue
            path = metadata.get("path")
            inline_text = metadata.get("text")
            if not path and not isinstance(inline_text, str):
                skipped_without_path += 1
                continue
            if path:
                snippet = self.text_loader.load(path, max_chars=request.max_chars)
            else:
                from ..ports.text import TextSnippet
                snippet = TextSnippet(inline_text[:request.max_chars],
                                      len(inline_text) > request.max_chars)
            if not snippet.text.strip():
                skipped_empty_snippet += 1
                continue
            tags = metadata.get("tags") or []
            if not isinstance(tags, list):
                tags = [str(tags)]
            documents.append(
                SemanticDocument(
                    source_id=match.record.source_id,
                    title=str(
                        metadata.get("title")
                        or match.record.source_id
                        or match.record.id
                    ),
                    snippet=snippet.text,
                    tags=[str(tag) for tag in tags],
                    score=float(match.score),
                    truncated=snippet.truncated,
                    path=str(path) if path else None,
                    source_type=match.record.source_type or None,
                    metadata=metadata,
                )
            )

        return SemanticMemoryResult(
            documents=documents,
            metadata={
                "backend": "vector",
                "embedding_model": request.embedding_model,
                "namespaces": namespaces,
                "source_type": request.source_type,
                "raw_result_count": len(matches),
                "selected_count": len(documents),
                "skipped_below_threshold": skipped_below_threshold,
                "skipped_without_path": skipped_without_path,
                "skipped_empty_snippet": skipped_empty_snippet,
                "skipped_invalid_digest": skipped_invalid_digest,
            },
        )
