from __future__ import annotations

import json
import sqlite3

import pytest

from galet_memory import (
    SemanticIngestionService,
    SemanticMemoryRequest,
    VectorSemanticMemory,
)
from galet_memory.ports import (
    EmbeddingMatch,
    EmbeddingRecord,
    FileTextLoader,
    SimpleKeywordExtractor,
)


class FakeIndex:
    """In-memory EmbeddingIndex; avoids the sqlite-vec dependency."""

    def __init__(self):
        self.records = {}

    def list_namespaces(self, account_name):
        return sorted({e.namespace for e in self.records.values()
                       if e.account_name == account_name})

    def query(self, *, account_name, namespaces, vector, limit, filters=None):
        matches = []
        for e in self.records.values():
            if e.account_name != account_name or e.namespace not in list(namespaces):
                continue
            score = sum(a * b for a, b in zip(e.vector, vector))
            matches.append(EmbeddingMatch(record=EmbeddingRecord(
                id=e.id, source_id=e.source_id, source_type=e.source_type,
                metadata=dict(e.metadata)), score=score))
        matches.sort(key=lambda m: -m.score)
        return matches[:limit]

    def query_by_keywords(self, *, account_name, namespaces, keywords, limit,
                          source_type=None):
        query_set = {str(k).lower() for k in keywords}
        if not query_set:
            return []
        matches = []
        for e in self.records.values():
            if e.account_name != account_name or e.namespace not in list(namespaces):
                continue
            if source_type and e.source_type != source_type:
                continue
            stored = {str(k).lower() for k in (e.metadata.get("keywords") or [])}
            overlap = query_set & stored
            if not overlap:
                continue
            matches.append(EmbeddingMatch(record=EmbeddingRecord(
                id=e.id, source_id=e.source_id, source_type=e.source_type,
                metadata=dict(e.metadata)),
                score=len(overlap) / len(query_set)))
        matches.sort(key=lambda m: -m.score)
        return matches[:limit]

    def upsert(self, embedding):
        self.records[embedding.id] = embedding

    def get_record(self, *, account_name, namespace, record_id):
        e = self.records.get(record_id)
        if e is None or e.account_name != account_name or e.namespace != namespace:
            return None
        return EmbeddingRecord(id=e.id, source_id=e.source_id,
                                source_type=e.source_type, metadata=dict(e.metadata))

    def list_records(self, *, account_name, namespace):
        return [EmbeddingRecord(id=e.id, source_id=e.source_id,
                                 source_type=e.source_type,
                                 metadata=dict(e.metadata))
                for e in self.records.values()
                if e.account_name == account_name and e.namespace == namespace]

    def store_keywords(self, *, account_name, namespace, record_id, keywords):
        e = self.records.get(record_id)
        if e is None or e.account_name != account_name or e.namespace != namespace:
            return False
        e.metadata["keywords"] = list(keywords)
        return True

    def delete_record(self, *, account_name, namespace, record_id):
        e = self.records.get(record_id)
        if e is None or e.account_name != account_name or e.namespace != namespace:
            return False
        del self.records[record_id]
        return True


class FakeEmbeddings:
    def __init__(self):
        self.calls = 0

    def embed(self, texts, *, model):
        self.calls += 1
        return [[0.1, 0.2] for _ in texts]


class FailingEmbeddings:
    def embed(self, texts, *, model):
        raise RuntimeError("embedding service unavailable")


def make_service(**kwargs):
    provider = FakeEmbeddings()
    index = FakeIndex()
    kwargs.setdefault("embeddings", provider)
    kwargs.setdefault("index", index)
    kwargs.setdefault("text_loader", FileTextLoader())
    return SemanticIngestionService(**kwargs), provider, index


def test_simple_extractor_ranks_by_frequency_and_drops_stopwords():
    extractor = SimpleKeywordExtractor()
    keywords = extractor.extract(
        "The orchard has apple trees. The apple trees are west of the orchard.")
    assert keywords[0] in ("apple", "orchard", "trees")
    assert "the" not in keywords and "are" not in keywords
    assert len(keywords) <= 10


