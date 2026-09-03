# Runbook

Operational procedures. Written to be followed by someone who did not build this.

---

## Starting from scratch

```bash
cp .env.example .env                    # set DEEPSEEK_API_KEY
docker compose up -d                    # Postgres + pgvector on :5434
uv venv --python 3.12
uv pip install -e ".[dev]"
./scripts/migrate.sh
python -u scripts/generate_seed_kb.py   # only if you need synthetic content
python -u scripts/seed_db.py
uvicorn app.main:app --app-dir api --reload
```

Then verify:

```bash
python -u scripts/smoke_test.py       # 59 end-to-end checks
python -u scripts/security_test.py    # 25 attacks, all must be blocked
python -u evals/run_eval.py           # retrieval quality gate
```

| URL | For |
|---|---|
| `/console` | Staff — queue, KB, approvals, analytics |
| `/demo` | Host page with the widget embedded |
| `/docs` | OpenAPI |

First start downloads ~2.5GB of model weights and takes several minutes. Later
starts load from `~/.cache/huggingface` in 10-30 seconds.

---

## Daily operations

### Site key — handing the widget to the host-app team

The site key is generated per install, so it is never committed. Get it from
**Console → Settings → Widget integration**, or:

```sql
SELECT site_key FROM tenants WHERE slug = 'mof-contracts';
```

Give the host-app team one line:

```html
<script src="https://<api-host>/static/embed.js"
        data-site-key="<key>" defer></script>
```

Then add their origin to the allow-list, or the API will reject the calls:

```sql
UPDATE tenants SET allowed_origins = ARRAY['https://tender.example.az']
WHERE slug = 'mof-contracts';
```

An empty array means "allow any origin" — acceptable locally, never in production.

Rotating the key (Console → Settings) **breaks every embedded widget** until the
host app is updated. Break-glass only.

### Health
```bash
curl -s localhost:8000/health | jq
```
`{"status":"ok","database":true}` — `degraded` means the database is unreachable.

### Verify the audit chain
```bash
curl -s localhost:8000/api/admin/audit/verify -b cookies.txt | jq
```
`{"valid": true}` is expected. A `broken_at_id` means the log was tampered with —
treat as a security incident, not a bug.

### Check the operator queue

The two kinds need different attention. Contact requests are people waiting for a
reply; content gaps are missing articles.

```sql
SELECT * FROM v_queue_health;

-- Anyone waiting more than a day is a service failure.
SELECT reference_code, contact_name, preferred_channel,
       coalesce(contact_phone, contact_email) AS reach_them,
       age(now(), created_at) AS waiting
FROM escalations
WHERE kind = 'contact_request' AND status = 'open'
ORDER BY created_at;
```

### Check what is being refused

```sql
SELECT category, count(*) FROM refusals
WHERE created_at > now() - interval '7 days' GROUP BY 1 ORDER BY 2 DESC;
```

Read this in both directions. Rising `political` or `abusive` means the guard is
working. Rising `out_of_scope` on questions that clearly are about the system means
the threshold is too high, the guard too aggressive, or the knowledge base too thin
— check the actual questions before assuming:

```sql
SELECT question, confidence FROM refusals
WHERE category = 'out_of_scope' ORDER BY created_at DESC LIMIT 30;
```

### Who did what

```sql
SELECT * FROM v_contributor_stats WHERE entries_authored > 0;
SELECT * FROM v_approver_stats;
SELECT * FROM v_operator_stats;   -- turned_into_kb_entries = flywheel health
```

### Watch cost
```sql
SELECT * FROM v_cost_daily ORDER BY day DESC LIMIT 7;
```
Expect roughly $0.0006 per answer. A sharp rise means either traffic growth or a
prompt regression sending too much context.

---

## Adding knowledge base content

**Through the console** (normal path): *Bilik bazası → + Yeni yazı* → *Təsdiqə
göndər* → a **manager** opens *Təsdiq* and publishes.

Four-eyes is enforced on identity: the approver cannot be the author, and an admin
cannot approve their own draft either.

Identical content is rejected with a 409 naming the existing entry — near-duplicates
split the retrieval signal and make ranking worse as the corpus grows.

**From a real conversation** (the flywheel): *Növbə* → open a content gap →
*Bilik bazasına əlavə et*. It lands as a **draft**; a manager still approves it.

**Read the «Axtarış nəticələri» block before writing new content.** If the right
entry is already there but ranked 7th, this is a *retrieval* problem, not a content
problem, and adding a near-duplicate makes retrieval worse rather than better.
Missing this distinction is how knowledge bases rot.

### Reindexing
Embeddings refresh automatically for entries whose `content_hash` changed.

```bash
curl -X POST localhost:8000/api/admin/kb/reindex -b cookies.txt          # stale only
curl -X POST 'localhost:8000/api/admin/kb/reindex?force=true' -b cookies.txt  # everything
```

Force is only needed after changing `EMBEDDING_MODEL`.

