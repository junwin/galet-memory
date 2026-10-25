"""Reviewed deferral retains all historical data while keeping v3 recall usable."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

from galet_memory.ports import SqliteVecEmbeddingIndex, StoredEmbedding
from galet_memory.ports.sqlite_resource import SqliteVecResourceIndex

spec = importlib.util.spec_from_file_location('migration_defer', Path(__file__).parents[1] / 'scripts/migrate_embedding_metadata_v3.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.fixture
def store(tmp_path):
    pytest.importorskip('sqlite_vec')
    notes = tmp_path / 'notes'
    notes.mkdir()
    (notes / 'ready.md').write_text('Orchard trees and apples')
    source, destination = tmp_path / 'source.sqlite', tmp_path / 'v3.sqlite'
    with SqliteVecEmbeddingIndex(source) as index:
        for identity in ('ready', 'defer'):
            index.upsert(StoredEmbedding(id=identity, account_name='john', namespace='books',
                source_id=identity + '.md', model='test', provider='fixture',
                vector=[1.0] + [0.0] * 1535, metadata={'text': 'Orchard trees'}))
    roots = {'notes': str(notes)}
    mapping = {'namespaces': {'books': {'external_root': 'notes', 'source_id_base': 'external_root'}}}
    return source, destination, mapping, roots


def test_deferred_migration_preserves_original_and_archives_vectors(store):
    source, destination, mapping, roots = store
    before = source.read_bytes()
    review = migration.migrate(source, destination, mapping, roots)
    assert review['ready'] == 1 and len(review['unresolved']) == 1
    dry = migration.migrate(source, destination, mapping, roots, defer_report=review)
    assert dry['ready'] == 1 and dry['deferred_count'] == 1 and not dry['unresolved']
    assert not destination.exists()
    result = migration.migrate(source, destination, mapping, roots, dry_run=False, defer_report=review)
    assert result['written'] and result['vector_archives']['vec_embeddings_v2_deferred'] == 1
    assert source.read_bytes() == before
    with SqliteVecResourceIndex(destination, initialize_schema=False) as index:
        conn = index._conn
        assert conn.execute('SELECT id FROM embedding_metadata').fetchall() == [('ready',)]
        assert conn.execute('SELECT id FROM vec_embeddings_v2').fetchall() == [('ready',)]
        assert conn.execute('SELECT count(*) FROM embedding_metadata_legacy').fetchone()[0] == 2
        assert conn.execute('SELECT id FROM embedding_migration_deferred').fetchall() == [('defer',)]
        archived = conn.execute('SELECT id,embedding,account_name,namespace,source_type FROM vec_embeddings_v2_deferred').fetchall()
    with SqliteVecEmbeddingIndex(source, initialize_schema=False) as original:
        expected = original._conn.execute("SELECT id,embedding,account_name,namespace,source_type FROM vec_embeddings_v2 WHERE id='defer'").fetchall()
    assert archived == expected


def test_new_or_changed_failures_still_block_apply(store):
    source, destination, mapping, roots = store
    review = migration.migrate(source, destination, mapping, roots)
    # A new missing source was not included in the reviewed errors.
    Path(roots['notes'], 'ready.md').unlink()
    result = migration.migrate(source, destination, mapping, roots, dry_run=False, defer_report=review)
    assert not result['written'] and result['deferred_count'] == 1
    assert [e['id'] for e in result['unresolved']] == ['ready']
    assert not destination.exists()
    review['unresolved'][0]['source_id'] = 'different.md'
    result = migration.migrate(source, destination, mapping, roots, defer_report=review)
    assert result['deferred_count'] == 0 and len(result['unresolved']) == 2


def test_deferral_is_not_automatic_and_resolved_records_are_active(store):
    source, destination, mapping, roots = store
    blocked = migration.migrate(source, destination, mapping, roots, dry_run=False)
    assert not blocked['written'] and not destination.exists()
    Path(roots['notes'], 'defer.md').write_text('A recovered orchard note')
    result = migration.migrate(source, destination, mapping, roots, dry_run=False, defer_report=blocked)
    assert result['written'] and result['ready'] == 2 and result['deferred_count'] == 0


def test_bad_review_and_archive_failure_cannot_modify_source(store, monkeypatch):
    source, destination, mapping, roots = store
    for bad in ({}, {'unresolved': ['wrong']}, {'unresolved': [{'id': ''}]}):
        with pytest.raises(ValueError):
            migration.migrate(source, destination, mapping, roots, defer_report=bad)
    review = migration.migrate(source, destination, mapping, roots)
    before = source.read_bytes()
    def fail(*args):
        raise RuntimeError('archive failed')
    monkeypatch.setattr(migration, 'archive_deferred_vectors', fail)
    with pytest.raises(RuntimeError, match='archive failed'):
        migration.migrate(source, destination, mapping, roots, dry_run=False, defer_report=review)
    assert source.read_bytes() == before and not destination.exists()
