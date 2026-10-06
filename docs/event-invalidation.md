# Invalidate an exchange by correlation ID

Both `SqliteEpisodicMemory` and `JsonlEpisodicMemory` implement the public
`EventStore.invalidate_exchange` operation:

```python
snapshot = memory.get_session(account_name="junwin", session_id=session_id)
result = memory.invalidate_exchange(
    account_name="junwin",
    session_id=session_id,
    correlation_id=correlation_id,
    expected_last_event_id=snapshot.last_event_id,  # optional conditional write
)
print(result.status, result.event_count, result.invalidated_digest_ids)
```

The account must own the session. The package resolves the existing correlation
links internally and invalidates all linked user, assistant, tool, media and
processing events in that session. Other sessions/accounts and correlations
remain intact. A trace ID is not a deletion target; child correlations require
explicit caller grouping.

| Outcome | Meaning |
| --- | --- |
| `invalidated` | One `events_invalidated` control event was appended. |
| `already_invalidated` | The original marker/result is returned; nothing is appended. |
| `not_found` | No eligible events are linked in this session; nothing is appended. |
| `EpisodicSessionNotFoundError` | Missing session or account ownership mismatch. |
| `EpisodicConcurrencyError` | A supplied non-None expected tail differs from the actual append tail. |
| `EpisodicCorrelationInvalidatedError` | A subsequent `append_event` attempts to extend an invalidated correlation. |

A repeated successful invalidation takes precedence over an old expected tail,
so retries remain idempotent. The typed result contains original target
`event_ids`, `event_count`, stored `invalidated_digest_ids`, `marker_event_id`,
and `unprovenanced_digests_invalidated`. The latter records that external
artifacts without reliable event provenance are excluded for the session;
external artifacts are not enumerated or physically removed from arbitrary
host indexes.

## Visibility and persistence

Original event rows and correlation links remain stored. A destructive
`clear_session_events` discards transcript events but preserves invalidation markers,
so previously excluded external digests stay excluded. The explicit-interface schema requires fresh storage; no migration, old-format
reader or restoration API is introduced. Normal recent/period/single-event/exchange reads exclude invalidated records
and internal control markers. `get_audit_snapshot` returns originals and markers
for account-scoped internal inspection. `get_session` returns metadata only. Raw reads must not be used as prompt or digest input.

`Session.last_event_id` always reflects the true raw append tail from
that read, even if its last visible event is different. Use this field for
conditional writes. `CurationService` uses the same snapshot's tail and refuses
stale archive writes if invalidation occurs during digest generation.

SQLite serializes resolution and marker insertion with `BEGIN IMMEDIATE`.
JSONL uses a reentrant store file lock across instances/processes and atomically
replaces one fsynced snapshot containing events, correlations and metadata. Locks are advisory; callers must use the package APIs
rather than editing storage directly.

## Digests and embeddings

Stored interval digests whose `source_event_ids` intersect invalidated data are
excluded, transitively, including digests derived from other affected digests.
Digests without provenance are conservatively excluded. Archive/reset boundaries
are evaluated against the raw event sequence before filtering, so hiding an
archive digest never reveals its archived messages. Cumulative generation skips
affected digests and still respects resets. An invalidated archive cannot be
republished through an idempotent archive retry or `retry_publication`.

`DigestPublisher.publish` now accepts `source_event_ids`. Custom implementations
must accept this keyword and preserve it in recall metadata. The built-in
`EmbeddingDigestPublisher` records those IDs automatically. Digest search
validates the session's ownership and digest provenance against persisted
markers before returning any archived digest. The source session must exist and belong
to the requesting account; orphaned or unowned digest records are excluded,
including stale publications
that finish after invalidation. New digests built from clean source events
remain eligible. Old preview/cumulative/overflow embeddings without sufficient
provenance are suppressed for that session. Original published files and
embedding records can remain stored; suppression persists after reopening.

For semantic searches that include digest embeddings, configure
`VectorSemanticMemory(..., episodic_store=memory)`. Digest records are suppressed
when no owning episodic store is configured. Ordinary semantic documents retain
their existing behavior. `EmbeddingDigestRecall` is a low-level index/text-loader
adapter; application prompt retrieval must use the owning episodic store's
`recall`, or apply `is_digest_valid` when integrating another retrieval path.
Direct external index queries and raw file reads cannot enforce memory rules.

The configured session overflow file is discarded during invalidation. Overflow
writes track the latest invalidation marker in a sidecar and never merge an old
file after invalidation, even if cleanup was interrupted. A file-cleanup error
may be reported after the marker has committed; event visibility is already
updated and retrying invalidation is safe.

## Application responsibility

Lucy must reject deleting a running exchange and synchronize that check with
its execution state. Appending an event and linking it are separate public
operations: the application must prevent that sequence racing deletion. Late
links to invalidated correlations are rejected; already appended unlinked events
cannot be identified as belonging to a correlation until a link is supplied.
This package change supplies the memory operation; the Lucy endpoint, identifier
rename and gptChum UI remain separate work.
