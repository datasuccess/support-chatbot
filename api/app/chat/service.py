"""Chat orchestration: retrieve -> gate on confidence -> stream a grounded answer.

The pipeline, and why each step is there:

  1. rewrite   — collapse multi-turn context into a standalone query
  2. retrieve  — hybrid search + cross-encoder rerank
  3. gate      — below the confidence threshold, escalate instead of guessing
  4. generate  — DeepSeek streams an answer grounded in the retrieved entries
  5. trace     — persist every candidate, score, token count and latency

Step 3 is the one that keeps this system honest. A support bot that says "I don't
know, here is how to reach a human" is far more useful to a ministry than one that
produces a fluent, confident, wrong answer about a procurement deadline.
"""
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.db import connection
from app.chat import prompts
from app.retrieval.search import RetrievalResult, search

log = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=120.0,
            max_retries=2,
        )
    return _client


@dataclass
class AnswerContext:
    conversation_id: str
    user_message_id: int
    rewritten_query: str
    retrieval: RetrievalResult
    escalate: bool


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return (
        prompt_tokens / 1e6 * settings.price_in_per_1m
        + completion_tokens / 1e6 * settings.price_out_per_1m
    )


async def rewrite_query(history: list[dict], question: str) -> str:
    """Resolve pronouns against conversation history. Falls back to the raw
    question — a rewrite failure must never take the whole answer down."""
    if not history:
        return question
    rendered = "\n".join(
        f"{'İstifadəçi' if m['role'] == 'user' else 'Köməkçi'}: {m['content']}"
        for m in history[-4:]
    )
    try:
        resp = await client().chat.completions.create(
            model=settings.deepseek_model,
            messages=[{
                "role": "user",
                "content": prompts.QUERY_REWRITE_PROMPT.format(history=rendered, question=question),
            }],
            temperature=0.0,
            max_tokens=200,
        )
        rewritten = (resp.choices[0].message.content or "").strip()
        return rewritten or question
    except Exception as exc:  # noqa: BLE001
        log.warning("query rewrite failed, using raw question: %s", exc)
        return question


async def prepare(tenant_id: int, history: list[dict], question: str) -> AnswerContext:
    rewritten = await rewrite_query(history, question)
    result = await search(tenant_id, rewritten)
    escalate = result.confidence < settings.confidence_threshold or not result.top
    return AnswerContext(
        conversation_id="", user_message_id=0,
        rewritten_query=rewritten, retrieval=result, escalate=escalate,
    )


async def stream_answer(
    tenant: dict,
    ctx: AnswerContext,
    history: list[dict],
    question: str,
) -> AsyncIterator[tuple[str, str]]:
    """Yield (event, data) pairs for SSE.

    Events: `token` (a chunk of answer text), `done` (JSON with usage + sources).
    """
    if ctx.escalate:
        text = prompts.LOW_CONFIDENCE_REPLY
        # Emitted in chunks so the widget's streaming renderer behaves identically
        # for escalations and real answers.
        for i in range(0, len(text), 24):
            yield "token", text[i : i + 24]
        yield "done", json.dumps({
            "escalated": True, "confidence": round(ctx.retrieval.confidence, 4),
            "sources": [], "prompt_tokens": 0, "completion_tokens": 0,
        }, ensure_ascii=False)
        return

    context = prompts.build_context([
        {"question": c.question, "answer": c.answer, "citation": c.citation}
        for c in ctx.retrieval.top
    ])
    system = prompts.SYSTEM_PROMPT.format(
        tenant_name=tenant["name"], scope_desc=tenant["scope_desc"], context=context
    )
    messages = [{"role": "system", "content": system}]
    for m in history[-6:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": question})

    started = time.perf_counter()
    prompt_tokens = completion_tokens = 0
    try:
        stream = await client().chat.completions.create(
            model=settings.deepseek_model,
            messages=messages,
            temperature=0.3,          # low: this is procedural text, not prose
            max_tokens=700,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens
                completion_tokens = chunk.usage.completion_tokens
            if chunk.choices and chunk.choices[0].delta.content:
                yield "token", chunk.choices[0].delta.content
    except Exception as exc:  # noqa: BLE001
        log.exception("generation failed")
        yield "token", (
            "\n\n⚠️ Texniki səbəbdən cavabı tamamlaya bilmədim. "
            "Zəhmət olmasa yenidən cəhd edin və ya dəstəyə yazın."
        )
        yield "done", json.dumps({"escalated": True, "error": str(exc)[:200], "sources": []})
        return

    yield "done", json.dumps({
        "escalated": False,
        "confidence": round(ctx.retrieval.confidence, 4),
        "sources": [
            {"entry_id": c.entry_id, "title": c.question, "citation": c.citation}
            for c in ctx.retrieval.top
        ],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "llm_ms": int((time.perf_counter() - started) * 1000),
    }, ensure_ascii=False)


async def persist_trace(
    message_id: int,
    ctx: AnswerContext,
    meta: dict,
) -> None:
    """Write the audit trail for one assistant message."""
    async with connection() as conn:
        await conn.execute(
            """
            INSERT INTO message_traces (
                message_id, answer_mode, confidence, escalated, rewritten_query,
                embed_model, rerank_model, llm_model, prompt_tokens,
                completion_tokens, cost_usd, retrieval_ms, llm_ms, total_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                message_id,
                "escalated" if ctx.escalate else "grounded",
                ctx.retrieval.confidence,
                ctx.escalate,
                ctx.rewritten_query,
                settings.embedding_model,
                settings.reranker_model,
                settings.deepseek_model,
                meta.get("prompt_tokens", 0),
                meta.get("completion_tokens", 0),
                estimate_cost(meta.get("prompt_tokens", 0), meta.get("completion_tokens", 0)),
                ctx.retrieval.elapsed_ms,
                meta.get("llm_ms", 0),
                ctx.retrieval.elapsed_ms + meta.get("llm_ms", 0),
            ),
        )
        used_ids = {c.entry_id for c in ctx.retrieval.top}
        for rank, c in enumerate(ctx.retrieval.candidates, start=1):
            await conn.execute(
                """
                INSERT INTO message_retrievals (message_id, entry_id, entry_version, rank,
                    vector_score, keyword_score, rrf_score, rerank_score, used)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (message_id, c.entry_id, c.version, rank, c.vector_score,
                 c.keyword_score, c.rrf_score, c.rerank_score, c.entry_id in used_ids),
            )
