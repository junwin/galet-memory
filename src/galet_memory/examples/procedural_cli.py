"""Inspect configured procedural memory or run a disposable scope demo."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence

from ..procedural import FileProceduralMemory, ProceduralLayout, ProceduralMemoryRequest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read scoped Markdown context and skills")
    parser.add_argument("--root", type=Path, help="Existing application storage root")
    parser.add_argument("--layout", choices=["default", "lucy"], default="default")
    parser.add_argument("--account", default="demo")
    parser.add_argument("--context", default="project")
    parser.add_argument("--project", default="")
    args = parser.parse_args(argv)
    try:
        layout = ProceduralLayout.lucy() if args.layout == "lucy" else ProceduralLayout()
        if args.root:
            if not args.root.is_dir():
                raise ValueError(f"root does not exist: {args.root}")
            result = FileProceduralMemory(args.root, layout).recall(
                ProceduralMemoryRequest(args.account, args.context,
                                        project_name=args.project))
        else:
            with TemporaryDirectory(prefix="galet-procedural-") as directory:
                memory = FileProceduralMemory(directory)
                repo = memory.repository
                repo.save_skill(account_name="demo", skill_name="review", scope="global",
                                text="Check the work", frontmatter={"mandatory_tools": ["read"]})
                repo.save_context(account_name="demo", context_name="project", scope="global",
                                  text="Keep a record", frontmatter={"imports": ["review"]})
                repo.save_context(account_name="demo", context_name="project", scope="project",
                                  project_name="shop", text="Review the shop")
                result = memory.recall(ProceduralMemoryRequest("demo", "project",
                                                                project_name="shop"))
                assert result.required_tools == ["read"]
                assert len(result.metadata["sources"]) == 2
        print(json.dumps(asdict(result), indent=2, default=str))
    except (ValueError, OSError, AssertionError) as exc:
        print(f"procedural memory example failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
