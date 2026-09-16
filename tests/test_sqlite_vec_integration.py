from __future__ import annotations

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")

from galet_memory.ports import StoredEmbedding
from galet_memory.ports.sqlite_vec import (
    EMBEDDING_DIMENSIONS,
    SqliteVecEmbeddingIndex,
)


def _vector(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


def test_sqlite_vec_index_round_trip_with_packaged_extension(tmp_path):
    db_path = tmp_path / "embeddings.sqlite"

    with SqliteVecEmbeddingIndex(db_path) as index:
        index.upsert(
            StoredEmbedding(
                id="alpha",
                account_name="acct",
                namespace="documents",
                vector=_vector(0),
                source_type="note",
                source_id="a",
                model="test-embedding",
                provider="test",
                metadata={"path": "a.md"},
            )
        )
        index.upsert(
            StoredEmbedding(
                id="beta",
                account_name="acct",
                namespace="documents",
                vector=_vector(1),
                source_type="note",
                source_id="b",
                model="test-embedding",
                provider="test",
                metadata={"path": "b.md"},
            )
        )

        assert index.list_namespaces("acct") == ["documents"]

        matches = index.query(
            account_name="acct",
            namespaces=["documents"],
            vector=_vector(0),
            limit=2,
        )

    assert [match.record.id for match in matches] == ["alpha", "beta"]
    assert matches[0].record.source_id == "a"
    assert matches[0].record.metadata["path"] == "a.md"
    assert matches[0].score > matches[1].score
