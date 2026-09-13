from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any, Sequence

from ..curation import CurationError, CurationService, DigestGenerationRequest
from ..episodic import (
    EpisodicEvent,
    EpisodicSessionQuery,
    SqliteEpisodicMemory,
)


class SuppliedDigestGenerator:
    """Digest generator used only to demonstrate logical archiving."""

    def __init__(self, digest: str) -> None:
        self.digest = digest

    def generate(self, request: DigestGenerationRequest) -> str:
        return self.digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and inspect episodic memory in a SQLite DB."
    )
    parser.add_argument("--db", required=True, help="Path to chat2.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create a session")
    create.add_argument("--account", required=True)
    create.add_argument("--agent", required=True)
    create.add_argument("--session-id")
    create.add_argument("--friendly-name")
    create.add_argument("--context-name")

    add = commands.add_parser("add", help="Append a text event")
    add.add_argument("session_id")
    add.add_argument("text")
    add.add_argument(
        "--role",
        choices=["user", "assistant", "tool", "system"],
        default="user",
    )
    add.add_argument("--actor", default="")
    add.add_argument("--kind", default="")

    show = commands.add_parser("show", help="Show one session")
    show.add_argument("session_id")
    show.add_argument(
        "--scope",
        choices=["active", "all", "archived"],
        default="active",
    )

    listing = commands.add_parser("list", help="List sessions")
    listing.add_argument("--account", required=True)
    listing.add_argument("--agent", default="")
    listing.add_argument("--limit", type=int, default=20)

    archive = commands.add_parser(
        "archive",
        help="Append supplied digest text as a logical archive boundary",
    )
    archive.add_argument("session_id")
    archive.add_argument("digest")
    archive.add_argument("--account", required=True)
    archive.add_argument("--idempotency-key")
    return parser


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _print(value: Any) -> None:
    print(
        json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
            default=_json_default,
        )
    )


def _create(args: argparse.Namespace, memory: SqliteEpisodicMemory) -> None:
    session = memory.create_session(
        account_name=args.account,
        agent_name=args.agent,
        session_id=args.session_id,
        friendly_name=args.friendly_name,
        context_name=args.context_name,
    )
    _print(asdict(session))


def _add(args: argparse.Namespace, memory: SqliteEpisodicMemory) -> None:
    event = memory.append_event(
        args.session_id,
        EpisodicEvent(
            role=args.role,
            actor=args.actor,
            kind=args.kind,
            content=args.text,
        ),
    )
    _print(asdict(event))


def _show(args: argparse.Namespace, memory: SqliteEpisodicMemory) -> None:
    session = memory.get_session(
        args.session_id,
        include_events=True,
        event_scope=args.scope,
    )
    if session is None:
        raise ValueError(f"Session not found: {args.session_id}")
    _print(asdict(session))


def _list(args: argparse.Namespace, memory: SqliteEpisodicMemory) -> None:
    sessions = memory.list_sessions(
        EpisodicSessionQuery(
            account_name=args.account,
            agent_name=args.agent,
            limit=args.limit,
        )
    )
    _print([asdict(session) for session in sessions])


def _archive(args: argparse.Namespace, memory: SqliteEpisodicMemory) -> None:
    service = CurationService(
        memory,
        SuppliedDigestGenerator(args.digest),
    )
    result = service.archive(
        account_name=args.account,
        session_id=args.session_id,
        idempotency_key=args.idempotency_key,
    )
    _print(asdict(result))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with SqliteEpisodicMemory(args.db) as memory:
            commands = {
                "create": _create,
                "add": _add,
                "show": _show,
                "list": _list,
                "archive": _archive,
            }
            commands[args.command](args, memory)
    except (CurationError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
