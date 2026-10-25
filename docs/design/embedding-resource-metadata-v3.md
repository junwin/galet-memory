# Embedding resource metadata v3 — separate design review

Configuration implementation update: [Configuring galet-memory](../configuration.md)
defines the implemented v1 configuration contract and shared-file reference
syntax. The configuration sections below record the earlier design discussion;
the implementation guide is authoritative for available APIs. The configuration guide describes the explicit metadata-version selection.

Status update: opt-in v3 runtime support now covers file and inline assets, plus
an isolated config-driven integration runner. Select `metadata_version: 3` under
`memory.semantic`; omitted values retain v2. The offline migration remains
explicit and retains the source database. See [the v3 runtime guide](../semantic-v3.md).
Digest/event-fragment publication and ownership-aware recall, keyword backfill
against source locators, and Lucy integration remain follow-up work. V3 recall
reports these unsupported resource types without returning their snapshots.

The remainder records the reviewed design and migration contract. The v2 adapter
must not open v3 databases and now rejects them before initializing schema.

## Problem found on develop

`ports/sqlite_vec.py` defines tables in Python strings and alters metadata when
an index opens. `semantic/ingestion.py` accepts absolute paths, stores them in
JSON, and includes namespace in its generated ID. Recall reads JSON `path` or
`text`. Digest publication stores an absolute path and session ID, with optional
digest provenance. Consequently a `source_id` resembling a filename is not a
reliable locator. `document_id` is also used inconsistently: ingestion sets it
to source ID whereas an older migration derives it from a content hash.

The keywords column and keyword fallback already exist on develop, but extraction
is optional in ingestion and absent from digest publication. Adding a column
alone therefore does not meet the requirement that new embeddings have keywords.

## Authoritative DDL

Review [embedding_metadata_v3.sql](../../src/galet_memory/schema/embedding_metadata_v3.sql) directly.
SQLite supports SQL DDL, constraints, indexes and transactional schema changes.
The migration executes this file; it does not maintain a second Python table
definition. Version 3 is specifically the **metadata** version; the existing
`vec_embeddings` / `vec_embeddings_v2` names describe separate vector layouts.
The schema version lives in a component-specific table, avoiding ownership of
the entire database's `PRAGMA user_version`.

This establishes the pattern for future schema work. Converting every existing
episodic/procedural/working-memory schema is separate work, not a claim of this PR.

Each metadata row keeps the existing embedding ID, account, optional namespace,
model, provider, dimensions and creation time. Source locator fields are ordinary
columns, independent of `source_type`, `source_id` and namespace.

| Resource | Required locator | Optional text |
| --- | --- | --- |
| Text file, Obsidian Markdown, repository document, PDF | `external_root`, `relative_path` to the file | Extracted text, digest, summary or abstract |
| Episodic digest | Root/path to its backing DB or published document, `session_id`, `digest_id` | Digest snapshot |
| Episodic fragment | Root/path to backing event store, `session_id`, ordered `source_event_ids` | Fragment snapshot |
| Arbitrary inline text | `resource_type=inline`, nonempty `text` | Text is the resource itself |

`representation` states whether text represents original content, digest,
summary, abstract or fragment. File format and storage location are different
concepts: PDF is a file, and extraction requires a PDF-capable loader. Extra
selectors such as PDF pages or text offsets can remain in `source_metadata`.
An inline row has its embedding ID as its address; externally backed rows always
retain their real source locator even when a text snapshot is also stored.

`source_metadata` preserves additional attributes and legacy audit data. Runtime
code must use the explicit columns as authoritative, never fall back to a JSON
absolute path. Existing `document_id` values are preserved without assigning new
meaning. No IDs or vectors are regenerated.

Namespaces are optional grouping labels. Changing one does not move a resource
or change its locator or embedding ID. New ingestion must stop deriving resource
identity from namespace; use a stable ID or account plus locator plus fragment
identity. Where the vec0 layout duplicates namespace, its filter column must be
updated in the same transaction as metadata when changing a namespace.

## Filesystem access

The embedding stores aliases, never machine-specific root directories. The host
supplies its effective, authorized `external_roots` mapping after config merging.
Resolving `obsidian` + `art/Music/Ode to Joy Schiller.md` uses exactly that mapping
and path, with no inserted account or namespace directory. Repo aliases work
identically. Roots in metadata do not grant access; host policy decides which
aliases an account/agent may read. Reads must check authorization on each access.

