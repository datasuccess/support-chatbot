"""Staff authentication and role-based access control.

Three roles, per the agreed model:
  support  — works the review queue, drafts KB entries, handles escalations
  manager  — approves/publishes entries (the 4-eyes control), sees analytics
  admin    — everything, plus staff and tenant management

End users of the widget are anonymous; they never authenticate here.
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, HTTPException, status

from app.core.config import settings
from app.core.db import fetch_one, execute

Role = Literal["admin", "support", "manager"]

# Argon2id — the current password-hashing recommendation, and unlike bcrypt its
# encoded hashes survive shell interpolation without $-expansion surprises.
_hasher = PasswordHasher()

SESSION_COOKIE = "sc_session"


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, raw)
        return True
    except VerifyMismatchError:
        return False


async def authenticate(tenant_id: int, email: str, password: str) -> dict | None:
    """Verify credentials, with per-account lockout.

    Per-IP rate limiting alone does not stop a distributed brute force against one
    account: an attacker with a hundred addresses gets a hundred times the budget.
    Lockout is keyed on the account, so it holds regardless of where attempts come
    from.
    """
    user = await fetch_one(
        """SELECT id, tenant_id, email, full_name, role, password_hash, is_active,
                  failed_login_attempts, locked_until
           FROM staff_users WHERE tenant_id = %s AND lower(email) = lower(%s)""",
        (tenant_id, email),
    )
    if not user or not user["is_active"]:
        return None

    if user["locked_until"] and user["locked_until"] > datetime.now(timezone.utc):
        return None

    if not verify_password(password, user["password_hash"]):
        attempts = (user["failed_login_attempts"] or 0) + 1
        if attempts >= settings.max_failed_logins:
            await execute(
                """UPDATE staff_users
                   SET failed_login_attempts = %s, locked_until = now() + (%s || ' minutes')::interval
                   WHERE id = %s""",
                (attempts, settings.lockout_minutes, user["id"]),
            )
        else:
            await execute(
                "UPDATE staff_users SET failed_login_attempts = %s WHERE id = %s",
                (attempts, user["id"]),
            )
        return None

    await execute(
        """UPDATE staff_users
           SET last_login_at = now(), failed_login_attempts = 0, locked_until = NULL
           WHERE id = %s""",
        (user["id"],),
    )
    return user


async def create_session(staff_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    row = await fetch_one(
        "INSERT INTO staff_sessions (staff_id, expires_at) VALUES (%s, %s) RETURNING id",
        (staff_id, expires),
    )
    return str(row["id"])


async def revoke_session(session_id: str) -> None:
    await execute(
        "UPDATE staff_sessions SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
        (session_id,),
    )


async def current_staff(sc_session: str | None = Cookie(default=None)) -> dict:
    """FastAPI dependency: resolves the session cookie to an active staff user."""
    if not sc_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    user = await fetch_one(
        """SELECT u.id, u.tenant_id, u.email, u.full_name, u.role, s.id AS session_id
           FROM staff_sessions s
           JOIN staff_users u ON u.id = s.staff_id
           WHERE s.id = %s::uuid
             AND s.revoked_at IS NULL
             AND s.expires_at > now()
             AND u.is_active""",
        (sc_session,),
    )
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or invalid")
    return user


def require_roles(*roles: Role):
    """Dependency factory guarding an endpoint behind specific roles.

    Admin is deliberately granted everything, so callers list only the
    non-admin roles that should also have access.
    """
    allowed = set(roles) | {"admin"}

    async def _guard(user: dict = Depends(current_staff)) -> dict:
        if user["role"] not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Requires role: {' or '.join(sorted(allowed))}",
            )
        return user

    return _guard


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
