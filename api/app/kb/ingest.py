"""Knowledge-base ingestion and embedding refresh.

Two properties matter here and both are driven by `content_hash`:

* Idempotence — re-running an import updates rows rather than duplicating them,
  keyed on (tenant, source, external_id).
* Cheap re-runs — an entry whose text has not changed keeps its embedding.
  Re-embedding an unchanged corpus is pure waste, and on CPU it is slow waste.
"""
import hashlib
import logging

from app.core.db import connection, fetch_all
from app.retrieval.models import embed_many

log = logging.getLogger(__name__)

EMBED_BATCH = 16


def content_hash(question: str, answer: str) -> str:
    return hashlib.sha256(f"{question}\x00{answer}".encode()).hexdigest()


def embedding_text(question: str, answer: str) -> str:
    """Embed question and answer together.

    Users frequently phrase a query using vocabulary from the answer body rather
    than the title ("faylı yükləyə bilmirəm" vs an entry titled "Sənəd formatları"),
    so indexing the title alone loses a lot of recall.
    """
    return f"{question}\n{answer}"


async def upsert_entries(
    tenant_id: int,
    rows: list[dict],
    source: str = "manual",
    status: str = "draft",
    created_by: int | None = None,
) -> dict[str, int]:
    """Insert or update KB entries. Returns counts for the ingest_runs record."""
    stats = {"seen": 0, "created": 0, "updated": 0, "skipped": 0}

    async with connection() as conn:
        for row in rows:
            stats["seen"] += 1
            question = (row.get("question") or "").strip()
            answer = (row.get("answer") or "").strip()
            if not question or not answer:
                stats["skipped"] += 1
                continue

            chash = content_hash(question, answer)
            external_id = row.get("external_id") or hashlib.sha256(
                question.encode()
            ).hexdigest()[:24]

            existing = await (await conn.execute(
                """SELECT id, version, content_hash, question, answer, status::text AS status
                   FROM kb_entries
                   WHERE tenant_id = %s AND source = %s AND external_id = %s""",
                (tenant_id, source, external_id),
            )).fetchone()

            if existing and existing["content_hash"] == chash:
                stats["skipped"] += 1
                continue

            if existing:
                # Snapshot the outgoing version before overwriting it.
                await conn.execute(
                    """INSERT INTO kb_entry_versions
                           (entry_id, version, question, answer, status, citation, changed_by, change_note)
                       VALUES (%s,%s,%s,%s,%s::kb_status,%s,%s,%s)
                       ON CONFLICT (entry_id, version) DO NOTHING""",
                    (existing["id"], existing["version"], existing["question"],
                     existing["answer"], existing["status"], row.get("citation"),
                     created_by, f"replaced by {source} ingest"),
                )
                await conn.execute(
                    """UPDATE kb_entries
                       SET question = %s, answer = %s, category = %s, tags = %s,
                           citation = %s, content_hash = %s,
                           version = version + 1, updated_at = now()
                       WHERE id = %s""",
                    (question, answer, row.get("category"), row.get("tags") or [],
                     row.get("citation"), chash, existing["id"]),
                )
                stats["updated"] += 1
            else:
                await conn.execute(
                    """INSERT INTO kb_entries
                           (tenant_id, question, answer, category, tags, citation,
                            status, source, external_id, content_hash, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s::kb_status,%s,%s,%s,%s)""",
                    (tenant_id, question, answer, row.get("category"),
                     row.get("tags") or [], row.get("citation"), status, source,
                     external_id, chash, created_by),
                )
                stats["created"] += 1

    return stats


async def refresh_embeddings(tenant_id: int, model: str, force: bool = False) -> dict[str, int]:
    """Embed any entry whose stored embedding is missing or stale."""
    if force:
        stale = await fetch_all(
            "SELECT id, question, answer, content_hash FROM kb_entries WHERE tenant_id = %s",
            (tenant_id,),
        )
    else:
        stale = await fetch_all(
            """SELECT e.id, e.question, e.answer, e.content_hash
               FROM kb_entries e
               LEFT JOIN kb_embeddings emb
                      ON emb.entry_id = e.id AND emb.model = %s
               WHERE e.tenant_id = %s
                 AND (emb.entry_id IS NULL OR emb.content_hash <> e.content_hash)""",
            (model, tenant_id),
        )

    if not stale:
        return {"embedded": 0, "total": 0}

    log.info("embedding %d entries with %s", len(stale), model)
    embedded = 0
    async with connection() as conn:
        for i in range(0, len(stale), EMBED_BATCH):
            batch = stale[i : i + EMBED_BATCH]
            vectors = embed_many([embedding_text(r["question"], r["answer"]) for r in batch])
            for row, vec in zip(batch, vectors):
                await conn.execute(
                    """INSERT INTO kb_embeddings (entry_id, model, content_hash, embedding)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (entry_id, model) DO UPDATE
                       SET embedding = EXCLUDED.embedding,
                           content_hash = EXCLUDED.content_hash,
                           created_at = now()""",
                    (row["id"], model, row["content_hash"], vec),
                )
                embedded += 1
            log.info("  embedded %d/%d", embedded, len(stale))

    return {"embedded": embedded, "total": len(stale)}
