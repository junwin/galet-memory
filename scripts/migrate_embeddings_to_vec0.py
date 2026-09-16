#!/usr/bin/env python3
"""Lift Lucy generic-store embedding documents into legacy sqlite-vec tables.

This compatibility migration reads Lucy's SQLite ``kv`` table directly, so it
has no runtime dependency on the Lucy package. Source kv rows are left intact.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

from galet_memory.ports.sqlite_vec import EMBEDDING_DIMENSIONS, load_sqlite_vec

_VEC_TABLE_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0("
    " id TEXT, embedding float[1536] distance_metric=cosine,"
    " account_name TEXT, namespace TEXT, source_type TEXT)"
)
_METADATA_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS embedding_metadata ("
    " id TEXT PRIMARY KEY, account_name TEXT NOT NULL, namespace TEXT NOT NULL,"
    " source_type TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL DEFAULT '',"
    " source_metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)"
)


def _created_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc).isoformat()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def migrate_embeddings_to_vec0(
    db_path: str | Path,
    account: str,
    extension_path: str | None = None,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"database does not exist: {path}")

    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        kv_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'"
        ).fetchone()
        if kv_exists is None:
            raise ValueError("Lucy generic-store kv table not found")
        prefix = f"embeddings/{account}/%"
        rows = conn.execute(
            "SELECT key, value FROM kv WHERE key LIKE ? ORDER BY key", (prefix,)
        ).fetchall()

        records: list[dict[str, Any]] = []
        skipped: list[str] = []
        counts: dict[str, int] = {}
        for key, raw in rows:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                skipped.append(str(key))
                continue
            vector = data.get("vector")
            if not isinstance(vector, list):
                skipped.append(str(key))
                continue
            if len(vector) != EMBEDDING_DIMENSIONS:
                raise ValueError(
                    f"record {data.get('id')!r} has {len(vector)} dimensions; "
                    f"expected {EMBEDDING_DIMENSIONS}"
                )
            if not data.get("id") or not data.get("namespace") or not data.get("account_name"):
                skipped.append(str(key))
                continue
            records.append(data)
            ns = str(data["namespace"])
            counts[ns] = counts.get(ns, 0) + 1

        summary: Dict[str, Any] = {
            "account": account,
            "db_path": str(path),
            "total_keys": len(rows),
            "parsed_counts": counts,
            "skipped": skipped,
            "dry_run": dry_run,
            "vec_counts": None,
            "metadata_counts": None,
        }
        if dry_run:
            return summary

        load_sqlite_vec(conn, extension_path)
        conn.execute(_VEC_TABLE_DDL)
        conn.execute(_METADATA_TABLE_DDL)
        conn.execute("BEGIN")
        try:
            for record in records:
                record_id = str(record["id"])
                rowids = conn.execute(
                    "SELECT rowid FROM vec_embeddings WHERE id=?", (record_id,)
                ).fetchall()
                for (rowid,) in rowids:
                    conn.execute("DELETE FROM vec_embeddings WHERE rowid=?", (rowid,))
                conn.execute(
                    "INSERT INTO vec_embeddings(id, embedding, account_name, namespace, source_type) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        record_id,
                        json.dumps(record["vector"]),
                        record["account_name"],
                        record["namespace"],
                        record.get("source_type", ""),
                    ),
                )
                conn.execute(
                    "INSERT INTO embedding_metadata(id, account_name, namespace, source_type, "
                    "source_id, source_metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET account_name=excluded.account_name, "
                    "namespace=excluded.namespace, source_type=excluded.source_type, "
                    "source_id=excluded.source_id, source_metadata=excluded.source_metadata, "
                    "created_at=excluded.created_at",
                    (
                        record_id,
                        record["account_name"],
                        record["namespace"],
                        record.get("source_type", ""),
                        record.get("source_id", ""),
                        json.dumps(record.get("source_metadata") or {}),
                        _created_at(record.get("created_at")),
                    ),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        summary["vec_counts"] = dict(
            conn.execute(
                "SELECT namespace, COUNT(*) FROM vec_embeddings "
                "WHERE account_name=? GROUP BY namespace",
                (account,),
            ).fetchall()
        )
        summary["metadata_counts"] = dict(
            conn.execute(
                "SELECT namespace, COUNT(*) FROM embedding_metadata "
                "WHERE account_name=? GROUP BY namespace",
                (account,),
            ).fetchall()
        )
        return summary
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--account", default="junwin")
    parser.add_argument("--extension-path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = migrate_embeddings_to_vec0(
            args.db_path, args.account, args.extension_path, dry_run=args.dry_run
        )
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
