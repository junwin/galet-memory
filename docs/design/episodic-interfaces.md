# Explicit episodic session and event interfaces

Status: proposal for review, issue #31. The signatures below describe the target
API, not methods available in the current release. No runtime or storage changes
are made by this proposal.

## Model and responsibilities

A session is an account-owned, ordered stream of events. Several agents can
participate in one session. Reading or managing that session does not require
selecting an agent.

| Field | Meaning | Example |
| --- | --- | --- |
| account_name | Owner used to authorise access | junwin |
| session_id | Identity of the conversation | session UUID |
| event_id | Identity of one stored event | event UUID |
| role | Participant category | user, assistant, tool, system |
| actor | Identity of the event producer | junwin, peace, system |
| kind | Meaning of the event | user_message, assistant_message, prompt_report |
| correlation association | Processing exchange to which the event belongs | inbound-message UUID |

The account is required on all public session/event operations. Missing and
other-account sessions have the same not-found behaviour. Session ownership
cannot be changed by a metadata update. Agent switching affects subsequent
events' actors, not the session's ownership or access key.

Galet-memory stores/retrieves records, generates/manages digests, and provides
memory search. Galet-prompt-builder selects records for a prompt, counts tokens,
and applies prompt budgets. Lucy supplies execution identity, manages running
work, and enforces tool permissions. No dependency from memory to prompt-builder
or Lucy is introduced.

## Contract organisation

```text
episodic/
    models.py              # persisted snapshots and typed results
    session_interface.py   # SessionStore
    event_interface.py     # EventStore and exchange access
    digest_interface.py    # DigestStore
    sqlite.py              # concrete implementation
```

The concrete store can implement all three interfaces. Splitting the contracts
does not require separate databases. Procedural, semantic and working memory
remain separate concerns and are outside this redesign.

## SessionStore

Abbreviated signatures (all arguments shown are keyword-only):

```python
create_session(*, account_name, session_id=None, friendly_name=None,
               context_name=None, tags=(), metadata=None) -> Session

get_session(*, account_name, session_id) -> Session | None

list_sessions(*, account_name, count=20, cursor=None) -> Page[Session]

update_session(*, account_name, session_id,
               changes: SessionChanges) -> Session

clear_session_events(*, account_name, session_id,
                     expected_last_event_id=None) -> ClearResult

delete_session(*, account_name, session_id) -> bool
delete_sessions(*, account_name, session_ids) -> list[str]
```

`Session` is metadata only; fetching it never loads the transcript. It contains
identity, friendly name, context name, tags, metadata, created/updated timestamps
and the actual `last_event_id` used for conditional writes. It has no owning
agent. Current `session_type`, `user_id`, participants and links need a consumer
audit before retaining or removing them; they are not mandatory access selectors.

`SessionChanges` has explicit editable fields. An unset field means unchanged;
`None` clears a nullable field; supplied tags/metadata replace their respective
values. IDs, ownership and timestamps are not patchable.

Listing uses updated time descending, with session ID as a deterministic tie
break. A continuation cursor includes the listing cutoff and position; newly
updated sessions are picked up on a fresh listing. Deletion returns whether a
session existed, is idempotent, and removes its owned records. Bulk deletion
validates ownership for the entire selection before modifying anything.

Content search is a separate, explicitly named operation if consumers need it;
`list_sessions` does not silently search transcript payloads. The current
`query` parameter is not copied into ordinary listing or event reads.

Clearing is explicitly destructive and distinct from ending the active prompt
history window. It preserves necessary invalidation state so excluded external
digests cannot reappear. Curation's archive/history boundary remains a separate
operation; the ambiguous `reset_session` name should not cover both meanings.

## EventStore

```python
append_event(*, account_name, session_id, event: NewEvent,
             expected_last_event_id=None) -> Event

append_events(*, account_name, session_id, events: Sequence[NewEvent],
              expected_last_event_id=None) -> list[Event]

get_event(*, account_name, session_id, event_id) -> Event | None

get_recent_events(*, account_name, session_id, count=10,
                  event_kinds=None, actors=None,
                  before_event_id=None) -> EventPage

get_events_by_period(*, account_name, session_id, start, end,
                     event_kinds=None, actors=None,
                     count=100, cursor=None) -> EventPage

get_exchange(*, account_name, session_id, correlation_id) -> Exchange | None

invalidate_exchange(*, account_name, session_id, correlation_id,
                    expected_last_event_id=None) -> InvalidationResult
```

`NewEvent` contains role, actor, kind, content, optional occurrence timestamp,
metadata and correlation association(s). `Event` adds event ID, session ID,
stored timestamp and immutable append sequence. Content is the original
JSON-compatible payload, not a stringified summary. The role/actor distinction
is documented rather than guessed from agent configuration.

Append persists the event and correlation association(s) atomically. It must not
require a second `link_event` call that can fail after storing an unlinked event.
Batch append is atomic. Supplying an expected tail enables a conditional write;
an explicit check for an empty stream needs a distinct guard representation,
since the existing `None` convention means no guard. Events remain immutable.

### Read semantics

