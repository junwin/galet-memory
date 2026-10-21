"""Public configuration assembly for the existing memory backends."""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import re
import sqlite3
from typing import Callable, Mapping, Protocol, Sequence

from .configuration import (DatabaseSettings, EmbeddingProfile, MemoryConfig,
                            MemoryConfigurationError, ResourceLocation, load_memory_config,
                            validate_relative_path)
from .episodic import SqliteEpisodicMemory
from .ports import EmbeddingCompatibilityError, SimpleKeywordExtractor, SqliteVecEmbeddingIndex, TextLoader
from .procedural import FileProceduralMemory
from .resources import ConfinedTextLoader, ResourceResolver, RootResourceResolver
from .semantic import (SemanticIngestionService, SemanticMemoryRequest,
                       SemanticMemoryResult, VectorSemanticMemory)
from .working import SqliteWorkingMemory


class ProfiledEmbeddingProvider(Protocol):
    """A provider factory promises one fixed embedding space, without fallback."""
    profile: EmbeddingProfile
    def embed(self, texts: Sequence[str], *, model: str) -> Sequence[Sequence[float]]: ...


EmbeddingProviderFactory = Callable[[EmbeddingProfile], ProfiledEmbeddingProvider]


class _CheckedEmbeddings:
    def __init__(self, provider: ProfiledEmbeddingProvider, profile: EmbeddingProfile):
        if getattr(provider, 'profile', None) != profile:
            raise MemoryConfigurationError('embedding provider factory returned a different or undeclared profile')
        self.provider, self.profile = provider, profile

    def embed(self, texts, *, model):
        if model != self.profile.model:
            raise EmbeddingCompatibilityError('requested embedding model differs from configured profile')
        vectors = self.provider.embed(texts, model=model)
        if len(vectors) != len(texts):
            raise EmbeddingCompatibilityError('embedding provider returned the wrong vector count')
        for vector in vectors:
            if len(vector) != self.profile.dimensions:
                raise EmbeddingCompatibilityError('embedding provider returned incompatible dimensions')
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                raise EmbeddingCompatibilityError('embedding provider returned non-finite or non-numeric values')
        return vectors


class _GaletProfileProvider:
    def __init__(self, api, profile):
        self.api, self.profile = api, profile

    def embed(self, texts, *, model):
        response = self.api.embed(model=model, input=list(texts))
        if response.model != self.profile.model:
            raise EmbeddingCompatibilityError('Galet returned a model different from the configured profile')
        return response.embeddings


def galet_embedding_factory(settings=None) -> EmbeddingProviderFactory:
    """Adapt explicit Galet Settings (or Galet's existing credential defaults).

    Uses a fixed provider implementation, not a router that can change providers.
    Credential lookup remains Galet's responsibility, including
    GALET_CREDENTIAL_PATH and named credential profiles when configured.
    """
    def factory(profile: EmbeddingProfile) -> ProfiledEmbeddingProvider:
        from galet.settings import Settings
        configured = settings if settings is not None else Settings()
        if profile.credential_profile is not None:
            configured = replace(configured, credential_profile=profile.credential_profile)
        if profile.provider == 'openai':
            from galet.openai_embedding import OpenAIEmbeddingApi
            api = OpenAIEmbeddingApi(settings=configured)
        elif profile.provider == 'mistral':
            from galet.mistral_embedding import MistralEmbeddingApi
            api = MistralEmbeddingApi(settings=configured)
        else:
            raise MemoryConfigurationError('supply embedding_provider_factory for provider ' + profile.provider)
        return _GaletProfileProvider(api, profile)
    return factory


class _Keywords:
    def __init__(self, maximum: int):
        self.extractor, self.maximum = SimpleKeywordExtractor(), maximum

    def extract(self, text, *, top_n=None):
        return self.extractor.extract(text, top_n=self.maximum if top_n is None else top_n)


