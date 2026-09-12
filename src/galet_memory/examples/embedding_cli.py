from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from typing import Sequence

from galet.embedding_router import EmbeddingRouter
from galet.mistral_embedding import MistralEmbeddingApi
from galet.openai_embedding import OpenAIEmbeddingApi
from galet.settings import Settings

from ..galet_adapter import GaletEmbeddingProvider
from ..ports import (
    DEFAULT_SQLITE_VEC_EXTENSION_PATH,
    SqliteVecEmbeddingIndex,
    StoredEmbedding,
)

DEFAULT_MODEL = "text-embedding-3-small"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add and query text embeddings in a Lucy-compatible DB."
    )
    parser.add_argument("--db", required=True, help="Path to embeddings-v2.sqlite")
    parser.add_argument(
        "--extension",
        default=DEFAULT_SQLITE_VEC_EXTENSION_PATH,
        help="Path to the sqlite-vec vec0 extension",
    )
    parser.add_argument("--account", default="demo")
    parser.add_argument("--namespace", default="demo")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--credential-path",
        help="Directory containing Galet credential files",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="Embed and store a text")
    add.add_argument("text")
    add.add_argument("--source-id")
    add.add_argument("--id", dest="record_id")
    add.add_argument("--source-type", default="sample")

    query = commands.add_parser("query", help="Run a similarity query")
    query.add_argument("text")
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--source-type")
    return parser


def _embedding_provider(credential_path: str | None) -> GaletEmbeddingProvider:
    settings = Settings(credential_path=credential_path)
    router = EmbeddingRouter(
        openai_api=OpenAIEmbeddingApi(settings=settings),
        mistral_api=MistralEmbeddingApi(settings=settings),
    )
    return GaletEmbeddingProvider(router)


def _provider_name(model: str) -> str:
    return "mistral" if model.startswith("mistral") else "openai"


def _add(args: argparse.Namespace, index: SqliteVecEmbeddingIndex) -> None:
    vector = _embedding_provider(args.credential_path).embed(
        [args.text], model=args.model
    )[0]
    source_id = args.source_id or hashlib.sha256(
        args.text.encode("utf-8")
    ).hexdigest()
    record_id = args.record_id or str(uuid.uuid4())
    index.upsert(
        StoredEmbedding(
            id=record_id,
            account_name=args.account,
            namespace=args.namespace,
            vector=vector,
            source_type=args.source_type,
            source_id=source_id,
            document_id=source_id,
            model=args.model,
            provider=_provider_name(args.model),
            metadata={"text": args.text},
        )
    )
    print(json.dumps({"id": record_id, "source_id": source_id}, indent=2))


def _query(args: argparse.Namespace, index: SqliteVecEmbeddingIndex) -> None:
    vector = _embedding_provider(args.credential_path).embed(
        [args.text], model=args.model
    )[0]
    filters = (
        {"source_type": args.source_type}
        if args.source_type
        else None
    )
    matches = index.query(
        account_name=args.account,
        namespaces=[args.namespace],
        vector=vector,
        limit=args.limit,
        filters=filters,
    )
    output = [
        {
            "score": match.score,
            "id": match.record.id,
            "source_id": match.record.source_id,
            "source_type": match.record.source_type,
            "text": match.record.metadata.get("text"),
            "metadata": dict(match.record.metadata),
        }
        for match in matches
    ]
    print(json.dumps(output, indent=2, ensure_ascii=False))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with SqliteVecEmbeddingIndex(
        args.db,
        sqlite_vec_extension_path=args.extension,
    ) as index:
        if args.command == "add":
            _add(args, index)
        else:
            _query(args, index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