- Filter visibility, event kinds and actors before applying `count`.
- `event_kinds=None` means all visible kinds; an empty collection means none.
  Apply the same rule to actor filters. Reject negative counts; zero returns an
  empty page. No implicit token budget shortens the result.
- Recent reads choose the latest matching events by append sequence, then
  return that selection in chronological append order. `before_event_id` is an
  exclusive anchor for reading older events; it must belong to this session.
- Period reads use timezone-aware UTC timestamps and a half-open interval
  `[start, end)`. Order by occurrence time then append sequence. Reject reversed
  intervals. A cursor binds the filters and initial append high-water mark so
  later appends do not shift a continuing read.
- Pages include events, continuation information and the actual snapshot tail.
  Counts refer to individual records, not exchanges; ten user messages require
  `event_kinds=["user_message"]`.
- Recent prompt-history reads use the active history window. Period and exchange
  reads include visible archived history for inspection. If an all-history recent
  operation is needed, name and document it explicitly instead of making it a
  hidden mode of prompt history retrieval.
- Normal reads exclude invalidated events and internal control records. Internal
  audit reads are separate and never supplied as prompt or digest input. A cursor
  does not override a later invalidation: continuing reads still hide its targets.

`Exchange` contains correlation ID, session ID and its linked visible events in
append order, including request, responses and associated processing records.
It is not assumed to have exactly one response. Lucy decides whether processing
is still running; memory handles atomic invalidation, idempotency and digest
provenance as today. Invalidation must prevent future appends extending an
invalidated correlation.

## Correlation representation: investigate with #30

Current SQLite permits multiple associations per event: its uniqueness constraint
is `(correlation_id, event_id)`, not `event_id`. It supports storing an event before
linking it, and reading by correlation. Those are implementation facts, not proof
that multiple correlations are needed by the application.

Review producers (Lucy chat, automation/delegation and other package consumers),
tests and history before choosing cardinality. If each event belongs to at most
one inbound processing exchange, prefer `correlation_id: str | None` on the record
and assess removing the mapping table. If multiple associations are legitimate,
expose typed `correlation_ids` and explain why. Either choice must appear in all
public event reads and tool serialization. Parent/child processing should not
silently make a trace ID an exchange deletion target.

## DigestStore and prompt-builder boundary

Keep stored interval/cumulative digests and their source event/digest provenance
explicit. Separate digest operations from transcript retrieval:

```python
get_digest(*, account_name, session_id, digest_id) -> Digest | None
list_digests(*, account_name, session_id, count=20, cursor=None) -> Page[Digest]
search_digests(*, account_name, query, session_id=None, count=3) -> list[DigestMatch]
is_digest_valid(*, account_name, session_id, digest_id=None,
                source_event_ids=None) -> bool
```

The detailed generation/publication contract must be reviewed against existing
`CurationService`, `DigestGenerator`, `DigestPublisher` and overflow consumers.
Generation, persistence, ranking and provenance validity stay in galet-memory.
Token allocation, selection for a particular prompt and snippet truncation stay
in galet-prompt-builder. The combined `EpisodicMemoryRequest`/`recall` contract is
replaced at its consumers rather than maintained as a second legacy API.

## Focused tools and a direct Python example

Galet-tools exposes a small schema per operation: `sessions_list`, `session_get`,
`events_recent`, `events_by_period`, `event_get`, `event_append`, `exchange_get`
and `exchange_invalidate`, plus required session-management tools. The trusted
execution context supplies the account; an argument cannot override ownership.
No tools require an agent to select a session, and no event-reading schema
includes digest queries or prompt token budgets. Existing permissions still apply.

Proposed Python usage:

```python
page = events.get_recent_events(
    account_name="junwin", session_id=session_id, count=10,
    event_kinds=["user_message"],
)
for event in page.events:
    print(event.actor, event.content)  # full user-message payload
```

Equivalent `events_recent` tool arguments:

```json
{"session_id": "...", "count": 10, "event_kinds": ["user_message"]}
```

## Implementation and verification sequence

1. Review these method names and semantics; audit current consumers and settle
   correlation cardinality under #30. Define exact typed models/cursors/errors.
2. Implement account-scoped session/event interfaces and SQLite persistence,
   including atomic correlated append and visibility/provenance tests. Assess
   JSONL against the same contract rather than leaving divergent semantics.
3. Update curation/digest consumers and galet-prompt-builder. Demonstrate that
   prompt-history selection and budgeting remain correct across agent changes,
   archives, clears and invalidations.
4. Replace the galet-tools dispatcher with focused Python/MCP handlers and wire
   them into Lucy, preserving allowlists and discovery/activation behaviour.
5. Run a disposable end-to-end example and model-driven Lucy tests: list sessions,
   inspect ten user messages, read a period, inspect an exchange and invalidate a
   completed exchange. Record actual tool arguments/results and whether requests
   succeed without corrective attempts. Do not count only mock tool tests as
   evidence of model usability.

Use a clean storage reset if the schema changes. Do not add migrations, old-format
readers or dual APIs. Coordinate releases across all affected repositories;
do not close #31 on the design document or the first package-only implementation.
