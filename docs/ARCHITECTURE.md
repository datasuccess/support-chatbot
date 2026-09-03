# Architecture

## Overview

Four deployable pieces, one database.

```
┌───────────────┐   iframe    ┌──────────────────────────────────────┐
│  host app     │────────────▶│  widget  (vanilla JS, no framework)  │
│  (e-tender    │  site key   └──────────────┬───────────────────────┘
│   portal)     │                            │ SSE over fetch
└───────────────┘                            │ Bearer <conversation token>
                                             ▼
┌───────────────┐             ┌──────────────────────────────────────┐
│ staff console │────────────▶│  FastAPI                             │
│ (/console)    │  cookie     │   /api/chat/*    public, token-bound │
└───────────────┘             │   /api/admin/*   staff, RBAC         │
                              └───┬──────────────────────┬───────────┘
                                  │                      │
                   ┌──────────────▼───────┐   ┌──────────▼──────────┐
                   │ local models (CPU)   │   │ DeepSeek V3 (API)   │
                   │  bge-m3 embeddings   │   │  answer generation  │
                   │  bge-reranker-v2-m3  │   │  query rewriting    │
                   └──────────────┬───────┘   └─────────────────────┘
                                  │
                   ┌──────────────▼───────────────────────┐
                   │ Postgres 16 + pgvector               │
                   │  knowledge base · embeddings         │
                   │  conversations · traces · audit      │
                   └──────────────────────────────────────┘
```

## The retrieval pipeline

This is the part that determines whether the product works. Everything else is
plumbing around it.

### 0. Input guard

Before anything is spent, a pattern check refuses political, abusive and
other-participants'-data questions outright — no retrieval, no LLM call, no cost.

The retrieval gate below is the *primary* control and does the real work. This
layer exists because a politically loaded question can incidentally share
vocabulary with the knowledge base ("tender saxtakarlığı" overlaps procurement
content) and might otherwise score high enough to reach the model. Structural
refusal beats asking the model nicely.

See `chat/guard.py`. False refusals are visible in `v_refusal_stats` and cheap to
correct; a political answer published under a ministry logo is not.

### 1. Query rewriting

Multi-turn conversation breaks naive retrieval. A user asks "təklifi necə
göndərim?", gets an answer, then asks "bəs onu ləğv etmək olar?". Sent to the
retriever as-is, that second query contains no retrievable content — the subject
lives in the previous turn.

DeepSeek collapses the exchange into a standalone question before retrieval runs.
Failures here fall back to the raw question rather than taking the answer down.

### 2. Hybrid search

Two searches run against the published, currently-valid entries:

**Vector** — cosine distance over `bge-m3` embeddings via pgvector HNSW. Catches
paraphrase: "pul nə vaxt gəlir?" matches an entry titled "Ödənişlərin icra
müddəti" despite sharing no words.

**Keyword** — Postgres full-text (`simple` configuration) blended with `pg_trgm`
similarity. Catches exact strings: button names, "ASAN İmza", "VÖEN", error codes.
Embeddings blur precisely these into general meaning.

Azerbaijani has no Postgres stemmer, so the `simple` configuration does no
morphological analysis at all. Trigram similarity compensates: it matches
"təklifin" against "təklif" on shared character sequences, which is a crude but
effective substitute for a real stemmer.

### 3. Reciprocal Rank Fusion

The two searches produce scores on incomparable scales — cosine similarity is
bounded 0..1, `ts_rank` is unbounded and corpus-dependent. Averaging them would be
meaningless.

RRF combines *ranks* instead:

```
score(d) = Σ  1 / (k + rank_i(d))        k = 60
```

A document ranked highly by either retriever scores well; one ranked highly by
both scores best. No tuning, no scale normalisation, no per-corpus calibration.

### 4. Cross-encoder reranking

The top 20 fused candidates go to `bge-reranker-v2-m3`, which reads query and
document **together** and scores the pair directly.

This is the largest single quality gain in the pipeline. A bi-encoder compresses
query and document into vectors independently and never actually compares them —
it compares two lossy summaries. A cross-encoder attends across both. The gap
widens on lower-resource languages, which is exactly the Azerbaijani case.

Measured cost: ~57ms per candidate on CPU, so 637ms for the default 10 candidates
— about 95% of total retrieval latency. See `EVALUATION.md` for the full breakdown
and ADR-010 for why the pool is 10 rather than 20.

### 5. Confidence gate

The reranker's top score — already 0..1, since `CrossEncoder` applies its own
sigmoid — is compared against `CONFIDENCE_THRESHOLD` (default 0.10, calibrated from
the measured distribution; see ADR-009). Below it, the bot does not call the
LLM at all — it returns a fixed message offering a human, and opens an escalation
so the support team sees the gap even if the user gives up and leaves.

There are in fact **two** thresholds, producing three outcomes:

