"""Hash-chained, append-only audit log.

Every row commits to its predecessor's hash. Rewriting or deleting any historical
row breaks the chain from that point onward, which `verify_chain` detects. The
database also blocks UPDATE/DELETE via trigger (see migration 002) — the chain is
the second line of defence, protecting against tampering with direct table access.
"""
import hashlib
import json
from typing import Any

from app.core.db import connection


def _canonical(payload: dict[str, Any]) -> str:
    """Stable JSON so the same logical row always hashes identically."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str | None, payload: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash or ''}|{_canonical(payload)}".encode()).hexdigest()


async def record(
    *,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    tenant_id: int | None = None,
    actor_id: int | None = None,
    actor_label: str = "system",
    before: dict | None = None,
    after: dict | None = None,
) -> str:
    """Append one audit entry and return its hash."""
    async with connection() as conn:
        # Lock so concurrent writers cannot both chain off the same predecessor.
        await conn.execute("LOCK TABLE audit_log IN SHARE ROW EXCLUSIVE MODE")
        cur = await conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1")
        prev = await cur.fetchone()
        prev_hash = prev["row_hash"] if prev else None

        payload = {
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "actor_label": actor_label,
            "before": before,
            "after": after,
        }
        row_hash = compute_hash(prev_hash, payload)

        await conn.execute(
            """
            INSERT INTO audit_log (tenant_id, actor_id, actor_label, action,
                                   entity_type, entity_id, before, after,
                                   prev_hash, row_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id, actor_id, actor_label, action, entity_type,
                str(entity_id) if entity_id is not None else None,
                json.dumps(before, default=str) if before else None,
                json.dumps(after, default=str) if after else None,
                prev_hash, row_hash,
            ),
        )
    return row_hash


async def verify_chain() -> dict[str, Any]:
    """Recompute the whole chain. Returns the first broken link, if any."""
    async with connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, tenant_id, actor_id, actor_label, action, entity_type,
                   entity_id, before, after, prev_hash, row_hash
            FROM audit_log ORDER BY id
            """
        )
        rows = await cur.fetchall()

    prev_hash = None
    for row in rows:
        payload = {
            "action": row["action"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "tenant_id": row["tenant_id"],
            "actor_id": row["actor_id"],
            "actor_label": row["actor_label"],
            "before": row["before"],
            "after": row["after"],
        }
        expected = compute_hash(prev_hash, payload)
        if expected != row["row_hash"] or (row["prev_hash"] or None) != prev_hash:
            return {"valid": False, "broken_at_id": row["id"], "checked": len(rows)}
        prev_hash = row["row_hash"]

    return {"valid": True, "broken_at_id": None, "checked": len(rows)}
