from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

from galet_memory.migrations.migrate_vec_embeddings_v2 import migrate_vec_embeddings_v2
from galet_memory.ports.sqlite_vec import load_sqlite_vec

_DIM = 1536


def _make_partitioned_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        load_sqlite_vec(conn)
        conn.execute(
            "CREATE VIRTUAL TABLE vec_embeddings USING vec0("
            "id TEXT PRIMARY KEY, embedding float[1536] distance_metric=cosine,"
            "account_name TEXT partition key, namespace TEXT partition key,"
            "source_type TEXT)"
        )
        vector = json.dumps([1.0] + [0.0] * (_DIM - 1))
        conn.execute(
            "INSERT INTO vec_embeddings(id, embedding, account_name, namespace, source_type) "
            "VALUES (?, ?, ?, ?, ?)",
            ("r1", vector, "junwin", "documents", "document"),
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_creates_partition_free_copy(tmp_path: Path):
    db = tmp_path / "embeddings.sqlite"
    _make_partitioned_db(db)
    result = migrate_vec_embeddings_v2(db)
    assert result["row_count"] == 1
    assert result["dimension"] == _DIM
    assert result["distance_metric"] == "cosine"
    assert "partition key" not in result["destination_ddl"].lower()

    conn = sqlite3.connect(str(db))
    try:
        load_sqlite_vec(conn)
        row = conn.execute(
            "SELECT id, account_name, namespace, source_type FROM vec_embeddings_v2"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("r1", "junwin", "documents", "document")


def test_migration_refuses_existing_destination(tmp_path: Path):
    db = tmp_path / "embeddings.sqlite"
    _make_partitioned_db(db)
    migrate_vec_embeddings_v2(db)
    with pytest.raises(FileExistsError, match="already exists"):
        migrate_vec_embeddings_v2(db)
