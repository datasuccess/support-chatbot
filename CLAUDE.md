# Support chatbot — working notes

Azerbaijani-language support assistant for the Ministry of Finance
government-contracts portal. Read `HANDOVER.md` first, then `docs/ARCHITECTURE.md`.

## Commands

```bash
docker compose up -d                                  # Postgres 16 + pgvector on :5434
./scripts/migrate.sh                                  # apply migrations
.venv/bin/uvicorn app.main:app --app-dir api --reload # API on :8000

.venv/bin/python -u scripts/smoke_test.py       # 59 end-to-end checks
.venv/bin/python -u scripts/security_test.py    # 25 attacks, all must be blocked
.venv/bin/python -u evals/run_eval.py           # retrieval quality gate
```

Always `.venv/bin/python`, never the system interpreter — the venv has torch and
the models.

Run **both** test suites after touching the API. The security suite is not
optional: every check corresponds to a real hole found in a pre-handover probe.

## Non-obvious things that will bite you

**`docker exec` needs `-i` with a pipe.** `docker exec ... psql < file.sql`
silently no-ops with exit 0. Use `cat file.sql | docker exec -i ... psql`.
`scripts/migrate.sh` already does this.

**Postgres is on 5434**, not 5432 or 5433 — other local projects use those.

**Add explicit casts to query parameters.** `%s` in `LIMIT`, `generate_series`,
`IS NULL` or any overloaded context throws "could not determine data type of
parameter $N". Write `%s::int`, `%s::text`, `%s::kb_status`.

**Never raise `HTTPException` inside middleware.** Middleware runs outside the
routing layer, so it never reaches FastAPI's exception handlers and surfaces as a
bare 500. Return a `JSONResponse` instead. This exact bug made every rate-limited
request a 500 instead of a 429.

**The CrossEncoder already applies a sigmoid.** `bge-reranker-v2-m3` scores arrive
in 0..1. Applying another one compresses everything into [0.5, 0.73] and silently
disables the confidence gate. See ADR-009.

**`audit_log` is append-only.** UPDATE and DELETE are blocked by trigger, and rows
are hash-chained. Never try to clean it up; entries referencing it cannot be
deleted either.

**Never `TRUNCATE ... CASCADE`.** Use `UPDATE ... SET NULL` then `DELETE`.

**Models load at startup** (~20-30s from cache, several minutes on first run while
~2.5GB downloads). Wait for `ready` in the log before testing.

**Rate limits will trip your test runs**: 20 messages/min, 30 sessions/min, 30
logins/5min per IP. The test helpers back off on 429 — keep that behaviour rather
than loosening the limiter to make tests pass.

## Conventions

- Migrations are numbered `.sql` files in `infra/postgres/migrations/`, applied by
  `scripts/migrate.sh`, tracked in `schema_migrations`. Never edit an applied one.
- Every domain table carries `tenant_id`, and every query filters on it.
- `OpenAI(...)` clients always take `timeout=` and `max_retries=` — the SDK default
  is no timeout, which turns one stuck call into an indefinite hang.
- Run long scripts with `python -u` so progress is visible.
- Comments explain *why*, not what. Match the surrounding density.

## Rules that are not negotiable

**Synthetic content must never reach a real user.** All 202 seed entries are
LLM-generated and wrong — fabricated button names, deadlines, thresholds.
`SELECT count(*) FROM kb_entries WHERE source='synthetic'` must be 0 before
go-live.

**Refusals never create operator tasks.** Three outcomes: `grounded`, `escalated`
(content gap, internal), `refused` (logged to `refusals`, no human involved). A
real operator task exists only when a person submits the contact form. See
`docs/OPERATIONS_MODEL.md`.

**Four-eyes is enforced on identity, not role.** An admin cannot approve their own
draft either.

**The bot answers only from retrieved knowledge-base entries.** If you change
`chat/prompts.py`, keep grounding stated as an absolute and re-run the security
suite — it tests political, off-topic, other-participants'-data and prompt-injection
refusal.

## Layout

```
api/app/core/        config, db pool, hash-chained audit, RBAC, session tokens,
                     proxy-aware IP resolution, rate limiting
api/app/retrieval/   bge-m3 + bge-reranker-v2-m3 (local, CPU), hybrid search
api/app/kb/          ingestion, versioning, embedding refresh
api/app/chat/        prompts, scope guard, orchestration, public streaming API
api/app/admin/       staff console API
console/index.html   staff console — one self-contained file, no build step
widget/              chat UI, embed loader, demo host page
evals/               golden set + evaluation harness
docs/                architecture, schema, decisions (16 ADRs), security, runbook
```

## Before proposing a design change

`docs/DECISIONS.md` has 16 ADRs with rejected alternatives and the cost of being
wrong — including choices that were made and later reversed. Check whether your
idea was already considered.
