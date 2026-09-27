# Relational episodic SQLite store

`SqliteEpisodicMemory` uses relational tables for newly created databases and
still reads and writes existing Lucy `kv`/`logs` databases. The schema is
selected inside galet-memory from the file itself; callers keep the same class
and interface. New databases use SQLite's default rollback journal. Existing
legacy files retain their WAL setting until migrated.

| Table | Key fields | Purpose |
| --- | --- | --- |
| `sessions` | `session_id`, `account_name`, `agent_name`, timestamps | Browse and select sessions without parsing JSON. |
| `events` | `event_id`, `session_id`, `sequence`, `kind`, `payload` | Read events in insertion order, with FK cleanup. |
| `event_correlations` | `correlation_id`, `event_id`, `sequence` | Retrieve linked events by run/correlation ID. |

Flexible metadata, participants, tags, links, and event payloads remain JSON
columns. The tables do not model UC1 run lineage or full diagnostic tool
arguments; those are separate changes.

## Migration

Stop writes while planning the switch, or plan a second copy after stopping
Lucy. A snapshot of a running database is internally consistent, but writes
after that snapshot will not appear in the destination.

```bash
python -m galet_memory.migrations.migrate_episodic_sqlite \
  /path/to/chat2.sqlite /path/to/chat2-relational.sqlite
```

The migration uses SQLite's backup API, so committed WAL content is included.
It refuses to overwrite a destination, reports counts, checks foreign keys,
and fails on dangling or duplicate correlation links. The old database remains
intact; a failed migration does not publish the destination. If old sessions
have been deleted but correlation sidecars still reference them, remove or
repair those orphaned pointers in a **copy** before rerunning the migration.

Inspect the resulting file with ordinary SQL:

```sql
SELECT session_id, account_name, updated_at FROM sessions ORDER BY updated_at DESC;
SELECT sequence, kind, role, payload FROM events WHERE session_id = ? ORDER BY sequence;
SELECT e.* FROM event_correlations c JOIN events e USING (event_id)
 WHERE c.correlation_id = ? ORDER BY c.sequence;
```

After comparing counts and representative histories, stop the application and
replace its configured episodic database with the validated new file, keeping
a backup of the old one. The existing `SqliteEpisodicMemory` call opens either
schema and needs no application code change. For direct diagnostics, the
`RelationalSqliteEpisodicMemory` class refuses to open a legacy `kv` database.
`JsonlEpisodicMemory` remains an independent storage implementation.
