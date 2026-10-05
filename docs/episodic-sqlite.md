# Episodic SQLite storage

`SqliteEpisodicMemory` implements the explicit session, event and digest contracts.
Schema version 2 is intentionally incompatible with the prior episodic schemas.
Use a fresh file; unsupported databases are rejected without modification.
There is no migration or legacy reader.

| Table | Purpose |
| --- | --- |
| store_info | Schema version and persisted cursor-signing key. |
| sessions | Account-owned metadata, timestamps, tags and metadata; no required agent. |
| events | Event ID, session ID, append sequence, occurrence/stored UTC timestamps, role, actor, kind, JSON content and metadata. |
| event_correlations | Atomic event-to-correlation associations. |

Foreign keys cascade event/link deletion. Correlations are currently many-to-many
and exposed as `Event.correlation_ids`; retaining the mapping avoids discarding
associations permitted by the previous design while #30 investigates cardinality.
Append order comes from events.sequence, not link insertion order.

SQLite uses its rollback journal and BEGIN IMMEDIATE for writes. Ownership,
expected-tail guards, event insertion and correlations are one transaction.
Read operations take a consistent SQLite snapshot. Signed cursors bind account,
session and filters; period/digest cursors also bind the initial append high-water
mark. Invalidation visibility is reevaluated on continuation, rather than reviving
records hidden after a page was obtained.

```sql
SELECT session_id, account_name, updated_at FROM sessions ORDER BY updated_at DESC;
SELECT sequence, kind, role, payload FROM events WHERE session_id = ? ORDER BY sequence;
SELECT e.* FROM event_correlations c JOIN events e USING (event_id)
 WHERE c.correlation_id = ? AND e.session_id = ? ORDER BY e.sequence;
```

These SQL queries show raw storage. Normal event reads exclude invalidated events
and internal controls. Curation has separate account-scoped active/transcript/audit
snapshots; audit originals must never become prompt or digest source input.

`JsonlEpisodicMemory` implements the same semantics in episodic-v2.jsonl. A complete
snapshot is written to a temporary file, fsynced and atomically replaced under a
reentrant cross-process lock. Its old per-session files are not read. Use SQLite
for larger stores; JSONL loads and replaces the complete small-store snapshot.
