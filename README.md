# galet-memory

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