def test_simple_extractor_honours_top_n_and_extra_stopwords():
    extractor = SimpleKeywordExtractor(stopwords=("apple",))
    keywords = extractor.extract("apple orchard orchard trees", top_n=2)
    assert "apple" not in keywords
    assert keywords[:2] == ["orchard", "trees"]


def test_keywords_derived_at_ingest_and_stored_in_metadata():
    service, provider, index = make_service(
        keyword_extractor=SimpleKeywordExtractor())
    result = service.ingest_text(
        account_name="acct", namespace="notes", source_id="orchard",
        text="The orchard has apple trees and pear trees.")
    assert result.status == "added"
    record = index.get_record(account_name="acct", namespace="notes",
                              record_id=result.record_id)
    assert "orchard" in record.metadata["keywords"]
    assert "trees" in record.metadata["keywords"]


def test_unchanged_text_stays_unchanged_with_keywords():
    service, provider, index = make_service(
        keyword_extractor=SimpleKeywordExtractor())
    text = "The orchard has apple trees."
    first = service.ingest_text(account_name="acct", namespace="notes",
                                source_id="note-1", text=text)
    second = service.ingest_text(account_name="acct", namespace="notes",
                                 source_id="note-1", text=text)
    assert (first.status, second.status) == ("added", "unchanged")
    assert provider.calls == 1


def test_without_extractor_no_keywords_key_is_written():
    service, provider, index = make_service()
    result = service.ingest_text(account_name="acct", namespace="notes",
                                 source_id="note-1", text="The orchard has apple trees.")
    record = index.get_record(account_name="acct", namespace="notes",
                              record_id=result.record_id)
    assert "keywords" not in record.metadata


def test_introducing_an_extractor_refreshes_an_old_record():
    text = "The orchard has apple trees."
    plain, provider, index = make_service()
    plain.ingest_text(account_name="acct", namespace="notes", source_id="n", text=text)
    with_extractor = SemanticIngestionService(
        embeddings=provider, index=index, text_loader=FileTextLoader(),
        keyword_extractor=SimpleKeywordExtractor())
    refreshed = with_extractor.ingest_text(
        account_name="acct", namespace="notes", source_id="n", text=text)
    assert refreshed.status == "updated"
    record = index.get_record(account_name="acct", namespace="notes",
                              record_id=refreshed.record_id)
    assert record.metadata["keywords"]


def test_ingest_folder_walks_suffixes_recursively(tmp_path):
    service, provider, index = make_service()
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("apple trees", encoding="utf-8")
    (tmp_path / "b.txt").write_text("pear trees", encoding="utf-8")
    (tmp_path / "c.json").write_text("{}", encoding="utf-8")
    (tmp_path / "sub" / "d.md").write_text("orchard notes", encoding="utf-8")
    results = service.ingest_folder(account_name="acct", namespace="docs",
                                    folder=tmp_path)
    names = sorted(r.source_id.rsplit("/", 1)[-1] for r in results)
    assert names == ["a.md", "b.txt", "d.md"]
    assert all(r.status == "added" for r in results)


def test_ingest_folder_non_recursive(tmp_path):
    service, provider, index = make_service()
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("apple trees", encoding="utf-8")
    (tmp_path / "sub" / "b.md").write_text("orchard", encoding="utf-8")
    results = service.ingest_folder(account_name="acct", namespace="docs",
                                    folder=tmp_path, recursive=False)
    assert [r.source_id.rsplit("/", 1)[-1] for r in results] == ["a.md"]


def test_ingest_folder_full_rerun_is_unchanged(tmp_path):
    service, provider, index = make_service()
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("apple trees", encoding="utf-8")
    (tmp_path / "sub" / "b.md").write_text("orchard", encoding="utf-8")
    first = service.ingest_folder(account_name="acct", namespace="docs",
                                  folder=tmp_path)
    assert {r.status for r in first} == {"added"}
    assert provider.calls == 2
    rerun = service.ingest_folder(account_name="acct", namespace="docs",
                                  folder=tmp_path)
    assert {r.status for r in rerun} == {"unchanged"}
    assert provider.calls == 2


