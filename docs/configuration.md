# Configuring galet-memory

Galet-memory accepts a versioned JSON object through `load_memory_config()` and
`build_memory()`. The same public entry points are used by the runnable sample,
integration tests and host applications. Existing constructor-based APIs remain
available.

This first configuration implementation assembles the **existing v2 semantic
backend**, v2 SQLite episodic memory, file procedural memory and SQLite working
memory. It does not activate the separate embedding metadata v3 proposal or
change Lucy's configuration. Galet-tools and galet-prompt-builder configuration
loaders are subsequent changes.

## Select one source

Configuration selection is:

1. Explicit JSON-compatible mapping passed as `config`.
2. Explicit file `path` (the sample exposes `--config`).
3. The file named by `GALET_MEMORY_CONFIG_PATH`.

There is no implicit working-directory file or usable storage default, and no
automatic merge. An invalid explicitly selected source fails; it never falls
through to another source. Importing the package does not discover configuration,
open storage or construct provider clients. Loaders return a validated immutable
settings snapshot; changes take effect on an explicit reload/reassembly.

```bash
export GALET_MEMORY_CONFIG_PATH=/home/junwin/.config/galet/memory.json
```

Each JSON document carries `config_version: 1`. This version is independent of
the database schema version. The authoritative, packaged JSON Schema is
[`configuration.schema.json`](../src/galet_memory/configuration.schema.json),
also available through `configuration_schema()`. Unknown fields, malformed JSON,
duplicate keys, invalid types and invalid references fail validation. Errors
identify the source/field without echoing configuration values.

## Share roots and profiles

A memory config can reference shared files:

```json
{
  "config_version": 1,
  "external_roots_file": "roots.json",
  "embedding_profiles_file": "embedding-profiles.json",
  "memory": {
    "semantic": {
      "embedding_profile": "semantic_default",
      "database": {
        "external_root": "memory_data",
        "relative_path": "semantic.sqlite",
        "create": true
      },
      "keywords": {"extractor": "simple", "max_keywords": 20}
    }
  }
}
```

`roots.json` contains:

```json
{
  "config_version": 1,
  "external_roots": {
    "memory_data": "/home/junwin/lucy_storage/data",
    "obsidian": "/home/junwin/Documents/mynotes"
  }
}
```

`embedding-profiles.json` contains:

```json
{
  "config_version": 1,
  "embedding_profiles": {
    "semantic_default": {
      "provider": "openai",
      "model": "text-embedding-3-small",
      "dimensions": 1536
    }
  }
}
```

References are relative to the containing config file, independent of the shell
working directory. Shared documents are terminal: recursive includes, conflicting
inline definitions and extra fields are rejected. Applications may instead supply
`external_roots` and `embedding_profiles` inline using the same shapes. Passing a
mapping with relative file references requires an explicit `base_dir`.

Root directory values must be absolute. Resource locations always use a root
alias plus a portable relative path. No account or namespace directory is added.
Missing aliases, absolute resource paths and parent traversal are rejected.

The shared profile document defines the portable profile portion of the agreed
ecosystem contract. It is **not** a newly implemented Galet general configuration
loader; `GALET_CONFIG_PATH` discovery is not added by this change. Galet remains
responsible for provider settings and credential resolution.

## Assemble memory with Galet

```python
from galet.settings import Settings
from galet_memory import build_memory, galet_embedding_factory, load_memory_config

config = load_memory_config()  # explicit file or object also supported
factory = galet_embedding_factory(Settings())

with build_memory(config, embedding_provider_factory=factory) as memory:
    memory.ingest_file(
        account_name="junwin",
        external_root="obsidian",
        relative_path="art/Music/Ode to Joy Schiller.md",
    )
    found = memory.recall(account_name="junwin", query="Schiller music")
```

The factory argument is optional: semantic assembly defaults to
`galet_embedding_factory()`. This constructs the fixed OpenAI or Mistral
implementation selected by the profile, with Galet's existing `Settings` defaults.
It does not use a router that can silently switch embedding spaces.

`GALET_CREDENTIAL_PATH` continues to identify Galet's credential directory. Direct
provider key environment variables and explicit Galet settings keep Galet's
existing precedence. A profile can include `credential_profile`; its definition
is supplied through Galet `Settings(credential_profiles=...)` and its selected
source is resolved by Galet. Secrets are never copied into memory JSON or metadata.
Galet versions supporting these settings are required (`galet>=0.1.24`).

Custom providers use the documented factory seam: the callable receives an
`EmbeddingProfile` and returns an object exposing the same `profile` plus
`embed(texts, *, model)`. The returned profile is checked before storage opens.
The provider must remain in that embedding space and must not silently fall back
to another provider/model. Provider clients are caller-owned; the runtime closes
the SQLite stores that it creates, including on partial assembly failure.

