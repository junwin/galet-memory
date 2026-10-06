from dataclasses import FrozenInstanceError
from pathlib import Path
import pytest
from galet_memory import (
    NewEvent,
    SessionChanges,
    UNSET,
    ProceduralMemoryRequest,
    SemanticMemoryRequest,
)


def test_neutral_contracts_are_immutable_and_explicit():
    event = NewEvent("user", "Hello", "junwin")
    assert event.correlation_ids == ()
    with pytest.raises(FrozenInstanceError):
        event.content = "changed"
    assert SessionChanges().friendly_name is UNSET
    assert ProceduralMemoryRequest("acct", "project").include_skills
    assert SemanticMemoryRequest("acct", "query").namespaces == ["external"]


def test_result_collections_are_not_shared():
    one, two = NewEvent("user", "one", "a"), NewEvent("user", "two", "a")
    one.metadata["tag"] = 1
    assert two.metadata == {}


def test_package_has_no_lucy_src_imports():
    root = Path(__file__).parents[1] / "src/galet_memory"
    source = "\n".join(p.read_text() for p in root.rglob("*.py"))
    assert "from src." not in source and "import src." not in source
