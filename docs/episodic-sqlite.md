# Episodic SQLite storage

`SqliteEpisodicMemory` has one implementation and one supported schema.

| Table | Key fields | Purpose |
| --- | --- | --- |
| `sessions` | `session_id`, `account_name`, `agent_name`, timestamps | Session metadata and selection. |
| `events` | `event_id`, `session_id`, `sequence`, `kind`, `payload` | Events in insertion order. |
| `event_correlations` | `correlation_id`, `event_id`, `sequence` | Links from request correlation IDs to events. |

Flexible metadata, participants, tags, links, and payloads remain JSON columns.
Foreign keys remove event/link records when a session is deleted. New files use
SQLite's default rollback journal; opening a current file preserves its journal
setting and stored records.

```python
from galet_memory import SqliteEpisodicMemory

memory = SqliteEpisodicMemory("/path/to/chat2.sqlite")
```

Existing relational databases need no conversion. Obsolete `kv`/`logs` files
and incomplete schemas are rejected before schema initialization changes them.
If history can be discarded, supply a new database filename. The package does
not migrate or fall back to another SQLite backend. `LegacySqliteEpisodicMemory`,
`RelationalSqliteEpisodicMemory`, `episodic.sqlite_v2`, and the episodic copy
migration have been removed. `SqliteEpisodicMemory` is the sole SQLite API.

```sql
SELECT session_id, account_name, updated_at FROM sessions ORDER BY updated_at DESC;
SELECT sequence, kind, role, payload FROM events WHERE session_id = ? ORDER BY sequence;
SELECT e.* FROM event_correlations c JOIN events e USING (event_id)
 WHERE c.correlation_id = ? ORDER BY c.sequence;
```

`JsonlEpisodicMemory` is an independently selected filesystem backend, not an
automatic fallback. Both backends recognize only the package's `session_digest`
and `session_reset` visibility boundaries.
