from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Sequence

from .embeddings import EmbeddingProvider


EmbeddingVectors = tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class EmbeddingCacheKey:
    """Exact identity of one embedding request."""

    model: str
    texts: tuple[str, ...]


@dataclass(frozen=True)
class EmbeddingCacheInfo:
    hits: int
    misses: int
    size: int


class EmbeddingCache:
    """Mutable cache state intended to live for one application request."""

    def __init__(self) -> None:
        self._values: dict[EmbeddingCacheKey, EmbeddingVectors] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: EmbeddingCacheKey) -> EmbeddingVectors | None:
        value = self._values.get(key)
        if value is None:
            self._misses += 1
            return None
        self._hits += 1
        return value

    def put(
        self,
        key: EmbeddingCacheKey,
        vectors: Sequence[Sequence[float]],
    ) -> EmbeddingVectors:
        immutable = tuple(
            tuple(float(value) for value in row) for row in vectors
        )
        self._values[key] = immutable
        return immutable

    def clear(self) -> None:
        self._values.clear()
        self._hits = 0
        self._misses = 0

    def info(self) -> EmbeddingCacheInfo:
        return EmbeddingCacheInfo(
            hits=self._hits,
            misses=self._misses,
            size=len(self._values),
        )


class CachingEmbeddingProvider:
    """Add request-scoped reuse to any embedding provider.

    Calls pass straight through unless the caller opens ``request_scope()``.
    A context variable isolates concurrent threads/tasks. Nested scopes reuse
    the active cache so independently composed memory components cooperate.
    """

    def __init__(self, provider: EmbeddingProvider) -> None:
        self.provider = provider
        self._active_cache: ContextVar[EmbeddingCache | None] = ContextVar(
            f"embedding_cache_{id(self)}",
            default=None,
        )

    @contextmanager
    def request_scope(self) -> Iterator[EmbeddingCache]:
        active = self._active_cache.get()
        if active is not None:
            yield active
            return

        cache = EmbeddingCache()
        token = self._active_cache.set(cache)
        try:
            yield cache
        finally:
            self._active_cache.reset(token)

    def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
    ) -> Sequence[Sequence[float]]:
        exact_texts = tuple(texts)
        cache = self._active_cache.get()
        if cache is None:
            return self.provider.embed(exact_texts, model=model)

        key = EmbeddingCacheKey(model=model, texts=exact_texts)
        cached = cache.get(key)
        if cached is not None:
            return cached

        # A failed provider call must remain retryable.
        vectors = self.provider.embed(exact_texts, model=model)
        return cache.put(key, vectors)


__all__ = [
    "CachingEmbeddingProvider",
    "EmbeddingCache",
    "EmbeddingCacheInfo",
    "EmbeddingCacheKey",
]
