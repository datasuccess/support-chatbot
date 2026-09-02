-- 005_attribution.sql — "who wrote what" reporting
-- The data was already captured (kb_entries.created_by/approved_by,
-- kb_entry_versions.changed_by, approvals.decided_by, audit_log). These views
-- make it answerable without hand-written SQL.

-- Content authorship per staff member.
CREATE VIEW v_contributor_stats AS
SELECT u.tenant_id,
       u.id                                     AS staff_id,
       u.full_name,
       u.email,
       u.role::text                             AS role,
       count(DISTINCT e.id)                     AS entries_authored,
       count(DISTINCT e.id) FILTER (WHERE e.status = 'published')        AS entries_published,
       count(DISTINCT e.id) FILTER (WHERE e.status = 'pending_approval') AS entries_awaiting_approval,
       count(DISTINCT e.id) FILTER (WHERE e.status = 'rejected')         AS entries_rejected,
       count(DISTINCT e.id) FILTER (WHERE e.source = 'from_conversation') AS entries_from_conversations,
       count(DISTINCT v.id)                     AS edits_made,
       max(e.created_at)                        AS last_authored_at
FROM staff_users u
LEFT JOIN kb_entries        e ON e.created_by = u.id
LEFT JOIN kb_entry_versions v ON v.changed_by = u.id
GROUP BY 1, 2, 3, 4, 5;

-- Approval activity per manager, including how fast they turn work around.
CREATE VIEW v_approver_stats AS
SELECT u.tenant_id,
       u.id                                                  AS staff_id,
       u.full_name,
       count(a.id)                                           AS decisions,
       count(a.id) FILTER (WHERE a.decision = 'approved')    AS approved,
       count(a.id) FILTER (WHERE a.decision = 'rejected')    AS rejected,
       round(avg(EXTRACT(EPOCH FROM (a.created_at - e.created_at)) / 3600)::numeric, 1)
                                                             AS avg_hours_to_decide,
       max(a.created_at)                                     AS last_decision_at
FROM staff_users u
JOIN approvals   a ON a.decided_by = u.id
JOIN kb_entries  e ON e.id = a.entry_id
GROUP BY 1, 2, 3;

-- Operator workload: who handles contact requests, and how quickly.
CREATE VIEW v_operator_stats AS
SELECT u.tenant_id,
       u.id                                                          AS staff_id,
       u.full_name,
       count(e.id)                                                   AS handled,
       count(e.id) FILTER (WHERE e.kind = 'contact_request')         AS contact_requests,
       count(e.id) FILTER (WHERE e.kind = 'content_gap')             AS content_gaps,
       count(e.id) FILTER (WHERE e.status = 'resolved')              AS resolved,
       count(e.id) FILTER (WHERE e.resolved_entry_id IS NOT NULL)    AS turned_into_kb_entries,
       round(avg(EXTRACT(EPOCH FROM (e.resolved_at - e.created_at)) / 3600)
             ::numeric, 1)                                           AS avg_hours_to_resolve
FROM staff_users u
JOIN escalations e ON e.assigned_to = u.id
GROUP BY 1, 2, 3;

-- Recent activity feed, straight from the audit log — the "who did what, when"
-- view a manager actually wants to look at.
CREATE VIEW v_activity_feed AS
SELECT a.tenant_id,
       a.id,
       a.created_at,
       a.actor_label,
       a.action,
       a.entity_type,
       a.entity_id,
       u.full_name,
       u.role::text AS role
FROM audit_log a
LEFT JOIN staff_users u ON u.id = a.actor_id
ORDER BY a.id DESC;

-- Operator queue health, split by kind. A contact request left open is a person
-- waiting for a call; a content gap left open is only a missing article.
CREATE VIEW v_queue_health AS
SELECT tenant_id,
       kind::text AS kind,
       count(*) FILTER (WHERE status = 'open')        AS open,
       count(*) FILTER (WHERE status = 'in_progress') AS in_progress,
       count(*) FILTER (WHERE status = 'resolved')    AS resolved,
       count(*) FILTER (WHERE status = 'open'
                          AND created_at < now() - interval '24 hours') AS open_over_24h,
       round(avg(EXTRACT(EPOCH FROM (first_response_at - created_at)) / 60)
             ::numeric, 1)                            AS avg_minutes_to_first_response
FROM escalations
GROUP BY 1, 2;

-- What is being refused, so the team can spot abuse and wrongly-refused questions.
CREATE VIEW v_refusal_stats AS
SELECT tenant_id,
       date_trunc('day', created_at)::date AS day,
       category,
       count(*) AS refusals
FROM refusals
GROUP BY 1, 2, 3;
