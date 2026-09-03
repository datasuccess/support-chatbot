# Database schema

Postgres 16 with `vector`, `pg_trgm` and `pgcrypto`. Migrations are numbered SQL
files in `infra/postgres/migrations/`, applied by `scripts/migrate.sh`, tracked in
`schema_migrations`.

Every domain table carries `tenant_id`. See ADR-001 and the multi-tenancy section
of `ARCHITECTURE.md`.

---

## Tenancy and staff

### `tenants`
One row per system served. `scope_desc` is injected into the system prompt as the
bot's scope guardrail, which is why the next ministry system is a row rather than
a fork.

| Column | Purpose |
|---|---|
| `site_key` | Public per-tenant key the widget presents. Not a secret — it ships in the host page — but it lets the API identify and rate-limit callers server-side, which CORS cannot do (ADR-012) |
| `allowed_origins` | Origins permitted to embed this tenant's widget, checked against Origin/Referer. Empty = allow any (local development only) |

### `staff_users`
Ministry staff only — end users of the widget are anonymous and never appear here.
`role` is the `staff_role` enum: `admin | support | manager`. Unique on
`(tenant_id, email)`, so the same person can hold different roles across tenants.

`failed_login_attempts` and `locked_until` implement per-account lockout. Per-IP
rate limiting cannot stop a distributed brute force against one account, and
tightening it instead would lock out everyone behind a shared office gateway
(ADR-014).

### `staff_sessions`
Opaque UUID sessions with server-side revocation. Partial index on non-revoked rows.

---

## Knowledge base

### `kb_entries`
The knowledge base itself.

| Column | Purpose |
|---|---|
| `status` | `draft → pending_approval → published`, plus `rejected` and `archived` |
| `valid_from` / `valid_to` | Effective dating. Retrieval filters on these, so an entry can be retired without deletion and past answers stay reconstructable |
| `source` | Provenance: `manual`, `synthetic`, `from_conversation`, or an ingest connector name |
| `external_id` | Stable key from the source system; unique with `(tenant_id, source)` so re-imports update rather than duplicate |
| `content_hash` | SHA-256 of question+answer. Drives both idempotent ingest and stale-embedding detection |
| `citation` | Source shown to the user under the answer |
| `version` | Incremented on every edit; the previous state lands in `kb_entry_versions` |

Indexes: trigram GIN on question and answer, full-text GIN on
`to_tsvector('simple', question || ' ' || answer)`. The `simple` configuration does
no stemming — Azerbaijani has no Postgres dictionary — so trigram similarity carries
the morphological load.

### `kb_entry_versions`
Append-only history. Every edit snapshots the outgoing version first.

### `kb_embeddings`
Separate from content, keyed `(entry_id, model)`. Holding embeddings apart means the
model can change without touching the knowledge base: write vectors under a new
model name, switch `EMBEDDING_MODEL`, delete the old rows. `content_hash` is copied
here so a refresh can tell which vectors went stale.

HNSW index on cosine distance. HNSW rather than IVFFlat because IVFFlat needs
training data to build meaningful lists and this corpus is small.

### `approvals`
Immutable record of each approve/reject decision, with the deciding user and note.

---

## Conversations and tracing

### `conversations`
`ip_hash` is a salted, truncated hash — never the address. `csat` holds the 1-5
end-of-conversation score.

`session_id` is now **server-generated and used only for correlation**. It grants
no access: callers address a conversation with an HMAC-signed token that commits
to its id (ADR-011). Previously the client supplied this value and the server
trusted it, which allowed posting into other people's conversations.

### `messages`
`role` is the `message_role` enum. Content only; everything analytical lives in the
two tables below, which keeps the transcript clean.

### `message_traces`
One row per assistant message: `answer_mode`, `confidence`, `escalated`, the
rewritten query, all three model names, token counts, `cost_usd`, and a latency
breakdown (`retrieval_ms` / `llm_ms` / `total_ms`).

`answer_mode` takes three values, and the distinction drives everything downstream:

| Value | Meaning | Creates |
|---|---|---|
| `grounded` | Answered from the knowledge base | nothing |
| `escalated` | In scope, no good match | a `content_gap` |
| `refused` | Out of scope | a `refusals` row — **never** an operator task |

### `message_retrievals`
One row per candidate considered — not just the ones used. Holds `vector_score`,
`keyword_score`, `rrf_score`, `rerank_score`, `rank` and `used`.

