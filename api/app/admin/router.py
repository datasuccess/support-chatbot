"""Staff console API: authentication, KB lifecycle, review queue, analytics, audit.

Role model (three roles, as agreed):
  support — drafts and edits entries, works the review queue, submits for approval
  manager — approves/rejects (the 4-eyes control), reads analytics and audit
  admin   — all of the above, plus staff management

`require_roles` treats admin as implicitly permitted everywhere, so each endpoint
only names the additional roles that may reach it.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.core import audit, security
from app.core.config import settings
from app.core.db import execute, fetch_all, fetch_one
from app.core.security import current_staff, require_roles
from app.kb import ingest

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --------------------------------------------------------------------- auth
class LoginRequest(BaseModel):
    email: str
    password: str
    tenant: str | None = None


@router.post("/login")
async def login(req: LoginRequest, response: Response) -> dict:
    tenant = await fetch_one(
        "SELECT id, name FROM tenants WHERE slug = %s",
        (req.tenant or settings.default_tenant,),
    )
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant")

    user = await security.authenticate(tenant["id"], req.email, req.password)
    if not user:
        # Deliberately identical message for unknown user and wrong password —
        # a different response would let an attacker enumerate valid accounts.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    session_id = await security.create_session(user["id"])
    response.set_cookie(
        security.SESSION_COOKIE, session_id,
        httponly=True, samesite="lax",
        secure=settings.app_env != "local",
        max_age=settings.session_ttl_hours * 3600,
    )
    await audit.record(
        action="login", entity_type="staff_user", entity_id=user["id"],
        tenant_id=tenant["id"], actor_id=user["id"], actor_label=user["email"],
    )
    return {"id": user["id"], "email": user["email"],
            "full_name": user["full_name"], "role": user["role"]}


@router.post("/logout")
async def logout(response: Response, user: dict = Depends(current_staff)) -> dict:
    await security.revoke_session(str(user["session_id"]))
    response.delete_cookie(security.SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_staff)) -> dict:
    return {"id": user["id"], "email": user["email"],
            "full_name": user["full_name"], "role": user["role"]}


# ----------------------------------------------------------------------- kb
class EntryCreate(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=3, max_length=8000)
    category: str | None = None
    tags: list[str] = []
    citation: str | None = None


class EntryUpdate(BaseModel):
    question: str | None = None
    answer: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    citation: str | None = None
    change_note: str | None = None


class DecisionRequest(BaseModel):
    note: str | None = None


@router.get("/kb")
async def list_entries(
    user: dict = Depends(require_roles("support", "manager")),
    status_filter: str | None = Query(default=None, alias="status"),
    category: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
) -> dict:
    where = ["tenant_id = %s"]
    params: list = [user["tenant_id"]]
    if status_filter:
        where.append("status = %s::kb_status")
        params.append(status_filter)
    if category:
        where.append("category = %s")
        params.append(category)
    if q:
        where.append("(question ILIKE %s OR answer ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])

    clause = " AND ".join(where)
    rows = await fetch_all(
        f"""SELECT id, question, answer, category, tags, citation, status::text AS status,
                   source, version, created_at, updated_at, approved_at
            FROM kb_entries WHERE {clause}
            ORDER BY updated_at DESC LIMIT %s::int OFFSET %s::int""",
        tuple(params + [limit, offset]),
    )
    total = await fetch_one(f"SELECT count(*) AS n FROM kb_entries WHERE {clause}", tuple(params))
    return {"items": rows, "total": total["n"], "limit": limit, "offset": offset}


@router.get("/kb/{entry_id}")
async def get_entry(entry_id: int, user: dict = Depends(require_roles("support", "manager"))) -> dict:
    row = await fetch_one(
        """SELECT id, question, answer, category, tags, citation, status::text AS status,
                  source, version, valid_from, valid_to, created_by, approved_by,
                  approved_at, created_at, updated_at
           FROM kb_entries WHERE id = %s AND tenant_id = %s""",
        (entry_id, user["tenant_id"]),
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    row["versions"] = await fetch_all(
        """SELECT version, question, answer, status::text AS status, change_note, created_at
           FROM kb_entry_versions WHERE entry_id = %s ORDER BY version DESC""",
        (entry_id,),
    )
    return row


@router.post("/kb", status_code=status.HTTP_201_CREATED)
async def create_entry(req: EntryCreate, user: dict = Depends(require_roles("support"))) -> dict:
    chash = ingest.content_hash(req.question, req.answer)
    row = await fetch_one(
        """INSERT INTO kb_entries (tenant_id, question, answer, category, tags, citation,
                                   status, source, external_id, content_hash, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,'draft','manual',%s,%s,%s) RETURNING id""",
        (user["tenant_id"], req.question, req.answer, req.category, req.tags,
         req.citation, chash[:24], chash, user["id"]),
    )
    await audit.record(
        action="kb.create", entity_type="kb_entry", entity_id=row["id"],
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
        after=req.model_dump(),
    )
    return {"id": row["id"], "status": "draft"}


@router.patch("/kb/{entry_id}")
async def update_entry(
    entry_id: int, req: EntryUpdate, user: dict = Depends(require_roles("support"))
) -> dict:
    current = await fetch_one(
        """SELECT id, question, answer, category, tags, citation, status::text AS status, version
           FROM kb_entries WHERE id = %s AND tenant_id = %s""",
        (entry_id, user["tenant_id"]),
    )
    if not current:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")

    merged = {
        "question": req.question or current["question"],
        "answer": req.answer or current["answer"],
        "category": req.category if req.category is not None else current["category"],
        "tags": req.tags if req.tags is not None else current["tags"],
        "citation": req.citation if req.citation is not None else current["citation"],
    }
    # Snapshot the version being replaced, then bump. Editing a published entry
    # sends it back to draft: changed content has not been approved.
    await execute(
        """INSERT INTO kb_entry_versions
               (entry_id, version, question, answer, status, citation, changed_by, change_note)
           VALUES (%s,%s,%s,%s,%s::kb_status,%s,%s,%s)
           ON CONFLICT (entry_id, version) DO NOTHING""",
        (entry_id, current["version"], current["question"], current["answer"],
         current["status"], current["citation"], user["id"], req.change_note),
    )
    await execute(
        """UPDATE kb_entries
           SET question=%s, answer=%s, category=%s, tags=%s, citation=%s,
               content_hash=%s, version=version+1, status='draft',
               approved_by=NULL, approved_at=NULL, updated_at=now()
           WHERE id=%s""",
        (merged["question"], merged["answer"], merged["category"], merged["tags"],
         merged["citation"], ingest.content_hash(merged["question"], merged["answer"]), entry_id),
    )
    await audit.record(
        action="kb.update", entity_type="kb_entry", entity_id=entry_id,
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
        before=dict(current), after=merged,
    )
    return {"id": entry_id, "status": "draft", "version": current["version"] + 1}


@router.post("/kb/{entry_id}/submit")
async def submit_for_approval(
    entry_id: int, user: dict = Depends(require_roles("support"))
) -> dict:
    updated = await fetch_one(
        """UPDATE kb_entries SET status='pending_approval', updated_at=now()
           WHERE id=%s AND tenant_id=%s AND status IN ('draft','rejected')
           RETURNING id, version""",
        (entry_id, user["tenant_id"]),
    )
    if not updated:
        raise HTTPException(status.HTTP_409_CONFLICT, "Entry is not in a submittable state")
    await audit.record(
        action="kb.submit", entity_type="kb_entry", entity_id=entry_id,
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
    )
    return {"id": entry_id, "status": "pending_approval"}


@router.post("/kb/{entry_id}/approve")
async def approve_entry(
    entry_id: int, req: DecisionRequest, user: dict = Depends(require_roles("manager"))
) -> dict:
    """Publish an entry. Enforces 4-eyes: the approver cannot be the author."""
    entry = await fetch_one(
        """SELECT id, version, created_by, status::text AS status
           FROM kb_entries WHERE id=%s AND tenant_id=%s""",
        (entry_id, user["tenant_id"]),
    )
    if not entry:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entry not found")
    if entry["status"] != "pending_approval":
        raise HTTPException(status.HTTP_409_CONFLICT, "Entry is not pending approval")
    if entry["created_by"] == user["id"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Four-eyes control: an entry must be approved by someone other than its author",
        )

    await execute(
        """UPDATE kb_entries SET status='published', approved_by=%s, approved_at=now(),
                                 valid_from=now(), updated_at=now()
           WHERE id=%s""",
        (user["id"], entry_id),
    )
    await execute(
        """INSERT INTO approvals (entry_id, version, decision, decided_by, note)
           VALUES (%s,%s,'approved',%s,%s)""",
        (entry_id, entry["version"], user["id"], req.note),
    )
    await audit.record(
        action="kb.approve", entity_type="kb_entry", entity_id=entry_id,
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
        after={"status": "published", "note": req.note},
    )
    return {"id": entry_id, "status": "published"}


@router.post("/kb/{entry_id}/reject")
async def reject_entry(
    entry_id: int, req: DecisionRequest, user: dict = Depends(require_roles("manager"))
) -> dict:
    entry = await fetch_one(
        "SELECT id, version FROM kb_entries WHERE id=%s AND tenant_id=%s AND status='pending_approval'",
        (entry_id, user["tenant_id"]),
    )
    if not entry:
        raise HTTPException(status.HTTP_409_CONFLICT, "Entry is not pending approval")
    await execute("UPDATE kb_entries SET status='rejected', updated_at=now() WHERE id=%s", (entry_id,))
    await execute(
        """INSERT INTO approvals (entry_id, version, decision, decided_by, note)
           VALUES (%s,%s,'rejected',%s,%s)""",
        (entry_id, entry["version"], user["id"], req.note),
    )
    await audit.record(
        action="kb.reject", entity_type="kb_entry", entity_id=entry_id,
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
        after={"status": "rejected", "note": req.note},
    )
    return {"id": entry_id, "status": "rejected"}


@router.post("/kb/{entry_id}/archive")
async def archive_entry(
    entry_id: int, user: dict = Depends(require_roles("manager"))
) -> dict:
    """Retire an entry by closing its validity window rather than deleting it,
    so historical answers stay reconstructable."""
    await execute(
        """UPDATE kb_entries SET status='archived', valid_to=now(), updated_at=now()
           WHERE id=%s AND tenant_id=%s""",
        (entry_id, user["tenant_id"]),
    )
    await audit.record(
        action="kb.archive", entity_type="kb_entry", entity_id=entry_id,
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
    )
    return {"id": entry_id, "status": "archived"}


@router.post("/kb/reindex")
async def reindex(user: dict = Depends(require_roles("manager")), force: bool = False) -> dict:
    result = await ingest.refresh_embeddings(user["tenant_id"], settings.embedding_model, force)
    await audit.record(
        action="kb.reindex", entity_type="tenant", entity_id=user["tenant_id"],
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
        after=result,
    )
    return result


# -------------------------------------------------------------- review queue
class ResolveRequest(BaseModel):
    resolution_note: str | None = None


class PromoteRequest(BaseModel):
    """Turn a real conversation into a knowledge-base entry.

    This is the flywheel the whole system is built around: every question the bot
    could not answer becomes content that it can answer next time.
    """
    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=3, max_length=8000)
    category: str | None = None
    tags: list[str] = []
    citation: str | None = None


@router.get("/escalations")
async def list_escalations(
    user: dict = Depends(require_roles("support", "manager")),
    status_filter: str = Query(default="open", alias="status"),
    limit: int = Query(default=50, le=200),
) -> dict:
    rows = await fetch_all(
        """SELECT e.id, e.reason, e.status::text AS status, e.contact_note,
                  e.created_at, e.assigned_to, e.conversation_id,
                  m.content AS bot_answer, t.confidence,
                  (SELECT content FROM messages um
                    WHERE um.conversation_id = e.conversation_id AND um.role = 'user'
                    ORDER BY um.created_at DESC LIMIT 1) AS user_question
           FROM escalations e
           LEFT JOIN messages m       ON m.id = e.message_id
           LEFT JOIN message_traces t ON t.message_id = e.message_id
           WHERE e.tenant_id = %s AND e.status = %s::escalation_status
           ORDER BY e.created_at DESC LIMIT %s::int""",
        (user["tenant_id"], status_filter, limit),
    )
    return {"items": rows, "count": len(rows)}


@router.get("/escalations/{escalation_id}")
async def get_escalation(
    escalation_id: int, user: dict = Depends(require_roles("support", "manager"))
) -> dict:
    row = await fetch_one(
        """SELECT e.*, e.status::text AS status FROM escalations e
           WHERE e.id = %s AND e.tenant_id = %s""",
        (escalation_id, user["tenant_id"]),
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escalation not found")
    # Full transcript, so the agent has the same context the user had.
    row["transcript"] = await fetch_all(
        """SELECT role::text AS role, content, created_at FROM messages
           WHERE conversation_id = %s ORDER BY created_at""",
        (row["conversation_id"],),
    )
    # What the retriever considered — often the entry existed but ranked poorly,
    # which is a retrieval fix rather than a content fix.
    row["retrieved"] = await fetch_all(
        """SELECT mr.rank, mr.rerank_score, mr.used, e2.id AS entry_id, e2.question
           FROM message_retrievals mr
           LEFT JOIN kb_entries e2 ON e2.id = mr.entry_id
           WHERE mr.message_id = %s ORDER BY mr.rank LIMIT 10""",
        (row["message_id"],),
    )
    return row


@router.patch("/escalations/{escalation_id}")
async def update_escalation(
    escalation_id: int,
    req: ResolveRequest,
    new_status: str = Query(alias="status"),
    user: dict = Depends(require_roles("support", "manager")),
) -> dict:
    if new_status not in {"open", "in_progress", "resolved", "dismissed"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid status")
    await execute(
        """UPDATE escalations
           SET status = %s::escalation_status, assigned_to = %s, resolution_note = %s,
               resolved_at = CASE WHEN %s IN ('resolved','dismissed') THEN now() ELSE NULL END
           WHERE id = %s AND tenant_id = %s""",
        (new_status, user["id"], req.resolution_note, new_status, escalation_id, user["tenant_id"]),
    )
    await audit.record(
        action=f"escalation.{new_status}", entity_type="escalation", entity_id=escalation_id,
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
        after={"status": new_status, "note": req.resolution_note},
    )
    return {"id": escalation_id, "status": new_status}


@router.post("/escalations/{escalation_id}/promote", status_code=status.HTTP_201_CREATED)
async def promote_to_kb(
    escalation_id: int, req: PromoteRequest, user: dict = Depends(require_roles("support"))
) -> dict:
    esc = await fetch_one(
        "SELECT id, conversation_id FROM escalations WHERE id=%s AND tenant_id=%s",
        (escalation_id, user["tenant_id"]),
    )
    if not esc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Escalation not found")

    chash = ingest.content_hash(req.question, req.answer)
    entry = await fetch_one(
        """INSERT INTO kb_entries (tenant_id, question, answer, category, tags, citation,
                                   status, source, external_id, content_hash, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,'draft','from_conversation',%s,%s,%s) RETURNING id""",
        (user["tenant_id"], req.question, req.answer, req.category, req.tags,
         req.citation, chash[:24], chash, user["id"]),
    )
    await execute(
        """UPDATE escalations SET resolved_entry_id=%s, status='resolved',
                                  assigned_to=%s, resolved_at=now()
           WHERE id=%s""",
        (entry["id"], user["id"], escalation_id),
    )
    await audit.record(
        action="escalation.promote", entity_type="kb_entry", entity_id=entry["id"],
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
        after={"from_escalation": escalation_id, "question": req.question},
    )
    # Draft, not published — it still needs a manager's approval.
    return {"entry_id": entry["id"], "status": "draft", "escalation_id": escalation_id}


# ----------------------------------------------------------------- analytics
@router.get("/analytics/overview")
async def analytics_overview(
    user: dict = Depends(require_roles("manager")),
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    since = date.today() - timedelta(days=days)
    tid = user["tenant_id"]

    volume = await fetch_all(
        "SELECT * FROM v_daily_volume WHERE tenant_id=%s AND day >= %s ORDER BY day",
        (tid, since),
    )
    deflection = await fetch_all(
        "SELECT * FROM v_deflection WHERE tenant_id=%s AND day >= %s ORDER BY day",
        (tid, since),
    )
    satisfaction = await fetch_all(
        "SELECT * FROM v_satisfaction WHERE tenant_id=%s AND day >= %s ORDER BY day",
        (tid, since),
    )
    cost = await fetch_all(
        "SELECT * FROM v_cost_daily WHERE tenant_id=%s AND day >= %s ORDER BY day",
        (tid, since),
    )
    kb_health = await fetch_one("SELECT * FROM v_kb_health WHERE tenant_id=%s", (tid,))

    totals = await fetch_one(
        """SELECT
             (SELECT count(*) FROM conversations WHERE tenant_id=%s AND started_at >= %s) AS conversations,
             (SELECT count(*) FROM escalations   WHERE tenant_id=%s AND status='open')    AS open_escalations,
             (SELECT round(avg(csat),2) FROM conversations
                WHERE tenant_id=%s AND csat IS NOT NULL AND started_at >= %s)             AS avg_csat""",
        (tid, since, tid, tid, since),
    )
    return {
        "period_days": days,
        "totals": totals,
        "kb_health": kb_health,
        "daily_volume": volume,
        "deflection": deflection,
        "satisfaction": satisfaction,
        "cost": cost,
    }


@router.get("/analytics/gaps")
async def content_gaps(
    user: dict = Depends(require_roles("support", "manager")),
    limit: int = Query(default=50, le=200),
) -> dict:
    """Questions the bot could not answer — the support team's work queue."""
    rows = await fetch_all(
        "SELECT * FROM v_unanswered WHERE tenant_id=%s LIMIT %s::int",
        (user["tenant_id"], limit),
    )
    return {"items": rows}


