"""Hybrid retrieval: vector + keyword, fused with RRF, then cross-encoder reranked.

Why hybrid rather than vectors alone. This knowledge base is full of exact UI
strings, field names and procedure names ("Təklif ver", "ASAN İmza"). Embeddings
blur those into general meaning; keyword matching nails them. Conversely, a user
asking "pul nə vaxt gəlir?" will never keyword-match an entry titled "Ödənişlərin
icra müddəti" — that is where vectors win. Each covers the other's failure mode.

Fusion uses Reciprocal Rank Fusion, which combines *ranks* rather than scores.
Cosine similarity and ts_rank live on incomparable scales, so mixing the raw
numbers would be meaningless; ranks are directly comparable.
"""
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.db import connection
from app.retrieval.models import embed_one, rerank

RRF_K = 60  # standard damping constant; larger = flatter contribution by rank


@dataclass
class Candidate:
    entry_id: int
    question: str
    answer: str
    category: str | None
    citation: str | None
    version: int
    vector_score: float | None = None
    keyword_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None


@dataclass
class RetrievalResult:
    candidates: list[Candidate]          # everything considered, for the audit trail
    top: list[Candidate] = field(default_factory=list)  # what the LLM actually sees
    confidence: float = 0.0
    elapsed_ms: int = 0


async def _vector_search(conn, tenant_id: int, query: str, limit: int) -> list[dict]:
    vec = embed_one(query)
    cur = await conn.execute(
        """
        SELECT e.id, e.question, e.answer, e.category, e.citation, e.version,
               1 - (emb.embedding <=> %s::vector) AS score
        FROM kb_embeddings emb
        JOIN kb_entries e ON e.id = emb.entry_id
        WHERE e.tenant_id = %s
          AND e.status = 'published'
          AND e.valid_from <= now()
          AND (e.valid_to IS NULL OR e.valid_to > now())
        ORDER BY emb.embedding <=> %s::vector
        LIMIT %s::int
        """,
        (vec, tenant_id, vec, limit),
    )
    return await cur.fetchall()


async def _keyword_search(conn, tenant_id: int, query: str, limit: int) -> list[dict]:
    """Full-text ('simple' config — Azerbaijani has no Postgres stemmer) blended
    with trigram similarity, which absorbs typos and morphological suffixes."""
    cur = await conn.execute(
        """
        WITH q AS (SELECT plainto_tsquery('simple', %s) AS tsq, %s::text AS raw)
        SELECT e.id, e.question, e.answer, e.category, e.citation, e.version,
               (ts_rank(to_tsvector('simple', e.question || ' ' || e.answer), q.tsq)
                + similarity(e.question, q.raw) * 0.5) AS score
        FROM kb_entries e, q
        WHERE e.tenant_id = %s
          AND e.status = 'published'
          AND e.valid_from <= now()
          AND (e.valid_to IS NULL OR e.valid_to > now())
          AND (to_tsvector('simple', e.question || ' ' || e.answer) @@ q.tsq
               OR similarity(e.question, q.raw) > 0.15)
        ORDER BY score DESC
        LIMIT %s::int
        """,
        (query, query, tenant_id, limit),
    )
    return await cur.fetchall()


def _fuse(vector_rows: list[dict], keyword_rows: list[dict]) -> dict[int, Candidate]:
    pool: dict[int, Candidate] = {}

    def _ensure(row: dict) -> Candidate:
        if row["id"] not in pool:
            pool[row["id"]] = Candidate(
                entry_id=row["id"],
                question=row["question"],
                answer=row["answer"],
                category=row["category"],
                citation=row["citation"],
                version=row["version"],
            )
        return pool[row["id"]]

    for rank, row in enumerate(vector_rows, start=1):
        c = _ensure(row)
        c.vector_score = float(row["score"])
        c.vector_rank = rank
        c.rrf_score += 1.0 / (RRF_K + rank)

    for rank, row in enumerate(keyword_rows, start=1):
        c = _ensure(row)
        c.keyword_score = float(row["score"])
        c.keyword_rank = rank
        c.rrf_score += 1.0 / (RRF_K + rank)

    return pool


def _confidence(top_score: float) -> float:
    """Confidence for the gate, on a 0..1 scale.

    sentence-transformers' CrossEncoder already applies a sigmoid for single-label
    rerankers (`activation_fn=Sigmoid()`), so bge-reranker-v2-m3 scores arrive
    bounded in 0..1 and are used directly.

    Applying a second sigmoid here — as an earlier version did — compressed the
    whole range into [0.5, 0.73] and made any threshold below 0.5 unreachable,
    so nothing was ever rejected. The scale is heavily skewed toward zero, hence
    the low default threshold; see docs/RUNBOOK.md on tuning it.
    """
    return top_score


async def search(
    tenant_id: int,
    query: str,
    candidates: int | None = None,
    top_k: int | None = None,
) -> RetrievalResult:
    started = time.perf_counter()
    n_candidates = candidates or settings.retrieval_candidates
    n_top = top_k or settings.retrieval_top_k

    async with connection() as conn:
        vector_rows = await _vector_search(conn, tenant_id, query, n_candidates)
        keyword_rows = await _keyword_search(conn, tenant_id, query, n_candidates)

    pool = _fuse(vector_rows, keyword_rows)
    if not pool:
        return RetrievalResult(
            candidates=[], top=[], confidence=0.0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    fused = sorted(pool.values(), key=lambda c: c.rrf_score, reverse=True)[:n_candidates]

    # Cross-encoder pass over the shortlist. Scoring question+answer together
    # beats question alone: users often phrase a query using words that appear
    # in the answer body rather than the title.
    docs = [f"{c.question}\n{c.answer}" for c in fused]
    scores = rerank(query, docs)
    for c, s in zip(fused, scores):
        c.rerank_score = s

    ranked = sorted(fused, key=lambda c: c.rerank_score or -99, reverse=True)
    top = ranked[:n_top]
    confidence = _confidence(top[0].rerank_score) if top else 0.0

    return RetrievalResult(
        candidates=ranked,
        top=top,
        confidence=confidence,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
