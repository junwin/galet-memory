# galet-memory

Resource metadata redesign: see the [v3 schema review and migration plan](docs/design/embedding-resource-metadata-v3.md).
The v3 schema and migration are separate from the running configuration system;
the current live semantic adapter uses v2.

For application, environment-variable and shared-file configuration, see
[Configuring galet-memory](docs/configuration.md). The public `build_memory()`
entry point and `galet-memory-configured` sample use that same JSON contract.

Provider-neutral memory abstractions and reusable implementations for agent applications.

Galet-memory owns the neutral episodic, semantic, procedural, and
working-memory contracts, plus reusable persistence and retrieval. An
application decides when to record, recall, or archive; galet-memory determines
how those operations work. It can run standalone without a host application.
Galet supplies live model and embedding providers; offline samples use local fixtures.

The package includes concrete digest generation, non-destructive archive and
reset boundaries, and semantic ingestion and recall. The older episodic CLI
still accepts caller-supplied digest text as a separate example. See the
[galet architecture notes](https://github.com/junwin/galet/blob/main/docs/architecture.md)
for the intended boundaries between the galet packages.

## The CoALA memory model

Galet-memory implements the memory architecture described in the
[CoALA framework](https://arxiv.org/abs/2309.02427) (Sumers, Yang,
Narasimhan &amp; Yao, *Cognitive Architectures for Language Agents*). CoALA
describes a language agent as a decision cycle over four memory types, and
this package provides a store for each of them:

| CoALA memory | Galet-memory store | Role in the decision cycle |
| --- | --- | --- |
| **Working** | `SqliteWorkingMemory` | Scratch state for the current run/task, with optimistic versioning and TTL expiry |
| **Episodic** | `SqliteEpisodicMemory` / JSONL (`SessionStore`, `EventStore`, `DigestStore`) | The agent's stream of experiences: sessions, events, exchanges, and their digests |
| **Semantic** | `VectorSemanticMemory` + `SemanticIngestionService` | Knowledge derived from those experiences, queried by meaning |
| **Procedural** | `FileProceduralMemory` / `ContextProceduralMemory` | Skills and rules for acting: contexts, imports, and scope precedence |

CoALA assigns the decision cycle — *when* to record, recall, or archive — to
the agent's policy, not the memory system. That is exactly the boundary this
package keeps: the host application decides when an operation happens;
galet-memory defines how it works and stays neutral about the agent around
it. Companion packages handle the other CoALA layers: galet-prompt-builder
turns recalled memory into bounded prompts, and galet supplies the
model/embedding providers the memory stores plug into.

## Install and test from a checkout

For the configuration features on `develop`, run these commands from the
repository directory. Use Python 3.11 or later:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test,vec]"
python -m pytest
```

If your virtual environment already exists, activate it and run the last two
commands. Repeat the installation command after pulling changes that add or
change dependencies. It installs the declared requirements, including
`jsonschema`, and registers the command-line samples. A Git pull alone does
not update the environment's dependencies.

For a released version from PyPI:

```bash
python -m pip install "galet-memory[vec]"
```

The new configuration commands require a checkout containing this change until
it is included in a published release. Installing without `[vec]` omits the
sqlite-vec dependency needed by semantic storage.

## Configuration and running

A self-contained, copy-and-edit template is available at
[galet_memory_sample.json](galet_memory_sample.json). It includes roots, an
embedding profile, and settings for all four memory components in one file.
Replace the placeholder absolute root directories and create the data and
`digests` directories before assembling it. The template selects a live OpenAI
profile; Galet supplies its credentials. The initialized offline example below
creates its own directories and sample note.

Galet-memory is a library with command-line examples. The configuration sample
assembles memory, performs ingestion and recall, and exits; it is not a
background server.

Start with an offline example that needs no credentials. Choose a **new**
directory; initialization refuses to overwrite an existing directory:

```bash
galet-memory-configured --init-sample "$HOME/galet-memory-demo"
export GALET_MEMORY_CONFIG_PATH="$HOME/galet-memory-demo/memory.json"
galet-memory-configured --offline
```

Initialization creates the directories, a Markdown note, and three JSON files:

| File | Purpose |
| --- | --- |
| `memory.json` | Selects memory components, stores, embedding profile and keyword settings |
| `roots.json` | Maps external-root names to absolute directories |
| `embedding-profiles.json` | Defines provider, model and dimensions for each named profile |

The sample uses the same JSON contract and public loader as applications and
integration tests. It ingests the sample note and reports vector and keyword
matches. Re-running with the same configuration reuses the stores and reports
the unchanged source. Its fake provider has its own `sample/offline-hash-v1`
profile and `semantic-offline.sqlite` database.

You can select the configuration file explicitly instead:

```bash
galet-memory-configured --config "$HOME/galet-memory-demo/memory.json" --offline
```

Selection is explicit configuration object > explicit file > the file named by
`GALET_MEMORY_CONFIG_PATH`. Sources are not silently merged. Relative JSON
file references resolve from the containing config file, not the shell's current
directory. New database files require `create: true` and an existing parent
directory. Existing databases are validated, not automatically migrated.

### Use a live embedding provider

In the generated `memory.json`, change
`memory.semantic.embedding_profile` to `semantic_default` and change
`memory.semantic.database.relative_path` to `semantic-live.sqlite`.
The shared profile file already defines OpenAI's
`text-embedding-3-small` with 1536 dimensions. Then configure Galet credentials
and run without `--offline`:

```bash
export GALET_CREDENTIAL_PATH=/home/junwin/credential
galet-memory-configured --config "$HOME/galet-memory-demo/memory.json"
```

This makes paid embedding requests. Use a separate database for a different
embedding profile; offline and real vectors are incompatible.

| Environment variable | Meaning |
| --- | --- |
| `GALET_MEMORY_CONFIG_PATH` | Path to the memory JSON configuration file |
| `GALET_CREDENTIAL_PATH` | Galet's existing directory of provider credential files |

Galet also supports its existing provider-key environment variables and explicit
`Settings`. No `GALET_CONFIG_PATH` general config loader is introduced here.
See [Configuring galet-memory](docs/configuration.md) for complete JSON examples,
Galet integration and resource-access behavior.

`GALET_MEMORY_CONFIG_PATH` is used by `load_memory_config()`,
`build_memory()`, and `galet-memory-configured`. The older commands below
keep their explicit arguments and do **not** read this JSON configuration.

### Quick sample

A no-credential working-memory example — start a run, store a value with
optimistic versioning, finish the run (uses a temporary database):

```bash
galet-memory-working-road-test
```

Further runnable examples (episodic CLI, procedural recall, semantic and
digest road tests, and a keyword extraction check) are described in the
sections below.

## Dependency rule

> Applications may depend on `galet-memory`; `galet-memory` must never depend
> on an application.

In particular, this package never imports host-application code.
The host owns application-specific configuration merging, agent lookup,
request context and access policy. Galet-memory owns validation of its JSON
contract and assembly of its reusable components through `build_memory()`;
applications may also supply dependencies explicitly.

The episodic interfaces are explicit session/event contracts; see
[docs/design/episodic-interfaces.md](docs/design/episodic-interfaces.md).

## Storage ports

Reusable memory implementations depend on narrow, structurally typed ports:

- `EmbeddingProvider` turns text into vectors. `GaletEmbeddingProvider`
  adapts Galet's `EmbeddingApi`.
- `EmbeddingIndex` performs namespace-scoped similarity queries without
  exposing an application's storage records.
- `TextLoader` loads bounded text from paths already authorized by the host.
- `KeywordExtractor` derives keywords from text. `SimpleKeywordExtractor`
  is a dependency-free default; applications may supply their own (for
  example spaCy/NLTK-based) through the same port.
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
skills, session events, and the host's durable task storage. Run identifiers and
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

The examples in this section use the lower-level constructor/CLI APIs. The
configured path above always supplies keyword extraction and embedding-profile
checks; direct constructor callers choose those dependencies explicitly.

`SemanticIngestionService` owns add, refresh, unchanged-content detection, and
targeted removal of text or caller-authorized UTF-8 files. It uses the same
`EmbeddingProvider` and `EmbeddingIndex` ports as `VectorSemanticMemory`.
Sources are identified within an account and namespace; files are recalled
through `FileTextLoader`, while directly ingested text is stored for recall in
record metadata. An oversized source is rejected rather than silently
truncated during embedding. The current SQLite vec schema requires 1536
dimensions, so use a matching embedding model.

When the service is constructed with a `keyword_extractor`, keywords are
derived alongside the embedding vector at every ingest and stored with the
record. Existing records are unaffected: without an extractor no keywords are
written, and records ingested before keywords exist refresh once with
keywords on their next re-ingest.

`ingest_folder` walks a caller-authorized folder and ingests every matching
file with the same stable identity per resolved path, so unchanged files skip
the embedding call on re-runs:

```python
results = ingestion.ingest_folder(
    account_name="alice", namespace="docs", folder="/srv/agent-data/notes",
    suffixes=(".md", ".txt"), recursive=True,
)
```

By default an unreadable or oversized file stops the walk; pass
`skip_errors=True` to continue past individual failures.

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
  --db /path/to/embeddings.sqlite \
  --account junwin \
  --namespace demo \
  add "The allotment has runner beans and three apple trees."
```

