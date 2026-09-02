"""Async Postgres pool with pgvector type registration."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

_pool: AsyncConnectionPool | None = None


async def _configure(conn) -> None:
    """Runs on every new connection: vector adapters + dict rows."""
    await register_vector_async(conn)
    conn.row_factory = dict_row


async def open_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            configure=_configure,
            open=False,
        )
        await _pool.open()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection() -> AsyncIterator[Any]:
    pool = await open_pool()
    async with pool.connection() as conn:
        yield conn


async def fetch_all(sql: str, params: tuple | dict = ()) -> list[dict]:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: tuple | dict = ()) -> dict | None:
    async with connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params: tuple | dict = ()) -> None:
    async with connection() as conn:
        await conn.execute(sql, params)
