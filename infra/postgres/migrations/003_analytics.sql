-- 003_analytics.sql — reporting views for the manager dashboard
-- Views (not materialised): the data volume is small and managers need live numbers.

-- Daily conversation + message volume.
CREATE VIEW v_daily_volume AS
SELECT c.tenant_id,
       date_trunc('day', c.started_at)::date       AS day,
       count(DISTINCT c.id)                        AS conversations,
       count(m.id) FILTER (WHERE m.role = 'user')  AS user_messages,
       count(DISTINCT c.session_id)                AS sessions
FROM conversations c
LEFT JOIN messages m ON m.conversation_id = c.id
GROUP BY 1, 2;

-- Deflection: answered by the bot vs handed to a human. The headline metric —
-- this system exists to reduce calls to the support team.
CREATE VIEW v_deflection AS
SELECT c.tenant_id,
       date_trunc('day', m.created_at)::date AS day,
       count(*)                                             AS answers,
       count(*) FILTER (WHERE NOT t.escalated)              AS self_served,
       count(*) FILTER (WHERE t.escalated)                  AS escalated,
       round(100.0 * count(*) FILTER (WHERE NOT t.escalated)
             / nullif(count(*), 0), 1)                      AS deflection_pct
FROM messages m
JOIN message_traces t ON t.message_id = m.id
JOIN conversations c  ON c.id = m.conversation_id
WHERE m.role = 'assistant'
GROUP BY 1, 2;

-- Satisfaction: thumbs and CSAT side by side.
CREATE VIEW v_satisfaction AS
SELECT c.tenant_id,
       date_trunc('day', r.created_at)::date       AS day,
       count(*)                                    AS rated,
       count(*) FILTER (WHERE r.value = 1)         AS thumbs_up,
       count(*) FILTER (WHERE r.value = -1)        AS thumbs_down,
       round(100.0 * count(*) FILTER (WHERE r.value = 1)
             / nullif(count(*), 0), 1)             AS positive_pct
FROM ratings r
JOIN messages m      ON m.id = r.message_id
JOIN conversations c ON c.id = m.conversation_id
GROUP BY 1, 2;

-- Which KB entries actually earn their place. Entries with zero hits are
-- either badly worded or answering questions nobody asks.
CREATE VIEW v_entry_usage AS
SELECT e.tenant_id,
       e.id                                        AS entry_id,
       e.question,
       e.category,
       e.status,
       count(mr.id) FILTER (WHERE mr.used)         AS times_used,
       count(mr.id)                                AS times_retrieved,
       round(avg(mr.rerank_score) FILTER (WHERE mr.used)::numeric, 3) AS avg_rerank_score,
       max(m.created_at)                           AS last_used_at
FROM kb_entries e
LEFT JOIN message_retrievals mr ON mr.entry_id = e.id
LEFT JOIN messages m            ON m.id = mr.message_id
GROUP BY 1, 2, 3, 4, 5;

-- The content-gap queue: questions the bot could not confidently answer.
-- This is what the support team works through to grow the knowledge base.
CREATE VIEW v_unanswered AS
SELECT c.tenant_id,
       m.id            AS message_id,
       m.conversation_id,
       m.content       AS question,
       t.confidence,
       t.answer_mode,
       m.created_at
FROM messages m
JOIN message_traces t ON t.message_id = m.id
JOIN conversations c  ON c.id = m.conversation_id
WHERE m.role = 'assistant' AND t.escalated
ORDER BY m.created_at DESC;

-- Cost and latency tracking.
CREATE VIEW v_cost_daily AS
SELECT c.tenant_id,
       date_trunc('day', t.created_at)::date AS day,
       count(*)                              AS calls,
       sum(t.prompt_tokens)                  AS prompt_tokens,
       sum(t.completion_tokens)              AS completion_tokens,
       round(sum(t.cost_usd), 4)             AS cost_usd,
       round(avg(t.total_ms))                AS avg_total_ms,
       max(t.total_ms)                       AS max_total_ms
FROM message_traces t
JOIN messages m      ON m.id = t.message_id
JOIN conversations c ON c.id = m.conversation_id
GROUP BY 1, 2;

-- Knowledge base health at a glance.
CREATE VIEW v_kb_health AS
SELECT tenant_id,
       count(*)                                          AS total,
       count(*) FILTER (WHERE status = 'published')      AS published,
       count(*) FILTER (WHERE status = 'draft')          AS draft,
       count(*) FILTER (WHERE status = 'pending_approval') AS pending_approval,
       count(*) FILTER (WHERE source = 'synthetic')      AS synthetic,
       count(*) FILTER (WHERE valid_to IS NOT NULL
                          AND valid_to < now())          AS expired
FROM kb_entries
GROUP BY 1;
