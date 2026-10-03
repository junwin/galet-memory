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

The extraction must preserve database formats, retrieval results, prompts, and tool
permissions.

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
    digest_recall(episodic_request)
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

`SqliteEpisodicMemory` implements both the prompt-time `EpisodicMemory` and
session-management `EpisodicMemoryManager` contracts using only `sessions`,
`events`, and `event_correlations` tables with foreign keys. Existing relational
databases open directly; new files use SQLite's default rollback journal.
The obsolete `kv`/`logs` episodic backend, schema switching, and copy migration
are removed. Unsupported files are rejected without conversion; choose a fresh
file if old chat history can be discarded. See [SQLite storage](docs/episodic-sqlite.md).

`JsonlEpisodicMemory` implements the same contracts over a filesystem root.
It preserves Lucy's existing `sessions/<id>/meta.json`,
`sessions/<id>/events.jsonl`, and `correlations/<id>.jsonl` layout. Applications
can therefore select SQLite or JSONL at their composition root without their
handlers, endpoints, or prompt compiler knowing which medium is in use.

```python
from galet_memory import SqliteEpisodicMemory

episodic = SqliteEpisodicMemory("/path/to/chat2.sqlite")
session = episodic.get_session("existing-session-id")

# Or use the existing Lucy-compatible JSONL directory.
from galet_memory import JsonlEpisodicMemory

episodic = JsonlEpisodicMemory("/path/to/storage/data/chat2")
```
## Invalidate an exchange

`EpisodicMemoryManager.invalidate_events(account_name=..., session_id=...,
correlation_id=...)` hides linked events while retaining their originals. It is
idempotent, account/session scoped, and supports an optional expected append
tail. Normal reads, search, digest generation and prompt recall exclude the
invalidated content; archive/reset boundaries remain effective. See
[event invalidation](docs/event-invalidation.md) for the typed result, raw
inspection, concurrency contract and digest/embedding provenance requirements.

## Episodic memory road test

### Digest and reset boundaries

`CurationService.archive` generates a digest of eligible events since the last
archive or reset boundary, appends it without deleting history, and exposes it
as the start of the next active view. The previous digest is context for prompt
recall, but is not treated as a source event when generating the next interval
digest. `CurationService.reset_context` appends a boundary without a digest:
the active view starts empty while `event_scope="all"` retains the transcript.
The older storage-level `reset_session` deletes events and must not be used as
an application-facing context reset.

`CurationService.produce_cumulative_digest` rebuilds a derived digest from
committed interval digests. By default it starts after the latest reset;
`since_reset=False` includes older intervals. It does not rewrite any interval
or advance the archive boundary. `publish=True` updates a separate stable
`<session>_cumulative.md` artifact and embedding.

Archive boundary metadata records every source event ID, first/last source
timestamps, the prior boundary ID, the digest hash, and generator/model policy.
When archive publication fails after the boundary commits,
`DigestPublicationError.boundary_event_id` identifies that commit. Call
`retry_publication(account_name=..., session_id=...,
boundary_event_id=...)` to publish the stored digest without generating or
archiving again. Archive publications use the boundary ID in their file and
embedding IDs, so retries target the same artifact. Publication can repeat an
embedding API call; the stable ID prevents duplicate records. A plain preview
publication uses the session's stable path.

`GaletDigestGenerator` is a concrete generator built on Galet's `LLMApi`.
It groups every eligible event into bounded chunks and merges their summaries.
An individual event that exceeds the input limit raises an error; no archive
boundary is written. The default policy covers all events; tool events can be
excluded explicitly. Digests of older intervals remain
separate, rather than being repeatedly re-digested. A cumulative derived view
can be added separately without changing archive history.

Run the complete digest lifecycle without an API key. This creates a temporary
SQLite database, previews a digest, archives two intervals, resets context,
and checks that history is retained while the active interval starts fresh:

```bash
galet-memory-digest-road-test
```

To inspect the database afterward, use a new path with `--db`:

```bash
galet-memory-digest-road-test --db /tmp/galet-digest-road-test.sqlite
galet-memory-episodic --db /tmp/galet-digest-road-test.sqlite show digest-road-test --scope all
```

To exercise the concrete generator against a real model, use `--live`. Galet
resolves `OPENAI_API_KEY` or the credential directory; live runs make multiple
model requests and can incur API charges:

```bash
galet-memory-digest-road-test --live --model gpt-4o-mini \
  --credential-path /path/to/credentials
```

The fixture mode exercises the same generator, curation service, and SQLite
storage; only the model response is replaced with fixed text. The command
refuses an existing `--db` path to avoid changing a real chat database.

```python
from galet_memory import CurationService, GaletDigestGenerator, GaletDigestPolicy

generator = GaletDigestGenerator(llm_api, GaletDigestPolicy(model="gpt-4o-mini"))
curation = CurationService(episodic_memory, generator)
archived = curation.archive(account_name="demo", session_id="road-test")
curation.reset_context(account_name="demo", session_id="road-test")
```

Create a disposable database and session:

```bash
galet-memory-episodic --db /tmp/galet-chat.sqlite create \
  --account demo --agent lucy --session-id road-test \
  --friendly-name "Road test"
```

Append and inspect an event:

```bash
galet-memory-episodic --db /tmp/galet-chat.sqlite add \
  road-test "Hello episodic memory"

galet-memory-episodic --db /tmp/galet-chat.sqlite show road-test
```

Append supplied digest text as a logical archive boundary, then compare the
visible and complete histories:

```bash
galet-memory-episodic --db /tmp/galet-chat.sqlite archive \
  road-test "The earlier conversation was summarized." --account demo

galet-memory-episodic --db /tmp/galet-chat.sqlite show road-test
galet-memory-episodic --db /tmp/galet-chat.sqlite show road-test --scope all
```

The sample accepts digest text directly and does not call an LLM. Run it
against a disposable database or a copy while experimenting.

## Embedding memory road test

### Semantic ingestion and recall

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
