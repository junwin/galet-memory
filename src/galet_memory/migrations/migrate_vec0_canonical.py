from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import struct
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

from galet_memory.ports import StoredEmbedding
from galet_memory.ports.sqlite_vec import EMBEDDING_DIMENSIONS, SqliteVecEmbeddingIndex, load_sqlite_vec

_REQUIRED_LEGACY_METADATA_COLUMNS = {
    "id", "account_name", "namespace", "source_type", "source_id",
    "source_metadata", "created_at",
}

@dataclass(frozen=True)
class LegacyRecord:
    id: str
    account_name: str
    namespace: str
    vector: list[float]
    source_type: str
    source_id: str
    source_metadata: dict[str, Any]
    created_at: datetime

@dataclass(frozen=True)
class PreparedRecord:
    record: StoredEmbedding
    old_id: str
    id_replaced: bool
    document_id_verified: bool
    document_id_status: str
    provenance_known: bool

def _decode_vector(blob: bytes) -> list[float]:
    if len(blob) % 4:
        raise ValueError(f"invalid float32 vector blob length: {len(blob)}")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))

def _parse_created_at(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("embedding record has empty created_at")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _open_source_read_only(path: Path, extension_path: str | None) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    load_sqlite_vec(conn, extension_path)
    return conn

def _require_legacy_schema(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}
    missing = {"vec_embeddings", "embedding_metadata"} - tables
    if missing:
        raise ValueError("source is not a legacy Lucy vec0 store: " + ", ".join(sorted(missing)))
    present = {row[1] for row in conn.execute("PRAGMA table_info(embedding_metadata)").fetchall()}
    missing_columns = _REQUIRED_LEGACY_METADATA_COLUMNS - present
    if missing_columns:
        raise ValueError("embedding_metadata missing columns: " + ", ".join(sorted(missing_columns)))

def _read_legacy_records(conn: sqlite3.Connection) -> list[LegacyRecord]:
    _require_legacy_schema(conn)
    rows = conn.execute(
        "SELECT m.id, m.account_name, m.namespace, v.embedding, m.source_type, "
        "m.source_id, m.source_metadata, m.created_at "
        "FROM embedding_metadata m JOIN vec_embeddings v ON v.id=m.id "
        "ORDER BY m.account_name, m.namespace, m.id"
    ).fetchall()
    records: list[LegacyRecord] = []
    for row in rows:
        vector = _decode_vector(row[3])
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(f"record {row[0]!r} has {len(vector)} dimensions; expected {EMBEDDING_DIMENSIONS}")
        metadata = json.loads(row[6] or "{}")
        if not isinstance(metadata, dict):
            raise ValueError(f"record {row[0]!r} metadata must be an object")
        records.append(LegacyRecord(str(row[0]), str(row[1]), str(row[2]), vector, str(row[4] or ""), str(row[5] or ""), metadata, _parse_created_at(row[7])))
    return records

def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _resolve_document_id(metadata: dict[str, Any], trust_content_hash: bool) -> tuple[str, bool, str]:
    candidate = str(metadata.get("content_hash") or "")
    if len(candidate) != 64 or any(c not in "0123456789abcdefABCDEF" for c in candidate):
        return "", False, "missing_or_invalid_content_hash"
    candidate = candidate.lower()
    if trust_content_hash:
        return candidate, True, "trusted_content_hash"
    path_value = metadata.get("path")
    if not path_value:
        return "", False, "missing_path"
    source_path = Path(str(path_value)).expanduser()
    if not source_path.is_file():
        return "", False, "source_file_not_found"
    if _sha256_file(source_path) != candidate:
        return "", False, "hash_mismatch"
    return candidate, True, "verified"

def prepare_records(records: Iterable[LegacyRecord], *, assign_new_uuids: bool, trust_content_hash: bool, model: str, provider: str) -> list[PreparedRecord]:
    result: list[PreparedRecord] = []
    for legacy in records:
        id_replaced = not _is_uuid(legacy.id)
        new_id = str(uuid.uuid4()) if id_replaced and assign_new_uuids else legacy.id
        document_id, verified, status = _resolve_document_id(legacy.source_metadata, trust_content_hash)
        resolved_model = model.strip() or str(legacy.source_metadata.get("embedding_model") or legacy.source_metadata.get("model") or "")
        resolved_provider = provider.strip() or str(legacy.source_metadata.get("embedding_provider") or legacy.source_metadata.get("provider") or "")
        result.append(PreparedRecord(
            old_id=legacy.id,
            id_replaced=id_replaced,
            document_id_verified=verified,
            document_id_status=status,
            provenance_known=bool(resolved_model and resolved_provider),
            record=StoredEmbedding(
                id=new_id,
                account_name=legacy.account_name,
                namespace=legacy.namespace,
                vector=legacy.vector,
                source_type=legacy.source_type,
                source_id=legacy.source_id,
                document_id=document_id,
                model=resolved_model,
                provider=resolved_provider,
                metadata=legacy.source_metadata,
                created_at=legacy.created_at,
            ),
        ))
    return result

def _summary(prepared: Sequence[PreparedRecord]) -> Dict[str, Any]:
    namespaces = Counter((p.record.account_name, p.record.namespace) for p in prepared)
    return {
        "records": len(prepared),
        "valid_uuid_ids": sum(not p.id_replaced for p in prepared),
        "ids_requiring_assignment": sum(p.id_replaced for p in prepared),
        "verified_document_ids": sum(p.document_id_verified for p in prepared),
        "requires_source_rescan": sum(not p.document_id_verified for p in prepared),
        "known_provenance": sum(p.provenance_known for p in prepared),
        "unknown_provenance": sum(not p.provenance_known for p in prepared),
        "namespaces": {f"{a}/{n}": c for (a, n), c in sorted(namespaces.items())},
    }

def migrate_vec0_canonical(source: Path, destination: Path, extension_path: str | None = None, *, dry_run: bool = False, assign_new_uuids: bool = False, trust_content_hash: bool = False, model: str = "", provider: str = "") -> Dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if destination.exists():
        raise FileExistsError(f"destination already exists; refusing to overwrite: {destination}")
    conn = _open_source_read_only(source, extension_path)
    try:
        records = _read_legacy_records(conn)
    finally:
        conn.close()
    prepared = prepare_records(records, assign_new_uuids=assign_new_uuids, trust_content_hash=trust_content_hash, model=model, provider=provider)
    summary = _summary(prepared)
    summary.update({"source": str(source), "destination": str(destination), "dry_run": dry_run})
    if dry_run:
        return summary
    if any(p.id_replaced for p in prepared) and not assign_new_uuids:
        raise ValueError("legacy IDs require --assign-new-uuids before migration")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with SqliteVecEmbeddingIndex(destination, sqlite_vec_extension_path=extension_path) as index:
            for item in prepared:
                index.upsert(item.record)
        verify = sqlite3.connect(str(destination))
        try:
            count = verify.execute("SELECT COUNT(*) FROM embedding_metadata").fetchone()[0]
        finally:
            verify.close()
        errors: list[str] = []
        if count != len(prepared):
            errors.append(f"metadata count mismatch: expected {len(prepared)}, got {count}")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    summary["validated"] = not errors
    summary["validation_errors"] = errors
    return summary

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--extension-path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--assign-new-uuids", action="store_true")
    parser.add_argument("--trust-content-hash", action="store_true")
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    args = parser.parse_args(argv)
    try:
        result = migrate_vec0_canonical(Path(args.source), Path(args.destination), args.extension_path, dry_run=args.dry_run, assign_new_uuids=args.assign_new_uuids, trust_content_hash=args.trust_content_hash, model=args.model, provider=args.provider)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(result)
    return 0