def test_ingest_folder_skip_errors_continues_past_oversized(tmp_path):
    service, provider, index = make_service(max_chars=10)
    (tmp_path / "small.md").write_text("apple", encoding="utf-8")
    (tmp_path / "big.md").write_text("x" * 50, encoding="utf-8")
    with pytest.raises(ValueError):
        service.ingest_folder(account_name="acct", namespace="docs", folder=tmp_path)
    results = service.ingest_folder(account_name="acct", namespace="docs",
                                    folder=tmp_path, skip_errors=True)
    assert [r.status for r in results] == ["added"]


def test_ingest_folder_rejects_missing_directory(tmp_path):
    service, provider, index = make_service()
    with pytest.raises(ValueError, match="existing directory"):
        service.ingest_folder(account_name="acct", namespace="docs",
                             folder=tmp_path / "nope")


# --- keyword fallback recall ------------------------------------------------

def _ingest_orchard_note(index, provider):
    service = SemanticIngestionService(
        embeddings=provider, index=index, text_loader=FileTextLoader(),
        keyword_extractor=SimpleKeywordExtractor())
    service.ingest_text(account_name="acct", namespace="notes", source_id="note-1",
                        text="The orchard has apple trees and pear trees.")


def test_keyword_fallback_on_provider_failure(caplog):
    provider = FakeEmbeddings()
    index = FakeIndex()
    _ingest_orchard_note(index, provider)
    memory = VectorSemanticMemory(embeddings=FailingEmbeddings(), index=index,
                                  text_loader=FileTextLoader(),
                                  keyword_extractor=SimpleKeywordExtractor())
    result = memory.recall(SemanticMemoryRequest(
        account_name="acct", query="apple trees", namespaces=["notes"]))
    assert result.metadata["backend"] == "keyword"
    assert result.metadata["reason"] == "embedding_provider_unavailable"
    records = [record for record in caplog.records if 'semantic_embedding_failed' in record.message]
    assert len(records) == 1
    assert records[0].exc_info[0] is RuntimeError
    assert 'account=acct' in records[0].message
    assert 'apple trees' not in records[0].message

    assert result.metadata["query_keywords"]
    assert result.documents[0].source_id == "note-1"
    assert result.documents[0].snippet.startswith("The orchard")


def test_keyword_recall_when_embeddings_disabled():
    provider = FakeEmbeddings()
    index = FakeIndex()
    _ingest_orchard_note(index, provider)
    memory = VectorSemanticMemory(embeddings=provider, index=index,
                                  text_loader=FileTextLoader(),
                                  keyword_extractor=SimpleKeywordExtractor())
    result = memory.recall(SemanticMemoryRequest(
        account_name="acct", query="apple trees", namespaces=["notes"],
        use_embeddings=False))
    assert result.metadata["backend"] == "keyword"
    assert result.metadata["reason"] == "embedding_mode_disabled"
    assert result.documents[0].source_id == "note-1"


def test_embedding_mode_disabled_without_extractor_keeps_old_behavior():
    provider = FakeEmbeddings()
    index = FakeIndex()
    _ingest_orchard_note(index, provider)
    memory = VectorSemanticMemory(embeddings=provider, index=index,
                                  text_loader=FileTextLoader())
    result = memory.recall(SemanticMemoryRequest(
        account_name="acct", query="apple trees", namespaces=["notes"],
        use_embeddings=False))
    assert result.documents == []
    assert result.metadata == {"backend": "vector",
                               "reason": "embedding_mode_disabled"}


def test_provider_failure_reraises_without_fallback():
    provider = FakeEmbeddings()
    index = FakeIndex()
    _ingest_orchard_note(index, provider)
    memory = VectorSemanticMemory(embeddings=FailingEmbeddings(), index=index,
                                  text_loader=FileTextLoader())
    with pytest.raises(RuntimeError):
        memory.recall(SemanticMemoryRequest(
            account_name="acct", query="apple trees", namespaces=["notes"]))


