from dataclasses import FrozenInstanceError

import pytest

from galet_memory import (
    EpisodicEvent,
    EpisodicMemoryRequest,
    EpisodicMemoryResult,
    EpisodicSessionQuery,
    ProceduralMemoryRequest,
    SemanticMemoryRequest,
)


def test_contracts_are_available_from_package_root() -> None:
    assert EpisodicMemoryRequest("acct", "agent").max_events == 6
    assert EpisodicSessionQuery("acct").limit == 20
    assert ProceduralMemoryRequest("acct", "project").include_skills is True
    assert SemanticMemoryRequest("acct", "query").namespaces == ["external"]


def test_request_contracts_are_immutable() -> None:
    request = EpisodicMemoryRequest("acct", "agent")
    with pytest.raises(FrozenInstanceError):
        request.max_events = 10


def test_result_collections_are_not_shared() -> None:
    first = EpisodicMemoryResult()
    second = EpisodicMemoryResult()
    first.events.append(EpisodicEvent(role="user", content="hello"))
    assert second.events == []


def test_package_has_no_lucy_src_imports() -> None:
    from pathlib import Path

    package_root = Path(__file__).parents[1] / "src" / "galet_memory"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package_root.rglob("*.py")
    )
    assert "from src." not in source
    assert "import src." not in source
