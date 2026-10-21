"""Default confined filesystem resolver for explicitly trusted root mappings."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol

from .configuration import MemoryConfigurationError, ResourceLocation, validate_relative_path
from .ports.text import FileTextLoader, TextSnippet


class ResourceResolver(Protocol):
    """Host implementations may enforce a narrower account/agent policy."""
    def resolve(self, location: ResourceLocation, *, must_exist: bool = True,
                directory: bool = False) -> Path: ...
    def authorize_path(self, path: str | Path) -> Path: ...


class RootResourceResolver:
    """The supplied mapping is the allowlist; names add no account/namespace."""
    def __init__(self, roots: Mapping[str, str | Path]):
        self._roots = {alias: Path(path) for alias, path in roots.items()}
        if any(not root.is_absolute() for root in self._roots.values()):
            raise MemoryConfigurationError('external roots must be absolute')

    def resolve(self, location: ResourceLocation, *, must_exist: bool = True,
                directory: bool = False) -> Path:
        if location.external_root not in self._roots:
            raise PermissionError(f'unauthorized external_root: {location.external_root}')
        relative = validate_relative_path(location.relative_path, directory=directory)
        root = self._roots[location.external_root].resolve(strict=True)
        if not root.is_dir():
            raise MemoryConfigurationError('external_root must name a directory')
        candidate = root / relative
        path = candidate.resolve(strict=must_exist)
        if not path.is_relative_to(root):
            raise PermissionError('resource escapes external_root')
        if not must_exist and not path.exists():
            # Creation permits a missing leaf, never implicit directory creation.
            parent = candidate.parent.resolve(strict=True)
            if not parent.is_dir() or not parent.is_relative_to(root):
                raise PermissionError('resource parent escapes external_root')
        if path.exists() and (not path.is_dir() if directory else not path.is_file()):
            raise MemoryConfigurationError('resource has the wrong filesystem type')
        return path

    def authorize_path(self, path: str | Path) -> Path:
        """Reauthorize legacy absolute metadata paths on every text read."""
        candidate = Path(path)
        if not candidate.is_absolute():
            raise PermissionError('legacy metadata path must be absolute')
        resolved = candidate.resolve(strict=True)
        # Deterministic containment checks; never search for files by name.
        for alias, configured in self._roots.items():
            try:
                root = configured.resolve(strict=True)
            except FileNotFoundError:
                continue
            if resolved.is_relative_to(root):
                return self.resolve(ResourceLocation(alias, resolved.relative_to(root).as_posix()))
        raise PermissionError('metadata path is outside authorized roots')


class ConfinedTextLoader:
    def __init__(self, resolver: ResourceResolver, loader=None):
        self.resolver = resolver
        self.default_loader = loader is None
        self.loader = FileTextLoader() if loader is None else loader

    def load(self, path: str | Path, *, max_chars: int | None) -> TextSnippet:
        authorized = self.resolver.authorize_path(path)
        if self.default_loader and authorized.suffix.lower() not in ('.txt', '.md'):
            raise ValueError('default configured text loader supports .txt and .md; supply a host loader for other formats')
        return self.loader.load(authorized, max_chars=max_chars)
