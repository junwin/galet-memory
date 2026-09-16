from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

from scripts.migrate_embeddings_to_vec0 import migrate_embeddings_to_vec0
from galet_memory.ports.sqlite_vec import load_sqlite_vec

_DIM = 1536


def _make_lucy_kv_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        record = {
            "id": "note-a",
            "namespace": "documents",
            "account_name": "junwin",
            "vector": [1.0] + [0.0] * (_DIM - 1),
            "source_type": "document",
            "source_id": "notes/a.md",
            "source_metadata": {"path": "/notes/a.md"},
            "created_at": "2026-09-01T12:00:00+00:00",
        }
        conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (?, ?, ?)",
            (
                "embeddings/junwin/documents/note-a.json",
                json.dumps(record),
                "2026-09-01T12:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_dry_run_reads_lucy_kv_without_creating_vec_tables(tmp_path: Path):
    db = tmp_path / "legacy.sqlite"
    _make_lucy_kv_db(db)
    summary = migrate_embeddings_to_vec0(db, "junwin", dry_run=True)
    assert summary["total_keys"] == 1
    assert summary["parsed_counts"] == {"documents": 1}

    conn = sqlite3.connect(str(db))
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='vec_embeddings'"
        ).fetchone()
    finally:
        conn.close()
    assert exists is None


def test_migration_lifts_kv_record_and_leaves_source_row(tmp_path: Path):
    db = tmp_path / "legacy.sqlite"
    _make_lucy_kv_db(db)
    summary = migrate_embeddings_to_vec0(db, "junwin")
    assert summary["vec_counts"] == {"documents": 1}
    assert summary["metadata_counts"] == {"documents": 1}

    conn = sqlite3.connect(str(db))
    try:
        load_sqlite_vec(conn)
        kv_count = conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
        row = conn.execute(
            "SELECT id, account_name, namespace, source_type FROM vec_embeddings"
        ).fetchone()
    finally:
        conn.close()
    assert kv_count == 1
    assert row == ("note-a", "junwin", "documents", "document")
