from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .embeddings import EmbeddingMatch, EmbeddingRecord, StoredEmbedding

DEFAULT_SQLITE_VEC_EXTENSION_PATH = "/usr/local/lib/sqlite-vec/vec0.so"
EMBEDDING_DIMENSIONS = 1536
SUPPORTED_VECTOR_TABLES = frozenset({"vec_embeddings", "vec_embeddings_v2"})

_VECTOR_V2_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings_v2 USING vec0(
    id TEXT PRIMARY KEY,
    embedding float[1536] distance_metric=cosine,
    account_name TEXT,
    namespace TEXT,
    source_type TEXT
)
"""

_METADATA_DDL = """
CREATE TABLE IF NOT EXISTS embedding_metadata (
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

_INDEX_DDLS = (
    "CREATE INDEX IF NOT EXISTS idx_emb_meta_account_ns "
    "ON embedding_metadata(account_name, namespace)",
    "CREATE INDEX IF NOT EXISTS idx_emb_meta_source "
    "ON embedding_metadata(source_type, source_id)",
    "CREATE INDEX IF NOT EXISTS idx_emb_meta_document_id "
    "ON embedding_metadata(document_id)",
)


class EmbeddingCompatibilityError(ValueError):
    pass


class SqliteVecEmbeddingIndex:
    """Read/query adapter for Lucy's existing sqlite-vec schema."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        sqlite_vec_extension_path: str = DEFAULT_SQLITE_VEC_EXTENSION_PATH,
        vector_table: str = "vec_embeddings_v2",
        initialize_schema: bool = True,
    ) -> None:
        if vector_table not in SUPPORTED_VECTOR_TABLES:
            raise ValueError(f"unsupported vector table: {vector_table!r}")
        self.vector_table = vector_table
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._lock = threading.RLock()
        try:
            self._conn.enable_load_extension(True)
            self._conn.load_extension(sqlite_vec_extension_path)
            if initialize_schema:
                self._initialize_schema()
            self._validate_schema()
        except BaseException:
            self._conn.close()
            raise

    def _initialize_schema(self) -> None:
        if self.vector_table == "vec_embeddings_v2":
            self._conn.execute(_VECTOR_V2_DDL)
        self._conn.execute(_METADATA_DDL)
        for ddl in _INDEX_DDLS:
            self._conn.execute(ddl)

    def _validate_schema(self) -> None:
        vector_table = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE name = ?",
            (self.vector_table,),
        ).fetchone()
        if vector_table is None:
            raise EmbeddingCompatibilityError(
                f"vector table not found: {self.vector_table}"
            )
        required = {
            "id", "account_name", "namespace", "source_type",
            "source_id", "source_metadata",
        }
        rows = self._conn.execute(
            "PRAGMA table_info(embedding_metadata)"
        ).fetchall()
        present = {str(row[1]) for row in rows}
        missing = required - present
        if missing:
            raise EmbeddingCompatibilityError(
                "embedding_metadata is missing columns: "
                + ", ".join(sorted(missing))
            )

    def list_namespaces(self, account_name: str) -> Sequence[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT namespace FROM embedding_metadata "
                "WHERE account_name = ? ORDER BY namespace",
                (account_name,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def upsert(self, embedding: StoredEmbedding) -> None:
        if len(embedding.vector) != EMBEDDING_DIMENSIONS:
            raise EmbeddingCompatibilityError(
                "stored embedding dimension must be "
                f"{EMBEDDING_DIMENSIONS}, got {len(embedding.vector)}"
            )
        created_at = embedding.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        dimensions = len(embedding.vector)
        vector_json = json.dumps(list(embedding.vector))
        metadata_json = json.dumps(dict(embedding.metadata))

        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    f"DELETE FROM {self.vector_table} WHERE id = ?",
                    (embedding.id,),
                )
                self._conn.execute(
                    f"INSERT INTO {self.vector_table}"
                    "(id, account_name, namespace, source_type, embedding) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        embedding.id,
                        embedding.account_name,
                        embedding.namespace,
                        embedding.source_type,
                        vector_json,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO embedding_metadata("
                    "id, account_name, namespace, source_type, source_id, "
                    "document_id, model, provider, dimensions, "
                    "source_metadata, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "account_name=excluded.account_name, "
                    "namespace=excluded.namespace, "
                    "source_type=excluded.source_type, "
                    "source_id=excluded.source_id, "
                    "document_id=excluded.document_id, "
                    "model=excluded.model, provider=excluded.provider, "
                    "dimensions=excluded.dimensions, "
                    "source_metadata=excluded.source_metadata, "
                    "created_at=excluded.created_at",
                    (
                        embedding.id,
                        embedding.account_name,
                        embedding.namespace,
                        embedding.source_type,
                        embedding.source_id,
                        embedding.document_id,
                        embedding.model,
                        embedding.provider,
                        dimensions,
                        metadata_json,
                        created_at.isoformat(),
                    ),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def query(
        self,
        *,
        account_name: str,
        namespaces: Sequence[str],
        vector: Sequence[float],
        limit: int,
        filters: Optional[Mapping[str, Any]] = None,
    ) -> Sequence[EmbeddingMatch]:
        if limit <= 0:
            return []
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise EmbeddingCompatibilityError(
                "query embedding dimension must be "
                f"{EMBEDDING_DIMENSIONS}, got {len(vector)}"
            )
        unknown_filters = set(filters or {}) - {"source_type"}
        if unknown_filters:
            raise ValueError(
                "unsupported embedding filters: "
                + ", ".join(sorted(unknown_filters))
            )

        encoded_vector = json.dumps(list(vector))
        matches: list[EmbeddingMatch] = []
        with self._lock:
            for namespace in namespaces:
                sql = (
                    f"SELECT id, distance FROM {self.vector_table} "
                    "WHERE embedding MATCH ? AND k = ? "
                    "AND account_name = ? AND namespace = ?"
                )
                params: list[Any] = [
                    encoded_vector, limit, account_name, namespace,
                ]
                if filters and "source_type" in filters:
                    sql += " AND source_type = ?"
                    params.append(filters["source_type"])
                rows = self._conn.execute(sql, params).fetchall()
                metadata = self._metadata_by_id(
                    [str(row[0]) for row in rows]
                )
                for record_id, distance in rows:
                    record = metadata.get(str(record_id))
                    if record is not None:
                        matches.append(
                            EmbeddingMatch(record, 1.0 - float(distance))
                        )

        matches.sort(key=lambda match: match.score, reverse=True)
        return matches[:limit]

    def _metadata_by_id(
        self, record_ids: Sequence[str]
    ) -> dict[str, EmbeddingRecord]:
        if not record_ids:
            return {}
        placeholders = ", ".join("?" for _ in record_ids)
        rows = self._conn.execute(
            "SELECT id, source_id, source_type, source_metadata "
            "FROM embedding_metadata WHERE id IN ("
            + placeholders
            + ")",
            list(record_ids),
        ).fetchall()
        return {
            str(row[0]): EmbeddingRecord(
                id=str(row[0]),
                source_id=str(row[1]),
                source_type=str(row[2] or ""),
                metadata=json.loads(row[3] or "{}"),
            )
            for row in rows
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteVecEmbeddingIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
