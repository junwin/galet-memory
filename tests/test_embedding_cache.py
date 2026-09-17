import pytest

from galet_memory import (
    CachingEmbeddingProvider,
    EmbeddingDigestRecall,
    EpisodicMemoryRequest,
    SemanticMemoryRequest,
    VectorSemanticMemory,
)
from galet_memory.ports import TextSnippet


class RecordingProvider:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False

    def embed(self, texts, *, model):
        self.calls.append((tuple(texts), model))
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[float(len(text)), 1.0] for text in texts]


class EmptyIndex:
    def list_namespaces(self, account_name):
        return ["documents", "digests"]

    def query(self, **kwargs):
        return []


class EmptyTextLoader:
    def load(self, path, *, max_chars):
        return TextSnippet("")


def test_reuses_exact_request_inside_scope():
    underlying = RecordingProvider()
    provider = CachingEmbeddingProvider(underlying)

    with provider.request_scope() as cache:
        first = provider.embed(["attention"], model="model-a")
        second = provider.embed(["attention"], model="model-a")

        assert first == second
        assert cache.info().hits == 1
        assert cache.info().misses == 1
        assert cache.info().size == 1

    assert underlying.calls == [(('attention',), "model-a")]


def test_model_and_ordered_texts_are_part_of_key():
    underlying = RecordingProvider()
    provider = CachingEmbeddingProvider(underlying)

    with provider.request_scope() as cache:
        provider.embed(["one", "two"], model="model-a")
        provider.embed(["two", "one"], model="model-a")
        provider.embed(["one", "two"], model="model-b")

    assert cache.info().hits == 0
    assert cache.info().misses == 3
    assert len(underlying.calls) == 3


def test_calls_outside_scope_are_not_cached_and_scopes_are_isolated():
    underlying = RecordingProvider()
    provider = CachingEmbeddingProvider(underlying)

    provider.embed(["same"], model="model")
    provider.embed(["same"], model="model")
    with provider.request_scope():
        provider.embed(["same"], model="model")
        provider.embed(["same"], model="model")
    with provider.request_scope():
        provider.embed(["same"], model="model")

    assert len(underlying.calls) == 4


def test_failed_calls_are_not_cached():
    underlying = RecordingProvider()
    provider = CachingEmbeddingProvider(underlying)

    with provider.request_scope() as cache:
        underlying.fail = True
        with pytest.raises(RuntimeError, match="unavailable"):
            provider.embed(["retry"], model="model")
        underlying.fail = False
        provider.embed(["retry"], model="model")

    assert len(underlying.calls) == 2
    assert cache.info().hits == 0
    assert cache.info().misses == 2
    assert cache.info().size == 1


def test_semantic_and_digest_recall_share_one_query_embedding():
    underlying = RecordingProvider()
    provider = CachingEmbeddingProvider(underlying)
    index = EmptyIndex()
    loader = EmptyTextLoader()
    semantic = VectorSemanticMemory(
        embeddings=provider,
        index=index,
        text_loader=loader,
    )
    digests = EmbeddingDigestRecall(
        embeddings=provider,
        index=index,
        text_loader=loader,
        embedding_model="text-embedding-3-small",
    )

    with provider.request_scope() as cache:
        digests(EpisodicMemoryRequest("acct", "agent", query="attention"))
        semantic.recall(
            SemanticMemoryRequest(
                account_name="acct",
                query="attention",
                embedding_model="text-embedding-3-small",
            )
        )

    assert underlying.calls == [
        (("attention",), "text-embedding-3-small")
    ]
    assert cache.info().hits == 1
    assert cache.info().misses == 1