def test_keyword_fallback_skips_records_without_keywords():
    provider = FakeEmbeddings()
    index = FakeIndex()
    plain = SemanticIngestionService(embeddings=provider, index=index,
                                     text_loader=FileTextLoader())
    plain.ingest_text(account_name="acct", namespace="notes", source_id="plain",
                      text="Completely unrelated content about tax returns.")
    memory = VectorSemanticMemory(embeddings=FailingEmbeddings(), index=index,
                                  text_loader=FileTextLoader(),
                                  keyword_extractor=SimpleKeywordExtractor())
    result = memory.recall(SemanticMemoryRequest(
        account_name="acct", query="tax returns", namespaces=["notes"]))
    assert result.documents == []
    assert result.metadata["backend"] == "keyword"


def test_store_keywords_enables_fallback_for_backfilled_records():
    provider = FakeEmbeddings()
    index = FakeIndex()
    plain = SemanticIngestionService(embeddings=provider, index=index,
                                     text_loader=FileTextLoader())
    plain.ingest_text(account_name="acct", namespace="notes", source_id="plain",
                      text="Completely unrelated content about tax returns.")
    records = index.list_records(account_name="acct", namespace="notes")
    assert index.store_keywords(account_name="acct", namespace="notes",
                                record_id=records[0].id,
                                keywords=["tax", "returns"]) is True
    assert index.store_keywords(account_name="acct", namespace="notes",
                                record_id="missing", keywords=[]) is False
    memory = VectorSemanticMemory(embeddings=FailingEmbeddings(), index=index,
                                  text_loader=FileTextLoader(),
                                  keyword_extractor=SimpleKeywordExtractor())
    result = memory.recall(SemanticMemoryRequest(
        account_name="acct", query="tax returns", namespaces=["notes"]))
    assert result.documents[0].source_id == "plain"


def test_sqlite_keywords_column_migration_and_query(tmp_path):
    sqlite_vec = pytest.importorskip("sqlite_vec")
    from galet_memory.ports import SqliteVecEmbeddingIndex

    db = tmp_path / "semantic.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE embedding_metadata ("
        "id TEXT PRIMARY KEY, account_name TEXT NOT NULL, "
        "namespace TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT '', "
        "source_id TEXT NOT NULL DEFAULT '', "
        "document_id TEXT NOT NULL DEFAULT '', "
        "model TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', "
        "dimensions INTEGER NOT NULL DEFAULT 1536, "
        "source_metadata TEXT NOT NULL DEFAULT '{}', "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO embedding_metadata "
        "(id, account_name, namespace, source_metadata, created_at) "
        "VALUES ('r1', 'acct', 'notes', ?, '2026-01-01T00:00:00+00:00')",
        (json.dumps({"text": "The orchard has apple trees.",
                     "keywords": ["orchard", "apple", "trees"]}),),
    )
    conn.execute(
        "INSERT INTO embedding_metadata "
        "(id, account_name, namespace, source_metadata, created_at) "
        "VALUES ('r2', 'acct', 'notes', ?, '2026-01-01T00:00:00+00:00')",
        (json.dumps({"text": "Unrelated content about taxes."}),),
    )
    conn.commit()
    conn.close()

    with SqliteVecEmbeddingIndex(db) as index:
        columns = {
            str(row[1])
            for row in index._conn.execute("PRAGMA table_info(embedding_metadata)")
        }
        assert "keywords" in columns

        # Legacy row: keywords live only in source_metadata; the query falls back.
        matches = index.query_by_keywords(
            account_name="acct", namespaces=["notes"],
            keywords=["apple", "orchard"], limit=5)
        assert [match.record.id for match in matches] == ["r1"]

        # Backfill r2, then it matches too.
        assert index.store_keywords(account_name="acct", namespace="notes",
                                    record_id="r2", keywords=["taxes"]) is True
        assert index.store_keywords(account_name="acct", namespace="notes",
                                    record_id="missing", keywords=[]) is False
        matches = index.query_by_keywords(
            account_name="acct", namespaces=["notes"],
            keywords=["taxes"], limit=5)
        assert [match.record.id for match in matches] == ["r2"]
