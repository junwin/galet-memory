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


def test_most_specific_context_replaces_body_and_frontmatter(tmp_path):
    memory = FileProceduralMemory(tmp_path, context_resolution="most_specific")
    repo = memory.repository
    repo.save_context(account_name="alice", context_name="shop", scope="global",
                      text="Global body", frontmatter={"imports": ["global-only"],
                      "mandatory_tools": ["global-tool"], "search_namespaces": ["global"],
                      "tag": "global"})
    repo.save_context(account_name="alice", context_name="shop", text="Account body",
                      frontmatter={"imports": ["style"], "tag": "account"})
    repo.save_skill(account_name="alice", skill_name="style", scope="global",
                    text="Global style", frontmatter={"mandatory_tools": ["shared-tool"]})
    repo.save_skill(account_name="alice", skill_name="style", text="Account style")
    result = memory.recall(ProceduralMemoryRequest("alice", "shop"))
    assert result.text == "Account body"
    assert result.imports == ["style"]
    assert result.skills[0].text == "Account style"
    assert result.required_tools == []
    assert result.search_namespaces == []
    assert result.tag == "account"
    assert [s["scope"] for s in result.metadata["sources"]] == ["account"]
    bob = memory.recall(ProceduralMemoryRequest("bob", "shop", skill_names=("style",)))
    assert bob.text == "Global body"
    assert bob.skills[0].text == "Global style"
    assert bob.missing_imports == ["global-only"]
    assert bob.required_tools == ["global-tool", "shared-tool"]


def test_effective_listing_raw_read_project_precedence_and_empty_override(tmp_path):
    memory = FileProceduralMemory(tmp_path, context_resolution="most_specific")
    repo = memory.repository
    repo.save_context(account_name="alice", context_name="shop", scope="global", text="Global")
    repo.save_context(account_name="alice", context_name="shared", scope="global", text="Shared")
    repo.save_context(account_name="alice", context_name="shop", text="Account")
    repo.save_context(account_name="alice", context_name="private", text="Private")
    repo.save_context(account_name="alice", context_name="shop", scope="project",
                      project_name="boutique", text="")
    assert repo.list_resolved_context_names("alice") == ["private", "shared", "shop"]
    assert repo.list_resolved_context_names("bob") == ["shared", "shop"]
    assert repo.read_effective_context("alice", "shop")[1] == "Account"
    assert repo.read_effective_context("bob", "shop")[1] == "Global"
    assert repo.read_effective_context("bob", "private") is None
    result = memory.recall(ProceduralMemoryRequest("alice", "shop", project_name="boutique"))
    assert result.text == ""
    assert result.metadata["sources"][0]["scope"] == "project"


def test_edit_inherited_context_copies_into_account_without_changing_global(tmp_path):
    repo = FileProceduralMemory(tmp_path, context_resolution="most_specific").repository
    repo.save_context(account_name="alice", context_name="shop", scope="global",
                      text="Shared body", frontmatter={"imports": ["style"], "custom": 42})
    # A project definition must never be copied upward into account scope.
    repo.save_context(account_name="alice", context_name="shop", scope="project",
                      project_name="boutique", text="Project private")
    repo.update_context(account_name="alice", context_name="shop", project_name="boutique",
                        frontmatter={"mandatory_tools": ["inspect"]}, inherit_existing=True)
    assert repo.read_context("alice", "shop") == (
        {"imports": ["style"], "custom": 42, "mandatory_tools": ["inspect"]}, "Shared body")
    assert repo.read_context("bob", "shop", scope="global") == (
        {"imports": ["style"], "custom": 42}, "Shared body")


def test_global_named_skill_without_context_and_read_only_lookup(tmp_path):
    memory = FileProceduralMemory(tmp_path, context_resolution="most_specific")
    memory.repository.save_skill(account_name="alice", skill_name="writing", scope="global",
                                 text="Write clearly")
    result = memory.recall(ProceduralMemoryRequest("bob", "none", skill_names=("writing",)))
    assert result.skills[0].text == "Write clearly"
    assert result.skills[0].metadata["scope"] == "global"
    assert not (tmp_path / "procedural/accounts").exists()


def test_invalid_context_resolution_fails_fast(tmp_path):
    with pytest.raises(ValueError, match="context_resolution"):
        FileProceduralMemory(tmp_path, context_resolution="typo")