This is what makes an answer explainable after the fact. When a support agent asks
"why did it say that?", this table shows exactly what was retrieved, how each
candidate scored at each stage, and which entry versions were live at the time.
It also distinguishes the two failure modes that look identical from outside: the
entry did not exist (content gap) versus the entry existed but ranked poorly
(retrieval gap).

---

## Feedback and escalation

### `ratings`
👍/👎 per message, unique per message so a user can change their mind. A 👎 opens
an escalation automatically.

### `escalations`
The operator queue, split by `kind` (the `escalation_kind` enum). These are
different things with different urgency, and conflating them buries the ones that
matter (ADR-013):

| `kind` | Created by | Means | Contact details |
|---|---|---|---|
| `content_gap` | System, automatically | The knowledge base is missing something | none |
| `contact_request` | The user, explicitly | **A person is waiting for a reply** | required |

Contact-request columns: `reference_code` (shown to the user, e.g. `DS-WNUHQH`),
`contact_name`, `contact_phone`, `contact_email`, `preferred_channel`.
`first_response_at` is set once and never moved — it measures how long the person
waited before anyone touched their request.

`reason` remains `low_confidence`, `user_request` or `negative_rating`.
`resolved_entry_id` links to the entry the escalation became, closing the flywheel.

### `refusals`
Out-of-scope questions — political, abusive, requests for other participants'
data, or simply unrelated. Logged for monitoring and **deliberately kept out of
the operator queue**: nobody owes these people a callback, and routing them to
humans would swamp the real requests.

`category` is `out_of_scope`, `political`, `abusive` or `personal_data`. Watch it
in both directions: rising political refusals means the guard works; rising
`out_of_scope` on legitimate questions means it is too aggressive.

---

## Audit

### `audit_log`
Append-only and hash-chained. `prev_hash` and `row_hash` form the chain;
UPDATE and DELETE are blocked by trigger.

`tenant_id` and `actor_id` are **deliberately not foreign keys** — an audit record
must outlive the entity it describes. See ADR-007.

### `ingest_runs`
Per-run counters for the automated connectors in Phase 10.

---

## Analytics views

Plain views, not materialised: the data volume is small and managers need live
numbers.

| View | Answers |
|---|---|
| `v_daily_volume` | How much is it used? |
| `v_deflection` | **The headline metric** — what share of answers avoided a human? |
| `v_satisfaction` | 👍/👎 and CSAT over time |
| `v_entry_usage` | Which entries earn their place; which are never retrieved |
| `v_unanswered` | The content-gap queue |
| `v_cost_daily` | Tokens, cost and latency per day |
| `v_kb_health` | Counts by status, plus **how many synthetic rows remain** |

### Attribution views (migration 005)

The data was always captured — `kb_entries.created_by`/`approved_by`,
`kb_entry_versions.changed_by`, `approvals.decided_by`, `audit_log`. These views
make it answerable without hand-written SQL.

| View | Answers |
|---|---|
| `v_contributor_stats` | **Who wrote what** — authored, published, awaiting approval, rejected, entries from conversations, edits made |
| `v_approver_stats` | Who approved or rejected what, and average hours to decide |
| `v_operator_stats` | Operator workload; crucially, **how often gaps became KB entries** — the best single indicator of whether the flywheel turns |
| `v_activity_feed` | Chronological who-did-what, joined to staff names |
| `v_queue_health` | Open/in-progress/resolved by kind, items open over 24h, time to first response |
| `v_refusal_stats` | Refusals per day by category |

---

## Entity relationships

```
tenants ─┬─ staff_users ── staff_sessions
         │
         ├─ kb_entries ─┬─ kb_entry_versions
         │              ├─ kb_embeddings
         │              └─ approvals
         │
         ├─ conversations ── messages ─┬─ message_traces
         │                             ├─ message_retrievals ──▶ kb_entries
         │                             └─ ratings
         │
         ├─ escalations ──▶ conversations, messages, kb_entries

         ├─ refusals ──▶ conversations, messages
         └─ ingest_runs

audit_log   (no FKs — intentionally detached)
```

## Migration history

| File | Adds |
|---|---|
| `001_init.sql` | Tenants, staff, sessions, KB entries + versions + embeddings, approvals |
| `002_conversations.sql` | Conversations, messages, traces, retrievals, ratings, escalations, hash-chained audit, ingest runs |
| `003_analytics.sql` | Seven operational views |
| `004_security_and_contact.sql` | Site keys, allowed origins, account lockout, `escalation_kind` split, contact-request columns, `refusals` |
| `005_attribution.sql` | Six attribution and queue-health views |
