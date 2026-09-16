from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

from galet_memory.ports.sqlite_vec import load_sqlite_vec

SOURCE_TABLE = "vec_embeddings"
DESTINATION_TABLE = "vec_embeddings_v2"
_FLOAT_DIMENSION_RE = re.compile(r"float\s*\[\s*(\d+)\s*\]", re.IGNORECASE)
_DISTANCE_METRIC_RE = re.compile(r"distance_metric\s*=\s*([A-Za-z_]+)", re.IGNORECASE)
_PARTITION_KEY_RE = re.compile(r"\s+partition\s+key", re.IGNORECASE)
_VIRTUAL_TABLE_PREFIX_RE = re.compile(
    r"^(CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(\"?[A-Za-z_][A-Za-z0-9_]*\"?)",
    re.IGNORECASE,
)

def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'

def _connect(db_path: Path, extension_path: str | None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    load_sqlite_vec(conn, extension_path)
    return conn

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

def _table_ddl(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if row is None or not row[0]:
        raise ValueError(f"table not found in database: {table}")
    return str(row[0])

def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_quoted(table)})").fetchall()
    if not rows:
        raise ValueError(f"table declares no columns: {table}")
    return [str(row[1]) for row in rows]

def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    if row is None:
        raise ValueError(f"validation query returned no result: {sql}")
    return int(row[0])

def _vector_signature(ddl: str) -> Tuple[int, str]:
    dimension = _FLOAT_DIMENSION_RE.search(ddl)
    metric = _DISTANCE_METRIC_RE.search(ddl)
    if dimension is None or metric is None:
        raise ValueError(f"cannot determine vec0 signature from: {ddl!r}")
    return int(dimension.group(1)), metric.group(1).lower()

def destination_ddl(source_ddl: str) -> str:
    if "using vec0" not in source_ddl.lower():
        raise ValueError(f"{SOURCE_TABLE} is not a vec0 virtual table")
    match = _VIRTUAL_TABLE_PREFIX_RE.match(source_ddl)
    if match is None:
        raise ValueError(f"cannot parse virtual table declaration: {source_ddl!r}")
    if match.group(2).strip('"').lower() != SOURCE_TABLE.lower():
        raise ValueError("unexpected source virtual table name")
    without_partition_keys = _PARTITION_KEY_RE.sub("", source_ddl)
    return match.group(1) + DESTINATION_TABLE + without_partition_keys[match.end():]

def migrate_vec_embeddings_v2(db_path: Path, extension_path: str | None = None) -> Dict[str, Any]:
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"embeddings database does not exist: {resolved}")
    conn = _connect(resolved, extension_path)
    try:
        if _table_exists(conn, DESTINATION_TABLE):
            raise FileExistsError(f"{DESTINATION_TABLE} already exists")
        source_ddl = _table_ddl(conn, SOURCE_TABLE)
        destination_schema = destination_ddl(source_ddl)
        columns = _column_names(conn, SOURCE_TABLE)
        column_list = ", ".join(_quoted(c) for c in columns)
        dimension, distance_metric = _vector_signature(source_ddl)
        conn.execute("BEGIN")
        try:
            conn.execute(destination_schema)
            conn.execute(f"INSERT INTO {DESTINATION_TABLE} ({column_list}) SELECT {column_list} FROM {SOURCE_TABLE}")
            if _vector_signature(_table_ddl(conn, DESTINATION_TABLE)) != (dimension, distance_metric):
                raise ValueError("destination vector signature mismatch")
            source_count = _scalar(conn, f"SELECT COUNT(*) FROM {SOURCE_TABLE}")
            destination_count = _scalar(conn, f"SELECT COUNT(*) FROM {DESTINATION_TABLE}")
            if source_count != destination_count:
                raise ValueError("destination row count mismatch")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {
        "db_path": str(resolved),
        "source_table": SOURCE_TABLE,
        "destination_table": DESTINATION_TABLE,
        "destination_ddl": destination_schema,
        "dimension": dimension,
        "distance_metric": distance_metric,
        "row_count": source_count,
    }

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--extension-path", default=None)
    args = parser.parse_args(argv)
    try:
        result = migrate_vec_embeddings_v2(Path(args.db_path), args.extension_path)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(f"[OK] copied {result['row_count']} row(s) to {DESTINATION_TABLE}")
    return 0
