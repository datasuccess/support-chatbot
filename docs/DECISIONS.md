# Architecture Decision Records

Each record states the decision, the reasoning, what was rejected, and the cost of
being wrong. Records are append-only; superseded ones are marked, not deleted.

---

## ADR-001 — Postgres + pgvector rather than a dedicated vector database

**Status:** accepted · 2026-09-02

**Context.** The knowledge base is ~200 entries today and unlikely to exceed a few
thousand. Retrieval needs vector similarity, keyword search and relational joins
against conversations and audit records.

**Decision.** One Postgres instance with `pgvector` and `pg_trgm`.

**Rejected.**
* *Qdrant / Weaviate / Pinecone* — a network hop and a second system to operate,
  back up and secure, in exchange for scale characteristics that do not matter
  below roughly a million vectors.
* *In-memory brute force* — genuinely viable at 200 rows, but throws away keyword
  search and every relational query, and would need replacing later anyway.

**Cost of being wrong.** Low. Migrating out of pgvector at a few thousand vectors
is a day of work.

---

## ADR-002 — Local embeddings, external generation

**Status:** accepted · 2026-09-02

**Context.** DeepSeek sells chat models but no embeddings endpoint, so "use
DeepSeek" cannot cover retrieval. The content is Azerbaijani, a lower-resource
language where embedding quality varies sharply between models.

**Decision.** `bge-m3` embeddings and `bge-reranker-v2-m3` reranking, both running
locally on CPU. DeepSeek V3 for generation and query rewriting only.

**Reasoning.** Zero marginal cost, no second API vendor, re-embedding after a model
change costs only CPU time, and strong multilingual coverage including Azerbaijani.
The stakeholder confirmed data may leave the country, so residency was not the
deciding factor — but keeping embeddings local means the *corpus* never leaves
regardless.

**Rejected.**
* *OpenAI `text-embedding-3-large`* — good, but a second vendor and weaker on
  low-resource Turkic languages than `bge-m3`.
* *Self-hosted generation* — no GPU available; a CPU-hosted model good enough for
  Azerbaijani generation does not exist at usable latency.

**Cost of being wrong.** Low. Embeddings live in their own table keyed by model
name, so switching means re-running one script.

---

## ADR-003 — Hybrid retrieval with RRF, plus a cross-encoder reranker

**Status:** accepted · 2026-09-02

**Context.** The corpus is full of exact UI strings ("Təklif ver", "ASAN İmza",
"VÖEN") alongside conceptual questions phrased nothing like the entry titles.

**Decision.** Run vector and keyword search in parallel, fuse with Reciprocal Rank
Fusion (k=60), rerank the top 20 with a cross-encoder, keep the top 3.

**Reasoning.** Each retriever covers the other's failure mode. RRF combines ranks
rather than scores, avoiding the meaningless exercise of normalising cosine
similarity against `ts_rank`. The reranker then does the comparison neither
bi-encoder can: reading query and document together.

`evals/run_eval.py` measures all four configurations so this decision stays
falsifiable rather than assumed.

**Rejected.**
* *Vector-only* — misses exact terminology, which is most of a UI help corpus.
* *Keyword-only* — Azerbaijani morphology without a stemmer makes this weak.
* *Weighted score blending* — requires per-corpus calibration that drifts as
  content grows.

**Cost of being wrong.** Low; the harness will show it.

---

## ADR-004 — Confidence gate and escalation instead of always answering

**Status:** accepted · 2026-09-02

**Context.** A ministry system. A fluent wrong answer about a procurement deadline
is worse than no answer, because users act on it.

**Decision.** Below `CONFIDENCE_THRESHOLD`, do not call the LLM. Return a fixed
message offering a human, and open an escalation automatically.

**Reasoning.** The failure mode of retrieval-augmented generation is the model
filling gaps from general knowledge. The only reliable defence is refusing to
generate when retrieval came back weak. Auto-opening the escalation means the
support team sees the content gap even when the user silently gives up.

**Cost of being wrong.** A threshold set too high produces unnecessary escalations
— annoying but safe. Too low produces confident wrong answers — the failure that
damages trust in the system. The asymmetry justifies erring high.

---

## ADR-005 — Answers are generated, not returned verbatim

**Status:** accepted · 2026-09-02 · **supersedes an earlier verbatim-only recommendation**

**Context.** The original plan assumed procurement *law* content, where paraphrase
carries legal risk. The stakeholder clarified the scope: these are "how do I use
the system" questions, non-confidential, with no legal-interpretation component.

**Decision.** DeepSeek rewrites retrieved entries into a direct answer, streamed
token by token, with emoji and markdown formatting.

**Reasoning.** The risk that justified verbatim-only does not exist at this scope.
A wrong answer means a confused supplier retries a form, not a missed statutory
deadline. Rewriting handles multi-turn conversation and question phrasing that
verbatim retrieval cannot.

**Residual risk, and how it is handled.** The exact wording shown to users is not
individually approved. Mitigations: strict grounding in the system prompt, a
visible source citation under every answer, the confidence gate, and the review
queue surfacing drift.

**Reversal path.** If generation proves unreliable in Azerbaijani, `answer_mode`
already distinguishes `grounded` from `escalated`; adding `verbatim` is a small
change in `chat/service.py`.

---

## ADR-006 — Three roles, with four-eyes approval

**Status:** accepted · 2026-09-02

**Decision.** `support` (drafts, works the queue), `manager` (approves, sees
analytics and audit), `admin` (everything plus staff management). Publishing
requires a different person from the author, enforced in the API.

**Reasoning.** The stakeholder asked for exactly three roles. Four-eyes is the one
control worth keeping from the larger enterprise list: it costs almost nothing to
build now and is awkward to retrofit once content is flowing.

**Note.** Admin is implicitly permitted everywhere, but the four-eyes check is on
identity, not role — an admin still cannot approve their own draft.

---

## ADR-007 — Hash-chained, append-only audit log

**Status:** accepted · 2026-09-02

**Decision.** `audit_log` rejects UPDATE and DELETE via database trigger, and each
row's hash commits to its predecessor's.

**Reasoning.** Two independent layers. The trigger stops accidental and casual
modification. The chain detects tampering by anyone who can bypass the trigger —
including someone with direct database access — because altering any historical row
breaks every hash after it. `GET /api/admin/audit/verify` recomputes the chain.

**Note.** `audit_log.tenant_id` and `actor_id` are deliberately *not* foreign keys.
An audit record must outlive the entity it describes; an FK from an append-only
table makes every referenced row permanently undeletable. This was found by hitting
exactly that error during the first schema test.

---

## ADR-008 — Synthetic seed content, clearly marked

**Status:** accepted · 2026-09-02 · **must be reversed before go-live**

**Context.** The real knowledge base was not available. Retrieval, the widget, the
review queue and the analytics all needed realistic content to be built against.

**Decision.** Generate ~200 Azerbaijani Q&A pairs with DeepSeek, written with
`source='synthetic'`.

**Risk.** This content is plausible and wrong — invented button names and
deadlines. Plausible-wrong is more dangerous than obviously-wrong, because nobody
double-checks it.

**Control.** `SELECT count(*) FROM kb_entries WHERE source='synthetic'` must return
0 before go-live. Documented in the README, `docs/RUNBOOK.md`, and the generator's
own docstring.
