"""Retrieval evaluation harness.

Reports hit@1 / hit@3 / hit@5 plus mean reciprocal rank, and compares the hybrid
pipeline against vector-only and keyword-only baselines. The comparison is the
point: it shows whether the reranker and the hybrid fusion are actually paying for
the complexity they add, rather than assuming they are.

    python -u evals/run_eval.py
    python -u evals/run_eval.py --json out.json    # machine-readable, for CI

Exit code is non-zero when hit@3 falls below --min-hit3, which makes this usable
as a CI gate on knowledge-base changes.
"""
import argparse
import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.core.config import settings  # noqa: E402
from app.core.db import close_pool, connection, fetch_one  # noqa: E402
from app.retrieval.search import _fuse, _keyword_search, _vector_search, search  # noqa: E402
from evals.golden_set import GOLDEN, OUT_OF_SCOPE  # noqa: E402


def matches(entry_text: str, keywords: list[str]) -> bool:
    """A hit if any expected keyword appears (case-insensitively) in the entry."""
    low = entry_text.lower()
    return any(k.lower() in low for k in keywords)


def summarise(ranks: list[int | None], label: str) -> dict:
    n = len(ranks)
    hit1 = sum(1 for r in ranks if r == 1)
    hit3 = sum(1 for r in ranks if r and r <= 3)
    hit5 = sum(1 for r in ranks if r and r <= 5)
    mrr = sum(1 / r for r in ranks if r) / n if n else 0.0
    return {
        "pipeline": label, "cases": n,
        "hit@1": round(100 * hit1 / n, 1),
        "hit@3": round(100 * hit3 / n, 1),
        "hit@5": round(100 * hit5 / n, 1),
        "mrr": round(mrr, 3),
        "misses": sum(1 for r in ranks if r is None),
    }


async def eval_hybrid(tenant_id: int) -> tuple[list[int | None], list[dict], float]:
    ranks: list[int | None] = []
    details: list[dict] = []
    total_ms = 0.0
    for case in GOLDEN:
        t0 = time.perf_counter()
        result = await search(tenant_id, case["q"], candidates=20, top_k=5)
        total_ms += (time.perf_counter() - t0) * 1000
        rank = None
        for i, c in enumerate(result.candidates[:5], start=1):
            if matches(f"{c.question} {c.answer}", case["keywords"]):
                rank = i
                break
        ranks.append(rank)
        details.append({
            "question": case["q"],
            "rank": rank,
            "confidence": round(result.confidence, 3),
            "top": result.candidates[0].question if result.candidates else None,
        })
    return ranks, details, total_ms / max(len(GOLDEN), 1)


async def eval_baseline(tenant_id: int, mode: str) -> list[int | None]:
    """Vector-only or keyword-only, with no reranking — the comparison baselines."""
    ranks: list[int | None] = []
    async with connection() as conn:
        for case in GOLDEN:
            if mode == "vector":
                rows = await _vector_search(conn, tenant_id, case["q"], 5)
            elif mode == "keyword":
                rows = await _keyword_search(conn, tenant_id, case["q"], 5)
            else:  # fused, but not reranked
                v = await _vector_search(conn, tenant_id, case["q"], 20)
                k = await _keyword_search(conn, tenant_id, case["q"], 20)
                pool = _fuse(v, k)
                fused = sorted(pool.values(), key=lambda c: c.rrf_score, reverse=True)[:5]
                rows = [{"question": c.question, "answer": c.answer} for c in fused]
            rank = None
            for i, r in enumerate(rows[:5], start=1):
                if matches(f"{r['question']} {r['answer']}", case["keywords"]):
                    rank = i
                    break
            ranks.append(rank)
    return ranks


async def eval_out_of_scope(tenant_id: int) -> dict:
    """Off-topic questions should land below the confidence threshold."""
    correctly_rejected = 0
    rows = []
    for q in OUT_OF_SCOPE:
        result = await search(tenant_id, q)
        rejected = result.confidence < settings.confidence_threshold
        correctly_rejected += rejected
        rows.append({"question": q, "confidence": round(result.confidence, 3),
                     "rejected": rejected})
    return {
        "cases": len(OUT_OF_SCOPE),
        "correctly_rejected": correctly_rejected,
        "rate": round(100 * correctly_rejected / max(len(OUT_OF_SCOPE), 1), 1),
        "detail": rows,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hit3", type=float, default=90.0)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--tenant", default=None)
    args = ap.parse_args()

    tenant = await fetch_one(
        "SELECT id, name FROM tenants WHERE slug = %s",
        (args.tenant or settings.default_tenant,),
    )
    if not tenant:
        sys.exit("tenant not found — run scripts/seed_db.py first")

    print(f"tenant: {tenant['name']}")
    print(f"cases : {len(GOLDEN)} golden + {len(OUT_OF_SCOPE)} out-of-scope\n")

    print("running baselines...")
    vec_ranks = await eval_baseline(tenant["id"], "vector")
    kw_ranks = await eval_baseline(tenant["id"], "keyword")
    rrf_ranks = await eval_baseline(tenant["id"], "rrf")
    print("running hybrid + reranker...")
    hyb_ranks, details, avg_ms = await eval_hybrid(tenant["id"])

    rows = [
        summarise(kw_ranks, "keyword only"),
        summarise(vec_ranks, "vector only"),
        summarise(rrf_ranks, "hybrid RRF (no rerank)"),
        summarise(hyb_ranks, "hybrid + reranker"),
    ]

    print(f"\n{'pipeline':26} {'hit@1':>7} {'hit@3':>7} {'hit@5':>7} {'MRR':>7} {'miss':>6}")
    print("-" * 64)
    for r in rows:
        print(f"{r['pipeline']:26} {r['hit@1']:6.1f}% {r['hit@3']:6.1f}% "
              f"{r['hit@5']:6.1f}% {r['mrr']:7.3f} {r['misses']:6}")

    oos = await eval_out_of_scope(tenant["id"])
    print(f"\nout-of-scope rejection: {oos['correctly_rejected']}/{oos['cases']} "
          f"({oos['rate']}%)  threshold={settings.confidence_threshold}")
    print(f"avg retrieval latency : {avg_ms:.0f} ms")

    misses = [d for d in details if d["rank"] is None or d["rank"] > 3]
    if misses:
        print(f"\n{len(misses)} case(s) outside top-3:")
        for m in misses:
            print(f"  rank={m['rank']}  conf={m['confidence']}  q: {m['question']}")
            print(f"      got: {m['top']}")

    final = rows[-1]
    payload = {"pipelines": rows, "out_of_scope": oos,
               "avg_latency_ms": round(avg_ms), "details": details}
    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"\nwrote {args.json_out}")

    await close_pool()

    if final["hit@3"] < args.min_hit3:
        sys.exit(f"\nFAIL: hit@3 {final['hit@3']}% < required {args.min_hit3}%")
    print(f"\nPASS: hit@3 {final['hit@3']}% >= {args.min_hit3}%")


if __name__ == "__main__":
    asyncio.run(main())
