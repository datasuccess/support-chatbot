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

---

## ADR-009 — Confidence threshold calibrated from measurement, not intuition

**Status:** accepted · 2026-09-02

**Context.** `CONFIDENCE_THRESHOLD` shipped at 0.45, chosen as a plausible-looking
midpoint. The first evaluation run rejected **0 of 4** off-topic questions.

**Root cause.** `sentence-transformers`' `CrossEncoder` already applies a sigmoid
for single-label rerankers (`activation_fn=Sigmoid()`). `_confidence()` applied a
second one, compressing every score into `sigmoid(0)=0.500`..`sigmoid(1)=0.731`.
No threshold below 0.5 could ever fire.

**Decision.** Use the reranker score directly, and set the threshold to **0.10**
from the measured distribution: in-scope median 0.968 and 10th percentile 0.319,
against an off-topic ceiling of 0.0033 — roughly 30x clearance.

**What this says about the process.** Retrieval metrics did not catch it, and could
not have: ranking is invariant under any monotonic transform, so hit@k was
unaffected. Only the out-of-scope test exposed it. A suite that measured retrieval
alone would have shipped a bot that answered every question about the weather.

**Follow-up.** Re-calibrate against real content before go-live. The runbook has
the queries.

---

## ADR-010 — Ten rerank candidates, not twenty

**Status:** accepted · 2026-09-02

**Context.** Reranking is ~95% of retrieval latency at roughly 57ms per candidate
on CPU, scaling linearly: 20 candidates cost 1144ms, 10 cost 637ms, 5 cost 310ms.

**Decision.** Default to 10.

**Reasoning.** Retrieval quality measured identically at 5, 10 and 20 on the golden
set. Ten keeps a reasonable pool for the reranker while halving the latency, and
lands perceived time-to-first-token near 2 seconds.

**Caveat.** This was measured on synthetic content where the correct entry is
almost always already ranked first by vector search. Real content is likely to need
a wider pool. `RETRIEVAL_CANDIDATES` is configurable; re-measure at Phase 8 rather
than assuming this default still holds.

---

## ADR-011 — Signed conversation tokens instead of client-supplied session ids

**Status:** accepted · 2026-09-02

**Context.** A pre-handover security probe found the widget's `session_id` was
made up by the client and trusted by the server. Sending someone else's id
appended messages to *their* conversation and pulled *their* history into the LLM
context. `/rate` and `/csat` had the same shape: any `message_id` was accepted.

**Decision.** The server mints an HMAC-signed token committing to a conversation
id. Every endpoint derives its conversation from the token; ownership is never a
caller-supplied parameter.

**Why not a cookie.** The widget runs in a cross-origin iframe, where third-party
cookie behaviour varies by browser and is actively being restricted. A bearer
token in a header is explicit and works everywhere.

**Scope.** This proves "you started this conversation" — the exact authority an
anonymous support chat needs, and no more. It is not a login.

---

## ADR-012 — Site keys, because CORS is not a server-side control

**Status:** accepted · 2026-09-02

**Context.** `WIDGET_ALLOWED_ORIGINS` looked like access control. It is not: CORS
is enforced by the *browser*. The probe confirmed a plain `curl` with
`Origin: https://evil.example` returned 200.

**Decision.** Per-tenant public site keys, validated server-side, plus an optional
per-tenant origin allow-list checked against Origin/Referer.

**Honest limits.** A site key ships in the page and is not a secret; headers can be
forged. This identifies callers and enables per-tenant rate limiting — it does not
authenticate them. Genuine abuse resistance is rate limiting plus the fact that the
endpoint exposes nothing but public knowledge-base content.

---

## ADR-013 — Refusals are not escalations

**Status:** accepted · 2026-09-02

**Context.** Every low-confidence answer auto-created an operator task. A political
question therefore generated work for a human who owed nobody a reply.

**Decision.** Three outcomes, not two:

| Outcome | Record | Operator task? |
|---|---|---|
| `grounded` | normal answer | no |
| `escalated` | `escalations(kind='content_gap')` | no — content work |
| `refused` | `refusals` | **never** |

A real operator task is created only when a person fills in the contact form with
their name and a way to reach them.

**Reasoning.** A queue's value is inversely proportional to how much of it is
noise. Burying three people waiting for a callback under two hundred off-topic
questions makes the queue worthless, and the team stops looking at it.

---

## ADR-014 — Per-account lockout over aggressive per-IP login limits

**Status:** accepted · 2026-09-02

**Context.** The login limiter was 5 attempts per 5 minutes per IP. Ministry staff
share office NAT gateways, so five typos from one floor would lock out everyone
behind that address.

**Decision.** Loosen the per-IP limit to 30/5min; add per-account lockout at 5
failures for 15 minutes.

**Reasoning.** These control different attacks. Per-IP blunts naive scripted
attempts from one address. Per-account is the real brute-force defence and holds
regardless of how many addresses an attacker has. Only the second justifies being
tight, and only it fails safe for legitimate users — one account locks, not a
building.

**Found by:** running the security and end-to-end suites back to back. The second
suite could not log in at all, which is exactly what a shared office gateway would
have experienced on day one.

---

## ADR-015 — Duplicate knowledge-base content is a 409, not a 500

**Status:** accepted · 2026-09-02

**Context.** Creating an entry with content identical to an existing one raised a
raw `UniqueViolation`, surfacing as an unexplained 500.

**Decision.** Detect it and return 409 naming the existing entry. Applied to
creation, promotion from a conversation, and editing.

**Why not just allow duplicates.** Near-identical entries split the retrieval
signal between them and make ranking worse as content grows. The constraint is
worth keeping; only the error handling was wrong.

---

## ADR-016 — The staff console is one static HTML file

**Status:** accepted · 2026-09-02

**Context.** The console was the largest remaining gap. The obvious choice was
Next.js, which the team already uses.

**Decision.** A single self-contained HTML file served by the API at `/console`.
No build step, no `node_modules`, no bundler.

**Reasoning.** The API is the real contract, and it is fully tested. The console is
forms and tables over it. A single file can be read, hosted or handed over by
anyone without a toolchain, and being served from the API origin means the session
cookie is same-origin — no CORS credential handling at all.

**When to revisit.** If the console grows real client-side state — offline drafts,
optimistic updates, complex routing — port it. Until then this is less to maintain,
not a compromise.
