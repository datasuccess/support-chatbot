# Roadmap and phase status

Honest status. "Built" means working and exercised locally, not sketched.

---

## ✅ Phase 0 — Scaffold
Docker Compose (Postgres 16 + pgvector on :5434), numbered SQL migrations with a
tracking table, `uv` + Python 3.12 environment, private repository.

## ✅ Phase 1 — Knowledge base
202 synthetic Azerbaijani Q&A across 10 categories, generated with DeepSeek
(~$0.05). Idempotent ingest keyed on `content_hash`. Embedding refresh that skips
unchanged entries. Full version history.

**Caveat:** the content is invented. See ADR-008 and the runbook.

## ✅ Phase 2 — Retrieval
Hybrid vector + keyword search, RRF fusion, `bge-reranker-v2-m3` cross-encoder.
Eval harness comparing four pipeline configurations, with a CI-usable exit code.

## ✅ Phase 3 — Answer service
Query rewriting for multi-turn, confidence gate, grounded generation streamed over
SSE, source citations, automatic escalation below threshold, full retrieval and
cost tracing.

## ✅ Phase 4 — Widget
Streaming render, Azerbaijani UI, emoji and markdown, 👍/👎, CSAT, "Dəstəyə yaz"
escalation, typing indicator, iframe isolation, `embed.js` loader, demo host page.

Accessibility built in: `aria-live` on the transcript, visible focus rings,
keyboard operation, `prefers-reduced-motion`, labelled controls. **Not yet audited.**

## ✅ Phase 5 — Console API
Full KB lifecycle with four-eyes approval, review queue with retrieval diagnostics,
promote-conversation-to-KB, analytics endpoints, audit read and chain verification,
staff management.

**Gap:** API only. No console UI — staff currently work through `/docs` or curl.

## ✅ Phase 6 — Security foundations
Argon2id passwords, `httpOnly` sessions with revocation, three-role RBAC, four-eyes
on identity, sliding-window rate limiting, CORS allow-list, hash-chained
append-only audit, IP hashing, XSS-safe rendering.

## ✅ Phase 7 — Analytics
Seven views: volume, deflection, satisfaction, entry usage, content gaps, cost,
KB health. Exposed through manager-only endpoints.

**Gap:** no dashboard UI.

---

## ⬜ Phase 8 — Real content and pre-launch audits
- Replace synthetic content with ministry-approved entries
- Golden set rewritten by a procurement domain expert
- WCAG 2.1 AA audit
- Penetration test
- Load test
- Retention and deletion policy

## ⬜ Phase 9 — Deployment
- Server provisioning, TLS, reverse proxy
- Secrets management (out of `.env`)
- Backup with a **tested** restore
- Monitoring, alerting, on-call
- DR plan with agreed RTO/RPO

## ⬜ Phase 10 — Shadow mode
Run silently alongside human agents for 4-6 weeks. Measure agreement before any
citizen sees an answer. The strongest available de-risking step, and cheap.

## ⬜ Phase 11 — Second tenant
Onboard the next ministry system. The schema supports it; the path is untested.

## ⬜ Phase 12 — Automated ingest
Connectors pulling from source systems into `kb_entries`, using `ingest_runs` for
observability. `external_id` + `content_hash` already make re-syncs idempotent.

---

## Deliberately not built

| Thing | Why |
|---|---|
| Console UI | API-first was the right order; the UI is a known, well-understood build |
| Answer caching | No measured need at current traffic |
| Multi-worker deployment | In-memory rate limiter assumes one process |
| Self-hosted generation | No GPU, and CPU generation in Azerbaijani is not viable |
| Ticketing integration | The target system was never named |
| Russian / English | Stakeholder confirmed Azerbaijani only |

---

## The two things that decide whether this succeeds

**Retrieval quality (Phase 2).** Most support-bot failures are retrieval failures
wearing an LLM costume. `evals/run_eval.py` exists so this stays measured rather
than assumed.

**Content coverage (Phase 8 onward).** 200 entries is thin. The most likely
complaint is "it doesn't know", not "it answered wrong". The review-queue flywheel
— every unanswered question becoming an entry — is the core loop, not a
nice-to-have.