@router.get("/analytics/entry-usage")
async def entry_usage(
    user: dict = Depends(require_roles("manager")),
    limit: int = Query(default=100, le=500),
) -> dict:
    """Which entries earn their place, and which are never retrieved."""
    rows = await fetch_all(
        """SELECT * FROM v_entry_usage WHERE tenant_id=%s
           ORDER BY times_used DESC NULLS LAST LIMIT %s::int""",
        (user["tenant_id"], limit),
    )
    return {"items": rows}


# --------------------------------------------------------------------- audit
@router.get("/audit")
async def read_audit(
    user: dict = Depends(require_roles("manager")),
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = Query(default=100, le=500),
) -> dict:
    where = ["tenant_id = %s"]
    params: list = [user["tenant_id"]]
    if entity_type:
        where.append("entity_type = %s")
        params.append(entity_type)
    if entity_id:
        where.append("entity_id = %s")
        params.append(entity_id)
    rows = await fetch_all(
        f"""SELECT id, actor_label, action, entity_type, entity_id, before, after, created_at
            FROM audit_log WHERE {' AND '.join(where)}
            ORDER BY id DESC LIMIT %s::int""",
        tuple(params + [limit]),
    )
    return {"items": rows}


@router.get("/audit/verify")
async def verify_audit(user: dict = Depends(require_roles("manager"))) -> dict:
    """Recompute the hash chain end to end and report any break."""
    return await audit.verify_chain()