Query it:

```bash
galet-memory-embeddings \
  --db /path/to/embeddings.sqlite \
  --account junwin \
  --namespace demo \
  query "What fruit trees are in the allotment?"
```

Query it with path to credentials:

```bash
galet-memory-embeddings \
  --credential-path /path/to/credentials \
  --db /path/to/embeddings.sqlite \
  --account junwin \
  --namespace demo \
  query "What fruit trees are in the allotment?"
```

Use `--extension` when vec0 is not installed at
`/usr/local/lib/sqlite-vec/vec0.so`. The current schema requires
1536-dimension vectors, so the default model is
`text-embedding-3-small`. Run against a copy of a production database when
experimenting.

## Keywords and fallback recall

Keywords give semantic memory a degradation path when an embedding service is
unavailable, and a source of derived terms for each stored record.

### Ingestion

`SemanticIngestionService(keyword_extractor=SimpleKeywordExtractor())` derives
keywords from every ingested text or file alongside the embedding vector. The
`KeywordExtractor` port is structural; `SimpleKeywordExtractor` is a
dependency-free default (token frequency with a small built-in stopword list,
extendable via `stopwords=(...)`). Applications can supply their own extractor
— for example a spaCy/NLTK-based one — without changing galet-memory.

Keywords are stored in the `keywords` column of `embedding_metadata` (a JSON
array). The lower-level `SqliteVecEmbeddingIndex` constructor, with its default
`initialize_schema=True`, can add a missing keywords column via `ALTER TABLE`.
The configured `build_memory()` path opens existing stores without schema
initialization and rejects a missing keywords column; it requires an explicit
migration. Records whose keywords exist only inside `source_metadata` (from
earlier ingestion) can still be matched by the lower-level keyword query.