The built-in Galet adapter checks the returned model. Every provider result is
checked for vector count, dimensions and finite numeric values. Incompatible
vectors/models raise an error instead of being disguised as unavailable-provider
keyword fallback. Ordinary provider outages can still fall back to keywords.

The current SQLite vector backend supports **1536 dimensions**. Other dimensions
are rejected before provider construction or storage creation. A provider/model
must actually produce that dimensionality; this change does not add dimension
shortening parameters to Galet. Existing semantic metadata must have the same
provider, model and dimensions as the selected profile across the whole index;
equal dimensions alone do not establish compatibility. Unknown provenance or a
different profile requires an explicit data migration/reindexing decision.

## Enabled memory components

Only components present under `memory` are assembled. At least one is required.
The [complete installed sample](../src/galet_memory/examples/config/memory.json)
contains all four. Their settings are:

| Component | Required settings | Optional settings/defaults |
| --- | --- | --- |
| semantic | database, embedding_profile | max_chars=32000; simple keyword extractor, max_keywords=20 |
| episodic | database | digests_directory locator |
| procedural | root directory locator | context_resolution=merge (or most_specific) |
| working | database | max_value_bytes=65536 |

Every database is a root-relative locator, with `create` defaulting to **false**.
`create: true` permits a missing database file with an existing authorized parent
directory. It does not create directories, overwrite files, or upgrade an existing
schema. Database files must be distinct across components. Existing v3 metadata
and noncanonical semantic layouts are rejected before any writer opens them.
Old stores missing keyword columns require an explicit migration rather than an
automatic schema alteration on open.

`memory.episodic`, `memory.procedural` and `memory.working` expose their existing
APIs on the returned runtime. This assembly does not automatically configure
digest generation/publication or episodic digest search; those remain explicit
host services until their configuration contract is added.

Semantic ingestion always supplies a keyword extractor and records its configured
provider/model. `memory.recall()` searches all namespaces for the given account
when no namespace filter is supplied. The current v2 ingestion API uses the
internal default grouping `external`; grouping is never a directory. The existing
v2 ID derivation still includes namespace; decoupling that identity belongs to
the v3 adapter work. Account/session/request identities remain call arguments.

## Resource access

`RootResourceResolver` treats the explicitly supplied root mapping as its
allowlist. Hosts can pass a narrower `resource_resolver` implementing
`resolve(location, *, must_exist, directory)` and `authorize_path(path)` for
account/agent policy. A mapping does not grant cross-account access by itself;
the application chooses the authorized mapping/resolver for each runtime.

Source reads reauthorize paths through the resolver every time and reject symlink
escapes. The default text loader supports `.md` and `.txt`. A host can supply
`text_loader` for PDF or other extraction; it receives an authorized path from
the same wrapper. This does not claim protection against hostile concurrent
filesystem replacement between path validation and opening.

Configured ingestion records `external_root` and `relative_path` in v2 metadata,
alongside the legacy absolute `path` needed by the current adapter. The explicit
v3 locator columns are a separate deployment. Existing absolute metadata paths
are readable only when they remain inside the resolver's authorized roots.

## Run the same sample that tests exercise

Install `galet-memory[vec]`, then create a new sample directory:

```bash
galet-memory-configured --init-sample /tmp/my-memory-sample
export GALET_MEMORY_CONFIG_PATH=/tmp/my-memory-sample/memory.json
galet-memory-configured --offline
```

The module form also works:

```bash
python -m galet_memory.examples.configured_memory --config /tmp/my-memory-sample/memory.json --offline
```

Initialization copies the packaged JSON files, fills in absolute roots for the
chosen directory and writes a sample Markdown note. It refuses an existing
directory. The offline profile is explicitly `sample/offline-hash-v1` and uses
`semantic-offline.sqlite`; fake vectors cannot be labeled OpenAI vectors.
The sample exercises assembly, ingestion, vector recall and keyword recall.

For a live provider, edit the semantic profile selection to `semantic_default`,
choose a different database filename such as `semantic-live.sqlite`, configure
Galet credentials, and omit `--offline`. No live request is made by the tests.

Integration tests initialize these same packaged JSON files in a temporary
directory, load them through the public loader, inject `OfflineSampleProvider`,
and exercise real sqlite-vec storage. They do not construct a separate test-only
configuration shape. Tests also cover selection precedence, invalid shared
references, import-time behavior, path authorization, model/dimension mismatches,
and cleanup. Galet response-model verification is tested with a stub API response.
