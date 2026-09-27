"""Copy Lucy's kv/logs episodic records into an inspectable SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from galet_memory.episodic.sqlite import SqliteEpisodicMemory
from galet_memory.episodic.sqlite_v2 import RelationalSqliteEpisodicMemory


def migrate_episodic_sqlite(source: str | Path, destination: str | Path) -> dict[str, int]:
    """Snapshot a live source, validate every episodic pointer, and publish a new file.

    Neither the source nor an existing destination is modified. A failed copy
    leaves no destination. The SQLite backup API includes committed WAL content.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or not source.is_file():
        raise ValueError('source must exist and differ from destination')
    if destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_name(destination.name + '.migrating')
    snapshot = destination.with_name(destination.name + '.snapshot')
    if temporary.exists() or snapshot.exists():
        raise FileExistsError('migration temporary file already exists')
    counts = {'sessions': 0, 'events': 0, 'correlations': 0}
    try:
        with sqlite3.connect(source) as live, sqlite3.connect(snapshot) as backup:
            live.backup(backup)
        with SqliteEpisodicMemory(snapshot, initialize_schema=False) as old, \
             RelationalSqliteEpisodicMemory(temporary) as new:
            with old._lock:
                rows = old._conn.execute(
                    "SELECT key FROM kv WHERE key LIKE 'sessions/%/meta.json' ORDER BY key"
                ).fetchall()
                for (key,) in rows:
                    session_id = key[len('sessions/'):-len('/meta.json')]
                    session = old.get_session(session_id, event_scope='all')
                    if session is None or session.session_id != session_id:
                        raise ValueError(f'invalid session metadata: {key}')
                    with new._lock, new._conn:
                        new._write_session(session)
                        new._insert_events(session_id, session.events)
                        new._write_session(session)  # Preserve original updated_at.
                    counts['sessions'] += 1
                    counts['events'] += len(session.events)
                links = old._conn.execute(
                    "SELECT key, line FROM logs WHERE key LIKE 'correlations/%.jsonl' ORDER BY key, seq"
                ).fetchall()
                for key, line in links:
                    correlation_id = key[len('correlations/'):-len('.jsonl')]
                    pointer = json.loads(line)
                    with new._lock, new._conn:
                        row = new._conn.execute(
                            'SELECT session_id FROM events WHERE event_id=?', (pointer['event_id'],)
                        ).fetchone()
                        if row is None or row[0] != pointer['session_id']:
                            raise ValueError(f'dangling correlation link in {key}: {pointer!r}')
                        new._conn.execute(
                            'INSERT INTO event_correlations(correlation_id,event_id,linked_at) VALUES (?,?,?)',
                            (correlation_id, pointer['event_id'], pointer['ts']),
                        )
                    counts['correlations'] += 1
            bad = new._conn.execute('PRAGMA foreign_key_check').fetchall()
            if bad:
                raise ValueError(f'foreign key errors: {bad!r}')
        temporary.replace(destination)
        return counts
    finally:
        snapshot.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(migrate_episodic_sqlite(args.source, args.destination))


if __name__ == '__main__':
    main()
