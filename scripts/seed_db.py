"""Seed the local database: tenant, staff users, and the synthetic knowledge base.

Local development only. The staff passwords here are fixed and weak on purpose so
the team can log in without a credential exchange; `--env` guards against ever
pointing this at a real deployment.

Usage:
    python -u scripts/seed_db.py                 # tenant + staff + KB + embeddings
    python -u scripts/seed_db.py --skip-embed    # skip the slow CPU embedding pass
"""
import argparse
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.core.config import settings  # noqa: E402
from app.core.db import close_pool, fetch_one, execute  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.kb import ingest  # noqa: E402

TENANT = {
    "slug": "mof-contracts",
    "name": "Dövlət Satınalmaları Portalı",
    "scope_desc": (
        "Bu sistem dövlət satınalmaları (e-tender) portalıdır: təşkilatların qeydiyyatı, "
        "tenderlərin axtarışı, təkliflərin hazırlanması və göndərilməsi, sənədlərin "
        "elektron imzalanması, müqavilələrin bağlanması, ödənişlər və şikayət prosedurları."
    ),
}

STAFF = [
    ("admin@mof.local",   "Sistem Administratoru", "admin",   "dev12345"),
    ("support@mof.local", "Dəstək Əməkdaşı",       "support", "dev12345"),
    ("manager@mof.local", "Dəstək Meneceri",       "manager", "dev12345"),
    # A second support account, so the four-eyes rule can actually be exercised
    # locally: one person drafts, a different person approves.
    ("support2@mof.local", "Dəstək Əməkdaşı 2",    "support", "dev12345"),
]


async def seed_tenant() -> int:
    row = await fetch_one("SELECT id FROM tenants WHERE slug = %s", (TENANT["slug"],))
    if row:
        await execute(
            "UPDATE tenants SET name=%s, scope_desc=%s WHERE id=%s",
            (TENANT["name"], TENANT["scope_desc"], row["id"]),
        )
        print(f"tenant exists: {TENANT['slug']} (id={row['id']})")
        return row["id"]
    created = await fetch_one(
        "INSERT INTO tenants (slug, name, scope_desc) VALUES (%s,%s,%s) RETURNING id",
        (TENANT["slug"], TENANT["name"], TENANT["scope_desc"]),
    )
    print(f"tenant created: {TENANT['slug']} (id={created['id']})")
    return created["id"]


async def seed_staff(tenant_id: int) -> None:
    for email, name, role, password in STAFF:
        row = await fetch_one(
            """INSERT INTO staff_users (tenant_id, email, full_name, password_hash, role)
               VALUES (%s,%s,%s,%s,%s::staff_role)
               ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id""",
            (tenant_id, email, name, hash_password(password), role),
        )
        print(f"  staff {'created' if row else 'exists '}: {email:22} {role}")


async def seed_kb(tenant_id: int, path: pathlib.Path) -> None:
    if not path.exists():
        print(f"!! {path} not found — run scripts/generate_seed_kb.py first")
        return
    items = json.loads(path.read_text(encoding="utf-8"))
    # Published directly: this is dev seed data, and routing 200 rows through the
    # approval workflow would only get in the way. Real content follows the
    # draft -> pending_approval -> published path.
    stats = await ingest.upsert_entries(
        tenant_id, items, source="synthetic", status="published"
    )
    print(f"  kb: {stats}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--kb", default="data/seed_kb.json")
    args = ap.parse_args()

    if settings.app_env != "local":
        sys.exit(f"refusing to seed: APP_ENV is '{settings.app_env}', not 'local'")

    tenant_id = await seed_tenant()
    await seed_staff(tenant_id)
    await seed_kb(tenant_id, pathlib.Path(args.kb))

    if not args.skip_embed:
        print("embedding (cpu, this takes a few minutes on first run)...")
        result = await ingest.refresh_embeddings(tenant_id, settings.embedding_model)
        print(f"  embeddings: {result}")

    counts = await fetch_one(
        """SELECT (SELECT count(*) FROM kb_entries WHERE tenant_id=%s) AS entries,
                  (SELECT count(*) FROM kb_embeddings emb JOIN kb_entries e ON e.id=emb.entry_id
                    WHERE e.tenant_id=%s) AS embeddings""",
        (tenant_id, tenant_id),
    )
    print(f"\ndone: {counts['entries']} entries, {counts['embeddings']} embeddings")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
