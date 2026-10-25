"""Explicit, versioned configuration loading; no configuration discovery on import."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

from jsonschema import Draft202012Validator


class MemoryConfigurationError(ValueError):
    """A selected configuration source or a cross-field reference is invalid."""


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: str
    model: str
    dimensions: int
    credential_profile: str | None = None

    @property
    def space(self) -> tuple[str, str, int]:
        return self.provider, self.model, self.dimensions


@dataclass(frozen=True)
class ResourceLocation:
    external_root: str
    relative_path: str


@dataclass(frozen=True)
class DatabaseSettings:
    location: ResourceLocation
    create: bool = False


@dataclass(frozen=True)
class SemanticSettings:
    database: DatabaseSettings
    embedding_profile: str
    max_chars: int = 32000
    max_keywords: int = 20
    metadata_version: int = 2


@dataclass(frozen=True)
class EpisodicSettings:
    database: DatabaseSettings
    digests_directory: ResourceLocation | None = None


@dataclass(frozen=True)
class ProceduralSettings:
    root: ResourceLocation
    context_resolution: str = 'merge'


@dataclass(frozen=True)
class WorkingSettings:
    database: DatabaseSettings
    max_value_bytes: int = 65536


@dataclass(frozen=True)
class MemoryConfig:
    external_roots: Mapping[str, Path]
    embedding_profiles: Mapping[str, EmbeddingProfile]
    semantic: SemanticSettings | None = None
    episodic: EpisodicSettings | None = None
    procedural: ProceduralSettings | None = None
    working: WorkingSettings | None = None


def validate_relative_path(value: str, *, directory: bool = False) -> str:
    if (not isinstance(value, str) or not value or '\x00' in value
            or '\\' in value or ':' in value
            or PureWindowsPath(value).is_absolute()
            or PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts):
        raise MemoryConfigurationError('relative_path must be a relative POSIX path without traversal')
    normalized = PurePosixPath(value).as_posix()
    if not directory and normalized == '.':
        raise MemoryConfigurationError('file relative_path must name a file')
    return normalized


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MemoryConfigurationError(f'duplicate configuration key: {key}')
        result[key] = value
    return result


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise MemoryConfigurationError(f'cannot load configuration {path}: {exc}') from exc
    if not isinstance(raw, dict):
        raise MemoryConfigurationError(f'{path}: configuration must be a JSON object')
    return raw


def configuration_schema() -> dict:
    """Return the packaged JSON Schema used by the public loader."""
    return json.loads(files('galet_memory').joinpath('configuration.schema.json').read_text(encoding='utf-8'))


def _validate(raw: Mapping, source: str, definition: str | None = None) -> None:
    schema = configuration_schema()
    if definition:
        schema = {'$defs': schema['$defs'], '$ref': '#/$defs/' + definition}
    error = next(Draft202012Validator(schema).iter_errors(raw), None)
    if error:
        field = '.'.join(str(p) for p in error.absolute_path) or '<root>'
        # Do not echo invalid values; caller-supplied config may contain secrets.
        raise MemoryConfigurationError(f'{source}: {field}: invalid configuration ({error.validator})')


def load_memory_config(config: Mapping[str, Any] | None = None, *,
                       path: str | Path | None = None,
                       base_dir: str | Path | None = None) -> MemoryConfig:
    """Select object > explicit file > GALET_MEMORY_CONFIG_PATH; never merge.

    Shared file references are relative to their containing config file. An
    object with relative file references requires an explicit base_dir. Root
    directory values themselves must be absolute. Validation never opens a DB.
    """
    base = Path(base_dir).resolve() if base_dir is not None else None
    source = 'configuration object'
    if config is not None:
        if not isinstance(config, Mapping):
            raise MemoryConfigurationError('configuration object must be a mapping')
        raw = dict(config)
    else:
        selected = path if path is not None else os.environ.get('GALET_MEMORY_CONFIG_PATH')
        if selected is None or not str(selected).strip():
            raise MemoryConfigurationError('supply config, path, or GALET_MEMORY_CONFIG_PATH')
        config_path = Path(selected).expanduser().resolve()
        raw = _read_json(config_path)
        source, base = str(config_path), config_path.parent
    _validate(raw, source)

    def shared(key: str, definition: str) -> dict:
        reference = Path(raw[key]).expanduser()
        if not reference.is_absolute():
            if base is None:
                raise MemoryConfigurationError(f'{key}: relative file reference requires base_dir')
            reference = base / reference
        reference = reference.resolve()
        document = _read_json(reference)
        _validate(document, str(reference), definition)
        # Shared documents are terminal: no recursive includes or cycles.
        return document

    root_values = raw.get('external_roots')
    if root_values is None:
        root_values = shared('external_roots_file', 'rootsDocument')['external_roots']
    roots = {}
    for alias, value in root_values.items():
        root = Path(value).expanduser()
        if not root.is_absolute() or '\x00' in value:
            raise MemoryConfigurationError(f'external_roots.{alias}: root must be absolute')
        roots[alias] = root
    profile_values = raw.get('embedding_profiles', {})
    if 'embedding_profiles_file' in raw:
        profile_values = shared('embedding_profiles_file', 'profilesDocument')['embedding_profiles']
    profiles = {name: EmbeddingProfile(**value) for name, value in profile_values.items()}

    def location(value: dict, *, directory: bool = False) -> ResourceLocation:
        alias = value['external_root']
        if alias not in roots:
            raise MemoryConfigurationError(f'unknown external_root: {alias}')
        return ResourceLocation(alias, validate_relative_path(value['relative_path'], directory=directory))

    def database(value: dict) -> DatabaseSettings:
        return DatabaseSettings(location(value), value.get('create', False))

    memory = raw['memory']
    semantic = episodic = procedural = working = None
    if 'semantic' in memory:
        value = memory['semantic']
        name = value['embedding_profile']
        if name not in profiles:
            raise MemoryConfigurationError(f'unknown embedding_profile: {name}')
        # The existing vec0 adapter supports exactly this layout. Do not claim
        # arbitrary dimensions until a separately reviewed backend supports them.
        if profiles[name].dimensions != 1536:
            raise MemoryConfigurationError('current sqlite-vec backend requires dimensions=1536')
        semantic = SemanticSettings(database(value['database']), name,
                                    value.get('max_chars', 32000),
                                    value.get('keywords', {}).get('max_keywords', 20),
                                    value.get('metadata_version', 2))
    if 'episodic' in memory:
        value = memory['episodic']
        episodic = EpisodicSettings(database(value['database']),
            location(value['digests_directory'], directory=True) if 'digests_directory' in value else None)
    if 'procedural' in memory:
        value = memory['procedural']
        procedural = ProceduralSettings(location(value['root'], directory=True), value.get('context_resolution', 'merge'))
    if 'working' in memory:
        value = memory['working']
        working = WorkingSettings(database(value['database']), value.get('max_value_bytes', 65536))
    return MemoryConfig(MappingProxyType(roots), MappingProxyType(profiles),
                        semantic, episodic, procedural, working)
