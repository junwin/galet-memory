from __future__ import annotations

from typing import Sequence

from galet.embedding_interface import EmbeddingApi


class GaletEmbeddingProvider:
    """Expose Galet's embedding API through galet-memory's narrow port."""

    def __init__(self, api: EmbeddingApi) -> None:
        self.api = api

    def embed(
        self, texts: Sequence[str], *, model: str
    ) -> Sequence[Sequence[float]]:
        return self.api.embed(model=model, input=list(texts)).embeddings
