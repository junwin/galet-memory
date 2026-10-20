from __future__ import annotations

import pytest

from galet_memory import SemanticIngestionService
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

    def upsert(self, embedding):
        self.records[embedding.id] = embedding

    def get_record(self, *, account_name, namespace, record_id):
        e = self.records.get(record_id)
        if e is None or e.account_name != account_name or e.namespace != namespace:
            return None
        return EmbeddingRecord(id=e.id, source_id=e.source_id,
                                source_type=e.source_type, metadata=dict(e.metadata))

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
    assert sorted(r.source_id.rsplit("/", 1)[-1] for r in results) ==         ["a.md", "b.txt", "d.md"]
    assert all(r.status == "added" for r in results)


def test_ingest_folder_non_recursive_and_unchanged_rerun(tmp_path):
    service, provider, index = make_service()
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("apple trees", encoding="utf-8")
    (tmp_path / "sub" / "b.md").write_text("orchard", encoding="utf-8")
    results = service.ingest_folder(account_name="acct", namespace="docs",
                                    folder=tmp_path, recursive=False)
    assert [r.source_id.rsplit("/", 1)[-1] for r in results] == ["a.md"]
    rerun = service.ingest_folder(account_name="acct", namespace="docs",
                                  folder=tmp_path, recursive=True)
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