The migration resolver rejects unknown aliases, absolute/drive/UNC paths, parent
traversal, NULs, and symlink escapes using resolved-path containment. SQL checks
validate portable locator syntax; filesystem authorization cannot be implemented
by DDL. Runtime ingestion, recall, keyword backfill and file loading must all use
the same host resolver. A missing or denied resource must be reported, never
searched for or loaded via its legacy JSON path. Digest/event reads must also
validate account/session ownership and invalidation before returning snapshots.
Filesystem race protection against hostile concurrent symlink replacement needs
host-level safe-open support; path validation alone is not that guarantee.

## Keywords

`keywords` is a required JSON array of strings, populated from the embedding input
at creation, including digests and fragments. The array can be empty if extraction
finds no usable terms; it must never be omitted. Validate member types in the
writer. Use the dependency-free extractor by default, with an explicit host
override. Store one authoritative keyword value rather than divergent JSON copies.
Fallback recall must preserve account, source type, optional namespace and
invalidation filters and report keyword scoring separately from vector scoring.
No namespace filter should mean all authorized namespaces for that account.

The offline migration preserves existing keywords or derives them using local
Unicode token frequency from stored text or the explicitly located `.md`/`.txt`
source. It does not call an embedding API. Newly read file contents are current
source text, **not proof** of the original embedding input; rerun ingestion later
if content has changed. PDF or DB-backed rows without stored text/keywords are
unresolved until supplied with extracted text or backfilled through a supported
loader. This tool does not parse PDF or event database formats.

## Migration and the books ambiguity

The example mapping records the requested prefixes:

| Legacy namespace | Root | Prefix |
| --- | --- | --- |
| books | obsidian | books |
| vol_3 | obsidian | journal/vol_3 |
| vol_4 | obsidian | journal/vol_4 |
| vol_5 | obsidian | journal/vol_5 |
| vol_6 | obsidian | journal/vol_6 |
| vol_7 | obsidian | journal/vol_7 |
| zen | obsidian | zen |

These are migration hints only. They do not create a permanent namespace/path
coupling. `source_id_base=namespace_prefix` prefixes a source ID unless the same
prefix is already present. `source_id_base=external_root` treats the source ID as
root-relative. The mode must be explicit; there is no directory search.

The supplied examples (`BooksRead/...`, `authors/Carlo Rovelli/...`) do not prove
whether `books/` belongs before those paths. The example mapping uses the requested
`books` prefix but **does not claim those sample rows will resolve**. The dry run
reports missing paths. Set `books.source_id_base` to `external_root` only if these
paths are already relative to the Obsidian root, or specify exact per-record
overrides using the full embedding ID:

```json
{
  "overrides": {
    "FULL-EMBEDDING-ID": {
      "external_root": "obsidian",
      "relative_path": "BooksRead/Extending my book list-2c10d012.md"
    }
  }
}
```

Explicit per-row root/path overrides take priority. An existing absolute JSON
path can be converted only under an explicitly selected authorized root. Relative
legacy JSON paths require an explicit override because their base is unknown.
Old rows lacking dimensions require an explicit `legacy_dimensions` mapping
value; model/provider provenance is preserved where present and never invented.
Digest IDs and event IDs are not inferred from filenames or namespace labels.

From a checkout of this branch, prepare an authorized roots JSON file containing
the effective `external_roots` mapping and copy/edit the example mapping. Run:

```bash
python scripts/migrate_embedding_metadata_v3.py old.sqlite new-v3.sqlite \
  --mapping locations.json --roots authorized-roots.json
```

The default is dry-run. It validates every prepared row against the actual DDL
and returns unresolved IDs and reasons. Any unresolved row prevents applying the
entire migration. Unknown roots, missing files, invalid locators and incomplete
digest/event identities must be resolved explicitly. Do not discard such rows.
After reviewing the report, rerun the same command with `--apply`.

