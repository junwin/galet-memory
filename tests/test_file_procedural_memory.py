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


def test_list_read_and_update_preserve_context_fields(tmp_path):
    repo = FileProceduralMemory(tmp_path, ProceduralLayout.lucy()).repository
    assert repo.list_context_names("alice") == []
    assert repo.read_context("alice", "shop") is None
    repo.save_context(account_name="alice", context_name="shop", text="Original",
                      frontmatter={"imports": ["writing"], "allowed_tools": ["search"]})
    repo.save_context(account_name="alice", context_name="other", text="Other")
    assert repo.list_context_names("alice") == ["other", "shop"]
    repo.update_context(account_name="alice", context_name="shop",
                        frontmatter={"mandatory_tools": ["inspect"]})
    fields, body = repo.read_context("alice", "shop")
    assert body == "Original"
    assert fields == {"imports": ["writing"], "allowed_tools": ["search"],
                      "mandatory_tools": ["inspect"]}
    repo.update_context(account_name="alice", context_name="shop", text="Changed")
    assert repo.read_context("alice", "shop")[1] == "Changed"


@pytest.mark.parametrize("invalid", ["../secret", "a/b", "..", ""])
def test_invalid_names_cannot_escape_root(tmp_path, invalid):
    memory = FileProceduralMemory(tmp_path)
    with pytest.raises(ValueError, match="invalid procedural name"):
        memory.repository.save_context(account_name="alice", context_name=invalid,
                                       text="x")


def test_named_skills_merge_with_context_imports_and_report_missing(tmp_path):
    memory = FileProceduralMemory(tmp_path, ProceduralLayout.lucy())
    repo = memory.repository
    for name in ("image-cli", "filepaths", "project-only"):
        repo.save_skill(account_name="alice", skill_name=name, text=f"Instructions {name}",
                        frontmatter={"mandatory_tools": ["execute"]})
    repo.save_context(account_name="alice", context_name="images", text="Image project",
                      frontmatter={"imports": ["filepaths", "project-only"]})
    result = memory.recall(ProceduralMemoryRequest("alice", "images",
                          skill_names=("image-cli", "filepaths", "image-cli", "missing")))
    assert [skill.name for skill in result.skills] == ["image-cli", "filepaths", "project-only"]
    assert result.imports == ["filepaths", "project-only"]
    assert result.missing_imports == ["missing"]
    assert result.required_tools == ["execute"]
    assert result.resolved_text.count("Instructions filepaths") == 1


@pytest.mark.parametrize("context", ["", "none", "absent"])
def test_named_skills_load_without_existing_context(tmp_path, context):
    memory = FileProceduralMemory(tmp_path, ProceduralLayout.lucy())
    memory.repository.save_skill(account_name="alice", skill_name="writing", text="Write clearly")
    result = memory.recall(ProceduralMemoryRequest("alice", context, skill_names=("writing",)))
    assert [skill.name for skill in result.skills] == ["writing"]
    assert result.text == ""
    assert not (tmp_path / "contexts").exists()
    assert not memory.recall(ProceduralMemoryRequest("bob", "", skill_names=("writing",))).skills


def test_named_skill_paths_cannot_escape_root(tmp_path):
    with pytest.raises(ValueError, match="invalid procedural name"):
        FileProceduralMemory(tmp_path).recall(
            ProceduralMemoryRequest("alice", "", skill_names=("../secret",)))
