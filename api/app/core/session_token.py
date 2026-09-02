"""Server-issued, HMAC-signed conversation tokens for the public chat API.

The problem this replaces
-------------------------
The widget used to send a `session_id` it made up itself, and the server trusted
it. A security probe confirmed the consequence: posting another user's session_id
appended messages to *their* conversation and pulled *their* history into the LLM
context. Ratings and CSAT had the same shape of hole.

The fix
-------
The server mints a token that commits to the conversation id. The client cannot
forge one, and every subsequent request derives its conversation from the token
rather than from anything the caller claims. Ownership stops being a parameter.

This is a bearer token, not a login. It proves "you started this conversation",
which is exactly the authority an anonymous support chat needs — no more.
"""
import base64
import hashlib
import hmac
import json
import time

from app.core.config import settings

TOKEN_TTL_SECONDS = 12 * 3600


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> bytes:
    return hmac.new(settings.auth_session_secret.encode(), payload, hashlib.sha256).digest()


def issue(conversation_id: str, tenant_id: int, ttl: int = TOKEN_TTL_SECONDS) -> str:
    payload = json.dumps(
        {"cid": conversation_id, "tid": tenant_id, "exp": int(time.time()) + ttl},
        separators=(",", ":"),
    ).encode()
    return f"{_b64e(payload)}.{_b64e(_sign(payload))}"


def verify(token: str) -> dict | None:
    """Return the token's claims, or None if it is forged, malformed or expired."""
    try:
        payload_b64, signature_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        # compare_digest, not ==, so the check does not leak signature bytes
        # through response timing.
        if not hmac.compare_digest(_b64d(signature_b64), _sign(payload)):
            return None
        claims = json.loads(payload)
    except (ValueError, KeyError, json.JSONDecodeError):
        return None

    if claims.get("exp", 0) < time.time():
        return None
    if not claims.get("cid") or not claims.get("tid"):
        return None
    return claims