Apply opens the source read-only, holds a consistent DB snapshot, copies the whole
database using SQLite backup, renames old metadata to `embedding_metadata_legacy`,
then installs and populates v3 metadata in a transaction. Destination must not
exist. Existing vector tables, IDs and vector data are preserved by backup; they
are not re-embedded or rewritten. SQLite backup also captures committed WAL data.
The source remains unchanged; rollback is retaining the source and its old runtime.
The legacy table is audit data, not a live compatibility interface.

This migration handles the legacy **metadata** layout, including `dimensions` or
`dims`. It does not convert an old vector layout to `vec_embeddings_v2`; choose
the coordinated runtime/vector upgrade separately. Row validation checks metadata
constraints, not vector dimensional correctness or model compatibility. A final
deployment check must inspect vector/metadata IDs and dimensions with sqlite-vec.

## Coordinated runtime work after schema review

1. Package the reviewed SQL resources and have initialization execute them only
   for new stores. Opening old schemas validates version and directs the operator
   to an explicit migration; it must not silently mutate schema.
2. Add typed resource locators to embedding ports, writers and query results;
   update ingestion, digest publication, fragment creation and keyword backfill.
   Require keywords on every creation path and identity independent of namespace.
3. Update vector and keyword recall to use the authorized resolver, optional
   namespaces and explicit locators; preserve episodic ownership/invalidation.
4. Wire Lucy's effective root configuration and permitted aliases into those ports,
   including PDF extraction where supported. Update galet-tools ingestion callers.
5. Run the dry run on John's actual database, resolve reported exceptions, verify
   vector/metadata correspondence and recall parity, then switch both database
   configuration and runtime together. Never deploy the v3 DB with the v2 writer.

## Validation in this PR

Standalone stdlib tests cover source immutability, copy preservation, schema
constraints, dry-run/overwrite behavior, deterministic prefix handling, book
exceptions, inline/digest/fragment locators, and filesystem containment:

```bash
python -m unittest discover -s tests -p test_embedding_metadata_v3_offline.py -v
```

This deliberately provides the design and porting tool for review before changing
live behavior. It does not claim live ingestion/recall has been converted.

## Configuration implementation and ecosystem direction

The first implementation is documented in [Configuring galet-memory](../configuration.md).
It provides a versioned JSON Schema, explicit object/file/environment loading,
shared roots/profile files, memory assembly and a packaged offline sample. Tests
load the same sample JSON and inject a fixed sample provider. It defaults to the
v2 backend; explicit v3 file/inline runtime support is described in the runtime guide.

The agreed four-package convention is:

| Package | Configuration file selector | Status in this change |
| --- | --- | --- |
| galet | `GALET_CONFIG_PATH` | Future general config loader; existing credential settings unchanged |
| galet-memory | `GALET_MEMORY_CONFIG_PATH` | Implemented |
| galet-tools | `GALET_TOOLS_CONFIG_PATH` | Subsequent change |
| galet-prompt-builder | `GALET_PROMPT_BUILDER_CONFIG_PATH` | Subsequent change |

Each package owns its schema, selects explicit object > explicit file > package
variable > sufficient documented defaults, and does not silently merge sources.
Configuration and database schema versions are distinct. Only explicit loading
or assembly discovers settings; importing libraries does not initialize services.

Roots and embedding profiles are defined once in shared terminal documents.
The implemented reference keys are `external_roots_file` and
`embedding_profiles_file`; paths are relative to the containing config file.
Inline equivalents are supported, but simultaneous inline/file definitions are
rejected. Terminal documents prevent recursive include cycles.

Galet owns provider authentication, credential references and named embedding
profiles. Memory selects a profile and validates its provider/model/dimensions
against the index. `GALET_CREDENTIAL_PATH` retains its credential-directory
meaning. The memory implementation adapts Galet's existing Settings/provider APIs;
it does not add a Galet general config loader or copy secrets into memory config.

Tools will consume the same authorized roots and shared memory services, while
owning handler permissions/server settings. Prompt-builder will own default budgets
and limits with validated per-request overrides. Host composition must construct
shared dependencies once and inject them, avoiding recursive discovery or duplicate
clients/stores. Lucy can explicitly supply validated configuration rather than rely
on process-wide environment selection.

Next stages are tools/prompt-builder loaders and their combined sample, plus
v3 digest/event integration and migration deployment. Existing MCP behavior and
application constructors require compatibility coverage during those stages.
