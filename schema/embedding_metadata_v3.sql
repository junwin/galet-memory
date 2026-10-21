-- REVIEW CANDIDATE: resource metadata version 3; not installed by the v2 adapter.
-- Run in a transaction against a NEW database copy after renaming legacy metadata.
-- Vector tables and embedding IDs are preserved. Requires SQLite JSON functions.
CREATE TABLE embedding_schema_version (
    component TEXT PRIMARY KEY NOT NULL CHECK (component = 'embedding_metadata'),
    version INTEGER NOT NULL CHECK (version = 3)
);
INSERT INTO embedding_schema_version VALUES ('embedding_metadata', 3);

CREATE TABLE embedding_metadata (
    id TEXT PRIMARY KEY NOT NULL,
    account_name TEXT NOT NULL CHECK (length(account_name) > 0),
    namespace TEXT NOT NULL DEFAULT '', -- optional grouping, NEVER a path
    source_type TEXT NOT NULL DEFAULT '', -- descriptive legacy/application category
    source_id TEXT NOT NULL DEFAULT '', -- original application identifier
    document_id TEXT NOT NULL DEFAULT '', -- preserved; not reinterpreted as a hash
    resource_type TEXT NOT NULL CHECK
        (resource_type IN ('file', 'digest', 'event_fragment', 'inline')),
    external_root TEXT,
    relative_path TEXT,
    session_id TEXT,
    digest_id TEXT,
    source_event_ids TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(source_event_ids) AND json_type(source_event_ids) = 'array'),
    representation TEXT NOT NULL DEFAULT 'original' CHECK
        (representation IN ('original', 'digest', 'summary', 'abstract', 'fragment')),
    text TEXT, -- optional embedded text/summary snapshot, not proof of source validity
    keywords TEXT NOT NULL
        CHECK (json_valid(keywords) AND json_type(keywords) = 'array'),
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    source_metadata TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(source_metadata) AND json_type(source_metadata) = 'object'),
    created_at TEXT NOT NULL,
    CHECK (
        (resource_type = 'inline' AND external_root IS NULL
            AND relative_path IS NULL AND text IS NOT NULL AND length(trim(text)) > 0)
        OR
        (resource_type <> 'inline' AND external_root IS NOT NULL
            AND length(trim(external_root)) > 0 AND relative_path IS NOT NULL
            AND length(relative_path) > 0 AND relative_path <> '.'
            AND substr(relative_path, 1, 1) <> '/'
            AND instr(relative_path, char(92)) = 0
            AND instr(relative_path, ':') = 0
            AND instr(relative_path, char(0)) = 0
            AND instr('/' || relative_path || '/', '/../') = 0
            AND instr('/' || relative_path || '/', '/./') = 0
            AND instr(relative_path, '//') = 0)
    ),
    CHECK (resource_type <> 'digest' OR
        (session_id IS NOT NULL AND length(session_id) > 0
         AND digest_id IS NOT NULL AND length(digest_id) > 0)),
    CHECK (resource_type <> 'event_fragment' OR
        (session_id IS NOT NULL AND length(session_id) > 0
         AND json_array_length(source_event_ids) > 0))
);
CREATE INDEX idx_emb_v3_account_namespace ON embedding_metadata(account_name, namespace);
CREATE INDEX idx_emb_v3_resource ON embedding_metadata(account_name, external_root, relative_path);
CREATE INDEX idx_emb_v3_digest ON embedding_metadata(account_name, session_id, digest_id);
