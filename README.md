# Support Chatbot — Ministry of Finance, Government Contracts

An Azerbaijani-language support assistant for the government-contracts (e-tender)
portal. It answers "how do I use this system" questions from a curated knowledge
base, hands anything it cannot answer to a human, and turns those handovers back
into knowledge-base entries.

Built to be multi-tenant from the first commit: the next ministry system is a row
in `tenants`, not a fork.

---

## ⚠️ Current state: development seed data

The knowledge base is **202 synthetically generated entries** (`source='synthetic'`).
They are plausible-sounding and **wrong** — invented button names, invented
deadlines, invented thresholds. They exist so retrieval, the widget and the
analytics could be built and measured before the real content arrived.

Before anything reaches a real user:

```sql
SELECT count(*) FROM kb_entries WHERE source = 'synthetic';  -- must be 0
```

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md#replacing-the-synthetic-knowledge-base).

---

## How it works

```
     end user (widget, embedded in the host app)
                      │
                      ▼
        ┌─────────────────────────────┐
        │  1. rewrite  multi-turn → standalone query
        │  2. retrieve hybrid search + rerank
        │  3. gate     confidence < threshold → escalate
        │  4. generate DeepSeek, grounded, streamed
        │  5. trace    every candidate + score + cost
        └─────────────────────────────┘
                      │
        ┌─────────────┴──────────────┐
        ▼                            ▼
   grounded answer            escalation → support team
   + source citation                  │
   + 👍/👎 rating                      ▼
                              promote to KB entry
                              (draft → manager approves)
```

The confidence gate at step 3 is the load-bearing part. A support bot that says
"I don't know, here is how to reach a human" is worth far more to a ministry than
one that produces a fluent, confident, wrong answer about a procurement deadline.

## Stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + SSE | Streaming answers; Python is where the retrieval stack lives |
| Database | Postgres 16 + pgvector | Vectors, full-text and relational data in one system |
| Embeddings | `BAAI/bge-m3` (local, CPU) | Strong multilingual coverage; no data leaves the machine |
| Reranking | `BAAI/bge-reranker-v2-m3` (local, CPU) | Cross-encoder; the largest single quality gain |
| Generation | DeepSeek V3 (`deepseek-chat`) | Cheap, streams well, ~$0.0006 per answer |
| Widget | Vanilla JS in an iframe | No framework, no host CSS collisions |

Embedding and reranking run **locally**. Only the generation step calls an external
API — and only after the question has already been matched to approved content.

## Quick start

```bash
cp .env.example .env          # add DEEPSEEK_API_KEY
docker compose up -d          # Postgres + pgvector on :5434
uv venv --python 3.12 && uv pip install -e ".[dev]"
./scripts/migrate.sh

python -u scripts/generate_seed_kb.py   # ~200 synthetic AZ Q&A (~$0.05, ~6 min)
python -u scripts/seed_db.py            # tenant + staff + KB + embeddings

uvicorn app.main:app --app-dir api --reload
```

Then open:

| URL | What |
|---|---|
| http://localhost:8000/console | **Staff console** — queue, KB, approvals, analytics |
| http://localhost:8000/demo | Host page with the widget embedded |
| http://localhost:8000/widget | The chat UI on its own |
| http://localhost:8000/docs | OpenAPI reference |
| http://localhost:8000/health | Health check |

**Local staff accounts** (password `dev12345` for all):

| Email | Role | Can |
|---|---|---|
| `admin@mof.local` | admin | Everything, plus staff management |
| `support@mof.local` | support | Draft entries, work the review queue |
| `support2@mof.local` | support | A second author, so 4-eyes can be tested |
| `manager@mof.local` | manager | Approve/publish, analytics, audit |

## Evaluating retrieval

```bash
python -u evals/run_eval.py
```

Reports hit@1/3/5 and MRR for the full pipeline against keyword-only, vector-only
and un-reranked baselines, so the added complexity has to justify itself. Exits
non-zero below the hit@3 threshold, which makes it usable as a CI gate.

Current measured results — **hit@3 100%, off-topic rejection 4/4, retrieval 1.4s,
~$0.0007 per answer**, with **59/59 end-to-end** and **25/25 security** checks passing. Read
[`docs/EVALUATION.md`](docs/EVALUATION.md) for the caveats: the synthetic dataset
is too easy to discriminate between pipelines, and these numbers will move once
real content lands.

```bash
python -u scripts/smoke_test.py     # 59 end-to-end checks
python -u scripts/security_test.py  # 25 attacks, each passing only when blocked
```

Picking this up on another machine or handing it over? Start with
[`HANDOVER.md`](HANDOVER.md).

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Component design, retrieval pipeline, request lifecycle |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | Every table and view, and why it exists |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Architecture decision records, including rejected options |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model, RBAC, PII handling, known gaps |
| [`docs/OPERATIONS_MODEL.md`](docs/OPERATIONS_MODEL.md) | How work reaches a human; content gaps vs contact requests |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operational procedures, go-live checklist |
| [`HANDOVER.md`](HANDOVER.md) | Getting started on a new machine; current status |
| [`docs/API.md`](docs/API.md) | Endpoint reference |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | Measured results, latency, cost, and what they do not prove |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phase status: what is built, what is not |

## Repository layout

```
api/app/
  core/        config, db pool, hash-chained audit, RBAC, rate limiting
  retrieval/   local models + hybrid search
  kb/          ingestion, versioning, embedding refresh
  chat/        prompts, scope guard, orchestration, public streaming API
  admin/       staff console API
infra/postgres/migrations/   numbered SQL, applied by scripts/migrate.sh
widget/        chat UI, embed loader, demo host page
console/       staff console (single self-contained HTML file)
evals/         golden set + evaluation harness
scripts/       generate seed KB, seed database, migrate
docs/          the documents listed above
```