### Backfilling existing records

`galet-memory-embeddings backfill-keywords` derives keywords for records that
were ingested before keyword extraction existed — without re-embedding:

```bash
galet-memory-embeddings \
  --db /path/to/embeddings.sqlite \
  --account junwin \
  --namespace demo \
  backfill-keywords
```

It reads each record's stored inline text or recorded path, extracts keywords
with `SimpleKeywordExtractor`, and writes them with a metadata-only update:
no vector writes and no embedding API calls. Records with no reachable text
(for example a moved or deleted file) are skipped and counted. Run it against a
copy of a production database first.

### Fallback recall

`VectorSemanticMemory(keyword_extractor=...)` embeds the query and queries by
vector as usual. If the embedding provider fails (network, quota, outage) and
a keyword extractor is configured, recall degrades to keyword matching instead
of raising:

```python
memory = VectorSemanticMemory(
    embeddings=embeddings, index=index,
    text_loader=FileTextLoader(),
    keyword_extractor=SimpleKeywordExtractor(),
)
result = memory.recall(SemanticMemoryRequest(
    account_name="alice", query="apple trees", namespaces=["notes"],
))
if result.metadata["backend"] == "keyword":
    # embedding service was unavailable; scores are keyword-overlap fractions
    ...
```

Keyword results are marked `metadata={"backend": "keyword", "reason":
"embedding_provider_unavailable", "query_keywords": [...]}` so callers can
distinguish them from vector results (`backend="vector"`). Keyword scores are
the fraction of query keywords present in a record's stored keywords; they are
**not comparable** with embedding similarity scores, and `score_threshold`
applies to them unchanged. Recall with `use_embeddings=False` also routes to
keyword recall when an extractor is configured. Without a configured extractor
the previous behavior is preserved: provider failures propagate and
`use_embeddings=False` returns an empty result.

Run a disposable check of the default extractor:

```bash
galet-memory-keywords-road-test
```