# --------------------------------------------------------------------- staff
class StaffCreate(BaseModel):
    email: str
    full_name: str
    password: str = Field(min_length=8)
    role: str


@router.get("/staff")
async def list_staff(user: dict = Depends(require_roles())) -> dict:
    rows = await fetch_all(
        """SELECT id, email, full_name, role::text AS role, is_active, last_login_at, created_at
           FROM staff_users WHERE tenant_id=%s ORDER BY created_at""",
        (user["tenant_id"],),
    )
    return {"items": rows}


@router.post("/staff", status_code=status.HTTP_201_CREATED)
async def create_staff(req: StaffCreate, user: dict = Depends(require_roles())) -> dict:
    if req.role not in {"admin", "support", "manager"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid role")
    row = await fetch_one(
        """INSERT INTO staff_users (tenant_id, email, full_name, password_hash, role)
           VALUES (%s,%s,%s,%s,%s::staff_role)
           ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id""",
        (user["tenant_id"], req.email, req.full_name,
         security.hash_password(req.password), req.role),
    )
    if not row:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already exists for this tenant")
    await audit.record(
        action="staff.create", entity_type="staff_user", entity_id=row["id"],
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
        after={"email": req.email, "role": req.role},
    )
    return {"id": row["id"], "email": req.email, "role": req.role}


@router.patch("/staff/{staff_id}/deactivate")
async def deactivate_staff(staff_id: int, user: dict = Depends(require_roles())) -> dict:
    if staff_id == user["id"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot deactivate your own account")
    await execute(
        "UPDATE staff_users SET is_active = FALSE WHERE id=%s AND tenant_id=%s",
        (staff_id, user["tenant_id"]),
    )
    await execute(
        "UPDATE staff_sessions SET revoked_at = now() WHERE staff_id=%s AND revoked_at IS NULL",
        (staff_id,),
    )
    await audit.record(
        action="staff.deactivate", entity_type="staff_user", entity_id=staff_id,
        tenant_id=user["tenant_id"], actor_id=user["id"], actor_label=user["email"],
    )
    return {"id": staff_id, "is_active": False}