def _read_existing(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def _validate_existing(path: Path, kind: str, profile: EmbeddingProfile | None):
    try:
        with closing(_read_existing(path)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if kind == 'semantic':
                if 'embedding_schema_version' in tables:
                    raise MemoryConfigurationError('metadata v3 requires the future v3 adapter; this assembler uses v2')
                if not {'embedding_metadata', 'vec_embeddings_v2'} <= tables:
                    raise MemoryConfigurationError('semantic store must use canonical metadata and vec_embeddings_v2')
                required = {'id', 'account_name', 'namespace', 'source_type', 'source_id', 'document_id',
                            'model', 'provider', 'dimensions', 'source_metadata', 'keywords', 'created_at'}
                columns = {r[1] for r in conn.execute('PRAGMA table_info(embedding_metadata)')}
                if not required <= columns:
                    raise MemoryConfigurationError('semantic store needs an explicit canonical/keywords migration')
                sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='vec_embeddings_v2'").fetchone()[0]
                if not re.search(r'USING\s+vec0\s*\(', sql, re.I) or not re.search(r'float\s*\[\s*1536\s*\]', sql, re.I):
                    raise MemoryConfigurationError('semantic vector table is not the supported 1536-dimension vec0 layout')
                spaces = conn.execute('SELECT DISTINCT provider,model,dimensions FROM embedding_metadata').fetchall()
                if any(tuple(space) != profile.space for space in spaces):
                    raise MemoryConfigurationError('stored embedding provider/model/dimensions differ from selected profile; reindex explicitly')
            elif kind == 'episodic':
                if not {'store_info', 'sessions', 'events', 'event_correlations'} <= tables:
                    raise MemoryConfigurationError('episodic store needs the explicit v2 schema')
            elif kind == 'working':
                if not {'working_runs', 'working_values'} <= tables:
                    raise MemoryConfigurationError('working store has an incompatible schema')
    except sqlite3.Error as exc:
        raise MemoryConfigurationError(f'{kind} database is invalid: {path}') from exc


@dataclass
class MemoryRuntime:
    config: MemoryConfig
    resolver: ResourceResolver
    semantic: VectorSemanticMemory | None = None
    ingestion: SemanticIngestionService | None = None
    episodic: SqliteEpisodicMemory | None = None
    procedural: FileProceduralMemory | None = None
    working: SqliteWorkingMemory | None = None
    _index: SqliteVecEmbeddingIndex | None = field(default=None, repr=False)
    _cleanup: ExitStack = field(default_factory=ExitStack, repr=False)
    _closed: bool = field(default=False, repr=False)

    def ingest_file(self, *, account_name: str, external_root: str,
                    relative_path: str, namespace: str = 'external', tags=()):
        if self._closed or self.ingestion is None:
            raise RuntimeError('semantic memory is unavailable or closed')
        relative_path = validate_relative_path(relative_path)
        location = ResourceLocation(external_root, relative_path)
        path = self.resolver.resolve(location)
        snippet = self.ingestion.text_loader.load(path, max_chars=self.ingestion.max_chars + 1)
        if snippet.truncated:
            raise ValueError('source exceeds max_chars; split it before indexing')
        # Keep v2 path metadata for compatibility while also recording the exact
        # root-relative source location. v3 column writes are a separate change.
        return self.ingestion.ingest_text(
            account_name=account_name, namespace=namespace,
            source_id=external_root + ':' + relative_path,
            text=snippet.text, source_type='file', title=path.name, tags=tags,
            metadata={'path': str(path), 'external_root': external_root,
                      'relative_path': relative_path},
        )

    def recall(self, *, account_name: str, query: str, namespaces=None,
               use_embeddings: bool = True, top_k: int = 3,
               max_chars: int = 9000, score_threshold: float = 0.25):
        if self._closed or self.semantic is None:
            raise RuntimeError('semantic memory is unavailable or closed')
        selected = list(namespaces) if namespaces is not None else list(self._index.list_namespaces(account_name))
        if not selected:
            return SemanticMemoryResult(metadata={'selected_count': 0, 'namespaces': []})
        profile = self.config.embedding_profiles[self.config.semantic.embedding_profile]
        return self.semantic.recall(SemanticMemoryRequest(
            account_name=account_name, query=query, namespaces=selected,
            use_embeddings=use_embeddings, top_k=top_k, max_chars=max_chars,
            score_threshold=score_threshold, embedding_model=profile.model))

    def close(self):
        if not self._closed:
            self._closed = True
            self._cleanup.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def build_memory(config: MemoryConfig | Mapping | None = None, *, path=None,
                 base_dir=None, embedding_provider_factory: EmbeddingProviderFactory | None = None,
                 resource_resolver: ResourceResolver | None = None,
                 text_loader: TextLoader | None = None) -> MemoryRuntime:
    """Load/validate config, preflight paths/profile, then assemble enabled stores.

    No recursive package discovery. Provider clients supplied by a factory remain
    caller-owned; the returned context manager closes all stores it opens.
    Existing DBs are never implicitly upgraded. Creation requires create=true and
    existing parent directories. An explicit host resolver may narrow access.
    Custom text loaders receive paths authorized by the same resolver on each read.
    """
    settings = config if isinstance(config, MemoryConfig) else load_memory_config(config, path=path, base_dir=base_dir)
    resolver = resource_resolver if resource_resolver is not None else RootResourceResolver(settings.external_roots)
    profile = settings.embedding_profiles[settings.semantic.embedding_profile] if settings.semantic else None
    # A caller can also construct typed settings directly; enforce backend limits.
    if profile is not None and profile.dimensions != 1536:
        raise MemoryConfigurationError('current sqlite-vec backend requires dimensions=1536')
    paths = {}
    for kind in ('semantic', 'episodic', 'working'):
        component = getattr(settings, kind)
        if component is None:
            continue
        database: DatabaseSettings = component.database
        target = resolver.resolve(database.location, must_exist=not database.create)
        if target in paths.values():
            raise MemoryConfigurationError('memory components must use distinct database files')
        if target.exists():
            _validate_existing(target, kind, profile)
        paths[kind] = target
    procedural_root = resolver.resolve(settings.procedural.root, directory=True) if settings.procedural else None
    digests = (resolver.resolve(settings.episodic.digests_directory, directory=True)
               if settings.episodic and settings.episodic.digests_directory else None)
    embeddings = (_CheckedEmbeddings((embedding_provider_factory or galet_embedding_factory())(profile), profile)
                  if profile is not None else None)
    loader = ConfinedTextLoader(resolver, text_loader)
    runtime = MemoryRuntime(settings, resolver)
    try:
        # Episodic validation runs before semantic/working stores are opened.
        if settings.episodic:
            target = paths['episodic']
            runtime.episodic = SqliteEpisodicMemory(target, digests_root=digests,
                                                   initialize_schema=not target.exists())
            runtime._cleanup.callback(runtime.episodic.close)
        if settings.semantic:
            target = paths['semantic']
            index = SqliteVecEmbeddingIndex(target, initialize_schema=not target.exists())
            runtime._index = index
            runtime._cleanup.callback(index.close)
            keywords = _Keywords(settings.semantic.max_keywords)
            runtime.ingestion = SemanticIngestionService(embeddings=embeddings, index=index,
                text_loader=loader, keyword_extractor=keywords, model=profile.model,
                provider=profile.provider, max_chars=settings.semantic.max_chars)
            runtime.semantic = VectorSemanticMemory(embeddings=embeddings, index=index,
                text_loader=loader, episodic_store=runtime.episodic, keyword_extractor=keywords)
        if settings.procedural:
            runtime.procedural = FileProceduralMemory(procedural_root,
                context_resolution=settings.procedural.context_resolution)
        if settings.working:
            runtime.working = SqliteWorkingMemory(paths['working'], max_value_bytes=settings.working.max_value_bytes)
            runtime._cleanup.callback(runtime.working.close)
        return runtime
    except BaseException:
        runtime.close()
        raise
