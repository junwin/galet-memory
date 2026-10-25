"""V3 file/inline ingestion and recall through explicit authorized locators."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from datetime import datetime, timezone

from ..configuration import ResourceLocation, validate_relative_path
from ..ports.embeddings import EmbeddingResource, EmbeddingMatch, StoredEmbedding
from .ingestion import SemanticIngestionResult
from .vector_memory import VectorSemanticMemory


class ResourceIngestionService:
    def __init__(self, *, embeddings, index, text_loader, resolver, keyword_extractor,
                 model, provider, max_chars=32000):
        self.embeddings, self.index, self.text_loader = embeddings, index, text_loader
        self.resolver, self.keyword_extractor = resolver, keyword_extractor
        self.model, self.provider, self.max_chars = model, provider, max_chars

    def ingest_file(self, *, account_name, external_root, relative_path, namespace='', tags=()):
        path = validate_relative_path(relative_path)
        authorized = self.resolver.resolve(ResourceLocation(external_root, path))
        snippet = self.text_loader.load(authorized, max_chars=self.max_chars + 1)
        if snippet.truncated or len(snippet.text) > self.max_chars:
            raise ValueError('source exceeds max_chars; split it before indexing')
        resource = EmbeddingResource('file', external_root, path)
        return self._ingest(account_name, namespace, external_root + ':' + path,
                            snippet.text, resource, authorized.name, tags)

    def ingest_text(self, *, account_name, source_id, text, namespace='', title=None, tags=()):
        return self._ingest(account_name, namespace, source_id, text,
                            EmbeddingResource('inline', text=text), title or source_id, tags)

    def _ingest(self, account, namespace, source_id, text, resource, title, tags):
        if not account or not source_id or not text.strip():
            raise ValueError('account_name, source_id and nonempty text are required')
        if len(text) > self.max_chars:
            raise ValueError('source exceeds max_chars; split it before indexing')
        previous = self.index.find_resource(account_name=account, resource=resource, source_id=source_id)
        identity = [account, resource.resource_type, resource.external_root, resource.relative_path]
        if resource.resource_type == 'inline':
            identity.append(source_id)
        record_id = previous.id if previous else hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        keywords = list(self.keyword_extractor.extract(text))
        details = {'content_hash': content_hash, 'embedding_model': self.model,
                   'title': title, 'tags': list(tags), 'keywords': keywords}
        if previous and previous.metadata == details and previous.resource == resource:
            if previous.namespace != namespace:
                self.index.reclassify(account_name=account, record_id=record_id, namespace=namespace)
            return SemanticIngestionResult(record_id, previous.source_id, 'unchanged', content_hash)
        vectors = self.embeddings.embed([text], model=self.model)
        if len(vectors) != 1:
            raise ValueError('embedding provider returned the wrong vector count')
        # Preserve legacy IDs/source identifiers after migration; locators own identity.
        self.index.upsert(StoredEmbedding(id=record_id, account_name=account, namespace=namespace,
            vector=vectors[0], source_type=previous.source_type if previous else resource.resource_type,
            source_id=previous.source_id if previous else source_id,
            document_id=previous.document_id if previous else source_id,
            model=self.model, provider=self.provider, metadata=details,
            resource=resource, keywords=keywords,
            created_at=previous.created_at if previous else datetime.now(timezone.utc)))
        return SemanticIngestionResult(record_id, previous.source_id if previous else source_id,
                                        'updated' if previous else 'added', content_hash)


class ResourceSemanticMemory(VectorSemanticMemory):
    def __init__(self, *, resolver, **kwargs):
        super().__init__(**kwargs)
        self.resolver = resolver

    def recall(self, request):
        if not request.namespaces:
            namespaces = list(self.index.list_namespaces(request.account_name))
            if not namespaces:
                from .interface import SemanticMemoryResult
                return SemanticMemoryResult(metadata={'selected_count': 0, 'namespaces': []})
            request = replace(request, namespaces=namespaces)
        return super().recall(request)

    def _build_result(self, request, namespaces, matches, *, extra_metadata):
        selected, unavailable = [], []
        for match in matches:
            resource = match.record.resource
            if match.score < request.score_threshold:
                selected.append(match)
                continue
            if resource is None or resource.resource_type not in ('file', 'inline'):
                # Digest/event support needs store-specific ownership/invalidation
                # resolution. Never expose their text snapshots as ordinary files.
                unavailable.append({'id': match.record.id, 'reason': 'unsupported_resource_type'})
                continue
            metadata = dict(match.record.metadata)
            for key in ('path', 'text', 'external_root', 'relative_path'):
                metadata.pop(key, None)
            metadata['resource_type'] = resource.resource_type
            if resource.resource_type == 'file':
                try:
                    path = self.resolver.resolve(ResourceLocation(resource.external_root, resource.relative_path))
                except (OSError, ValueError) as exc:
                    unavailable.append({'id': match.record.id, 'reason': type(exc).__name__})
                    continue
                metadata.update(path=str(path), external_root=resource.external_root,
                                relative_path=resource.relative_path)
            else:
                metadata['text'] = resource.text
            selected.append(EmbeddingMatch(replace(match.record, metadata=metadata), match.score))
        result = super()._build_result(request, namespaces, selected, extra_metadata=extra_metadata)
        result.metadata['unavailable_resources'] = unavailable
        return result
