"""Public widget API: session issuing, streaming chat, ratings, contact requests.

Authorisation model
-------------------
End users are anonymous, so there is no login. Authority instead comes from two
things the caller cannot forge:

* a per-tenant **site key**, identifying which widget is calling. Checked
  server-side, because CORS is enforced by the browser and does nothing against a
  direct HTTP client.
* a **signed session token** committing to one conversation id. Every endpoint
  derives its conversation from the token, so "which conversation am I acting on"
  is never a caller-supplied parameter.

An earlier version trusted a client-supplied `session_id`, which allowed posting
into another user's conversation, reading their history into the LLM context, and
overwriting their ratings. Ownership is now structural rather than assumed.
"""
import hashlib
import json
import logging
import secrets
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.chat import service
from app.core import session_token
from app.core.config import settings
from app.core.db import connection, execute, fetch_all, fetch_one
from app.core.net import client_ip

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

MAX_MESSAGE_CHARS = 2000
HISTORY_TURNS = 6


# ------------------------------------------------------------------ schemas
class SessionRequest(BaseModel):
    site_key: str = Field(min_length=8, max_length=128)
    external_ref: str | None = Field(default=None, max_length=128)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class RateRequest(BaseModel):
    message_id: int
    value: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=1000)


class CsatRequest(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


class ContactRequest(BaseModel):
    """A person asking an operator to get back to them.

    Contact details are required — an operator cannot act on a request with no way
    to reach the person, and a queue full of unanswerable tickets is worse than an
    empty one.
    """
    name: str = Field(min_length=2, max_length=120)
    channel: str = Field(pattern="^(phone|email)$")
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


# ------------------------------------------------------------------ helpers
def _hash_ip(request: Request) -> str:
    """Salted, truncated hash — the address itself is never stored."""
    return hashlib.sha256(
        f"{settings.auth_session_secret}|{client_ip(request)}".encode()
    ).hexdigest()[:32]


def _origin_of(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    referer = request.headers.get("referer")
    if referer:
        parts = referer.split("/")
        if len(parts) >= 3:
            return f"{parts[0]}//{parts[2]}"
    return None


async def _tenant_by_site_key(site_key: str, request: Request) -> dict:
    tenant = await fetch_one(
        """SELECT id, slug, name, scope_desc, allowed_origins
           FROM tenants WHERE site_key = %s AND is_active""",
        (site_key,),
    )
    if not tenant:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid site key")

    # Server-side origin check. Weak on its own (an attacker sets any header they
    # like) but it stops the widget being embedded on unauthorised sites, which
    # CORS alone cannot do for non-browser callers.
    allowed = tenant["allowed_origins"] or []
    if allowed:
        origin = _origin_of(request)
        if origin and origin not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin not allowed for this site key")
    return tenant


async def _conversation(authorization: str | None) -> dict:
    """Resolve the bearer token to a conversation. This is the ownership check."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing session token")
    claims = session_token.verify(authorization[7:].strip())
    if not claims:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session token")

    conv = await fetch_one(
        """SELECT c.id, c.tenant_id, c.session_id,
                  t.name AS tenant_name, t.scope_desc
           FROM conversations c JOIN tenants t ON t.id = c.tenant_id
           WHERE c.id = %s::uuid""",
        (claims["cid"],),
    )
    if not conv or conv["tenant_id"] != claims["tid"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Conversation not found")
    return conv


async def _owned_message(conversation_id, message_id: int) -> dict:
    """Confirm a message belongs to the caller's conversation.

    Without this, /rate and /csat accepted any id in the database — which the
    security probe used to rate and escalate other users' messages.
    """
    row = await fetch_one(
        """SELECT id FROM messages
           WHERE id = %s AND conversation_id = %s AND role = 'assistant'""",
        (message_id, conversation_id),
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found in this conversation")
    return row


async def _history(conversation_id) -> list[dict]:
    rows = await fetch_all(
        """SELECT role::text AS role, content FROM messages
           WHERE conversation_id = %s AND role IN ('user','assistant')
           ORDER BY created_at DESC LIMIT %s::int""",
        (conversation_id, HISTORY_TURNS),
    )
    return list(reversed(rows))


async def _add_message(conversation_id, role: str, content: str) -> int:
    row = await fetch_one(
        """INSERT INTO messages (conversation_id, role, content)
           VALUES (%s, %s::message_role, %s) RETURNING id""",
        (conversation_id, role, content),
    )
    return row["id"]


def _sse(event: str, data: str) -> str:
    payload = "\n".join(f"data: {line}" for line in data.split("\n"))
    return f"event: {event}\n{payload}\n\n"


def _reference_code() -> str:
    """Short, unambiguous code the user can quote when following up."""
    alphabet = "ACDEFGHJKLMNPQRTUVWXY34789"  # no look-alike characters
    return "DS-" + "".join(secrets.choice(alphabet) for _ in range(6))


# ----------------------------------------------------------------- endpoints
@router.post("/session")
async def create_session(req: SessionRequest, request: Request) -> dict:
    """Start a conversation and return its signed token."""
    tenant = await _tenant_by_site_key(req.site_key, request)
    conv = await fetch_one(
        """INSERT INTO conversations (tenant_id, session_id, external_ref, user_agent, ip_hash)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (tenant["id"], secrets.token_urlsafe(18), req.external_ref,
         request.headers.get("user-agent", "")[:400], _hash_ip(request)),
    )
    return {
        "session_token": session_token.issue(str(conv["id"]), tenant["id"]),
        "tenant_name": tenant["name"],
    }


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    conv = await _conversation(authorization)
    tenant = {"id": conv["tenant_id"], "name": conv["tenant_name"],
              "scope_desc": conv["scope_desc"]}

    history = await _history(conv["id"])
    await _add_message(conv["id"], "user", req.message)
    await execute("UPDATE conversations SET last_at = now() WHERE id = %s", (conv["id"],))

    ctx = await service.prepare(tenant["id"], history, req.message)

    async def generator() -> AsyncIterator[str]:
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
            message_id = await _add_message(conv["id"], "assistant", answer)
            await service.persist_trace(message_id, ctx, meta)

            if ctx.mode == "refused":
                # Out of scope. Logged for abuse monitoring, but deliberately NOT
                # turned into an operator task — nobody owes this person a call.
                await execute(
                    """INSERT INTO refusals (tenant_id, conversation_id, message_id,
                                             question, category, confidence)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (tenant["id"], conv["id"], message_id, req.message,
                     ctx.refusal_category or "out_of_scope", ctx.retrieval.confidence),
                )
            elif ctx.mode == "escalated":
                # In scope, but the knowledge base is missing something. An
                # internal content gap — still not an operator task.
                await execute(
                    """INSERT INTO escalations (tenant_id, conversation_id, message_id,
                                                reason, kind)
                       VALUES (%s, %s, %s, 'low_confidence', 'content_gap')""",
                    (tenant["id"], conv["id"], message_id),
                )

            meta["message_id"] = message_id
            meta["mode"] = ctx.mode
            # Only invite a handover when the bot actually fell short. Offering it
            # under every answer signals no confidence in the bot and trains users
            # to skip it entirely.
            meta["offer_contact"] = ctx.mode in ("escalated", "refused")
            yield _sse("done", json.dumps(meta, ensure_ascii=False))

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/rate")
async def rate(req: RateRequest, authorization: str | None = Header(default=None)) -> dict:
    conv = await _conversation(authorization)
    await _owned_message(conv["id"], req.message_id)

    async with connection() as conn:
        await conn.execute(
            """INSERT INTO ratings (message_id, value, comment) VALUES (%s, %s, %s)
               ON CONFLICT (message_id) DO UPDATE
               SET value = EXCLUDED.value, comment = EXCLUDED.comment""",
            (req.message_id, req.value, req.comment),
        )
        if req.value == -1:
            # A thumbs-down is a content gap, not a request to be called back.
            await conn.execute(
                """INSERT INTO escalations (tenant_id, conversation_id, message_id,
                                            reason, kind)
                   VALUES (%s, %s, %s, 'negative_rating', 'content_gap')""",
                (conv["tenant_id"], conv["id"], req.message_id),
            )
    return {"ok": True}


@router.post("/csat")
async def csat(req: CsatRequest, authorization: str | None = Header(default=None)) -> dict:
    conv = await _conversation(authorization)
    await execute(
        """UPDATE conversations SET csat = %s, csat_comment = %s, csat_at = now()
           WHERE id = %s""",
        (req.score, req.comment, conv["id"]),
    )
    return {"ok": True}


@router.post("/contact")
async def contact(
    req: ContactRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    """"Operatorla əlaqə" — the user asks a human to get back to them.

    Creates a real operator task carrying the full transcript, so the person never
    has to repeat their question, and returns a reference code so they can see the
    request landed somewhere.
    """
    conv = await _conversation(authorization)

    if req.channel == "phone" and not req.phone:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Phone number required")
    if req.channel == "email" and not req.email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Email required")

    # One open request per conversation: clicking twice should not create two
    # tickets for the same person.
    existing = await fetch_one(
        """SELECT id, reference_code FROM escalations
           WHERE conversation_id = %s AND kind = 'contact_request'
             AND status IN ('open','in_progress')
           ORDER BY created_at DESC LIMIT 1""",
        (conv["id"],),
    )
    if existing:
        return {"ok": True, "reference_code": existing["reference_code"], "duplicate": True}

    last_message = await fetch_one(
        """SELECT id FROM messages WHERE conversation_id = %s AND role = 'assistant'
           ORDER BY created_at DESC LIMIT 1""",
        (conv["id"],),
    )
    code = _reference_code()
    row = await fetch_one(
        """INSERT INTO escalations
               (tenant_id, conversation_id, message_id, reason, kind, reference_code,
                contact_name, contact_phone, contact_email, preferred_channel, contact_note)
           VALUES (%s, %s, %s, 'user_request', 'contact_request', %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (conv["tenant_id"], conv["id"], last_message["id"] if last_message else None,
         code, req.name, req.phone, req.email, req.channel, req.note),
    )
    log.info("contact request %s created (escalation %s)", code, row["id"])
    return {"ok": True, "reference_code": code, "duplicate": False}
