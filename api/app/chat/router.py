"""Public widget API: streaming chat, ratings, CSAT and escalation.

These endpoints are unauthenticated by design — the widget is embedded in the host
application and end users are anonymous. Protection is origin allow-listing plus
per-session rate limiting, not user identity.
"""
import hashlib
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.chat import service
from app.core.config import settings
from app.core.db import connection, fetch_one, fetch_all, execute

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

MAX_MESSAGE_CHARS = 2000
HISTORY_TURNS = 6


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=128)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    tenant: str | None = None
    external_ref: str | None = Field(default=None, max_length=128)


class RateRequest(BaseModel):
    message_id: int
    value: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=1000)


class CsatRequest(BaseModel):
    session_id: str
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


class EscalateRequest(BaseModel):
    session_id: str
    note: str | None = Field(default=None, max_length=2000)
    message_id: int | None = None


def _hash_ip(request: Request) -> str:
    """Store a salted hash, never the address itself — PII minimisation."""
    ip = request.client.host if request.client else ""
    return hashlib.sha256(f"{settings.auth_session_secret}|{ip}".encode()).hexdigest()[:32]


async def _tenant(slug: str | None) -> dict:
    row = await fetch_one(
        "SELECT id, slug, name, scope_desc FROM tenants WHERE slug = %s AND is_active",
        (slug or settings.default_tenant,),
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown tenant")
    return row


async def _get_or_create_conversation(tenant: dict, req: ChatRequest, request: Request) -> str:
    row = await fetch_one(
        """SELECT id FROM conversations
           WHERE tenant_id = %s AND session_id = %s
           ORDER BY started_at DESC LIMIT 1""",
        (tenant["id"], req.session_id),
    )
    if row:
        await execute("UPDATE conversations SET last_at = now() WHERE id = %s", (row["id"],))
        return str(row["id"])
    created = await fetch_one(
        """INSERT INTO conversations (tenant_id, session_id, external_ref, user_agent, ip_hash)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (tenant["id"], req.session_id, req.external_ref,
         request.headers.get("user-agent", "")[:400], _hash_ip(request)),
    )
    return str(created["id"])


async def _history(conversation_id: str) -> list[dict]:
    rows = await fetch_all(
        """SELECT role::text AS role, content FROM messages
           WHERE conversation_id = %s::uuid AND role IN ('user','assistant')
           ORDER BY created_at DESC LIMIT %s::int""",
        (conversation_id, HISTORY_TURNS),
    )
    return list(reversed(rows))


async def _add_message(conversation_id: str, role: str, content: str) -> int:
    row = await fetch_one(
        """INSERT INTO messages (conversation_id, role, content)
           VALUES (%s::uuid, %s::message_role, %s) RETURNING id""",
        (conversation_id, role, content),
    )
    return row["id"]


def _sse(event: str, data: str) -> str:
    # Newlines must be escaped per line or the SSE frame terminates early.
    payload = "\n".join(f"data: {line}" for line in data.split("\n"))
    return f"event: {event}\n{payload}\n\n"


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    tenant = await _tenant(req.tenant)
    conversation_id = await _get_or_create_conversation(tenant, req, request)
    history = await _history(conversation_id)
    await _add_message(conversation_id, "user", req.message)

    ctx = await service.prepare(tenant["id"], history, req.message)

    async def generator() -> AsyncIterator[str]:
        # Tell the widget which conversation this is before any tokens arrive,
        # so it can attach ratings and escalations without waiting.
        yield _sse("meta", json.dumps({"conversation_id": conversation_id}))

        collected: list[str] = []
        meta: dict = {}
        try:
            async for event, data in service.stream_answer(tenant, ctx, history, req.message):
                if event == "token":
                    collected.append(data)
                    yield _sse("token", data)
                else:
                    meta = json.loads(data)
        finally:
            answer = "".join(collected)
            message_id = await _add_message(conversation_id, "assistant", answer)
            await service.persist_trace(message_id, ctx, meta)

            # Low confidence opens an escalation automatically, so the support
            # team sees the gap even if the user simply gives up and leaves.
            if ctx.escalate:
                await execute(
                    """INSERT INTO escalations (tenant_id, conversation_id, message_id, reason)
                       VALUES (%s, %s::uuid, %s, 'low_confidence')""",
                    (tenant["id"], conversation_id, message_id),
                )
            meta["message_id"] = message_id
            yield _sse("done", json.dumps(meta, ensure_ascii=False))

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/rate")
async def rate(req: RateRequest) -> dict:
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO ratings (message_id, value, comment) VALUES (%s, %s, %s)
               ON CONFLICT (message_id) DO UPDATE
               SET value = EXCLUDED.value, comment = EXCLUDED.comment""",
            (req.message_id, req.value, req.comment),
        )
        # A thumbs-down is a content gap the support team should see.
        if req.value == -1:
            await conn.execute(
                """INSERT INTO escalations (tenant_id, conversation_id, message_id, reason)
                   SELECT c.tenant_id, c.id, m.id, 'negative_rating'
                   FROM messages m JOIN conversations c ON c.id = m.conversation_id
                   WHERE m.id = %s""",
                (req.message_id,),
            )
    return {"ok": True}


@router.post("/csat")
async def csat(req: CsatRequest) -> dict:
    await execute(
        """UPDATE conversations SET csat = %s, csat_comment = %s, csat_at = now()
           WHERE session_id = %s""",
        (req.score, req.comment, req.session_id),
    )
    return {"ok": True}


@router.post("/escalate")
async def escalate(req: EscalateRequest) -> dict:
    """The 'Dəstəyə yaz' button. Hands the whole transcript to a human so the
    user never has to repeat themselves."""
    conv = await fetch_one(
        """SELECT id, tenant_id FROM conversations
           WHERE session_id = %s ORDER BY started_at DESC LIMIT 1""",
        (req.session_id,),
    )
    if not conv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    row = await fetch_one(
        """INSERT INTO escalations (tenant_id, conversation_id, message_id, reason, contact_note)
           VALUES (%s, %s, %s, 'user_request', %s) RETURNING id""",
        (conv["tenant_id"], conv["id"], req.message_id, req.note),
    )
    return {"ok": True, "escalation_id": row["id"]}
