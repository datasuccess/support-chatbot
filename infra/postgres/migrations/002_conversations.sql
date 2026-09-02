-- 002_conversations.sql — chat, auditing, ratings, escalation

CREATE TABLE conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id),
    session_id    TEXT NOT NULL,
    -- Opaque reference to the host app's user, if it passes one. Never an email.
    external_ref  TEXT,
    channel       TEXT NOT NULL DEFAULT 'widget',
    user_agent    TEXT,
    ip_hash       TEXT,               -- hashed, never raw: PII minimisation
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    csat          SMALLINT CHECK (csat BETWEEN 1 AND 5),
    csat_comment  TEXT,
    csat_at       TIMESTAMPTZ
);
CREATE INDEX ON conversations (tenant_id, started_at DESC);
CREATE INDEX ON conversations (session_id);

CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system');

CREATE TABLE messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            message_role NOT NULL,
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON messages (conversation_id, created_at);

-- One row per assistant message: everything needed to explain why it answered
-- the way it did, and what it cost.
CREATE TABLE message_traces (
    message_id       BIGINT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    answer_mode      TEXT NOT NULL,       -- grounded | escalated | out_of_scope
    confidence       REAL,
    escalated        BOOLEAN NOT NULL DEFAULT FALSE,
    rewritten_query  TEXT,                -- standalone query after context resolution
    embed_model      TEXT,
    rerank_model     TEXT,
    llm_model        TEXT,
    prompt_tokens    INTEGER,
    completion_tokens INTEGER,
    cost_usd         NUMERIC(10, 6),
    retrieval_ms     INTEGER,
    llm_ms           INTEGER,
    total_ms         INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every candidate considered, with all scores — the audit trail that makes a
-- past answer reconstructable.
CREATE TABLE message_retrievals (
    id            BIGSERIAL PRIMARY KEY,
    message_id    BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    entry_id      BIGINT REFERENCES kb_entries(id) ON DELETE SET NULL,
    entry_version INTEGER,
    rank          SMALLINT NOT NULL,
    vector_score  REAL,
    keyword_score REAL,
    rrf_score     REAL,
    rerank_score  REAL,
    used          BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ON message_retrievals (message_id);
CREATE INDEX ON message_retrievals (entry_id) WHERE used;

-- 👍 / 👎 per answer
CREATE TABLE ratings (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    value       SMALLINT NOT NULL CHECK (value IN (-1, 1)),
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id)
);

CREATE TYPE escalation_status AS ENUM ('open', 'in_progress', 'resolved', 'dismissed');

CREATE TABLE escalations (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenants(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id      BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    reason          TEXT NOT NULL,       -- low_confidence | user_request | negative_rating
    status          escalation_status NOT NULL DEFAULT 'open',
    contact_note    TEXT,                -- what the user typed when asking for a human
    assigned_to     BIGINT REFERENCES staff_users(id),
    -- The flywheel: an escalation resolved into a new KB entry.
    resolved_entry_id BIGINT REFERENCES kb_entries(id) ON DELETE SET NULL,
    resolution_note TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX ON escalations (tenant_id, status, created_at DESC);

-- Append-only, hash-chained audit log. Each row commits to the previous one,
-- so any deletion or edit of history breaks the chain and is detectable.
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    -- Deliberately NOT foreign keys. An audit record must survive deletion of
    -- the thing it describes; an FK from an append-only table would otherwise
    -- make every referenced row permanently undeletable.
    tenant_id   BIGINT,
    actor_id    BIGINT,
    actor_label TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    before      JSONB,
    after       JSONB,
    prev_hash   TEXT,
    row_hash    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (tenant_id, created_at DESC);
CREATE INDEX ON audit_log (entity_type, entity_id);

-- Block UPDATE/DELETE at the database level, not just in application code.
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- For the automated ingest connectors in Phase 10.
CREATE TABLE ingest_runs (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL REFERENCES tenants(id),
    source       TEXT NOT NULL,
    status       TEXT NOT NULL,
    rows_seen    INTEGER NOT NULL DEFAULT 0,
    rows_created INTEGER NOT NULL DEFAULT 0,
    rows_updated INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
