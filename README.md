# galet-memory

**Memory layer for agent applications.** Galet-memory owns neutral episodic, semantic, procedural, and working-memory contracts, reusable persistence and retrieval, and the design of digest and archive behavior. An application decides when to record, recall, or archive; galet-memory determines how those operations work. It must remain usable without Lucy or galet-prompt-builder.

The package includes concrete digest generation, non-destructive archive and reset boundaries, and semantic ingestion and recall. The older episodic CLI still accepts caller-supplied digest text as a separate example. See the [Galet package responsibilities](https://github.com/junwin/galet/blob/main/docs/architecture.md) for the intended boundaries.

Provider-neutral memory abstractions and reusable implementations for agent applications.

This package is being extracted from Lucy's `src/coala_memory` package. It will own
memory models, interfaces, retrieval and ranking logic, and reusable persistence
implementations.

## Dependency rule

> Applications may depend on `galet-memory`; `galet-memory` must never depend
> on an application.

In particular, this repository must not import from Lucy's `src` package.
Lucy-specific configuration, dependency injection, storage adapters, agent lookup,
request context, and tool handlers remain in Lucy.

## Planned extraction order

1. Neutral episodic, semantic, procedural, and working-memory interfaces and models.
2. Retrieval, ranking, and digest logic.
3. Reusable SQLite and vector-backed implementations behind package-owned ports.
4. Lucy compatibility re-exports and adapters.
5. Exact-commit integration in Lucy followed by parity and full-suite testing.

The episodic interface redesign is a coordinated breaking change; see the
[explicit session/event contracts](docs/design/episodic-interfaces.md).

## Storage ports

Reusable memory implementations depend on narrow, structurally typed ports:

- `EmbeddingProvider` turns text into vectors. `GaletEmbeddingProvider`
  adapts Galet's `EmbeddingApi`.
- `EmbeddingIndex` performs namespace-scoped similarity queries without
  exposing an application's storage records.
- `TextLoader` loads bounded text from paths already authorized by the host.
- `ContextRepository` returns neutral context and skill snapshots.

Host applications adapt their storage and configuration to these ports. The
package includes `VectorSemanticMemory`, `EmbeddingDigestRecall`,
`ContextProceduralMemory`, and a basic `FileTextLoader`.

## Working memory lifetime

`SqliteWorkingMemory` holds temporary JSON state under an account and run ID,
optionally scoped to a task. The application starts a run with a fixed TTL and
passes that identity to agents that should share state. Writes require an
expected version; a stale update fails instead of overwriting another agent's
work. Missing keys return `None`, while a finished or expired run raises
`WorkingRunUnavailable`. `finish_run` removes values immediately; expiry is
enforced on reads and writes even before `purge_expired` removes old rows.

```python
from galet_memory import SqliteWorkingMemory

with SqliteWorkingMemory("/path/to/working.sqlite") as working:
    working.start_run("alice", "run-uuid", ttl_seconds=3600)
    plan = working.put("alice", "run-uuid", "plan", {"next": "review"},
                       expected_version=0)
    working.put("alice", "run-uuid", "plan", {"next": "report"},
                expected_version=plan.version)
    working.finish_run("alice", "run-uuid")
```

The database is a temporary-state store, separate from procedural contexts,
skills, session events, and Lucy's durable tasklists. Run identifiers and
access to them remain the host application's responsibility. The default
maximum value is 64 KiB. Run a disposable handoff and expiry example:

```bash
galet-memory-working-road-test
```

## Procedural memory locations


`FileProceduralMemory` reads and writes Markdown contexts and skills under a
root chosen by the application. The default `ProceduralLayout` has global,
account, and account/project locations; same-named contexts contribute text
in that order. Imported skills resolve to the most specific definition.
Frontmatter supports `imports`, `tag`, `mandatory_tools`, and
`search_namespaces`. The result includes source paths and scopes.

```python
from galet_memory import FileProceduralMemory, ProceduralMemoryRequest

memory = FileProceduralMemory("/srv/agent-data")
result = memory.recall(ProceduralMemoryRequest(
    account_name="alice", context_name="shop", project_name="boutique"
))
```

Recall is read-only by default. Applications explicitly call
`memory.repository.save_context(...)` or `save_skill(...)` to create files.
For Lucy's existing `contexts/<account>/*.md` and
`skills/<account>/*.md` layout, pass `ProceduralLayout.lucy()` and point
`root` at the directory containing `contexts` and `skills`.

Contexts default to `context_resolution="merge"` for compatibility. Pass
`context_resolution="most_specific"` to `FileProceduralMemory` (or
`FileContextRepository`) to select only the most specific existing context.
This replaces the entire definition, including imports, tools, tag, and search
namespaces; an empty account file still overrides the global file. Skills
always select the most specific existing definition independently of this policy.

Custom `ProceduralLayout` paths can preserve an existing directory structure:

```python
layout = ProceduralLayout(
    global_contexts="contexts", account_contexts="contexts/{account}",
    project_contexts=None,
    global_skills="skills", account_skills="skills/{account}",
    project_skills=None,
)
memory = FileProceduralMemory(root, layout, context_resolution="most_specific")
```

`repository.list_resolved_context_names(account)` lists visible names across
scopes without duplicates. `read_effective_context` reads the most specific raw
file; `resolve`/`recall` applies the configured context policy. Existing
scope-specific read/list methods keep their original behavior. To edit an
inherited context without modifying a shared file, use
`update_context(..., scope="account", inherit_existing=True)`; its body and
frontmatter are copied from a less-specific definition before applying changes.
Writes still target the explicit scope. Recall and listing do not create files.

Run a disposable example, or inspect existing files without changing them:

```bash
galet-memory-procedural
galet-memory-procedural --root /path/to/storage/data --layout lucy \
  --account alice --context shop
```

The root and layout are supplied when composing the application; request
fields select the account, context, and optional project. Galet-memory owns
path validation, Markdown parsing, imports, and scope precedence.

## Request-scoped embedding reuse


Semantic document recall and episodic digest recall commonly embed the same
query. Wrap their shared provider once and open a cache scope around the
application request:

```python
from galet_memory import CachingEmbeddingProvider

embeddings = CachingEmbeddingProvider(base_embeddings)
semantic_memory = VectorSemanticMemory(embeddings=embeddings, ...)
digest_recall = EmbeddingDigestRecall(embeddings=embeddings, ...)

with embeddings.request_scope() as cache:
    digest_recall(digest_search_request)
    semantic_memory.recall(semantic_request)

print(cache.info())
```

The cache key contains the exact embedding model and the exact ordered input
texts. A model change therefore cannot reuse vectors from the previous model.
Calls outside a request scope pass through uncached, concurrent contexts are
isolated, and failed provider calls are never stored.

`SqliteVecEmbeddingIndex` reads the existing 1536-dimension sqlite-vec
schema. It defaults to `vec_embeddings_v2` and joins results to
`embedding_metadata`; the original `vec_embeddings` table can be selected
explicitly for legacy reads. Internal tables generated by vec0 are never
accessed directly.

## Explicit episodic interfaces

`SessionStore`, `EventStore`, and `DigestStore` have named operations in
`episodic/session_interface.py`, `event_interface.py`, and `digest_interface.py`.
A session belongs to an account and can contain messages from several agents.
An event records its role, actor, kind, complete content and correlation IDs.
The caller does not select an agent to read or manage a conversation.

```python
from galet_memory import SqliteEpisodicMemory, NewEvent

with SqliteEpisodicMemory("/path/to/new-chat.sqlite") as memory:
    session = memory.create_session(account_name="junwin")
    memory.append_event(
        account_name="junwin", session_id=session.session_id,
        event=NewEvent("user", "Hello", "junwin", correlation_ids=("request-uuid",)),
    )
    page = memory.get_recent_events(
        account_name="junwin", session_id=session.session_id, count=10,
        event_kinds=["user_message"],
    )
    for event in page.events:
        print(event.content, event.correlation_ids)
```

Filters apply before counts. Recent reads return the last N matching active
records in append order; reports and tool calls do not consume a user-message
limit. Period reads use timezone-aware `[start, end)` boundaries and return
occurrence-time order with append sequence as the tie break. Event reads do not
count tokens or truncate content. Galet-prompt-builder owns prompt budgets.

`get_session` reads metadata only. `SessionChanges` expresses typed updates;
`EMPTY_TAIL` explicitly guards an empty event stream. Every operation is scoped
to the account. `get_exchange` reads linked request/response/processing events;
`invalidate_exchange` hides them idempotently and excludes affected digests.
See [event invalidation](docs/event-invalidation.md).

Both SQLite and JSONL implement the same public contracts. This is a **breaking
interface/storage change**: use a fresh database or directory. Existing episodic
schemas/layouts are rejected; there are no migrations, compatibility readers or
combined `recall` API. [SQLite storage](docs/episodic-sqlite.md) describes the
relational schema. JSONL uses a complete snapshot replaced atomically under a
cross-process lock; it is intended for small stores.

The branch implements the galet-memory package and its examples. Updating
**galet-tools, galet-prompt-builder and Lucy is required before releasing this
change into their environments**. Focused model tools and live Lucy acceptance
tests are the next coordinated steps under #31.

## Episodic memory road test

### Inspect user messages and exchanges

Use a fresh path for the new episodic schema. The account is explicit for every
command; session metadata does not require an agent. Actor identifies the
producer of each appended event.

```bash
galet-memory-episodic --db /tmp/galet-chat-new.sqlite --account demo create --session-id road-test
galet-memory-episodic --db /tmp/galet-chat-new.sqlite --account demo append road-test "Hello" --actor demo --correlation request-1
galet-memory-episodic --db /tmp/galet-chat-new.sqlite --account demo append road-test "Welcome" --role assistant --actor peace --correlation request-1
galet-memory-episodic --db /tmp/galet-chat-new.sqlite --account demo recent road-test --count 10 --kind user_message
galet-memory-episodic --db /tmp/galet-chat-new.sqlite --account demo exchange road-test request-1
galet-memory-episodic --db /tmp/galet-chat-new.sqlite --account demo invalidate road-test request-1
```

Other commands include `list`, `show` (session metadata), `event`, `period`,
`update`, `clear`, `delete` and the supplied-text `archive` demonstration. Use
`--help` on the CLI or a subcommand for its arguments. Period timestamps require
a timezone, for example `--start 2026-10-01T00:00:00Z --end 2026-10-02T00:00:00Z`.

### Check the digest lifecycle

Run the fixture-based archive, cumulative digest and reset checks with fresh
temporary storage:

```bash
galet-memory-digest-road-test
```

To use a real model through Galet, supply `--live --model gpt-4o-mini` and, if
needed, `--credential-path /path/to/credentials`. Live mode makes paid requests.

## Semantic ingestion and recall

`SemanticIngestionService` owns add, refresh, unchanged-content detection, and
targeted removal of text or caller-authorized UTF-8 files. It uses the same
`EmbeddingProvider` and `EmbeddingIndex` ports as `VectorSemanticMemory`.
Sources are identified within an account and namespace; files are recalled
through `FileTextLoader`, while directly ingested text is stored for recall in
record metadata. An oversized source is rejected rather than silently
truncated during embedding. The current SQLite vec schema requires 1536
dimensions, so use a matching embedding model.

Run a complete add → recall → edit → refresh → recall → delete sequence in a
temporary SQLite vec database without credentials:

```bash
pip install -e '.[vec]'
galet-memory-semantic-road-test
```

To make real embedding calls through Galet, set `OPENAI_API_KEY` or supply a
credential directory, then run:

```bash
galet-memory-semantic-road-test --live --model text-embedding-3-small \
  --credential-path /path/to/credentials
```

Live mode makes paid embedding requests. Both modes check account isolation,
unchanged source handling, refresh, and deletion.

Install the package, set `OPENAI_API_KEY` (or use Galet's
`GALET_CREDENTIAL_PATH`), and point the CLI at a copy or test embedding
database.

Add a sample:

```bash
galet-memory-embeddings \
  --db /home/junwin/lucy_storage/data/embeddings-v2.sqlite \
  --account junwin \
  --namespace demo \
  add "The allotment has runner beans and three apple trees."
```

Query it:

```bash
galet-memory-embeddings \
  --db /home/junwin/lucy_storage/data/embeddings-v2.sqlite \
  --account junwin \
  --namespace demo \
  query "What fruit trees are in the allotment?"
```

Query it with path to credentials:

```bash
galet-memory-embeddings \
  --credential-path /home/zzzzzz/credential \
  --db /home/junwin/lucy_storage/data/embeddings-v2.sqlite \
  --account junwin \
  --namespace demo \
  query "What fruit trees are in the allotment?"
```

Use `--extension` when vec0 is not installed at
`/usr/local/lib/sqlite-vec/vec0.so`. The current schema requires
1536-dimension vectors, so the default model is
`text-embedding-3-small`. Run against a copy of a production database when
experimenting.
