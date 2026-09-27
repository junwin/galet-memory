import pytest

from galet_memory import FileProceduralMemory, ProceduralLayout, ProceduralMemoryRequest


def test_scoped_contexts_skills_and_precedence(tmp_path):
    memory = FileProceduralMemory(tmp_path)
    repo = memory.repository
    repo.save_context(account_name="alice", context_name="shop", scope="global",
                      text="Global rules", frontmatter={"imports": ["style"],
                                                      "mandatory_tools": ["search"]})
    repo.save_context(account_name="alice", context_name="shop", scope="account",
                      text="Alice rules", frontmatter={"imports": ["missing"],
                                                     "search_namespaces": ["notes"]})
    repo.save_context(account_name="alice", context_name="shop", scope="project",
                      project_name="boutique", text="Boutique rules",
                      frontmatter={"imports": ["style"], "mandatory_tools": ["publish"]})
    repo.save_skill(account_name="alice", skill_name="style", scope="global",
                    text="Generic style", frontmatter={"mandatory_tools": ["search"]})
    repo.save_skill(account_name="alice", skill_name="style", scope="project",
                    project_name="boutique", text="Boutique style",
                    frontmatter={"mandatory_tools": ["image"]})

    result = memory.recall(ProceduralMemoryRequest("alice", "shop", project_name="boutique"))
    assert result.text == "Global rules\n\nAlice rules\n\nBoutique rules"
    assert result.resolved_text.endswith("## skill: style\nBoutique style")
    assert result.imports == ["style", "missing"]
    assert result.missing_imports == ["missing"]
    assert result.required_tools == ["search", "publish", "image"]
    assert result.search_namespaces == ["notes"]
    assert result.skills[0].metadata["scope"] == "project"
    assert [source["scope"] for source in result.metadata["sources"]] == ["global", "account", "project"]
    assert memory.recall(ProceduralMemoryRequest("bob", "shop")).text == "Global rules"


def test_recall_does_not_create_and_lucy_layout_reads_existing_files(tmp_path):
    memory = FileProceduralMemory(tmp_path, ProceduralLayout.lucy())
    absent = memory.recall(ProceduralMemoryRequest("alice", "project"))
    assert absent.metadata["reason"] == "context_not_found"
    assert not (tmp_path / "contexts").exists()
    memory.repository.save_skill(account_name="alice", skill_name="develop",
                                 text="Run tests", frontmatter={"mandatory_tools": ["bash"]})
    memory.repository.save_context(account_name="alice", context_name="project",
                                   text="Build the app", frontmatter={"imports": ["develop"]})
    result = memory.recall(ProceduralMemoryRequest("alice", "project"))
    assert result.resolved_text == "Build the app\n\n## skill: develop\nRun tests"
    assert result.required_tools == ["bash"]
    assert (tmp_path / "contexts/alice/project.md").is_file()
    assert (tmp_path / "skills/alice/develop.md").is_file()


@pytest.mark.parametrize("invalid", ["../secret", "a/b", "..", ""])
def test_invalid_names_cannot_escape_root(tmp_path, invalid):
    memory = FileProceduralMemory(tmp_path)
    with pytest.raises(ValueError, match="invalid procedural name"):
        memory.repository.save_context(account_name="alice", context_name=invalid,
                                       text="x")
