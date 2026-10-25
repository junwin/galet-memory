from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from galet_memory import build_memory, load_memory_config
from galet_memory.examples.configured_memory import initialize_sample, OfflineSampleProvider
from galet_memory.examples.reembed_missing_profile import rescan, main
from galet_memory.ports.sqlite_resource import SqliteVecResourceIndex


@pytest.fixture
def source(tmp_path):
    pytest.importorskip('sqlite_vec')
    path = initialize_sample(tmp_path / 'fixture')
    raw = json.loads(path.read_text())
    raw['memory']['semantic']['metadata_version'] = 3
    config = load_memory_config(raw, base_dir=path.parent)
    notes = config.external_roots['notes']
    (notes / 'second.md').write_text('Second orchard note')
    with build_memory(config, embedding_provider_factory=OfflineSampleProvider) as memory:
        selected = memory.ingest_file(account_name='john', namespace='vol_7', external_root='notes', relative_path='orchard.md')
        untouched = memory.ingest_file(account_name='john', namespace='books', external_root='notes', relative_path='second.md')
        memory._index._conn.execute("UPDATE embedding_metadata SET provider='',model='' WHERE id=?", (selected.record_id,))
        # Audit tables must survive rescanning unchanged.
        memory._index._conn.execute('CREATE TABLE embedding_migration_deferred(id TEXT,reason TEXT)')
        memory._index._conn.execute("INSERT INTO embedding_migration_deferred VALUES ('deferred','missing file')")
    db = config.external_roots['memory_data'] / 'semantic-offline.sqlite'
    return config, db, tmp_path / 'verified.sqlite', selected.record_id, untouched.record_id


def test_dry_run_has_no_provider_calls_or_destination(source):
    config, db, destination, *_ = source
    before = db.read_bytes()
    def fail(profile):
        raise AssertionError('provider must not be constructed')
    report = rescan(config, source=db, destination=destination, account='john', namespace='vol_7',
                    expected_count=1, provider_factory=fail)
    assert report['selected'] == 1 and report['unchanged'] == 1 and not report['errors']
    assert not report['written'] and not destination.exists() and db.read_bytes() == before


def test_apply_preserves_ids_untouched_vector_and_audits(source):
    config, db, destination, selected, untouched = source
    profile = config.embedding_profiles[config.semantic.embedding_profile]
    calls = []
    class Provider:
        def __init__(self, p): self.profile = p
        def embed(self, texts, *, model):
            calls.extend(texts)
            return [[0.0, 1.0] + [0.0] * 1534 for _ in texts]
    before = db.read_bytes()
    with SqliteVecResourceIndex(db, initialize_schema=False) as index:
        vector_before = index._conn.execute('SELECT embedding FROM vec_embeddings_v2 WHERE id=?', (untouched,)).fetchone()[0]
        record_before = index.get_record(account_name='john', namespace='vol_7', record_id=selected)
    report = rescan(config, source=db, destination=destination, account='john', namespace='vol_7',
                    expected_count=1, apply=True, provider_factory=Provider)
    assert report['written'] and report['updated'] == 1 and len(calls) == 1
    assert db.read_bytes() == before
    with SqliteVecResourceIndex(destination, initialize_schema=False) as index:
        assert index._conn.execute('SELECT embedding FROM vec_embeddings_v2 WHERE id=?', (untouched,)).fetchone()[0] == vector_before
        record = index.get_record(account_name='john', namespace='vol_7', record_id=selected)
        assert record.id == record_before.id and record.source_id == record_before.source_id
        assert record.document_id == record_before.document_id and record.created_at == record_before.created_at
        assert record.resource.external_root == record_before.resource.external_root
        assert record.resource.relative_path == record_before.resource.relative_path
        assert index._conn.execute('SELECT DISTINCT provider,model,dimensions FROM embedding_metadata').fetchall() == [profile.space]
        assert index._conn.execute('SELECT * FROM embedding_migration_deferred').fetchall() == [('deferred','missing file')]
    # Configured assembly now accepts a store that previously failed compatibility checks.
    from galet_memory.configuration import DatabaseSettings, ResourceLocation
    verified_config = replace(config, external_roots={**config.external_roots, 'verified':destination.parent},
        semantic=replace(config.semantic, database=DatabaseSettings(ResourceLocation('verified', destination.name))))
    with build_memory(verified_config, embedding_provider_factory=OfflineSampleProvider) as runtime:
        assert runtime.recall(account_name='john', query='orchard', use_embeddings=False).documents


def test_bad_files_or_count_fail_before_calls(source):
    config, db, destination, *_ = source
    (config.external_roots['notes'] / 'orchard.md').unlink()
    def fail(profile): raise AssertionError('provider must not be constructed')
    report = rescan(config, source=db, destination=destination, account='john', namespace='vol_7',
                    expected_count=91, apply=True, provider_factory=fail)
    assert len(report['errors']) == 2 and not destination.exists()


def test_provider_failure_and_dimension_mismatch_leave_no_output(source):
    config, db, destination, *_ = source
    before = db.read_bytes()
    class Invalid(OfflineSampleProvider):
        def embed(self, texts, *, model): return [[1.0]] * len(texts)
    with pytest.raises(ValueError, match='dimensions'):
        rescan(config, source=db, destination=destination, account='john', namespace='vol_7',
               expected_count=1, apply=True, provider_factory=Invalid)
    assert db.read_bytes() == before and not destination.exists()
    assert not list(destination.parent.glob('.memory-rescan-*'))


def test_unknown_rows_outside_selection_are_not_silently_relabelled(source):
    config, db, destination, _, untouched = source
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE embedding_metadata SET provider='' WHERE id=?", (untouched,))
    report = rescan(config, source=db, destination=destination, account='john', namespace='vol_7')
    assert report['selected'] == 1 and any('unselected record' in e for e in report['errors'])


def test_cli_refuses_overwrite_and_apply_without_expected_count(source, tmp_path, capsys):
    config, db, destination, *_ = source
    with pytest.raises(ValueError, match='expected-count'):
        rescan(config, source=db, destination=destination, account='john', namespace='vol_7', apply=True)
    assert main(['--config', str(db.parent.parent / 'memory.json'), '--source', str(db),
                 '--destination', str(destination), '--account', 'john', '--namespace', 'vol_7',
                 '--expected-count', '1']) == 0
    assert json.loads(capsys.readouterr().out)['selected'] == 1
    destination.write_text('keep me')
    with pytest.raises(FileExistsError):
        rescan(config, source=db, destination=destination, account='john', namespace='vol_7')
    assert destination.read_text() == 'keep me'
