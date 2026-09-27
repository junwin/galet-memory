"""Run the digest, archive, and reset lifecycle outside Lucy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence

from ..curation import CurationService
from ..digest_generation import GaletDigestGenerator, GaletDigestPolicy
from ..episodic import EpisodicEvent, SqliteEpisodicMemory


class FixtureModel:
    """Exercise the real digest generator and storage without a paid API call."""

    def create_response(self, *, model, input, temperature):
        return type("Response", (), {"output_text": "Fixture digest of supplied interval"})()


def run(db: Path, *, account: str, model: str, credential_path: str | None,
        live: bool, max_chars: int) -> dict:
    if db.exists():
        raise ValueError(f"database already exists: {db}; choose a new path")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    db.parent.mkdir(parents=True, exist_ok=True)
    if live:
        from galet.openai_responses import OpenAIResponsesApi
        from galet.settings import Settings

        llm = OpenAIResponsesApi(settings=Settings(credential_path=credential_path))
    else:
        llm = FixtureModel()

    with SqliteEpisodicMemory(db) as memory:
        memory.create_session(account_name=account, agent_name="road-test",
                              session_id="digest-road-test", friendly_name="Digest road test")
        session_id = "digest-road-test"
        service = CurationService(memory, GaletDigestGenerator(llm, GaletDigestPolicy(model)))

        first = memory.append_event(session_id, EpisodicEvent("user", "The project will use SQLite."))
        second = memory.append_event(session_id, EpisodicEvent("assistant", "Agreed; test migration on a copy."))
        preview = service.produce_digest(account_name=account, session_id=session_id, max_chars=max_chars)
        assert preview.source_event_ids == (first.event_id, second.event_id)
        assert len(memory.get_session(session_id, event_scope="all").events) == 2
        archive_one = service.archive(account_name=account, session_id=session_id,
                                      max_chars=max_chars, idempotency_key="archive-one")
        assert archive_one.boundary_event.metadata["source_event_count"] == 2
        assert [e.event_id for e in memory.get_session(session_id, event_scope="archived").events] == [first.event_id, second.event_id]

        third = memory.append_event(session_id, EpisodicEvent("user", "The migration check passed."))
        second_preview = service.produce_digest(account_name=account, session_id=session_id,
                                                max_chars=max_chars)
        assert second_preview.source_event_ids == (third.event_id,)
        archive_two = service.archive(account_name=account, session_id=session_id,
                                      max_chars=max_chars, idempotency_key="archive-two")
        assert archive_two.boundary_event.metadata["source_event_count"] == 1

        cumulative = service.produce_cumulative_digest(
            account_name=account, session_id=session_id, max_chars=max_chars)
        assert cumulative.source_event_ids == (
            archive_one.boundary_event.event_id, archive_two.boundary_event.event_id)

        reset = service.reset_context(account_name=account, session_id=session_id,
                                      idempotency_key="reset-one")
        assert memory.get_session(session_id, event_scope="active").events == []
        fourth = memory.append_event(session_id, EpisodicEvent("user", "A fresh topic begins."))
        after_reset = service.produce_digest(account_name=account, session_id=session_id,
                                             max_chars=max_chars)
        assert after_reset.source_event_ids == (fourth.event_id,)
        all_events = memory.get_session(session_id, event_scope="all").events
        assert len(all_events) == 7  # four messages, two digests, one reset
        return {
            "database": str(db), "mode": "live" if live else "fixture",
            "session_id": session_id, "preview_digest": preview.digest,
            "first_archive_digest": archive_one.digest,
            "second_archive_digest": archive_two.digest,
            "cumulative_digest": cumulative.digest,
            "reset_boundary_id": reset.boundary_event.event_id,
            "after_reset_digest": after_reset.digest,
            "active_events": len(memory.get_session(session_id, event_scope="active").events),
            "all_events": len(all_events),
            "checks": "passed",
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exercise galet-memory digest and reset behavior in a fresh SQLite session.")
    parser.add_argument("--db", type=Path, help="New SQLite path; default uses a temporary database")
    parser.add_argument("--account", default="demo")
    parser.add_argument("--live", action="store_true", help="Call the real OpenAI model through Galet (may incur cost)")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--credential-path", help="Directory containing Galet credential files")
    parser.add_argument("--max-chars", type=int, default=32000)
    args = parser.parse_args(argv)
    try:
        if args.db:
            report = run(args.db, account=args.account, model=args.model,
                         credential_path=args.credential_path, live=args.live, max_chars=args.max_chars)
        else:
            with TemporaryDirectory(prefix="galet-digest-") as temporary:
                report = run(Path(temporary) / "chat.sqlite", account=args.account,
                             model=args.model, credential_path=args.credential_path,
                             live=args.live, max_chars=args.max_chars)
                report["database"] = "temporary (removed after run)"
        print(json.dumps(report, indent=2))
    except (ValueError, OSError, AssertionError, RuntimeError) as exc:
        print(f"digest road test failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
