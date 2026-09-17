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
    session_id = args.session_id
    db = args.episodic_db
    _run(
        _episodic_command(
            db,
            "create",
            "--account",
            args.account,
            "--agent",
            "lucy",
            "--session-id",
            session_id,
            "--friendly-name",
            "Road test",
        )
    )
    _run(_episodic_command(db, "add", session_id, "Hello episodic memory"))
    _run(_episodic_command(db, "show", session_id))
    _run(
        _episodic_command(
            db,
            "archive",
            session_id,
            "The earlier conversation was summarized.",
            "--account",
            args.account,
        )
    )
    for scope in ("active", "archived", "all"):
        _run(_episodic_command(db, "show", session_id, "--scope", scope))
    _run(_episodic_command(db, "list", "--account", args.account))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    credential_path = Path(args.credential_path)
    database = Path(args.db)
    episodic_database = Path(args.episodic_db)
    if not credential_path.exists():
        raise SystemExit(f"credential path does not exist: {credential_path}")
    if not database.exists():
        raise SystemExit(f"database does not exist: {database}")
    if not episodic_database.exists():
        raise SystemExit(f"database does not exist: {episodic_database}")

    _run_embedding_examples(args)
    _run_episodic_examples(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
