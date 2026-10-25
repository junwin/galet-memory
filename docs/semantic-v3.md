# Semantic resource metadata v3: file and inline runtime

This stage activates the reviewed v3 metadata schema for **file and inline
resources** through the public configuration/assembly APIs. It does not change
Lucy or migrate any database automatically. Digest/event-fragment metadata is
preserved by migration and represented by the index, but v3 recall reports these
resources as unsupported and returns no snapshot. Their publication and
ownership/invalidation-aware loading remain a coordinated follow-up. Do not
switch a mixed production store until that work and local migration checks pass.

## Select v3 explicitly

Keep `config_version: 1` and add `metadata_version: 3` under `memory.semantic`.
Use a **new database name**, or an explicitly migrated copy:

```json
{
  "embedding_profile": "semantic_default",
  "metadata_version": 3,
  "database": {
    "external_root": "memory_data",
    "relative_path": "semantic-v3.sqlite",
    "create": true
  }
}
```

This is the semantic section, not a complete configuration. Other settings in
`galet_memory_sample.json` remain applicable. Omission of `metadata_version`
continues to select v2. Database schema and configuration versions are separate.
Opening an existing database never upgrades its schema. Both adapters reject the
other metadata version, including direct low-level constructor use. New v3 stores
execute the packaged `galet_memory/schema/embedding_metadata_v3.sql`; the offline
migration executes the same file. Vector dimensions remain fixed at 1536.

## Run high-level checks with your configuration

From the checkout, refresh dependencies and console commands:

```bash
python -m pip install -e ".[test,vec]"
galet-memory-integration --config /path/to/memory_config.json \
  --metadata-version 3 --work-dir /tmp/memory-v3-check
```

`--config` can be omitted when `GALET_MEMORY_CONFIG_PATH` is set. The work
directory must not exist. The runner retains configured component options and
source roots, but redirects **every writable component** to that new directory:
semantic/episodic/working databases, procedural files and digests. It does not
open the database filenames in your config. Original source files are read-only.
Without source arguments, it creates one small fixture in its workspace, so an
unused placeholder `notes` directory does not prevent the test.

To read one of your own text/Markdown assets, supply its exact authorized root
and relative filename, plus a query containing some words from the document:

```bash
galet-memory-integration --config /path/to/memory_config.json \
  --metadata-version 3 --work-dir /tmp/memory-v3-obsidian-check \
  --account junwin --source-root obsidian \
  --source-file "relative/path/to/note.md" --query "words from the note"
```

By default this uses deterministic `sample/offline-hash-v1` embeddings in a new
isolated store. These check plumbing, persistence and keyword behaviour, **not
semantic relevance from OpenAI**. To retain your selected real embedding profile,
add `--live`; that makes paid provider calls and uses Galet credentials (including
`GALET_CREDENTIAL_PATH` or `--credential-path`). There is no silent substitution
between live and offline embedding spaces.

The runner prints JSON and saves `report.json` in the workspace. Exit status is
0 for all checks passing, 1 for failed runtime checks, 2 for setup errors. It tests
file ingestion, unchanged repeat ingestion, vector and keyword recall, account
isolation, source location and reopen persistence. Enabled episodic, procedural
and working components are written and read across reopening. V3 additionally
tests namespace-independent identity and inline text recall. It does not claim
to validate pre-existing databases or run migration against production data.

The module form works before reinstalling console commands:

```bash
python -m galet_memory.examples.integration_runner --config /path/to/memory_config.json \
  --metadata-version 3 --work-dir /tmp/memory-v3-module-check
```

## Resource access and identity

`MemoryRuntime.ingest_file(account_name=..., external_root=..., relative_path=...)`
uses the exact root and path; namespace never inserts a directory. V3 writers
store those values in ordinary columns, never an absolute JSON path. Recall
rechecks the exact root alias through the supplied resolver on every access.
Missing, denied or symlink-escaped assets are reported in `unavailable_resources`;
legacy JSON `path` or cached `text` cannot restore them. The default text loader
supports `.md`/`.txt`; hosts can inject an authorized extraction loader.

`memory.ingestion.ingest_text(account_name=..., source_id=..., text=...)` supports
inline assets. Their text is stored in the authoritative v3 `text` column and
must not be empty. Keywords are required on all v3 index writes (an explicit
empty array is allowed). Keyword results use the column, never a legacy JSON
fallback. New ingestion extracts keywords through the configured extractor.

File identity derives from account plus root/path; inline identity from account
plus source ID. Neither includes namespace. Reingesting unchanged content under
another namespace moves its grouping atomically in metadata and the vector table
without re-embedding. Low-level `index.reclassify(...)` also supports this.
Reingestion finds migrated records by locator and preserves their historical ID,
source ID, document ID and creation time. Ambiguous duplicate migrated locators
are rejected for explicit resolution. First refresh of old records lacking
content hashes may re-embed to establish current content provenance.

## Migration verification and remaining deployment work

The existing offline tool remains the entry point:

```bash
python scripts/migrate_embedding_metadata_v3.py old.sqlite migrated-v3.sqlite \
  --mapping locations.json --roots authorized-roots.json
```

It defaults to dry-run; unresolved locators block applying the entire migration.
After reviewing the report, `--apply` creates a separate copy, retaining the
original and its IDs/vectors. The runtime test suite now exercises a real
sqlite-vec v2 store → dry-run → migration → v3 recall → refresh sequence,
verifying original immutability, vector preservation, and ID/document/timestamp
preservation. These are fixture checks, not evidence about your actual database.

Before production: resolve all local mappings, check vector/metadata ID and profile
parity, implement digest/event publication and ownership-aware recall where used,
and wire the host's authorized roots into v3 assembly. Change the runtime and its
selected database together. Keep the old runtime/database pair for rollback.

## Defer reviewed migration failures

When a dry-run report has been reviewed, `--defer-report` accepts that report's
exact unresolved IDs and source identities. It is not a general ignore-errors
switch. New failures, or failures whose source ID/namespace changed, still block
applying the entire migration. Previously unresolved records that now validate
are migrated normally.

```bash
python scripts/migrate_embedding_metadata_v3.py \
  "$MEMORY_MIGRATION_DIR/source-copy.sqlite" \
  "$MEMORY_MIGRATION_DIR/migrated-v3.sqlite" \
  --mapping "$MEMORY_MIGRATION_DIR/locations.json" \
  --roots /path/to/memory_config.json \
  --defer-report "$MEMORY_MIGRATION_DIR/report.json" \
  > "$MEMORY_MIGRATION_DIR/reviewed-dry-run.json"
```

Review `ready`, `deferred_count`, and `unresolved` in the new report. Retain the
original report as the reviewed input. Add `--apply` to the same command only
when all remaining unresolved records have been addressed; use a different
output report name so the review file is not overwritten.

The destination contains only ready records in active v3 metadata and active
vector tables. All original metadata remains in `embedding_metadata_legacy`.
Deferred vector rows are copied exactly to ordinary `vec_embeddings_deferred`
and/or `vec_embeddings_v2_deferred` tables, then removed from active vector tables
**in the destination copy only**. `embedding_migration_deferred` lists deferred
IDs and reasons. No source records or vectors are deleted. Deferring vec0 rows
requires the installed `sqlite-vec` package or an explicit native library via
`--sqlite-vec-extension`. If archiving fails, the incomplete destination is
removed and the source stays unchanged.

Deferred rows are not exposed by recall and are not declared valid v3 resources.
Keep the source snapshot and archive tables for later recovery; reprocessing
deferred sources is separate work. This lets files proceed while digest or
unmapped-document records wait for their own integration.
