"""Exercise semantic ingestion and recall against disposable sqlite-vec storage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence

from ..ports import FileTextLoader, SqliteVecEmbeddingIndex
from ..semantic import SemanticIngestionService, SemanticMemoryRequest, VectorSemanticMemory


class FixtureEmbeddings:
    def embed(self, texts, *, model):
        vectors = []
        for text in texts:
            vector = [0.0] * 1536
            vector[0 if "orchard" in text.lower() else 1] = 1.0
            vectors.append(vector)
        return vectors


def run(root: Path, *, live: bool, model: str, credential_path: str | None) -> dict:
    if live:
        from galet.openai_embedding import OpenAIEmbeddingApi
        from galet.settings import Settings
        from ..galet_adapter import GaletEmbeddingProvider

        embeddings = GaletEmbeddingProvider(OpenAIEmbeddingApi(
            settings=Settings(credential_path=credential_path)))
    else:
        embeddings = FixtureEmbeddings()
    root.mkdir(parents=True, exist_ok=True)
    note = root / "garden.md"
    note.write_text("The orchard has apple trees.", encoding="utf-8")
    loader = FileTextLoader()
    with SqliteVecEmbeddingIndex(root / "embeddings.sqlite") as index:
        ingestion = SemanticIngestionService(embeddings=embeddings, index=index,
                                             text_loader=loader, model=model)
        memory = VectorSemanticMemory(embeddings=embeddings, index=index,
                                       text_loader=loader)
        request = SemanticMemoryRequest(account_name="demo", query="orchard",
                                        namespaces=["notes"], embedding_model=model,
                                        score_threshold=-1)
        added = ingestion.ingest_file(account_name="demo", namespace="notes", path=note,
                                      source_id="garden-note")
        unchanged = ingestion.ingest_file(account_name="demo", namespace="notes", path=note,
                                          source_id="garden-note")
        first = memory.recall(request).documents
        assert len(first) == 1 and "apple trees" in first[0].snippet
        note.write_text("The orchard has pear trees.", encoding="utf-8")
        updated = ingestion.ingest_file(account_name="demo", namespace="notes", path=note,
                                        source_id="garden-note")
        second = memory.recall(request).documents
        assert len(second) == 1 and "pear trees" in second[0].snippet
        assert memory.recall(SemanticMemoryRequest(
            account_name="other", query="orchard", namespaces=["notes"],
            embedding_model=model)).documents == []
        removed = ingestion.delete(account_name="demo", namespace="notes", source_id="garden-note")
        assert removed and memory.recall(request).documents == []
        assert (added.status, unchanged.status, updated.status) == ("added", "unchanged", "updated")
        return {"mode": "live" if live else "fixture", "statuses": [added.status,
                unchanged.status, updated.status, "deleted"], "first_snippet": first[0].snippet,
                "updated_snippet": second[0].snippet, "checks": "passed"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Road test semantic ingestion and recall in a temporary database.")
    parser.add_argument("--live", action="store_true", help="Use Galet's OpenAI embedding API (may incur cost)")
    parser.add_argument("--model", default="text-embedding-3-small")
    parser.add_argument("--credential-path")
    args = parser.parse_args(argv)
    try:
        with TemporaryDirectory(prefix="galet-semantic-") as directory:
            result = run(Path(directory), live=args.live, model=args.model,
                         credential_path=args.credential_path)
        print(json.dumps(result, indent=2))
    except (RuntimeError, ValueError, OSError, AssertionError) as exc:
        print(f"semantic road test failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
