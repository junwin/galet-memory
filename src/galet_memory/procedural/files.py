"""Filesystem-backed procedural context and skill behavior."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..ports.contexts import ContextSnapshot, SkillSnapshot
from .context_memory import result_from_snapshot
from .interface import ProceduralMemory, ProceduralMemoryRequest, ProceduralMemoryResult


@dataclass(frozen=True)
class ProceduralLayout:
    global_contexts: str | None = "procedural/global/contexts"
    account_contexts: str | None = "procedural/accounts/{account}/contexts"
    project_contexts: str | None = "procedural/accounts/{account}/projects/{project}/contexts"
    global_skills: str | None = "procedural/global/skills"
    account_skills: str | None = "procedural/accounts/{account}/skills"
    project_skills: str | None = "procedural/accounts/{account}/projects/{project}/skills"

    @classmethod
    def lucy(cls) -> "ProceduralLayout":
        return cls(None, "contexts/{account}", None, None, "skills/{account}", None)


def _name(value: str) -> str:
    if not value or value in (".", "..") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(f"invalid procedural name: {value!r}")
    return value


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}, text
    marker = text.index("\n---\n", 4)
    frontmatter = yaml.safe_load(text[4:marker]) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError(f"frontmatter must be a mapping: {path}")
    return frontmatter, text[marker + 5:]


class FileContextRepository:
    """Resolve scoped contexts and skill imports from configured Markdown paths."""

    def __init__(self, root: str | Path, layout: ProceduralLayout | None = None,
                 *, context_resolution: str = "merge") -> None:
        if context_resolution not in ("merge", "most_specific"):
            raise ValueError("context_resolution must be 'merge' or 'most_specific'")
        self.context_resolution = context_resolution
        self.root = Path(root).resolve()
        self.layout = layout or ProceduralLayout()

    def _path(self, template: str, account: str, project: str, name: str) -> Path:
        relative = template.format(account=_name(account), project=_name(project) if project else "")
        path = (self.root / relative / (_name(name) + ".md")).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("procedural path escapes configured root")
        return path

    def _find(self, kind: str, account: str, project: str, name: str):
        _name(account)
        if project:
            _name(project)
        for scope in ("global", "account", "project"):
            if scope == "project" and not project:
                continue
            template = getattr(self.layout, scope + "_" + kind)
            if template is None:
                continue
            path = self._path(template, account, project, name)
            if path.is_file():
                yield scope, path

    def get(self, account_name: str, context_name: str) -> ContextSnapshot | None:
        return self.resolve(account_name, context_name)

    def get_or_create(self, account_name: str, context_name: str) -> ContextSnapshot:
        existing = self.get(account_name, context_name)
        if existing is not None:
            return existing
        self.save_context(account_name=account_name, context_name=context_name, text="")
        return self.get(account_name, context_name)

    def list_context_names(self, account_name: str, *, project_name: str = "",
                           scope: str = "account") -> list[str]:
        template = getattr(self.layout, scope + "_contexts") if scope in ("global", "account", "project") else None
        if template is None or (scope == "project" and not project_name):
            return []
        directory = self._path(template, account_name, project_name, "placeholder").parent
        return sorted(path.stem for path in directory.glob("*.md") if path.is_file()
                      and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.stem))

    def list_resolved_context_names(self, account_name: str, *,
                                    project_name: str = "") -> list[str]:
        """List visible names across configured scopes, without duplicates."""
        return sorted({name for scope in ("global", "account", "project")
                       for name in self.list_context_names(
                           account_name, project_name=project_name, scope=scope)})

    def read_effective_context(self, account_name: str, context_name: str, *,
                               project_name: str = "") -> tuple[dict[str, Any], str] | None:
        """Read the most specific raw file; use resolve for merged recall."""
        matches = list(self._find("contexts", account_name, project_name, context_name))
        return _markdown(matches[-1][1]) if matches else None

    def read_context(self, account_name: str, context_name: str, *,
                     scope: str = "account", project_name: str = "") -> tuple[dict[str, Any], str] | None:
        template = getattr(self.layout, scope + "_contexts") if scope in ("global", "account", "project") else None
        if template is None or (scope == "project" and not project_name):
            raise ValueError("context scope is not configured")
        path = self._path(template, account_name, project_name, context_name)
        return _markdown(path) if path.is_file() else None

    def update_context(self, *, account_name: str, context_name: str,
                       text: str | None = None, frontmatter: Mapping[str, Any] | None = None,
                       scope: str = "account", project_name: str = "",
                       inherit_existing: bool = False) -> Path:
        current = self.read_context(account_name, context_name, scope=scope,
                                    project_name=project_name)
        if current is None and inherit_existing:
            # Copy a less-specific definition into the target scope before editing.
            matches = list(self._find("contexts", account_name, project_name, context_name))
            lower = {"global": (), "account": ("global",),
                     "project": ("global", "account")}[scope]
            inherited = [path for source_scope, path in matches if source_scope in lower]
            if inherited:
                current = _markdown(inherited[-1])
        fields, body = current if current is not None else ({}, "")
        fields.update(frontmatter or {})
        return self.save_context(account_name=account_name, context_name=context_name,
                                 text=body if text is None else text,
                                 frontmatter=fields, scope=scope, project_name=project_name)

    def resolve(self, account_name: str, context_name: str,
                project_name: str = "", *, skill_names: tuple[str, ...] = ()) -> ContextSnapshot | None:
        contexts = (list(self._find("contexts", account_name, project_name, context_name))
                    if context_name and context_name != "none" else [])
        if self.context_resolution == "most_specific":
            contexts = contexts[-1:]
        if not contexts and not skill_names:
            return None
        context_bodies: list[str] = []
        imports: list[str] = []
        required: list[str] = []
        namespaces: list[str] = []
        sources: list[dict[str, str]] = []
        tag = None
        updated = None
        for scope, path in contexts:
            fm, body = _markdown(path)
            if body.strip():
                context_bodies.append(body.strip())
            imports.extend(_strings(fm.get("imports")))
            required.extend(_strings(fm.get("mandatory_tools")))
            namespaces.extend(_strings(fm.get("search_namespaces")))
            tag = fm.get("tag") or tag
            updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            sources.append({"scope": scope, "path": str(path)})
        skills: list[SkillSnapshot] = []
        missing: list[str] = []
        resolved = list(context_bodies)
        for name in _unique(list(skill_names) + imports):
            matches = list(self._find("skills", account_name, project_name, name))
            if not matches:
                missing.append(name)
                continue
            scope, path = matches[-1]
            fm, body = _markdown(path)
            tools = _strings(fm.get("mandatory_tools"))
            required.extend(tools)
            skills.append(SkillSnapshot(name, body, tools,
                                        {**{k: v for k, v in fm.items() if k != "mandatory_tools"},
                                         "scope": scope, "path": str(path)}))
            if body.strip():
                resolved.append(f"## skill: {name}\n{body.strip()}")
        return ContextSnapshot(
            id=context_name, account_name=account_name, tag=tag,
            text="\n\n".join(context_bodies), resolved_text="\n\n".join(resolved),
            imports=_unique(imports), skills=skills, missing_imports=missing,
            required_tools=_unique(required), search_namespaces=_unique(namespaces),
            updated_at=updated, metadata={"sources": sources, "project_name": project_name},
        )

    def _save(self, kind: str, *, scope: str, account_name: str,
              project_name: str, name: str, text: str,
              frontmatter: Mapping[str, Any] | None) -> Path:
        if scope not in ("global", "account", "project"):
            raise ValueError("invalid procedural scope")
        if scope == "project" and not project_name:
            raise ValueError("project_name is required")
        template = getattr(self.layout, scope + "_" + kind)
        if template is None:
            raise ValueError(f"{scope} {kind} location is not configured")
        path = self._path(template, account_name, project_name, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"---\n{yaml.safe_dump(dict(frontmatter or {}), sort_keys=False, allow_unicode=True)}---\n{text}"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return path

    def save_context(self, *, account_name: str, context_name: str, text: str,
                     scope: str = "account", project_name: str = "",
                     frontmatter: Mapping[str, Any] | None = None) -> Path:
        return self._save("contexts", scope=scope, account_name=account_name,
                          project_name=project_name, name=context_name, text=text,
                          frontmatter=frontmatter)

    def save_skill(self, *, account_name: str, skill_name: str, text: str,
                   scope: str = "account", project_name: str = "",
                   frontmatter: Mapping[str, Any] | None = None) -> Path:
        return self._save("skills", scope=scope, account_name=account_name,
                          project_name=project_name, name=skill_name, text=text,
                          frontmatter=frontmatter)


class FileProceduralMemory(ProceduralMemory):
    def __init__(self, root: str | Path, layout: ProceduralLayout | None = None,
                 *, context_resolution: str = "merge") -> None:
        self.repository = FileContextRepository(
            root, layout, context_resolution=context_resolution)

    def recall(self, request: ProceduralMemoryRequest) -> ProceduralMemoryResult:
        if (not request.context_name or request.context_name == "none") and not request.skill_names:
            return ProceduralMemoryResult(account_name=request.account_name,
                                          metadata={"reason": "no_context"})
        context = self.repository.resolve(request.account_name, request.context_name,
                                          request.project_name, skill_names=request.skill_names)
        if (request.create_if_missing and request.context_name
                and request.context_name != "none"
                and (context is None or not context.metadata.get("sources"))):
            self.repository.save_context(account_name=request.account_name,
                                         context_name=request.context_name, text="",
                                         project_name=request.project_name,
                                         scope="project" if request.project_name else "account")
            context = self.repository.resolve(request.account_name, request.context_name,
                                              request.project_name, skill_names=request.skill_names)
        if context is None:
            return ProceduralMemoryResult(account_name=request.account_name,
                                          metadata={"reason": "context_not_found"})
        return result_from_snapshot(context, request)
