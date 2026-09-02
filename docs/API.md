# API reference

Base URL in local development: `http://localhost:8000`
Interactive reference: `/docs`

Two surfaces with different trust levels:

* **`/api/chat/*`** — public, anonymous, called by the widget
* **`/api/admin/*`** — staff, session-cookie authenticated, role-guarded

---

## Public — chat

### `POST /api/chat/stream`
Ask a question. Responds with Server-Sent Events.

```json
{ "session_id": "s_abc123…", "message": "Təklifi necə göndərim?",
  "tenant": "mof-contracts", "external_ref": null }
```

Events, in order:

| Event | Payload |
|---|---|
| `meta` | `{"conversation_id": "uuid"}` — sent before any tokens |
| `token` | A chunk of answer text (repeats) |
| `done` | `{"message_id", "escalated", "confidence", "sources": [...], "prompt_tokens", "completion_tokens", "llm_ms"}` |

`message_id` from `done` is what ratings and escalations attach to.

Uses SSE over `fetch` rather than `EventSource`, because `EventSource` cannot issue
a POST and the question must travel in a request body.

Rate limit: 20 requests/minute per IP.

### `POST /api/chat/rate`
```json
{ "message_id": 42, "value": 1, "comment": null }
```
`value` is `1` or `-1`. A `-1` opens an escalation automatically.

### `POST /api/chat/csat`
```json
{ "session_id": "s_abc123…", "score": 4, "comment": "Faydalı oldu" }
```
`score` is 1-5.

### `POST /api/chat/escalate`
The "Dəstəyə yaz" button. Hands the whole transcript to a human.
```json
{ "session_id": "s_abc123…", "note": "…", "message_id": 42 }
```

---

## Staff — authentication

### `POST /api/admin/login`
```json
{ "email": "support@mof.local", "password": "dev12345", "tenant": "mof-contracts" }
```
Sets an `httpOnly` session cookie. Rate limit: 5/5min per IP. Returns an identical
error for unknown email and wrong password, by design.

### `POST /api/admin/logout` · `GET /api/admin/me`

---

## Staff — knowledge base

Roles: **S** = support, **M** = manager. Admin has access to everything.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/kb` | S M | List; filters `status`, `category`, `q`, `limit`, `offset` |
| GET | `/api/admin/kb/{id}` | S M | One entry with its full version history |
| POST | `/api/admin/kb` | S | Create a draft |
| PATCH | `/api/admin/kb/{id}` | S | Edit — snapshots the old version, resets to `draft` |
| POST | `/api/admin/kb/{id}/submit` | S | Draft → `pending_approval` |
| POST | `/api/admin/kb/{id}/approve` | M | Publish. **Fails if approver == author** |
| POST | `/api/admin/kb/{id}/reject` | M | → `rejected`, with a note |
| POST | `/api/admin/kb/{id}/archive` | M | Retire by closing `valid_to` |
| POST | `/api/admin/kb/reindex` | M | Refresh stale embeddings; `?force=true` for all |

Editing a published entry sends it back to `draft` and clears its approval —
changed content has not been approved.

---

## Staff — review queue

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/escalations` | S M | Queue; `?status=open` |
| GET | `/api/admin/escalations/{id}` | S M | Transcript **plus** what the retriever considered |
| PATCH | `/api/admin/escalations/{id}?status=…` | S M | `open`/`in_progress`/`resolved`/`dismissed` |
| POST | `/api/admin/escalations/{id}/promote` | S | Turn the conversation into a KB draft |

`GET /escalations/{id}` returns a `retrieved` array with each candidate's rank and
rerank score. Read it before writing new content: if the right entry exists but
ranked poorly, adding a near-duplicate makes retrieval worse, not better.

`promote` creates a **draft**, never a published entry — a manager still approves.

---

## Staff — analytics

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/analytics/overview` | M | Volume, deflection, satisfaction, cost, KB health |
| GET | `/api/admin/analytics/gaps` | S M | Questions the bot could not answer |
| GET | `/api/admin/analytics/entry-usage` | M | Which entries earn their place |

`overview` takes `?days=30`.

---

## Staff — audit and users

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/api/admin/audit` | M | Filter by `entity_type`, `entity_id` |
| GET | `/api/admin/audit/verify` | M | Recompute the hash chain |
| GET | `/api/admin/staff` | admin | List staff |
| POST | `/api/admin/staff` | admin | Create staff |
| PATCH | `/api/admin/staff/{id}/deactivate` | admin | Deactivate and revoke sessions |

---

## Other

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + database check |
| GET | `/widget` | The chat UI |
| GET | `/demo` | Host page with the widget embedded |
| GET | `/static/embed.js` | Embeddable loader |
| GET | `/docs` | OpenAPI |

---

## Embedding the widget

```html
<script src="https://<api-host>/static/embed.js"
        data-api="https://<api-host>"
        data-tenant="mof-contracts" defer></script>
```

Adds a floating launcher and an iframe. The host origin must appear in
`WIDGET_ALLOWED_ORIGINS`.

## Errors

| Code | Meaning |
|---|---|
| 401 | Not authenticated, or session expired |
| 403 | Authenticated but the role is insufficient |
| 404 | Unknown tenant or entity |
| 409 | Workflow conflict — wrong state, or four-eyes violation |
| 422 | Validation failure |
| 429 | Rate limited; `Retry-After` header included |
| 500 | Generic. Details are logged, never returned |
