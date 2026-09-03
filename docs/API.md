# API reference

Base URL in local development: `http://localhost:8000`
Interactive reference: `/docs`

Two surfaces with different trust levels:

* **`/api/chat/*`** — public, anonymous, called by the widget. Authorised by a
  per-tenant **site key** plus a server-issued **conversation token**.
* **`/api/admin/*`** — staff, session-cookie authenticated, role-guarded.

---

## Authorisation model (public API)

End users never log in, so authority comes from two things the caller cannot forge:

| | What it proves | Where it comes from |
|---|---|---|
| **Site key** | Which widget is calling | `tenants.site_key` — public, ships in the page |
| **Conversation token** | You started *this* conversation | `POST /api/chat/session`, HMAC-signed |

Every chat endpoint derives its conversation from the token. "Which conversation
am I acting on" is never a caller-supplied parameter — an earlier version trusted a
client-supplied `session_id` and allowed posting into other people's
conversations. See ADR-011.

```
POST /api/chat/session   { site_key }        ->  { session_token }
POST /api/chat/*         Authorization: Bearer <session_token>
```

Tokens last 12 hours. CORS is *not* an access control here — it is browser-enforced
— so origin checking is done server-side against `tenants.allowed_origins`.

---

## Public — chat

### `POST /api/chat/session`
Start a conversation. Call once, then reuse the token.

```json
{ "site_key": "745896a8…", "external_ref": null }
```
→ `{ "session_token": "eyJ…", "tenant_name": "Dövlət Satınalmaları Portalı" }`

`401` for an unknown site key, `403` if the request's Origin is not in the tenant's
allow-list. Rate limit: 30/minute per IP.

### `POST /api/chat/stream`
Ask a question. Responds with Server-Sent Events.

```json
{ "message": "Təklifi necə göndərim?" }
```

| Event | Payload |
|---|---|
| `token` | A chunk of answer text (repeats) |
| `done` | `{ message_id, mode, offer_contact, confidence, sources[], refusal_category, prompt_tokens, completion_tokens, llm_ms }` |

**`mode` is the field that matters:**

| `mode` | Meaning | Side effect |
|---|---|---|
| `grounded` | Answered from the knowledge base | — |
| `escalated` | In scope, no good match | Logs a `content_gap` |
| `refused` | Out of scope — political, abusive, off-topic | Logs a `refusal`, **never** an operator task |

`offer_contact` is `true` only for `escalated` and `refused`. The widget uses it to
decide whether to show the handover button — see `OPERATIONS_MODEL.md`.

SSE runs over `fetch`, not `EventSource`: `EventSource` cannot POST, and both the
question and the bearer token must travel in the request.

Rate limit: 20/minute per IP.

### `POST /api/chat/rate`
```json
{ "message_id": 42, "value": 1, "comment": null }
```
`value` is `1` or `-1`. A `-1` logs a content gap. `404` if the message does not
belong to the caller's conversation.

### `POST /api/chat/csat`
```json
{ "score": 4, "comment": "Faydalı oldu" }
```
Applies to the token's conversation; no id is accepted.

### `POST /api/chat/contact`
**«Operatorla əlaqə»** — a person asking a human to get back to them. Creates a
real operator task carrying the full transcript.

```json
{ "name": "Elçin Məmmədov", "channel": "phone",
  "phone": "+994501112233", "email": null, "note": "…" }
```
→ `{ "ok": true, "reference_code": "DS-WNUHQH", "duplicate": false }`

Contact details are **required and validated**: `channel: "phone"` needs `phone`,
`channel: "email"` needs `email`, else `422`. A ticket nobody can action is worse
than no ticket.

Calling twice returns the existing open request with `duplicate: true` rather than
creating a second ticket for the same person.

---

## Staff — authentication

### `POST /api/admin/login`
```json
{ "email": "support@mof.local", "password": "dev12345", "tenant": "mof-contracts" }
```
Sets an `httpOnly` session cookie. Identical error for unknown email and wrong
password, by design. Rate limit 30/5min per IP; **per-account lockout** after 5
failures for 15 minutes (ADR-014).

### `POST /api/admin/logout` · `GET /api/admin/me`

---

## Staff — knowledge base

