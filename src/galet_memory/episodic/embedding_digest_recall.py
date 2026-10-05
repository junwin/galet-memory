from __future__ import annotations

import logging

from .models import DigestMatch, DigestSearchRequest
from ..ports import EmbeddingIndex, EmbeddingProvider, TextLoader


class EmbeddingDigestRecall:
    def __init__(
        self,
        *,
        embeddings: EmbeddingProvider,
        index: EmbeddingIndex,
        text_loader: TextLoader,
        namespaces: list[str] | None = None,
        score_threshold: float = 0.25,
        embedding_model: str = "text-embedding-3-small",
    ) -> None:
        self.embeddings = embeddings
        self.index = index
        self.text_loader = text_loader
        self.namespaces = list(namespaces or ["digests"])
        self.score_threshold = score_threshold
        self.embedding_model = embedding_model

    def __call__(self, request: DigestSearchRequest) -> list[DigestMatch]:
        query = request.query.strip()
        if not query:
            return []
        try:
            vector = self.embeddings.embed([query], model=self.embedding_model)[0]
            matches = self.index.query(
                account_name=request.account_name,
                namespaces=self.namespaces,
                vector=vector,
                limit=request.count,
            )
            digests: list[DigestMatch] = []
            for match in matches:
                if match.score < self.score_threshold:
                    continue
                metadata = dict(match.record.metadata)
                path = metadata.get("path")
                if not path:
                    continue
                snippet = self.text_loader.load(path, max_chars=None)
                if not snippet.text.strip():
                    continue
                digests.append(
                    DigestMatch(
                        session_id=match.record.source_id,
                        snippet=snippet.text,
                        score=float(match.score),
                        truncated=snippet.truncated,
                        metadata={
                            **metadata,
                            "namespace": "digests",
                            "embedding_model": self.embedding_model,
                        },
                    )
                )
            return digests
        except Exception as exc:
            logging.warning(
                "EmbeddingDigestRecall: failed for account=%s: %s",
                request.account_name,
                exc,
            )
            return []
