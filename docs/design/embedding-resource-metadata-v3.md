# Embedding resource metadata v3 — separate design review

Status: executable schema and offline migration candidate. This PR does **not**
switch the live embedding adapter or Lucy to v3. Do not point the current v2
adapter at the migrated database: it does not populate the required locator
columns. No production database has been accessed or migrated.

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

Review [embedding_metadata_v3.sql](../../schema/embedding_metadata_v3.sql) directly.
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

## Shared JSON configuration contract — agreed direction

Applications, runnable samples and tests must assemble and pass the same
versioned JSON-compatible configuration object into galet-memory's public
assembly entry point. Constructor-level dependency injection remains an internal
mechanism and a unit-testing seam; it must not be the only documented way to
configure memory. The entry point validates the contract before opening storage
or calling providers. No configuration loader or assembly API is implemented by
this schema-review PR yet.

The application owns reading/merging its configuration files and selecting the
memory configuration. Galet-memory owns validating that selected object and
assembling its memory components. Galet owns provider authentication, routing and
embedding calls. The contract includes the embedding profile shared across Galet,
ingestion, storage and recall. Any additional services supplied by the host
(provider factory, authorized resource resolver) must be explicit, documented
arguments; there must be no implicit dependency on Lucy's container.

Proposed minimal standalone configuration shape (illustrative, not yet an
accepted runtime format):

```json
{
  "config_version": 1,
  "external_roots": {
    "memory_data": "/absolute/path/to/sample/data",
    "obsidian": "/absolute/path/to/sample/notes"
  },
  "embedding_profiles": {
    "semantic_default": {
      "provider": "openai",
      "model": "text-embedding-3-small",
      "dimensions": 1536
    }
  },
  "memory": {
    "semantic": {
      "embedding_profile": "semantic_default",
      "database": {
        "external_root": "memory_data",
        "relative_path": "embeddings-v3.sqlite"
      },
      "keywords": {
        "extractor": "simple",
        "max_keywords": 20
      }
    }
  }
}
```

Configuration version and database schema version are separate concepts.
Namespace is an optional ingestion/query grouping, not an implicit directory or
required configuration partition. Account/session identity is request context,
not a hard-coded sample default shared by all users.

Provider selection and profile validation use the same contract in every
environment. The Galet adapter must expose actual provider/model/dimensions and
check index compatibility for ingestion and queries; equal dimensions do not
establish model compatibility. A fallback to a different embedding space cannot
silently reuse the index. Credentials are supplied through Galet's explicit
settings/provider factory and documented separately from this non-secret JSON.
The final contract must specify that factory's inputs and credential-resolution
rules; samples must not rely on undocumented machine-specific credentials.

The root mapping describes available locations; it does not grant an account
permission to read them. An application supplies a resolver enforcing its policy.
A standalone sample can explicitly construct the default confined resolver from
its trusted root mapping. Database creation requires resolving a missing leaf
under an authorized existing parent; source reads require an existing resource.
Both operations enforce containment.

Required implementation deliverables after review:

- A published JSON Schema, with an explicit version, required fields, defaults,
  rejection of unknown fields, profile references and clear validation errors.
  Semantic checks cover supported model/dimensions and authorized root aliases.
- One public assembly entry point accepting the parsed JSON object, plus explicit
  provider/resolver dependencies where needed. Its exact name is to be agreed.
- A checked-in complete sample JSON file and runnable example showing file load,
  application overrides, credential/provider construction, memory assembly,
  ingestion and recall. The sample must use the public entry point.
- A local test configuration assembled from that same example: replace root
  directories with temporary directories and inject a deterministic fake
  embedding provider through the documented factory seam. Use the same validator
  and assembly path, with no network or real credentials. Do not invent a
  test-only JSON shape or hide essential settings in fixtures.
- Configuration tests covering invalid/unknown settings, missing profiles, bad
  root references, invalid dimensions, provider/index incompatibility and defaults.
  At least one integration test must load the checked-in JSON and exercise
  assembly, ingestion and recall. Separate opt-in Galet provider tests use the
  same contract with real credentials.
- Lucy integration selecting its effective roots and profile into this object,
  then calling the same public entry point as the standalone sample.

This makes the sample an executable explanation of the contract and ensures
tests catch drift between documented JSON and actual assembly behavior.

## Agreed ecosystem configuration convention

The four Galet packages must work together using a common configuration-loading
convention, while each owns its schema and settings. This extends the JSON
assembly contract above; it does not replace its explicit configuration-object
entry point. These environment variables and loaders are proposed additions,
not existing implemented behavior:

| Package | Configuration file selector |
| --- | --- |
| galet | `GALET_CONFIG_PATH` |
| galet-memory | `GALET_MEMORY_CONFIG_PATH` |
| galet-tools | `GALET_TOOLS_CONFIG_PATH` |
| galet-prompt-builder | `GALET_PROMPT_BUILDER_CONFIG_PATH` |

All selectors point to JSON files. Each loader selects one source in this order:
explicit configuration object, explicit file path (including CLI `--config`),
package environment variable, then documented defaults if sufficient. It does
not silently merge competing sources. An explicitly selected missing or invalid
file fails with an actionable error. Applications may perform their own
intentional merge before passing the resulting object.

Only explicit loading/assembly invokes environment discovery. Importing a package
must not open files, initialize providers/storage, or capture these configuration
environment variables. Core services receive validated settings and dependencies,
so tests and multiple application instances can use distinct configurations in
one process without changing global environment state.

Shared ownership:

- Galet owns provider configuration, credential references and named embedding
  profiles. Memory references the selected profile by name and validates its
  compatibility with its index. Actual returned embedding provenance remains
  required.
- Root mappings have one authoritative configuration source, referenced by memory
  and tools. The host supplies account/agent authorization; a mapping alone never
  grants access. A standalone setup can use a shared roots JSON file; an
  application can supply the same validated mapping/resolver explicitly.
- Memory owns its stores, ingestion/retrieval defaults and keyword settings.
  Tools owns handler/server settings and tool permissions.
- Prompt-builder owns default budgets and limits. Request-specific settings
  remain explicit and are checked against configured limits; they do not rewrite
  the package's local configuration.

The file-reference JSON syntax and dependency assembly API still require design.
References to other config files should resolve relative to the containing config
file, independent of the process working directory. Resource locators continue
to resolve relative to named external roots. Assembly must reject missing
profiles, unknown roots, conflicting explicit/shared definitions and reference
cycles. Packages must not recursively instantiate each other or create duplicate
provider clients/storage instances; application/standalone composition resolves
shared dependencies once and injects them.

Preserve Galet's existing `GALET_CREDENTIAL_PATH` semantics: it identifies a
credential directory, not a general JSON config file. Its credential resolution
and named-profile behavior must remain backward compatible unless separately
reviewed. General config files carry credential references, not copied secrets.

Every package must ship a versioned schema and complete sample. A combined sample
must demonstrate all four using the same roots and embedding profile. Tests must
exercise file selection precedence, validation and shared-reference resolution
through the public loaders, plus a combined offline assembly test using temporary
roots and a fake provider. Existing MCP CLI behavior and application-supplied
constructor settings require compatibility coverage.

Suggested implementation sequence: agree the shared-reference syntax and loader
contracts; implement Galet profile/config loading; implement memory config and
resource resolution against the reviewed schema; adapt tools and prompt-builder;
then wire Lucy and run the combined sample. This PR remains the schema/design
review and offline metadata migration candidate; ecosystem loaders and live v3
adapters are not yet implemented.
