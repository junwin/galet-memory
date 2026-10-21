"""Reusable semantic source ingestion, independent of an agent application."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..ports import (
    EmbeddingIndex,
    EmbeddingProvider,
    KeywordExtractor,
    StoredEmbedding,
    TextLoader,
)


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
    When a keyword extractor is supplied, keywords are derived alongside the
    embedding vector and stored in record metadata, so records can later be
    matched by keyword if an embedding service is unavailable.
    """

    DEFAULT_FOLDER_SUFFIXES = (".md", ".txt")

    def __init__(self, *, embeddings: EmbeddingProvider, index: EmbeddingIndex,
                 text_loader: TextLoader,
                 keyword_extractor: KeywordExtractor | None = None,
                 model: str = "text-embedding-3-small",
                 provider: str = "",
                 max_chars: int = 32000) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self.embeddings = embeddings
        self.index = index
        self.text_loader = text_loader
        self.keyword_extractor = keyword_extractor
        self.model = model
        self.provider = provider
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

    def ingest_folder(self, *, account_name: str, namespace: str,
                      folder: str | Path,
                      suffixes: tuple[str, ...] = DEFAULT_FOLDER_SUFFIXES,
                      recursive: bool = True, tags: tuple[str, ...] = (),
                      skip_errors: bool = False) -> list[SemanticIngestionResult]:
        """Ingest every matching file under a caller-authorized folder.

        Files are selected by suffix (case-insensitive) and ingested in
        sorted, stable order with `ingest_file` semantics: stable source
        identity per resolved path, unchanged content skips the embedding
        call. With `skip_errors` the walk continues past individual file
        failures; otherwise the first failure (for example an oversized
        source) propagates.
        """
        root = Path(folder)
        if not root.is_dir():
            raise ValueError("folder must be an existing directory")
        lowered = {s.lower() for s in suffixes}
        candidates = sorted(root.rglob("*") if recursive else root.iterdir())
        results: list[SemanticIngestionResult] = []
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in lowered:
                continue
            try:
                results.append(self.ingest_file(
                    account_name=account_name, namespace=namespace,
                    path=path, tags=tags,
                ))
            except ValueError:
                if not skip_errors:
                    raise
        return results

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
        if self.keyword_extractor is not None:
            details["keywords"] = list(self.keyword_extractor.extract(text))
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
            document_id=source_id, model=self.model, provider=self.provider, metadata=details,
        ))
        return SemanticIngestionResult(record_id, source_id,
                                       "updated" if previous else "added", digest)

    def delete(self, *, account_name: str, namespace: str, source_id: str) -> bool:
        return self.index.delete_record(account_name=account_name, namespace=namespace,
                                        record_id=self.record_id(account_name, namespace, source_id))
