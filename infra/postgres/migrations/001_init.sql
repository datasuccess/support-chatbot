-- 001_init.sql — core schema
-- Support chatbot for Ministry of Finance government-contracts system.
-- Multi-tenant from day one: every domain table carries tenant_id.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- tenants
CREATE TABLE tenants (
    id          BIGSERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    -- Scope guardrail: injected into the system prompt so the bot declines
    -- anything outside this system rather than inventing an answer.
    scope_desc  TEXT NOT NULL DEFAULT '',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------ staff users
-- End users of the widget are anonymous chat sessions and are NOT stored here.
-- This table is ministry staff only.
CREATE TYPE staff_role AS ENUM ('admin', 'support', 'manager');

CREATE TABLE staff_users (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id),
    email         TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          staff_role NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE TABLE staff_sessions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    staff_id     BIGINT NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    expires_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX ON staff_sessions (staff_id) WHERE revoked_at IS NULL;

-- -------------------------------------------------------------- knowledge
CREATE TYPE kb_status AS ENUM ('draft', 'pending_approval', 'published', 'archived', 'rejected');

CREATE TABLE kb_entries (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id),
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    category      TEXT,
    tags          TEXT[] NOT NULL DEFAULT '{}',
    lang          TEXT NOT NULL DEFAULT 'az',
    status        kb_status NOT NULL DEFAULT 'draft',

    -- Effective dating: procedures change, and we must be able to prove what
    -- the bot was serving on any past date.
    valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to      TIMESTAMPTZ,

    -- Provenance. 'synthetic' marks development seed data that must never
    -- reach a real user; Phase 8 replaces every such row.
    source        TEXT NOT NULL DEFAULT 'manual',
    external_id   TEXT,
    content_hash  TEXT NOT NULL,

    -- Where this answer came from, shown to the user under the answer.
    citation      TEXT,

    created_by    BIGINT REFERENCES staff_users(id),
    approved_by   BIGINT REFERENCES staff_users(id),
    approved_at   TIMESTAMPTZ,
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source, external_id)
);
CREATE INDEX ON kb_entries (tenant_id, status);
CREATE INDEX ON kb_entries (tenant_id, category);
CREATE INDEX kb_entries_question_trgm ON kb_entries USING gin (question gin_trgm_ops);
CREATE INDEX kb_entries_answer_trgm   ON kb_entries USING gin (answer gin_trgm_ops);
-- Azerbaijani has no Postgres stemmer, so 'simple' + trigram carries keyword search.
CREATE INDEX kb_entries_fts ON kb_entries
    USING gin (to_tsvector('simple', question || ' ' || answer));

-- Full edit history — an append-only record of every version ever published.
CREATE TABLE kb_entry_versions (
    id          BIGSERIAL PRIMARY KEY,
    entry_id    BIGINT NOT NULL REFERENCES kb_entries(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    status      kb_status NOT NULL,
    citation    TEXT,
    changed_by  BIGINT REFERENCES staff_users(id),
    change_note TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entry_id, version)
);

-- Embeddings live apart from content so the model can be swapped and the
-- whole corpus re-embedded without touching the knowledge base itself.
CREATE TABLE kb_embeddings (
    entry_id     BIGINT NOT NULL REFERENCES kb_entries(id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding    vector(1024) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entry_id, model)
);
CREATE INDEX kb_embeddings_hnsw ON kb_embeddings
    USING hnsw (embedding vector_cosine_ops);

-- 4-eyes control: support drafts, manager approves.
CREATE TABLE approvals (
    id          BIGSERIAL PRIMARY KEY,
    entry_id    BIGINT NOT NULL REFERENCES kb_entries(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    decision    TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_by  BIGINT NOT NULL REFERENCES staff_users(id),
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
