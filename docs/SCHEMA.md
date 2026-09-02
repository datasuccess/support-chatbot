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

### `staff_users`
Ministry staff only — end users of the widget are anonymous and never appear here.
`role` is the `staff_role` enum: `admin | support | manager`. Unique on
`(tenant_id, email)`, so the same person can hold different roles across tenants.

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
One per `(tenant, session_id)`. `ip_hash` is a salted, truncated hash — never the
address. `csat` holds the 1-5 end-of-conversation score.

### `messages`
`role` is the `message_role` enum. Content only; everything analytical lives in the
two tables below, which keeps the transcript clean.

### `message_traces`
One row per assistant message: `answer_mode`, `confidence`, `escalated`, the
rewritten query, all three model names, token counts, `cost_usd`, and a latency
breakdown (`retrieval_ms` / `llm_ms` / `total_ms`).

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
The support team's work queue. `reason` is `low_confidence`, `user_request` or
`negative_rating`. `resolved_entry_id` links to the knowledge-base entry the
escalation became — closing the flywheel loop.

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
         └─ ingest_runs

audit_log   (no FKs — intentionally detached)
```
