# Architecture

## Overview

Four deployable pieces, one database.

```
┌───────────────┐   iframe    ┌──────────────────────────────────────┐
│  host app     │────────────▶│  widget  (vanilla JS, no framework)  │
│  (e-tender    │             └──────────────┬───────────────────────┘
│   portal)     │                            │ SSE over fetch
└───────────────┘                            ▼
                              ┌──────────────────────────────────────┐
                              │  FastAPI                             │
                              │   /api/chat/*    public, anonymous   │
                              │   /api/admin/*   staff, RBAC         │
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

Cost: ~200-400ms on CPU for 20 candidates. At this traffic level, free.

### 5. Confidence gate

The reranker's top logit is squashed through a sigmoid into 0..1 and compared
against `CONFIDENCE_THRESHOLD` (default 0.45). Below it, the bot does not call the
LLM at all — it returns a fixed message offering a human, and opens an escalation
so the support team sees the gap even if the user gives up and leaves.

Tuning this is a policy decision, not a technical one. Higher means fewer wrong
answers and more escalations; lower means the reverse. Set it from the eval
harness's out-of-scope rejection rate, then revisit with real traffic.

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
* **No streaming for escalations from the LLM.** Escalation replies are fixed text,
  chunked client-side to keep the rendering path identical.
* **Session identity is client-supplied.** A `session_id` from `sessionStorage`
  identifies a conversation. Adequate for anonymous support chat, not for anything
  requiring authenticated per-user history.
* **Query rewriting costs an extra LLM round trip** on every multi-turn message.
  Could be skipped when the question has no pronouns, at the cost of some accuracy.