Roles: **S** = support, **M** = manager. Admin has access to everything.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/kb` | S M | List; filters `status`, `category`, `q`, `limit`, `offset` |
| GET | `/api/admin/kb/{entry_id}` | S M | One entry with full version history |
| POST | `/api/admin/kb` | S | Create a draft |
| PATCH | `/api/admin/kb/{entry_id}` | S | Edit — snapshots the old version, resets to `draft` |
| POST | `/api/admin/kb/{entry_id}/submit` | S | Draft → `pending_approval` |
| POST | `/api/admin/kb/{entry_id}/approve` | M | Publish. **Fails if approver == author** |
| POST | `/api/admin/kb/{entry_id}/reject` | M | → `rejected`, with a note |
| POST | `/api/admin/kb/{entry_id}/archive` | M | Retire by closing `valid_to` |
| POST | `/api/admin/kb/reindex` | M | Refresh stale embeddings; `?force=true` for all |

Editing a published entry sends it back to `draft` and clears its approval —
changed content has not been approved.

**`409` on duplicate content**, naming the existing entry. Near-identical entries
split the retrieval signal and make ranking worse, so this is a guard, not a
nuisance (ADR-015).

---

## Staff — operator queue

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/escalations` | S M | Queue; `?status=open&kind=contact_request` |
| GET | `/api/admin/escalations/{escalation_id}` | S M | Transcript **plus** retrieval diagnostics |
| PATCH | `/api/admin/escalations/{escalation_id}?status=…` | S M | `open`/`in_progress`/`resolved`/`dismissed` |
| POST | `/api/admin/escalations/{escalation_id}/promote` | S | Turn the conversation into a KB draft |

**`kind` is the important filter.** `contact_request` is a person waiting for a
call; `content_gap` is a missing article. The list returns `counts` for both and
sorts contact requests first.

`GET /escalations/{id}` returns a `retrieved` array with each candidate's rank and
rerank score. Read it before writing new content: if the right entry exists but
ranked poorly, that is a *retrieval* problem and adding a near-duplicate makes it
worse.

`promote` creates a **draft**, never a published entry — a manager still approves.

---

## Staff — analytics and attribution

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/analytics/overview` | M | Volume, deflection, satisfaction, cost, KB health. `?days=30` |
| GET | `/api/admin/analytics/gaps` | S M | Questions the bot could not answer |
| GET | `/api/admin/analytics/entry-usage` | M | Which entries earn their place |
| GET | `/api/admin/analytics/contributors` | M | **Who wrote what** — authored, published, rejected, edits |
| GET | `/api/admin/analytics/approvers` | M | Who approved what, and how fast |
| GET | `/api/admin/analytics/operators` | M | Operator workload; gaps turned into entries |
| GET | `/api/admin/analytics/activity` | M | Chronological who-did-what, from the audit log |
| GET | `/api/admin/analytics/queue-health` | S M | Open/overdue by kind, time to first response |
| GET | `/api/admin/analytics/refusals` | M | What was refused and why. `?days=30` |

Watch refusals in both directions: rising political refusals means the guard is
working; rising `out_of_scope` on legitimate questions means it is too aggressive.

---

## Staff — audit, users, tenant

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/audit` | M | Filter by `entity_type`, `entity_id` |
| GET | `/api/admin/audit/verify` | M | Recompute the hash chain end to end |
| GET | `/api/admin/staff` | admin | List staff |
| POST | `/api/admin/staff` | admin | Create staff |
| PATCH | `/api/admin/staff/{staff_id}/deactivate` | admin | Deactivate and revoke sessions |
| GET | `/api/admin/tenant` | M | Scope text, site key, allowed origins |
| PATCH | `/api/admin/tenant` | admin | Update scope guardrail / origins |
| POST | `/api/admin/tenant/rotate-site-key` | admin | New key — **breaks every embedded widget** |

---

## Pages

| Path | Purpose |
|---|---|
| `/console` | Staff console (single self-contained HTML page) |
| `/demo` | Host page with the widget embedded; site key injected from the database |
| `/widget` | The chat UI on its own — takes `?k=<site key>` |
| `/static/embed.js` | Embeddable loader |
| `/health` | Liveness + database check |
| `/docs` | OpenAPI |

## Embedding the widget

```html
<script src="https://<api-host>/static/embed.js"
        data-site-key="<public site key>" defer></script>
```

Get the key from **Console → Settings → Widget integration**. The API origin is
derived from the script's own `src`, so the host page cannot point the widget
elsewhere. The host origin must appear in the tenant's `allowed_origins`.

## Errors

| Code | Meaning |
|---|---|
| 401 | Missing/invalid session token, bad site key, or expired staff session |
| 403 | Authenticated but insufficient role, or origin not allowed |
| 404 | Unknown entity — including a message outside your conversation |
| 409 | Workflow conflict: wrong state, four-eyes violation, or duplicate content |
| 422 | Validation failure — e.g. a contact request with no phone number |
| 429 | Rate limited; `Retry-After` header included |
| 500 | Generic. Details are logged, never returned |
