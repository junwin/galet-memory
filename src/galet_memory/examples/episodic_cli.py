"""Exercise the explicit session, event and exchange APIs without Lucy."""

from __future__ import annotations
import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Sequence

from ..curation import CurationError, CurationService
from ..episodic import NewEvent, SessionChanges, SqliteEpisodicMemory


class SuppliedDigestGenerator:
    def __init__(self, digest):
        self.digest = digest

    def generate(self, request):
        return self.digest


def _parser():
    parser = argparse.ArgumentParser(
        description="Inspect account-owned sessions and event records."
    )
    parser.add_argument(
        "--db", required=True, help="New-interface SQLite database (use a fresh file)"
    )
    parser.add_argument(
        "--account", required=True, help="Owning account for every operation"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create", help="Create a session; no agent selection required"
    )
    create.add_argument("--session-id")
    create.add_argument("--friendly-name")
    create.add_argument("--context-name")
    listing = commands.add_parser("list", help="List session metadata")
    listing.add_argument("--count", type=int, default=20)
    listing.add_argument("--cursor")
    for name in ("show", "clear", "delete"):
        cmd = commands.add_parser(name)
        cmd.add_argument("session_id")
    update = commands.add_parser("update")
    update.add_argument("session_id")
    update.add_argument("--friendly-name")
    update.add_argument("--context-name")
    append = commands.add_parser(
        "append", help="Append a message with atomic correlation associations"
    )
    append.add_argument("session_id")
    append.add_argument("text")
    append.add_argument(
        "--role", choices=["user", "assistant", "tool", "system"], default="user"
    )
    append.add_argument(
        "--actor", required=True, help="Producer identity, e.g. junwin or peace"
    )
    append.add_argument("--kind", default="")
    append.add_argument("--correlation", action="append", default=[])
    for name in ("recent", "period"):
        cmd = commands.add_parser(
            name, help="Read event payloads; filter before counting"
        )
        cmd.add_argument("session_id")
        cmd.add_argument("--count", type=int, default=10)
        cmd.add_argument("--kind", action="append", default=None)
        cmd.add_argument("--actor", action="append", default=None)
        if name == "recent":
            cmd.add_argument("--before-event-id")
        else:
            cmd.add_argument(
                "--start", required=True, help="Inclusive ISO timestamp with timezone"
            )
            cmd.add_argument(
                "--end", required=True, help="Exclusive ISO timestamp with timezone"
            )
            cmd.add_argument("--cursor")
    event = commands.add_parser("event")
    event.add_argument("session_id")
    event.add_argument("event_id")
    for name in ("exchange", "invalidate"):
        cmd = commands.add_parser(name)
        cmd.add_argument("session_id")
        cmd.add_argument("correlation_id")
    archive = commands.add_parser(
        "archive", help="Demonstrate curation with caller-supplied digest text"
    )
    archive.add_argument("session_id")
    archive.add_argument("digest")
    archive.add_argument("--idempotency-key")
    return parser


def _run(args, memory):
    owned = {"account_name": args.account}
    if args.command == "create":
        return memory.create_session(
            **owned,
            session_id=args.session_id,
            friendly_name=args.friendly_name,
            context_name=args.context_name,
        )
    if args.command == "list":
        return memory.list_sessions(**owned, count=args.count, cursor=args.cursor)
    owned["session_id"] = args.session_id
    if args.command == "show":
        return memory.get_session(**owned)
    if args.command == "clear":
        return memory.clear_session_events(**owned)
    if args.command == "delete":
        return {"deleted": memory.delete_session(**owned)}
    if args.command == "update":
        from ..episodic import UNSET

        return memory.update_session(
            **owned,
            changes=SessionChanges(
                friendly_name=(
                    args.friendly_name if args.friendly_name is not None else UNSET
                ),
                context_name=(
                    args.context_name if args.context_name is not None else UNSET
                ),
            ),
        )
    if args.command == "append":
        return memory.append_event(
            **owned,
            event=NewEvent(
                args.role,
                args.text,
                args.actor,
                args.kind,
                correlation_ids=tuple(args.correlation),
            ),
        )
    if args.command == "recent":
        return memory.get_recent_events(
            **owned,
            count=args.count,
            event_kinds=args.kind,
            actors=args.actor,
            before_event_id=args.before_event_id,
        )
    if args.command == "period":
        return memory.get_events_by_period(
            **owned,
            count=args.count,
            event_kinds=args.kind,
            actors=args.actor,
            start=datetime.fromisoformat(args.start.replace("Z", "+00:00")),
            end=datetime.fromisoformat(args.end.replace("Z", "+00:00")),
            cursor=args.cursor,
        )
    if args.command == "event":
        return memory.get_event(**owned, event_id=args.event_id)
    if args.command == "exchange":
        return memory.get_exchange(**owned, correlation_id=args.correlation_id)
    if args.command == "invalidate":
        return memory.invalidate_exchange(**owned, correlation_id=args.correlation_id)
    if args.command == "archive":
        return CurationService(memory, SuppliedDigestGenerator(args.digest)).archive(
            **owned, idempotency_key=args.idempotency_key
        )
    raise ValueError("unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with SqliteEpisodicMemory(args.db) as memory:
            value = _run(args, memory)
            if value is None:
                raise ValueError("record not found for account")
            data = asdict(value) if hasattr(value, "__dataclass_fields__") else value
            if hasattr(value, "event_count"):
                data["event_count"] = value.event_count
            print(
                json.dumps(
                    data, indent=2, ensure_ascii=False, default=lambda v: v.isoformat()
                )
            )
    except (CurationError, ValueError, OSError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
