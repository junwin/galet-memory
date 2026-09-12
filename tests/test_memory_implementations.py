from datetime import datetime
from types import SimpleNamespace

from galet_memory import (
    ContextProceduralMemory,
    EmbeddingDigestRecall,
    EpisodicMemoryRequest,
    ProceduralMemoryRequest,
    SemanticMemoryRequest,
    VectorSemanticMemory,
)
from galet_memory.galet_adapter import GaletEmbeddingProvider
from galet_memory.ports import (
    ContextSnapshot,
    EmbeddingMatch,
    EmbeddingRecord,
    SkillSnapshot,
    TextSnippet,
)


class FakeEmbeddings:
    def embed(self, texts, *, model):
        assert list(texts)
        return [[0.1, 0.2]]


class FakeIndex:
    def __init__(self, matches=()):
        self.matches = list(matches)
        self.query_args = None

    def list_namespaces(self, account_name):
        return ["documents", "digests"]

    def query(self, **kwargs):
        self.query_args = kwargs
        return self.matches


class FakeTextLoader:
    def __init__(self, text="loaded text", truncated=False):
        self.snippet = TextSnippet(text, truncated)

    def load(self, path, *, max_chars):
        return self.snippet


def _match(*, namespace_path="/notes/a.md", score=0.9):
    return EmbeddingMatch(
        EmbeddingRecord(
            id="r1",
            source_id="source-1",
            source_type="document",
            metadata={"path": namespace_path, "title": "A", "tags": ["x"]},
        ),
        score,
    )


def test_vector_semantic_memory_uses_ports():
    index = FakeIndex([_match()])
    memory = VectorSemanticMemory(
        embeddings=FakeEmbeddings(),
        index=index,
        text_loader=FakeTextLoader(truncated=True),
    )

    result = memory.recall(
        SemanticMemoryRequest(
            account_name="acct",
            query="hello",
            namespaces=["documents"],
            source_type="document",
        )
    )

    assert result.documents[0].snippet == "loaded text"
    assert result.documents[0].truncated is True
    assert index.query_args["filters"] == {"source_type": "document"}


def test_digest_recall_uses_ports():
    recall = EmbeddingDigestRecall(
        embeddings=FakeEmbeddings(),
        index=FakeIndex([_match()]),
        text_loader=FakeTextLoader("digest"),
    )

    results = recall(
        EpisodicMemoryRequest("acct", "agent", query="find this")
    )

    assert results[0].session_id == "source-1"
    assert results[0].snippet == "digest"
    assert results[0].metadata["namespace"] == "digests"


class FakeContexts:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get(self, account_name, context_name):
        return self.snapshot

    def get_or_create(self, account_name, context_name):
        return self.snapshot


def test_context_procedural_memory_maps_snapshot():
    updated = datetime(2026, 9, 12)
    snapshot = ContextSnapshot(
        id="project",
        account_name="acct",
        resolved_text="resolved",
        skills=[SkillSnapshot("develop", "how to", ["bash"])],
        required_tools=["bash"],
        search_namespaces=["documents"],
        updated_at=updated,
    )
    memory = ContextProceduralMemory(FakeContexts(snapshot))

    result = memory.recall(ProceduralMemoryRequest("acct", "project"))

    assert result.resolved_text == "resolved"
    assert result.skills[0].mandatory_tools == ["bash"]
    assert result.required_tools == ["bash"]
    assert result.metadata["updated_at"] == updated.isoformat()


def test_galet_embedding_provider_unwraps_response():
    class FakeApi:
        def embed(self, *, model, input):
            assert model == "model"
            assert input == ["one"]
            return SimpleNamespace(embeddings=[[1.0, 2.0]])

    provider = GaletEmbeddingProvider(FakeApi())
    assert provider.embed(["one"], model="model") == [[1.0, 2.0]]
