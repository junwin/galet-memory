"""Publish a session digest as a text document and searchable embedding."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from .ports.embeddings import EmbeddingIndex, EmbeddingProvider, StoredEmbedding


@dataclass(frozen=True)
class PublishedDigest:
    path: str
    embedding_id: str


class DigestPublisher(Protocol):
    def publish(self, *, account_name: str, session_id: str, digest: str) -> PublishedDigest: ...


class FilesystemDigestStore:
    """Stable path per account and session; repeated publication replaces the document."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, account_name: str, session_id: str) -> Path:
        for value in (account_name, session_id):
            if not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
                raise ValueError("invalid digest path component")
        return self.root / account_name / f"{session_id}.md"

    def write(self, *, account_name: str, session_id: str, digest: str) -> Path:
        path = self.path_for(account_name, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".digest-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(digest)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path


class EmbeddingDigestPublisher:
    """Coordinate the document and embedding through host-supplied providers."""

    def __init__(
        self,
        documents: FilesystemDigestStore,
        embeddings: EmbeddingProvider,
        index: EmbeddingIndex,
        *,
        model: str = "text-embedding-3-small",
        provider: str = "openai",
    ) -> None:
        self.documents = documents
        self.embeddings = embeddings
        self.index = index
        self.model = model
        self.provider = provider

    def publish(self, *, account_name: str, session_id: str, digest: str) -> PublishedDigest:
        if not digest.strip():
            raise ValueError("digest must not be empty")
        path = self.documents.path_for(account_name, session_id)
        vectors = self.embeddings.embed([digest], model=self.model)
        if len(vectors) != 1 or not vectors[0]:
            raise ValueError("embedding provider returned no vector")
        self.documents.write(account_name=account_name, session_id=session_id, digest=digest)
        embedding_id = f"digest:{account_name}:{session_id}"
        self.index.upsert(StoredEmbedding(
            id=embedding_id,
            account_name=account_name,
            namespace="digests",
            vector=list(vectors[0]),
            source_type="digest",
            source_id=session_id,
            document_id=embedding_id,
            model=self.model,
            provider=self.provider,
            metadata={"path": str(path), "session_id": session_id, "title": "Session digest"},
        ))
        return PublishedDigest(path=str(path), embedding_id=embedding_id)


__all__ = ["DigestPublisher", "PublishedDigest", "FilesystemDigestStore", "EmbeddingDigestPublisher"]
