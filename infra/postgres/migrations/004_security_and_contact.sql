-- 004_security_and_contact.sql
-- Closes the authorisation gaps found by the pre-handover security probe, and
-- separates "the bot could not answer" from "a person wants an operator to call".

-- ---------------------------------------------------------------- site keys
-- The widget authenticates to the API with a per-tenant public site key. This is
-- not a secret (it ships in the page) — it exists so the API can identify and
-- rate-limit a caller server-side. CORS cannot do this: it is enforced by the
-- browser, so curl ignores it entirely.
ALTER TABLE tenants ADD COLUMN site_key TEXT;
UPDATE tenants SET site_key = encode(gen_random_bytes(16), 'hex') WHERE site_key IS NULL;
ALTER TABLE tenants ALTER COLUMN site_key SET NOT NULL;
ALTER TABLE tenants ADD CONSTRAINT tenants_site_key_unique UNIQUE (site_key);

-- Origins allowed to embed this tenant's widget, checked server-side against
-- the Origin/Referer header. Empty array = allow any (local development only).
ALTER TABLE tenants ADD COLUMN allowed_origins TEXT[] NOT NULL DEFAULT '{}';

-- ------------------------------------------------------- account protection
-- Per-IP rate limiting does not stop a distributed brute force against one
-- account. Lockout is per-account and complements it.
ALTER TABLE staff_users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE staff_users ADD COLUMN locked_until TIMESTAMPTZ;

-- ----------------------------------------------------- conversation binding
-- Conversations are now addressed by a server-issued, HMAC-signed token rather
-- than a client-supplied string. session_id stays for correlation but no longer
-- grants access to anything.
ALTER TABLE conversations ADD COLUMN token_issued_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- ----------------------------------------------- content gap vs contact request
-- These were conflated. They are different things with different owners:
--   content_gap     — automatic, internal. The KB is missing something.
--                     Nobody calls the user back.
--   contact_request — explicit, user-initiated, with contact details.
--                     An operator owes this person a reply.
-- Auto-creating an operator task for every unanswered question floods the queue
-- with work nobody asked for, and buries the requests that matter.
CREATE TYPE escalation_kind AS ENUM ('content_gap', 'contact_request');

ALTER TABLE escalations ADD COLUMN kind escalation_kind NOT NULL DEFAULT 'content_gap';
UPDATE escalations SET kind = 'contact_request' WHERE reason = 'user_request';

-- Shown to the user so they can quote it when following up.
ALTER TABLE escalations ADD COLUMN reference_code TEXT;
ALTER TABLE escalations ADD CONSTRAINT escalations_reference_unique UNIQUE (reference_code);

-- How to reach them. Without this an operator cannot act on the request at all.
ALTER TABLE escalations ADD COLUMN contact_name  TEXT;
ALTER TABLE escalations ADD COLUMN contact_phone TEXT;
ALTER TABLE escalations ADD COLUMN contact_email TEXT;
ALTER TABLE escalations ADD COLUMN preferred_channel TEXT
    CHECK (preferred_channel IN ('phone', 'email') OR preferred_channel IS NULL);
ALTER TABLE escalations ADD COLUMN first_response_at TIMESTAMPTZ;

CREATE INDEX ON escalations (tenant_id, kind, status, created_at DESC);

-- ------------------------------------------------------------ refusal log
-- Out-of-scope questions (political, personal, off-topic) are REFUSED, not
-- escalated: they are not content gaps and must never become operator tasks.
-- Logged separately so the team can watch for abuse patterns and for genuine
-- questions being wrongly refused.
CREATE TABLE refusals (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL REFERENCES tenants(id),
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    message_id      BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    question        TEXT NOT NULL,
    category        TEXT NOT NULL,     -- out_of_scope | political | abusive | personal_data
    confidence      REAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON refusals (tenant_id, category, created_at DESC);
