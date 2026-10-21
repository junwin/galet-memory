"""Standalone tests: no providers, galet import, or live database required."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'migrate_embedding_metadata_v3.py'
spec = importlib.util.spec_from_file_location('metadata_v3', SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MetadataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.notes = self.root / 'notes'
        (self.notes / 'journal' / 'vol_3').mkdir(parents=True)
        self.file = self.notes / 'journal' / 'vol_3' / 'note.md'
        self.file.write_text('Mindfulness music Schiller music poetry', encoding='utf-8')
        self.source = self.root / 'old.sqlite'
        self.destination = self.root / 'new.sqlite'
        self.roots = {'obsidian': str(self.notes)}
        self.config = {'namespaces': {'vol_3': {'external_root': 'obsidian',
                       'prefix': 'journal/vol_3', 'source_id_base': 'namespace_prefix'}}}
        conn = sqlite3.connect(self.source)
        conn.executescript('''
            CREATE TABLE embedding_metadata (
                id TEXT PRIMARY KEY, account_name TEXT, namespace TEXT,
                source_type TEXT, source_id TEXT, source_metadata TEXT,
                model TEXT, provider TEXT, dimensions INTEGER, created_at TEXT);
            CREATE TABLE vec_embeddings_v2 (id TEXT PRIMARY KEY, embedding BLOB);
        ''')
        conn.execute('INSERT INTO embedding_metadata VALUES (?,?,?,?,?,?,?,?,?,?)',
                     ('abc', 'junwin', 'vol_3', 'notes', 'note.md', '{}',
                      'text-embedding-3-small', 'openai', 1536, '2026-08-08T16:20:56Z'))
        conn.execute('INSERT INTO vec_embeddings_v2 VALUES (?, ?)', ('abc', b'\x01\x02\x03\x04'))
        conn.commit()
        conn.close()

    def run_migration(self, **kwargs):
        return migration.migrate(self.source, self.destination, self.config, self.roots, **kwargs)

    def update(self, **values):
        conn = sqlite3.connect(self.source)
        conn.execute('UPDATE embedding_metadata SET ' + ','.join(f'{k}=?' for k in values), tuple(values.values()))
        conn.commit()
        conn.close()

    def test_copy_preserves_ids_vectors_and_source_then_namespace_can_change(self):
        before = self.source.read_bytes()
        report = self.run_migration(dry_run=False)
        self.assertTrue(report['written'])
        self.assertEqual(before, self.source.read_bytes())
        with sqlite3.connect(self.destination) as conn:
            self.assertEqual(conn.execute('SELECT id, embedding FROM vec_embeddings_v2').fetchone(), ('abc', b'\x01\x02\x03\x04'))
            row = conn.execute('SELECT external_root, relative_path, keywords FROM embedding_metadata').fetchone()
            self.assertEqual(row[:2], ('obsidian', 'journal/vol_3/note.md'))
            self.assertIn('music', json.loads(row[2]))
            conn.execute("UPDATE embedding_metadata SET namespace='renamed'")
            self.assertEqual(conn.execute('SELECT relative_path FROM embedding_metadata').fetchone()[0], row[1])
            self.assertEqual(conn.execute('SELECT version FROM embedding_schema_version').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT count(*) FROM embedding_metadata_legacy').fetchone()[0], 1)

    def test_dry_run_writes_nothing_and_overwrite_is_rejected(self):
        self.assertEqual(self.run_migration()['ready'], 1)
        self.assertFalse(self.destination.exists())
        self.destination.write_text('existing')
        with self.assertRaises(FileExistsError):
            self.run_migration(dry_run=False)

    def test_books_ambiguity_blocks_whole_migration(self):
        self.update(namespace='books', source_id='BooksRead/note.md')
        self.config['namespaces']['books'] = {'external_root': 'obsidian', 'prefix': 'books', 'source_id_base': 'namespace_prefix'}
        report = self.run_migration(dry_run=False)
        self.assertEqual(len(report['unresolved']), 1)
        self.assertFalse(self.destination.exists())

    def test_mapping_is_explicit_and_prefix_is_not_duplicated(self):
        self.update(source_id='journal/vol_3/note.md')
        self.assertEqual(self.run_migration()['ready'], 1)
        del self.config['namespaces']['vol_3']['source_id_base']
        self.assertEqual(len(self.run_migration()['unresolved']), 1)

    def test_root_relative_override_resolves_books_without_search(self):
        self.update(namespace='books', source_id='BooksRead/note.md')
        self.config['overrides'] = {'abc': {'external_root': 'obsidian', 'relative_path': 'journal/vol_3/note.md'}}
        self.assertEqual(self.run_migration()['ready'], 1)

    def test_traversal_unknown_alias_and_symlink_escape_rejected(self):
        outside = self.root / 'outside.md'
        outside.write_text('outside')
        (self.notes / 'link.md').symlink_to(outside)
        for alias, path in [('obsidian', '../outside.md'), ('unknown', 'note.md'),
                            ('obsidian', '/etc/passwd'), ('obsidian', 'C:/data'),
                            ('obsidian', 'link.md')]:
            with self.subTest(alias=alias, path=path), self.assertRaises((ValueError, FileNotFoundError)):
                migration.resolve_resource(self.roots, alias, path)

    def test_inline_digest_and_fragment_locators(self):
        for kind, extra in [('inline', {}), ('digest', {'session_id': 's', 'digest_id': 'd'}),
                            ('event_fragment', {'session_id': 's', 'source_event_ids': ['e1', 'e2']})]:
            with self.subTest(kind=kind):
                self.update(source_metadata=json.dumps({'text': 'Digest meaningful keywords'}))
                self.config['overrides'] = {'abc': {'resource_type': kind,
                    'relative_path': 'journal/vol_3/note.md', **extra}}
                self.assertEqual(self.run_migration()['ready'], 1)
        self.config['overrides']['abc'].pop('source_event_ids')
        self.assertEqual(len(self.run_migration()['unresolved']), 1)

    def test_ddl_rejects_bad_keywords_dimensions_and_paths(self):
        for override in [{'relative_path': '../escape'}, {'resource_type': 'invalid'}]:
            self.config['overrides'] = {'abc': override}
            self.assertEqual(len(self.run_migration()['unresolved']), 1)
        self.config.pop('overrides')
        self.update(dimensions=-1)
        self.assertEqual(len(self.run_migration()['unresolved']), 1)
        self.update(dimensions=1536, source_metadata='{"keywords": "not an array"}')
        self.assertEqual(len(self.run_migration()['unresolved']), 1)

    def test_real_vec0_table_survives_offline_metadata_migration(self):
        try:
            import sqlite_vec
        except ImportError:
            self.skipTest('optional sqlite-vec package unavailable')
        with sqlite3.connect(self.source) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.execute('DROP TABLE vec_embeddings_v2')
            conn.execute('CREATE VIRTUAL TABLE vec_embeddings_v2 USING vec0(id TEXT PRIMARY KEY, embedding float[1536])')
            vector = struct.pack('<1536f', *([1.0] + [0.0] * 1535))
            conn.execute('INSERT INTO vec_embeddings_v2 VALUES (?,?)', ('abc', vector))
        self.assertTrue(self.run_migration(dry_run=False)['written'])
        with sqlite3.connect(self.destination) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            self.assertEqual(conn.execute('SELECT id, embedding FROM vec_embeddings_v2').fetchone(), ('abc', vector))


if __name__ == '__main__':
    unittest.main()
