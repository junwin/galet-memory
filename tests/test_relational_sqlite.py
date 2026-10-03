import sqlite3

import pytest

from galet_memory.episodic.interface import EpisodicEvent
from galet_memory.episodic.management import EpisodicConcurrencyError
from galet_memory.episodic.sqlite import EpisodicCompatibilityError, SqliteEpisodicMemory


def test_relational_store_is_inspectable_and_cascades(tmp_path):
    path = tmp_path / 'new.sqlite'
    with SqliteEpisodicMemory(path) as store:
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



@pytest.mark.parametrize('initialize', [True, False])
@pytest.mark.parametrize('old_table', ['kv', 'logs'])
def test_sqlite_refuses_obsolete_database_without_modifying_it(tmp_path, initialize, old_table):
    path = tmp_path / 'old.sqlite'
    with sqlite3.connect(path) as conn:
        conn.execute(f'CREATE TABLE {old_table}(value TEXT)')
        conn.execute(f'INSERT INTO {old_table} VALUES (?)', ('retained',))
    original = path.read_bytes()
    with pytest.raises(EpisodicCompatibilityError, match='fresh database'):
        SqliteEpisodicMemory(path, initialize_schema=initialize)
    assert path.read_bytes() == original
    with sqlite3.connect(path) as conn:
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {old_table}


def test_existing_relational_database_reopens_without_schema_change(tmp_path):
    path = tmp_path / 'chat.sqlite'
    with SqliteEpisodicMemory(path) as store:
        store.create_session(account_name='a', agent_name='lucy', session_id='s')
        event = store.append_event('s', EpisodicEvent('user', 'keep'))
        store.link_event('correlation', 's', event.event_id)
    with SqliteEpisodicMemory(path, initialize_schema=False) as store:
        assert store.get_session('s').events[0].event_id == event.event_id
        assert store.get_events_by_correlation('correlation')[0].content == 'keep'
    with sqlite3.connect(path) as conn:
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            'sessions', 'events', 'event_correlations', 'sqlite_sequence'}


def test_incomplete_schema_is_rejected_before_initialization(tmp_path):
    path = tmp_path / 'incomplete.sqlite'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE sessions(session_id TEXT)')
    original = path.read_bytes()
    with pytest.raises(EpisodicCompatibilityError, match='incompatible episodic table'):
        SqliteEpisodicMemory(path)
    assert path.read_bytes() == original
