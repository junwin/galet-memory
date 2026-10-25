"""Opt-in metadata v3 adapter; never upgrades an existing database on open."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from importlib.resources import files
import json
import math
import re

from .embeddings import EmbeddingRecord, EmbeddingResource
from .sqlite_vec import SqliteVecEmbeddingIndex, EmbeddingCompatibilityError, _VECTOR_V2_DDL
from ..configuration import validate_relative_path


class SqliteVecResourceIndex(SqliteVecEmbeddingIndex):
    metadata_version = 3

    def _initialize_schema(self):
        tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables:
            # Existing stores are validated below, never repaired or altered here.
            return
        ddl = files('galet_memory').joinpath('schema/embedding_metadata_v3.sql').read_text(encoding='utf-8')
        if self.vector_table != 'vec_embeddings_v2':
            raise EmbeddingCompatibilityError('new v3 stores require vec_embeddings_v2')
        self._conn.executescript('BEGIN;\n' + ddl + '\n' + _VECTOR_V2_DDL + ';\nCOMMIT;')

    def _validate_schema(self):
        table = self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='embedding_schema_version'").fetchone()
        if not table:
            raise EmbeddingCompatibilityError('metadata v3 required; run the explicit offline migration on a copy')
        version = self._conn.execute(
            "SELECT version FROM embedding_schema_version WHERE component='embedding_metadata'"
        ).fetchone()
        if version != (3,):
            raise EmbeddingCompatibilityError('unsupported embedding metadata version')
        columns = {r[1] for r in self._conn.execute('PRAGMA table_info(embedding_metadata)')}
        if not {'resource_type', 'external_root', 'relative_path', 'session_id', 'digest_id',
                'source_event_ids', 'representation', 'text', 'keywords', 'dimensions',
                'provider', 'model', 'namespace', 'document_id', 'created_at'} <= columns:
            raise EmbeddingCompatibilityError('incomplete v3 metadata schema')
        super()._validate_schema()
        sql = self._conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (self.vector_table,)).fetchone()[0]
        if not re.search(r'USING\s+vec0\s*\(', sql, re.I) or not re.search(r'float\s*\[\s*1536\s*\]', sql, re.I):
            raise EmbeddingCompatibilityError('v3 runtime requires a 1536-dimension vec0 vector table')
        if self._conn.execute('SELECT 1 FROM embedding_metadata WHERE dimensions <> 1536 LIMIT 1').fetchone():
            raise EmbeddingCompatibilityError('metadata/vector dimensions differ')
        for first, second in (('embedding_metadata', self.vector_table), (self.vector_table, 'embedding_metadata')):
            if self._conn.execute(f'SELECT id FROM {first} EXCEPT SELECT id FROM {second} LIMIT 1').fetchone():
                raise EmbeddingCompatibilityError('vector/metadata IDs differ; verify the migrated copy')

    def _metadata_by_id(self, record_ids):
        if not record_ids:
            return {}
        cursor = self._conn.execute(
            'SELECT * FROM embedding_metadata WHERE id IN (' + ','.join('?' for _ in record_ids) + ')',
            list(record_ids))
        names = [c[0] for c in cursor.description]
        result = {}
        for row in cursor:
            values = dict(zip(names, row))
            resource = EmbeddingResource(**{name: (
                tuple(json.loads(values[name])) if name == 'source_event_ids' else values[name]
            ) for name in EmbeddingResource.__dataclass_fields__})
            metadata = json.loads(values['source_metadata'])
            # No legacy path/text/keyword fallback: v3 columns are authoritative.
            for key in (*EmbeddingResource.__dataclass_fields__, 'path', 'keywords'):
                metadata.pop(key, None)
            metadata['keywords'] = json.loads(values['keywords'])
            result[values['id']] = EmbeddingRecord(values['id'], values['source_id'],
                values['source_type'], metadata, resource, values['namespace'],
                values['document_id'], datetime.fromisoformat(values['created_at'].replace('Z', '+00:00')))
        return result

    def find_resource(self, *, account_name, resource, source_id=''):
        """Find migrated IDs by locator, preserving them on subsequent ingestion."""
        if resource.resource_type == 'inline':
            sql = "resource_type='inline' AND source_id=?"
            params = [source_id]
        else:
            sql = 'resource_type=? AND external_root=? AND relative_path=?'
            params = [resource.resource_type, resource.external_root, resource.relative_path]
        with self._lock:
            rows = self._conn.execute('SELECT id FROM embedding_metadata WHERE account_name=? AND ' + sql,
                                      [account_name, *params]).fetchall()
            if len(rows) > 1:
                raise EmbeddingCompatibilityError('ambiguous resource identity; resolve duplicate migrated rows explicitly')
            return self._metadata_by_id([r[0] for r in rows]).get(rows[0][0]) if rows else None

    def reclassify(self, *, account_name, record_id, namespace):
        """Change grouping atomically, preserving the ID, vector and source."""
        with self._lock:
            self._conn.execute('BEGIN')
            try:
                found = self._conn.execute('SELECT 1 FROM embedding_metadata WHERE id=? AND account_name=?',
                                          (record_id, account_name)).fetchone()
                if not found:
                    raise KeyError(record_id)
                self._conn.execute(f'UPDATE {self.vector_table} SET namespace=? WHERE id=?', (namespace, record_id))
                self._conn.execute('UPDATE embedding_metadata SET namespace=? WHERE id=?', (namespace, record_id))
                self._conn.execute('COMMIT')
            except BaseException:
                self._conn.execute('ROLLBACK')
                raise

    def upsert(self, embedding):
        resource = embedding.resource
        if not isinstance(resource, EmbeddingResource):
            raise ValueError('v3 writes require an explicit EmbeddingResource')
        if resource.resource_type not in ('file', 'inline', 'digest', 'event_fragment'):
            raise ValueError('unsupported resource_type')
        if resource.resource_type != 'inline':
            if not isinstance(resource.external_root, str) or not resource.external_root.strip():
                raise ValueError('external_root is required')
            if validate_relative_path(resource.relative_path) != resource.relative_path:
                raise ValueError('relative_path must be normalized')
        if not all(isinstance(v, str) and v.strip() for v in resource.source_event_ids):
            raise ValueError('source_event_ids must contain nonempty strings')
        keywords = embedding.keywords
        if keywords is None or isinstance(keywords, (str, bytes)) or any(not isinstance(k, str) or not k.strip() for k in keywords):
            raise ValueError('v3 keywords must be supplied as an array of strings')
        if len(embedding.vector) != 1536 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                               or not math.isfinite(v) for v in embedding.vector):
            raise EmbeddingCompatibilityError('v3 vectors must contain 1536 finite numbers')
        metadata = dict(embedding.metadata)
        for key in (*EmbeddingResource.__dataclass_fields__, 'path', 'keywords'):
            metadata.pop(key, None)
        created = embedding.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        values = {
            'id': embedding.id, 'account_name': embedding.account_name, 'namespace': embedding.namespace,
            'source_type': embedding.source_type, 'source_id': embedding.source_id,
            'document_id': embedding.document_id, 'model': embedding.model, 'provider': embedding.provider,
            'dimensions': len(embedding.vector), 'source_metadata': json.dumps(metadata),
            'keywords': json.dumps(list(keywords)), 'created_at': created.isoformat(),
            **asdict(resource),
        }
        values['source_event_ids'] = json.dumps(list(resource.source_event_ids))
        names = list(values)
        with self._lock:
            self._conn.execute('BEGIN')
            try:
                owner = self._conn.execute('SELECT account_name FROM embedding_metadata WHERE id=?', (embedding.id,)).fetchone()
                if owner and owner[0] != embedding.account_name:
                    raise PermissionError('embedding ID belongs to another account')
                self._conn.execute(f'DELETE FROM {self.vector_table} WHERE id=?', (embedding.id,))
                self._conn.execute(f'INSERT INTO {self.vector_table}(id, account_name, namespace, source_type, embedding) VALUES (?,?,?,?,?)',
                    (embedding.id, embedding.account_name, embedding.namespace, embedding.source_type, json.dumps(list(embedding.vector))))
                self._conn.execute('INSERT INTO embedding_metadata (' + ','.join(names) + ') VALUES (' +
                    ','.join('?' for _ in names) + ') ON CONFLICT(id) DO UPDATE SET ' +
                    ','.join(name + '=excluded.' + name for name in names if name != 'id'), list(values.values()))
                self._conn.execute('COMMIT')
            except BaseException:
                self._conn.execute('ROLLBACK')
                raise

    def store_keywords(self, *, account_name, namespace, record_id, keywords):
        if isinstance(keywords, (str, bytes)) or any(not isinstance(k, str) or not k.strip() for k in keywords):
            raise ValueError('keywords must be an array of strings')
        return super().store_keywords(account_name=account_name, namespace=namespace,
                                      record_id=record_id, keywords=keywords)
