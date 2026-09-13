# Curation and episodic event visibility

Status: proposed
Date: 2026-09-13

## Context

Curation currently serves two useful purposes:

1. Producing a digest of a conversation.
2. Archiving the earlier part of a conversation so a normal chat view starts
   with that digest rather than replaying every older event.

Lucy's current archive implementation writes the old events to a JSONL file,
clears the active session, and appends a summary event. That makes archiving a
destructive, multi-step operation. A failure between those steps can leave a
partial session, and correlation records can point to events that are no
longer present.

For the current phase, "archive" means changing which events are visible by
default. It does **not** mean physically removing old events from SQLite.

## Decisions

### Events remain immutable

Creating a digest or logically archiving a conversation does not delete,
rewrite, or reappend existing events. Original event identifiers, timestamps,
ordering, metadata, and correlation links remain valid.

### A digest can establish a visibility boundary

An archive operation appends a digest event and marks it as the latest
visibility boundary. A proposed neutral representation is:

```python
EpisodicEvent(
    role="system",
    actor="curation",
    kind="session_digest",
    content=digest_text,
    metadata={
        "visibility_boundary": True,
        "curation_version": 1,
    },
)
```

During migration, readers may also recognise Lucy's existing archive summary:

```python
event.kind == "summary"
and event.metadata.get("curation_mode") == "archive"
```

The compatibility rule is transitional. New applications should not need to
know Lucy's event terminology.

### Normal reads return the active event segment

The active segment starts at the most recent visibility-boundary digest and
includes that digest plus every later event. If no boundary exists, all events
are active.

Given this stored history:

```text
event 1
event 2
event 3
digest A       (visibility boundary)
event 4
event 5
```

a normal chat read returns:

```text
digest A
event 4
event 5
```

The older events remain available through an explicit complete-history read.

### Repeated curation is cumulative

When a conversation is curated again, the new digest is produced from the
current active segment: the previous digest plus the events that followed it.

```text
digest A
event 4
event 5
digest B       (new visibility boundary)
event 6
```

The next normal read returns `digest B` and `event 6`. A complete-history read
still returns every event.

### Digest publication is separate from visibility

Generating a digest, publishing a Markdown artifact, embedding a digest for
semantic retrieval, and establishing a chat visibility boundary are distinct
operations. A semantic-index failure must not corrupt episodic history or
leave a partially archived session.

At minimum the use cases are:

- **Preview digest:** generate text without changing storage.
- **Publish digest:** save and optionally embed the digest without changing
  event visibility.
- **Archive conversation:** append a boundary digest without deleting earlier
  events. Artifact publication may be requested independently.

## API direction

Episodic reads need an explicit scope. The exact spelling remains to be proven
through Lucy integration, but the intended behaviour is equivalent to:

```python
get_session(
    session_id,
    include_events=True,
    event_scope="active",  # "active", "all", or "archived"
)
```

| Scope | Returned events |
| --- | --- |
| `active` | Latest boundary digest and all later events; all events if no boundary exists |
| `all` | Complete immutable history |
| `archived` | Events before the latest boundary digest |

Normal chat endpoints should use `active`. Administrative, export, recovery,
and future physical-archive operations should request `all` explicitly.

Prompt-time recall and user-facing chat history are separate consumers. Chat
pagination should respect the visibility boundary. Prompt construction may
then apply its own event or token budget to the active segment.

## Ownership

`galet-memory` should own:

- Neutral episodic event and session models.
- SQLite persistence.
- Visibility-boundary recognition and scoped reads.
- Atomic appending of the boundary digest.

A curation service may also become reusable within `galet-memory`, but it
should depend on explicit ports for summarization, artifact publication, and
semantic indexing. SQLite persistence must not directly call an LLM or write
Markdown digest files.

Lucy should own:

- The `curate_chat` tool definition and request authorization.
- Selection of the LLM and summarization policy.
- Application configuration and dependency injection.
- User-facing endpoint behaviour.

Account identity must come from trusted execution context. Resolving a session
by identifier must verify that the session belongs to that account before
curation or artifact publication proceeds.

## Future physical archiving

A future phase may introduce a true archive for space management: old events
would be copied to durable archive storage and then removed from the active
SQLite database. This is deliberately **not covered by the current phase**.

Physical archiving will require separate design decisions, including:

- The durable archive format and storage provider.
- An atomic or resumable copy-verify-delete protocol.
- Retention periods and size thresholds.
- Recovery and rehydration of archived events.
- Correlation-link preservation or relocation.
- Encryption, access control, deletion, and audit requirements.
- Whether complete-history reads transparently include cold storage.
- How backups and concurrent event appends interact with compaction.

Logical visibility boundaries should remain valid even if physical archiving
is added later. This lets the current design improve chat behaviour without
prematurely choosing a cold-storage or deletion strategy.

## Current-phase non-goals

- Deleting events to reclaim SQLite space.
- Rewriting sessions with `reset` followed by repeated `append` operations.
- Defining retention or legal-deletion policy.
- Selecting a long-term archive storage provider.
- Making archived history permanently inaccessible.

## Required tests before Lucy switches behaviour

- With no digest boundary, an active read returns all events.
- An active read includes the latest boundary digest and all later events.
- An all-history read returns events on both sides of every boundary.
- An archived read excludes the latest boundary and later events.
- A second digest supersedes the first boundary for active reads.
- Digest creation never changes existing event IDs or timestamps.
- Existing Lucy archive-summary events are recognised during migration.
- Direct session lookup cannot cross account boundaries.
- Pagination and prompt budgets cannot accidentally reveal pre-boundary events.
- Failure to publish or embed a digest leaves episodic history unchanged.
