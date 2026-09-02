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

First start downloads ~2.5GB of model weights and takes several minutes. Later
starts load from `~/.cache/huggingface` in 10-30 seconds.

---

## Daily operations

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

### Check the escalation backlog
```sql
SELECT reason, count(*) FROM escalations WHERE status='open' GROUP BY reason;
```

### Watch cost
```sql
SELECT * FROM v_cost_daily ORDER BY day DESC LIMIT 7;
```
Expect roughly $0.0006 per answer. A sharp rise means either traffic growth or a
prompt regression sending too much context.

---

## Adding knowledge base content

**Through the console** (normal path):
1. `support` creates a draft → `POST /api/admin/kb`
2. `support` submits → `POST /api/admin/kb/{id}/submit`
3. `manager` approves → `POST /api/admin/kb/{id}/approve`
4. Reindex → `POST /api/admin/kb/reindex`

Four-eyes is enforced: the approver cannot be the author.

**From a real conversation** (the flywheel):
1. Open the review queue → `GET /api/admin/escalations?status=open`
2. Inspect one → `GET /api/admin/escalations/{id}` — includes the full transcript
   *and* what the retriever considered
3. Write the answer → `POST /api/admin/escalations/{id}/promote`
4. It lands as a **draft**; a manager still approves it

**Read the `retrieved` block before writing new content.** If the right entry is
already there but ranked 7th, this is a retrieval problem, not a content problem,
and adding a near-duplicate entry makes retrieval worse rather than better.

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
| 429 responses | Rate limiter | 20 chat/min per IP by design; raise in `core/ratelimit.py` if wrong |
| Widget blank in the host page | CORS | Add the host origin to `WIDGET_ALLOWED_ORIGINS` |

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
- [ ] TLS terminated in front of the API
- [ ] Retention and deletion policy agreed and implemented
- [ ] Backup configured **and a restore actually tested**
- [ ] WCAG 2.1 AA audit passed
- [ ] Penetration test passed
- [ ] Shadow mode run alongside human agents; agreement rate accepted
- [ ] Support team trained on the review queue
- [ ] Monitoring and on-call escalation in place
