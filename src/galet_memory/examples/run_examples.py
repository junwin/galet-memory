from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Sequence

DEFAULT_CREDENTIAL_PATH = "/home/junwin/credential"
DEFAULT_DB = "/home/junwin/lucy_storage/data/embeddings-v2.sqlite"
DEFAULT_EPISODIC_DB = "/home/junwin/lucy_storage/data/chat2.sqlite"
DEFAULT_ACCOUNT = "junwin"
DEFAULT_NAMESPACE = "demo"
DEFAULT_EPISODIC_SESSION_ID = "road-test"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the galet-memory example commands."
    )
    parser.add_argument("--credential-path", default=DEFAULT_CREDENTIAL_PATH)
    parser.add_argument("--db", default=DEFAULT_DB, help="Embedding database")
    parser.add_argument(
        "--episodic-db",
        default=DEFAULT_EPISODIC_DB,
        help="Episodic memory database",
    )
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--session-id",
        default=DEFAULT_EPISODIC_SESSION_ID,
        help="Episodic session ID",
    )
    return parser


def _run(command: list[str]) -> None:
    print("$", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def _embedding_command(
    credential_path: str,
    db: str,
    account: str,
    namespace: str,
    action: str,
    text: str,
) -> list[str]:
    return [
        "galet-memory-embeddings",
        "--credential-path",
        credential_path,
        "--db",
        db,
        "--account",
        account,
        "--namespace",
        namespace,
        action,
        text,
    ]


def _episodic_command(db: str, *arguments: str) -> list[str]:
    return ["galet-memory-episodic", "--db", db, *arguments]


def _run_embedding_examples(args: argparse.Namespace) -> None:
    sample_text = "The allotment has runner beans and three apple trees."
    question = "What fruit trees are in the allotment?"
    _run(
        _embedding_command(
            args.credential_path,
            args.db,
            args.account,
            args.namespace,
            "add",
            sample_text,
        )
    )
    _run(
        _embedding_command(
            args.credential_path,
            args.db,
            args.account,
            args.namespace,
            "query",
            question,
        )
    )


def _run_episodic_examples(args: argparse.Namespace) -> None:
    session_id, db = args.session_id, args.episodic_db

    def run(*arguments):
        _run(_episodic_command(db, "--account", args.account, *arguments))

    run("create", "--session-id", session_id, "--friendly-name", "Road test")
    run(
        "append",
        session_id,
        "Hello episodic memory",
        "--actor",
        args.account,
        "--correlation",
        "road-test-exchange",
    )
    run("show", session_id)
    run("recent", session_id, "--count", "10", "--kind", "user_message")
    run("exchange", session_id, "road-test-exchange")
    run("archive", session_id, "The earlier conversation was summarized.")
    run("recent", session_id)
    run("list")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    credential_path = Path(args.credential_path)
    database = Path(args.db)
    episodic_database = Path(args.episodic_db)
    if not credential_path.exists():
        raise SystemExit(f"credential path does not exist: {credential_path}")
    if not database.exists():
        raise SystemExit(f"database does not exist: {database}")
    if episodic_database.exists():
        raise SystemExit(f"choose a fresh episodic database: {episodic_database}")

    _run_embedding_examples(args)
    _run_episodic_examples(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