| Confidence | Mode | Consequence |
|---|---|---|
| ≥ `CONFIDENCE_THRESHOLD` (0.10) | `grounded` | Answer from the knowledge base |
| ≥ `REFUSAL_THRESHOLD` (0.05) | `escalated` | In scope but unmatched — offer a human, log a content gap |
| below | `refused` | Out of scope — flat refusal, **no operator task** |

The band between them is where a question looks like it belongs to this system but
the knowledge base cannot answer it. Collapsing these two (as an earlier version
did) floods the operator queue with off-topic questions nobody owes a reply to.
See `OPERATIONS_MODEL.md` and ADR-013.

Tuning this is a policy decision, not a technical one. Higher means fewer wrong
answers and more escalations; lower means the reverse. Measured separation on the
golden set is wide — in-scope median 0.968 against an off-topic ceiling of 0.0033 —
which is what makes the gate reliable. Re-calibrate on real content.

### 6. Grounded generation

DeepSeek receives the top 3 entries and a system prompt that forbids using
anything else. Temperature 0.3 — this is procedural text, not prose. The response
streams to the widget over SSE.

### 7. Tracing

Every assistant message writes:

* `message_traces` — confidence, mode, models, tokens, cost, latency breakdown
* `message_retrievals` — every candidate with all four scores and whether it was used

Any past answer can be reconstructed: which entries were retrieved, how they
scored, which version of each was live, what the answer cost.

## Why these choices

**Why not a vector database?** At 200-5000 entries, pgvector on Postgres is faster
than a network hop to a dedicated vector store, and keeps vectors, content,
conversations and audit in one transactional system. A separate vector DB would add
an operational component and a consistency problem to solve a scale problem that
does not exist here.

**Why local embeddings when the data may leave anyway?** Cost is zero, latency is
lower than an API round trip, there is no second vendor to procure, and re-embedding
the corpus after a model change costs nothing but CPU time. The data-residency
benefit is real but incidental.

**Why an iframe for the widget?** CSS isolation in both directions. A widget
injected into the host DOM inherits the host's stylesheet, which is the usual
source of "it looks broken on one page only" reports.

**Why in-process rate limiting?** See `core/ratelimit.py`. At this traffic level a
Redis dependency costs more operational surface than it buys. The trade-off — limits
are per-process, so multiple workers multiply the ceiling — is documented and must
be revisited before the API runs more than one worker.

## Public API authorisation

End users never log in, so authority comes from two unforgeable things:

* **Site key** — per-tenant, public, ships in the host page. Identifies the caller
  so the API can apply per-tenant origin rules and rate limits. CORS cannot do
  this: it is browser-enforced, and a direct HTTP client ignores it entirely.
* **Conversation token** — HMAC-signed, committing to one conversation id. Every
  endpoint derives its conversation from the token, so ownership is structural
  rather than a caller-supplied parameter.

A pre-handover probe found the earlier design trusted a client-supplied
`session_id`: sending someone else's appended messages to their conversation and
pulled their history into the LLM context. See `SECURITY.md` and ADR-011.

Behind a reverse proxy, `request.client.host` is the *proxy's* address, so per-IP
rate limiting collapses into one shared bucket. `core/net.py` honours
`X-Forwarded-For` only when the immediate peer is a configured trusted proxy —
trusting it unconditionally would let anyone sidestep the limiter with a header.

## The staff console

A single self-contained HTML file at `/console`. No build step, no bundler, no
`node_modules`. Served from the API origin, so the session cookie is same-origin
and there is no CORS credential handling at all.

The API is the real contract and is fully tested; the console is forms and tables
over it. Port it to a framework if it grows real client-side state — see ADR-016.

## Multi-tenancy

Every domain table carries `tenant_id`, and every query filters on it. Adding the
next ministry system means inserting a `tenants` row with its own `scope_desc`
(the guardrail text injected into the system prompt) and pointing a widget at it.

This was built in from the first migration deliberately. Retrofitting tenancy onto
a single-tenant schema means touching every table, every query and every index at
exactly the moment the system is already in production.

## Known architectural gaps

Honest list of what is not built:

* **No caching.** Identical questions re-run the full pipeline. Fine at current
  traffic; an obvious win later.
* **Single process.** No horizontal scaling story yet; the in-memory rate limiter
  assumes one worker.
* **The input guard is pattern-based.** It catches obvious cases, not creative
  phrasing. The retrieval gate is the control that actually holds; the guard is
  belt-and-braces for questions that overlap KB vocabulary.
* **No streaming for escalations from the LLM.** Escalation replies are fixed text,
  chunked client-side to keep the rendering path identical.
* **Conversation tokens are bearer tokens.** Anyone holding one can act on that
  conversation. Adequate for anonymous support chat; not sufficient if per-user
  authenticated history is ever required.
* **Query rewriting costs an extra LLM round trip** on every multi-turn message.
  Could be skipped when the question has no pronouns, at the cost of some accuracy.