---

## Replacing the synthetic knowledge base

**This must happen before any real user sees the bot.**

The seeded content is invented. It looks right and is not.

```sql
-- 1. Confirm what you are about to remove
SELECT count(*), category FROM kb_entries WHERE source='synthetic' GROUP BY category;
```

```bash
# 2. Import the real content (adapt scripts/seed_db.py, or add an Excel importer
#    against kb.ingest.upsert_entries with source='ministry-excel')
```

```sql
-- 3. Retire the synthetic rows. Archive rather than delete: message_retrievals
--    references them, and the audit trail must stay reconstructable.
UPDATE kb_entries SET status='archived', valid_to=now() WHERE source='synthetic';

-- 4. Verify. Must return 0.
SELECT count(*) FROM kb_entries WHERE source='synthetic' AND status='published';
```

```bash
# 5. Reindex and re-run the evals against the REAL content
curl -X POST 'localhost:8000/api/admin/kb/reindex' -b cookies.txt
python -u evals/run_eval.py
```

**Rewrite `evals/golden_set.py` too.** The current cases were written against
synthetic content. More importantly, an eval set written by the same model that
wrote the content measures self-consistency, not correctness. A procurement domain
expert should write the real one.

---

## Tuning the confidence threshold

`CONFIDENCE_THRESHOLD` (default 0.45) decides answer vs escalate.

```sql
-- Where do real confidences actually fall?
SELECT width_bucket(confidence, 0, 1, 10) AS decile,
       count(*), round(avg(confidence)::numeric, 3)
FROM message_traces GROUP BY 1 ORDER BY 1;

-- Are confident answers actually good?
SELECT t.escalated, r.value, count(*)
FROM message_traces t JOIN ratings r ON r.message_id = t.message_id
GROUP BY 1, 2;
```

Raise it if confident answers are collecting 👎. Lower it if escalations are full
of questions the knowledge base clearly covers.

The errors are not symmetric. Too high produces unnecessary escalations — annoying
but safe. Too low produces confident wrong answers — the failure that costs trust.
Err high.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| First request hangs 30s | Models loading lazily | Confirm startup ran `warmup()`; check logs for "ready" |
| `could not determine data type of parameter` | Untyped parameter in an overloaded context | Add an explicit cast (`%s::int`, `%s::text`) |
| Migration exits 0 but nothing applied | `docker exec` without `-i` | Use `cat file.sql \| docker exec -i …` — `scripts/migrate.sh` already does |
| Everything escalates | No embeddings | `SELECT count(*) FROM kb_embeddings;` then reindex |
| Answers ignore the knowledge base | Retrieval returning nothing | Check entries are `published` and within their validity window |
| 429 responses | Rate limiter | 20 messages/min, 30 sessions/min per IP by design; tune in `core/ratelimit.py` |
| All users rate-limited together | App behind a proxy with `TRUSTED_PROXIES` unset | Set it to the proxy's CIDR so `X-Forwarded-For` is honoured |
| Widget shows a connection error | Site key wrong, or origin not allow-listed | Check `tenants.site_key` and `tenants.allowed_origins` |
| Widget 401 on every message | Session token expired (12h) | Reload the page; it mints a new one |
| Staff account will not log in | Per-account lockout after 5 failures | `UPDATE staff_users SET failed_login_attempts=0, locked_until=NULL WHERE email=…` |
| Legitimate questions being refused | Guard too aggressive, or threshold too high | Inspect `refusals`; tune `chat/guard.py` or `REFUSAL_THRESHOLD` |

---

## Go-live checklist

Nothing here is optional.

- [ ] Synthetic content replaced; `v_kb_health.synthetic` is 0
- [ ] `evals/golden_set.py` rewritten by a domain expert against real content
- [ ] `python -u evals/run_eval.py` passes at the agreed hit@3 threshold
- [ ] `AUTH_SESSION_SECRET` regenerated (not the `.env.example` value)
- [ ] Seeded local staff accounts (`*.local`, password `dev12345`) removed
- [ ] `APP_ENV` set to something other than `local` (this also arms `secure` cookies)
- [ ] `WIDGET_ALLOWED_ORIGINS` restricted to real host origins
- [ ] `tenants.allowed_origins` set — an empty array allows any site to embed the widget
- [ ] Site key rotated after handover, if it was ever shared over an insecure channel
- [ ] `TRUSTED_PROXIES` set to the reverse proxy's CIDR, or per-IP limiting is useless
- [ ] `python -u scripts/security_test.py` passes with 0 vulnerable
- [ ] TLS terminated in front of the API
- [ ] Retention and deletion policy agreed and implemented
- [ ] Backup configured **and a restore actually tested**
- [ ] WCAG 2.1 AA audit passed
- [ ] Penetration test passed
- [ ] Shadow mode run alongside human agents; agreement rate accepted
- [ ] Support team trained on the review queue
- [ ] Monitoring and on-call escalation in place
