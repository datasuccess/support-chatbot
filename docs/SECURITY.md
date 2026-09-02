# Security

## Trust boundaries

```
untrusted ─── end users (anonymous, via the widget)
           ─── LLM output (treated as untrusted text, escaped before rendering)
semi-trusted ─ support / manager staff (authenticated, role-limited)
trusted ────── admin staff, database, server environment
```

## Authentication

**End users are not authenticated.** The widget is embedded in the host
application and chat is anonymous. Protection is origin allow-listing and rate
limiting, not identity. A `session_id` from `sessionStorage` groups messages into a
conversation — it is a convenience identifier, not a credential, and grants
access to nothing.

**Staff** authenticate with email + password against `staff_users`, scoped per
tenant. Passwords are hashed with **Argon2id** (`argon2-cffi` defaults). Sessions
are opaque UUIDs in an `httpOnly`, `sameSite=lax` cookie, `secure` outside local
development, with a 12-hour TTL and server-side revocation.

Login responses are deliberately identical for an unknown email and a wrong
password. A distinguishable response lets an attacker enumerate valid accounts.

## Authorisation

Three roles. `require_roles(...)` treats `admin` as implicitly permitted
everywhere, so endpoints name only the additional roles allowed.

| Capability | support | manager | admin |
|---|:--:|:--:|:--:|
| Read knowledge base | ✅ | ✅ | ✅ |
| Create / edit drafts | ✅ | — | ✅ |
| Submit for approval | ✅ | — | ✅ |
| Approve / reject / publish | — | ✅ | ✅ |
| Archive entries | — | ✅ | ✅ |
| Work the review queue | ✅ | ✅ | ✅ |
| Promote conversation → KB draft | ✅ | — | ✅ |
| Analytics dashboard | — | ✅ | ✅ |
| Read / verify audit log | — | ✅ | ✅ |
| Manage staff accounts | — | — | ✅ |

**Four-eyes control.** `POST /kb/{id}/approve` rejects the request when
`created_by == approver`. The check is on identity, not role, so an admin cannot
approve their own draft either.

## Data protection

**What is stored.** Conversation text, ratings, CSAT scores, escalation notes.

**What is deliberately not stored.**
* Raw IP addresses — hashed with a server-side secret, truncated to 32 chars
* End-user identity — only an opaque `external_ref` if the host app supplies one
* Credentials of any kind in the knowledge base

**What leaves the machine.** Only the generation step. The user's question, the
rewritten query and the retrieved entry text go to DeepSeek's API. Embeddings and
reranking are local. The stakeholder has confirmed the content is non-confidential
system-usage material.

**Retention.** Not yet implemented — see Known gaps.

## Input handling

| Vector | Control |
|---|---|
| XSS via LLM output | Widget escapes HTML **before** applying its markdown subset (`renderMarkdown`) |
| SQL injection | Parameterised queries throughout; no string interpolation of user input |
| Oversized input | `max_length=2000` on chat messages, enforced by Pydantic |
| Request flooding | Sliding-window limiter: 20 chat/min, 5 logins/5min per IP |
| Cross-origin abuse | CORS allow-list from `WIDGET_ALLOWED_ORIGINS` |
| Error-message leakage | Global handler returns a generic 500; details go to logs only |

## Prompt injection

The realistic attack is a user instructing the bot to ignore its constraints and
produce ministry-branded misinformation.

Current mitigations:
* Retrieved context is delimited and labelled in the prompt
* The system prompt states grounding as an absolute
* Scope guardrail per tenant, from `tenants.scope_desc`
* Low-confidence questions never reach the LLM at all
* The bot has no tools, no database writes, and no ability to act — the blast
  radius of a successful injection is one misleading reply to the attacker

Not mitigated: a determined user can probably get off-script output. Because the
bot cannot act on anything, this is a reputational rather than a technical risk.
It is on the pre-launch pen-test list.

## Audit

Two independent layers, described in ADR-007:

1. **Database triggers** reject UPDATE and DELETE on `audit_log`
2. **Hash chain** — each row commits to its predecessor; altering history breaks
   every subsequent hash

`GET /api/admin/audit/verify` recomputes the chain end to end and reports the first
break. Audited actions: login, KB create/update/submit/approve/reject/archive,
reindex, escalation transitions, promotion to KB, staff create/deactivate.

## Known gaps

These are **not** built. Listed explicitly so nobody assumes otherwise.

| Gap | Impact | When |
|---|---|---|
| No retention/deletion policy | Conversations accumulate indefinitely | Before go-live — legal requirement |
| No TLS configuration | Local development only | Deployment phase |
| No pen test | Unknown unknowns | Before public exposure |
| No WCAG audit | Widget is built to AA but unverified | Before public exposure |
| No secrets manager | `.env` on disk | Deployment phase |
| No brute-force lockout | Rate limiting only, no account lockout | Before go-live |
| No CSRF tokens | `sameSite=lax` covers the common case, not all | Before go-live |
| Rate limiting is per-process | Multiple workers multiply the ceiling | Before horizontal scaling |
| No backup/restore procedure | Data loss risk | Deployment phase |

## Reporting

Security issues in this repository should go to the ministry's IT security contact,
not into a public issue tracker.
