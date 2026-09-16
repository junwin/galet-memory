from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

from scripts.migrate_vec0_canonical import migrate_vec0_canonical
from galet_memory.ports.sqlite_vec import load_sqlite_vec

_DIM = 1536
_LEGACY_VEC_DDL = (
    "CREATE VIRTUAL TABLE vec_embeddings USING vec0("
    " id TEXT, embedding float[1536] distance_metric=cosine,"
    " account_name TEXT, namespace TEXT, source_type TEXT)"
)
_LEGACY_META_DDL = (
    "CREATE TABLE embedding_metadata ("
    " id TEXT PRIMARY KEY, account_name TEXT NOT NULL, namespace TEXT NOT NULL,"
    " source_type TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL DEFAULT '',"
    " source_metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)"
)


def _make_legacy_db(path: Path, source_file: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        load_sqlite_vec(conn)
        conn.execute(_LEGACY_VEC_DDL)
        conn.execute(_LEGACY_META_DDL)
        vector = [1.0] + [0.0] * (_DIM - 1)
        metadata = {
            "path": str(source_file),
            "content_hash": hashlib.sha256(source_file.read_bytes()).hexdigest(),
        }
        conn.execute(
            "INSERT INTO vec_embeddings(id, embedding, account_name, namespace, source_type) "
            "VALUES (?, ?, ?, ?, ?)",
            ("legacy-note-a", json.dumps(vector), "junwin", "documents", "document"),
        )
        conn.execute(
            "INSERT INTO embedding_metadata(id, account_name, namespace, source_type, "
            "source_id, source_metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-note-a", "junwin", "documents", "document", "notes/a.md",
                json.dumps(metadata), "2026-09-01T12:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def legacy_db(tmp_path: Path) -> tuple[Path, Path]:
    source_file = tmp_path / "a.md"
    source_file.write_bytes(b"canonical hash source\n")
    db_path = tmp_path / "legacy.sqlite"
    _make_legacy_db(db_path, source_file)
    return db_path, source_file


def test_dry_run_reports_without_writing(legacy_db, tmp_path):
    source, _ = legacy_db
    destination = tmp_path / "canonical.sqlite"
    before = source.read_bytes()
    summary = migrate_vec0_canonical(source, destination, dry_run=True)
    assert summary["records"] == 1
    assert summary["ids_requiring_assignment"] == 1
    assert summary["verified_document_ids"] == 1
    assert not destination.exists()
    assert source.read_bytes() == before


def test_actual_migration_requires_explicit_uuid_policy(legacy_db, tmp_path):
    source, _ = legacy_db
    destination = tmp_path / "canonical.sqlite"
    with pytest.raises(ValueError, match="assign-new-uuids"):
        migrate_vec0_canonical(source, destination)
    assert not destination.exists()


def test_migration_creates_validated_canonical_copy(legacy_db, tmp_path):
    source, _ = legacy_db
    destination = tmp_path / "canonical.sqlite"
    before = source.read_bytes()
    summary = migrate_vec0_canonical(
        source,
        destination,
        assign_new_uuids=True,
        model="text-embedding-3-small",
        provider="openai",
    )
    assert summary["validated"] is True
    assert summary["validation_errors"] == []
    assert summary["known_provenance"] == 1
    assert destination.exists()
    assert source.read_bytes() == before

    conn = sqlite3.connect(str(destination))
    try:
        row = conn.execute(
            "SELECT id, document_id, model, provider, dimensions, source_id, created_at "
            "FROM embedding_metadata"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] != "legacy-note-a"
    assert len(row[0]) == 36
    assert len(row[1]) == 64
    assert row[2:5] == ("text-embedding-3-small", "openai", _DIM)
    assert row[5] == "notes/a.md"


def test_destination_must_be_new(legacy_db, tmp_path):
    source, _ = legacy_db
    destination = tmp_path / "canonical.sqlite"
    destination.write_text("do not replace", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        migrate_vec0_canonical(source, destination, dry_run=True)
    assert destination.read_text(encoding="utf-8") == "do not replace"
