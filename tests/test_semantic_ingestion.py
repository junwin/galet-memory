from __future__ import annotations

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")

from galet_memory import SemanticIngestionService, SemanticMemoryRequest, VectorSemanticMemory
from galet_memory.ports import FileTextLoader, SqliteVecEmbeddingIndex


class FixtureEmbeddings:
    def __init__(self):
        self.calls = 0

    def embed(self, texts, *, model):
        self.calls += 1
        vectors = []
        for text in texts:
            vector = [0.0] * 1536
            vector[0 if "orchard" in text.lower() else 1] = 1.0
            vectors.append(vector)
        return vectors


def test_file_ingest_recall_refresh_and_delete(tmp_path):
    source = tmp_path / "note.md"
    source.write_text("The orchard has apple trees.", encoding="utf-8")
    provider = FixtureEmbeddings()
    with SqliteVecEmbeddingIndex(tmp_path / "semantic.sqlite") as index:
        ingestion = SemanticIngestionService(embeddings=provider, index=index,
                                             text_loader=FileTextLoader())
        memory = VectorSemanticMemory(embeddings=provider, index=index,
                                       text_loader=FileTextLoader())
        first = ingestion.ingest_file(account_name="acct", namespace="notes", path=source,
                                      source_id="note-1", tags=("garden",))
        assert first.status == "added"
        assert ingestion.ingest_file(account_name="acct", namespace="notes", path=source,
                                     source_id="note-1", tags=("garden",)).status == "unchanged"
        assert provider.calls == 1
        request = SemanticMemoryRequest(account_name="acct", query="orchard",
                                        namespaces=["notes"], score_threshold=0)
        found = memory.recall(request)
        assert found.documents[0].snippet == "The orchard has apple trees."
        assert found.documents[0].tags == ["garden"]
        assert memory.recall(SemanticMemoryRequest(account_name="other", query="orchard",
                                                   namespaces=["notes"])).documents == []

        source.write_text("The orchard has pear trees.", encoding="utf-8")
        assert ingestion.ingest_file(account_name="acct", namespace="notes", path=source,
                                     source_id="note-1", tags=("garden",)).status == "updated"
        assert memory.recall(request).documents[0].snippet == "The orchard has pear trees."
        assert ingestion.delete(account_name="other", namespace="notes", source_id="note-1") is False
        assert ingestion.delete(account_name="acct", namespace="notes", source_id="note-1") is True
        assert memory.recall(request).documents == []


def test_inline_text_and_oversize_are_explicit(tmp_path):
    provider = FixtureEmbeddings()
    with SqliteVecEmbeddingIndex(tmp_path / "semantic.sqlite") as index:
        ingestion = SemanticIngestionService(embeddings=provider, index=index,
                                             text_loader=FileTextLoader(), max_chars=100)
        with pytest.raises(ValueError, match="exceeds max_chars"):
            ingestion.ingest_text(account_name="acct", namespace="facts", source_id="long",
                                  text="x" * 101)
        assert provider.calls == 0
        ingestion.ingest_text(account_name="acct", namespace="facts", source_id="orchard",
                              text="The orchard is west of town.")
        memory = VectorSemanticMemory(embeddings=provider, index=index,
                                       text_loader=FileTextLoader())
        result = memory.recall(SemanticMemoryRequest(account_name="acct", query="orchard",
                                                     namespaces=["facts"], score_threshold=0))
        assert result.documents[0].snippet == "The orchard is west of town."
        assert result.documents[0].path is None
