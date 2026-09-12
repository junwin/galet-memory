import sqlite3
import threading

import pytest

from galet_memory.ports.sqlite_vec import (
    EMBEDDING_DIMENSIONS,
    EmbeddingCompatibilityError,
    SqliteVecEmbeddingIndex,
)
from galet_memory.ports import StoredEmbedding


def _index_with_connection(connection):
    index = object.__new__(SqliteVecEmbeddingIndex)
    index.vector_table = "vec_embeddings_v2"
    index._conn = connection
    index._lock = threading.RLock()
    return index


def _metadata_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE embedding_metadata (
            id TEXT PRIMARY KEY,
            account_name TEXT NOT NULL,
            namespace TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            document_id TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            dimensions INTEGER NOT NULL DEFAULT 1536,
            source_metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    return connection


def test_list_namespaces_uses_metadata_table():
    connection = _metadata_connection()
    connection.executemany(
        "INSERT INTO embedding_metadata "
        "(id, account_name, namespace, created_at) VALUES (?, ?, ?, ?)",
        [
            ("1", "acct", "documents", "2026-09-12T00:00:00+00:00"),
            ("2", "acct", "digests", "2026-09-12T00:00:00+00:00"),
            ("3", "other", "private", "2026-09-12T00:00:00+00:00"),
        ],
    )
    index = _index_with_connection(connection)
    assert index.list_namespaces("acct") == ["digests", "documents"]
    index.close()


def test_metadata_rows_are_mapped_to_port_records():
    connection = _metadata_connection()
    connection.execute(
        "INSERT INTO embedding_metadata "
        "(id, account_name, namespace, source_type, source_id, "
        "source_metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "record-1", "acct", "documents", "note", "source-1",
            '{"path": "/notes/a.md"}', "2026-09-12T00:00:00+00:00",
        ),
    )
    index = _index_with_connection(connection)
    record = index._metadata_by_id(["record-1"])["record-1"]
    assert record.source_id == "source-1"
    assert record.metadata["path"] == "/notes/a.md"
    index.close()


def test_query_rejects_wrong_dimensions_before_sql():
    index = _index_with_connection(_metadata_connection())
    with pytest.raises(EmbeddingCompatibilityError, match="1536"):
        index.query(
            account_name="acct",
            namespaces=["documents"],
            vector=[0.1],
            limit=3,
        )
    index.close()


def test_query_rejects_unknown_filters():
    index = _index_with_connection(_metadata_connection())
    with pytest.raises(ValueError, match="unsupported embedding filters"):
        index.query(
            account_name="acct",
            namespaces=["documents"],
            vector=[0.0] * EMBEDDING_DIMENSIONS,
            limit=3,
            filters={"namespace": "other"},
        )
    index.close()


def test_only_known_vector_tables_are_accepted():
    with pytest.raises(ValueError, match="unsupported vector table"):
        SqliteVecEmbeddingIndex(
            ":memory:",
            vector_table="vec_embeddings_v2; DROP TABLE embedding_metadata",
        )


class RecordingConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), tuple(params)))
        return self


def test_upsert_writes_vector_and_metadata_in_one_transaction():
    connection = RecordingConnection()
    index = _index_with_connection(connection)
    index.upsert(
        StoredEmbedding(
            id="r1",
            account_name="acct",
            namespace="demo",
            vector=[0.0] * EMBEDDING_DIMENSIONS,
            source_type="sample",
            source_id="s1",
            model="text-embedding-3-small",
            provider="openai",
            metadata={"text": "hello"},
        )
    )
    statements = [call[0] for call in connection.calls]
    assert statements[0] == "BEGIN"
    assert statements[-1] == "COMMIT"
    assert any("INSERT INTO vec_embeddings_v2" in sql for sql in statements)
    assert any("INSERT INTO embedding_metadata" in sql for sql in statements)
