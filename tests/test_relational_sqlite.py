import sqlite3

import pytest

from galet_memory.episodic.interface import EpisodicEvent
from galet_memory.episodic.management import EpisodicConcurrencyError
from galet_memory.episodic.sqlite import EpisodicCompatibilityError, LegacySqliteEpisodicMemory, SqliteEpisodicMemory
from galet_memory.episodic.sqlite_v2 import RelationalSqliteEpisodicMemory
from galet_memory.migrations.migrate_episodic_sqlite import migrate_episodic_sqlite


def test_relational_store_is_inspectable_and_cascades(tmp_path):
    path = tmp_path / 'new.sqlite'
    with RelationalSqliteEpisodicMemory(path) as store:
        store.create_session(account_name='a', agent_name='lucy', session_id='s')
        first = store.append_event('s', EpisodicEvent(role='user', content='hello'))
        second = store.append_event_if_tail(
            's', EpisodicEvent(role='assistant', content={'answer': 'hi'}),
            expected_last_event_id=first.event_id,
        )
        with pytest.raises(EpisodicConcurrencyError):
            store.append_event_if_tail('s', EpisodicEvent(role='user', content='bad'),
                                       expected_last_event_id=first.event_id)
        store.link_event('run', 's', first.event_id)
        store.link_event('run', 's', second.event_id)
        assert store.update_session('s', {'tags': None}).tags == []
        assert [e.event_id for e in store.get_events_by_correlation('run')] == [first.event_id, second.event_id]
        with sqlite3.connect(path) as conn:
            assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
            assert conn.execute('SELECT count(*) FROM events WHERE session_id=?', ('s',)).fetchone()[0] == 2
        store.delete_session('s')
        assert store.get_events_by_correlation('run') == []


def test_migration_preserves_metadata_order_payloads_and_links(tmp_path):
    old_path, new_path = tmp_path / 'old.sqlite', tmp_path / 'new.sqlite'
    with LegacySqliteEpisodicMemory(old_path) as old:
        original = old.create_session(account_name='a', agent_name='lucy', session_id='s', tags=['keep'])
        events = old.add_events('s', [EpisodicEvent(role='user', content='hello'),
                                     EpisodicEvent(role='assistant', content={'answer': 'hi'})])
        old.link_event('run', 's', events[0].event_id)
        old.link_event('run', 's', events[1].event_id)
        original = old.get_session('s', event_scope='all')
    assert migrate_episodic_sqlite(old_path, new_path) == {'sessions': 1, 'events': 2, 'correlations': 2, 'skipped_orphan_links': 0}
    with RelationalSqliteEpisodicMemory(new_path) as new:
        current = new.get_session('s', event_scope='all')
        assert current.updated_at == original.updated_at
        assert current.tags == ['keep']
        assert [e.content for e in current.events] == ['hello', {'answer': 'hi'}]
        assert [e.event_id for e in new.get_events_by_correlation('run')] == [e.event_id for e in events]
    with pytest.raises(FileExistsError):
        migrate_episodic_sqlite(old_path, new_path)


def test_migration_reports_dangling_links_and_preserves_valid_data(tmp_path):
    old_path, new_path = tmp_path / 'old.sqlite', tmp_path / 'new.sqlite'
    with LegacySqliteEpisodicMemory(old_path) as old:
        old.create_session(account_name='a', agent_name='lucy', session_id='s')
        old.link_event('run', 's', 'missing')
    assert migrate_episodic_sqlite(old_path, new_path) == {
        'sessions': 1, 'events': 0, 'correlations': 0, 'skipped_orphan_links': 1,
    }
    with RelationalSqliteEpisodicMemory(new_path) as new:
        assert new.get_session('s') is not None
        assert new.get_events_by_correlation('run') == []


def test_relational_backend_refuses_legacy_file(tmp_path):
    old_path = tmp_path / 'old.sqlite'
    with LegacySqliteEpisodicMemory(old_path):
        pass
    with pytest.raises(EpisodicCompatibilityError, match='migrate'):
        RelationalSqliteEpisodicMemory(old_path)


def test_public_backend_opens_existing_legacy_without_switching_schema(tmp_path):
    path = tmp_path / 'old.sqlite'
    with LegacySqliteEpisodicMemory(path) as old:
        old.create_session(account_name='a', agent_name='lucy', session_id='s')
    with SqliteEpisodicMemory(path) as store:
        assert store.get_session('s') is not None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='kv'").fetchone()
