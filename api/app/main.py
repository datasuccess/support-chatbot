"""FastAPI application entrypoint."""
import asyncio
import logging
from contextlib import asynccontextmanager

import pathlib

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.admin.router import router as admin_router
from app.chat.router import router as chat_router
from app.core.config import settings
from app.core.db import close_pool, fetch_one, open_pool
from app.core.ratelimit import chat_limiter, client_key, login_limiter
from app.retrieval.models import warmup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("support-chatbot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await open_pool()
    # Load the embedding + reranker models once at startup. Doing it lazily on the
    # first request would hand that user a 30-second wait.
    log.info("warming up local models (cpu)...")
    await asyncio.to_thread(warmup)
    log.info("ready")
    yield
    await close_pool()


app = FastAPI(
    title="Support Chatbot API",
    description="Support assistant for the Ministry of Finance government-contracts system",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    if path == "/api/chat/stream":
        chat_limiter.check(client_key(request))
    elif path == "/api/admin/login":
        login_limiter.check(client_key(request))
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Never leak internals to the widget; the detail goes to the log instead."""
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health() -> dict:
    try:
        await fetch_one("SELECT 1 AS ok")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "env": settings.app_env}


WIDGET_DIR = pathlib.Path(__file__).resolve().parents[2] / "widget"
app.mount("/static", StaticFiles(directory=WIDGET_DIR), name="static")


@app.get("/widget", include_in_schema=False)
async def widget_page() -> FileResponse:
    """Serve the chat UI. In production this is fronted by nginx; serving it from
    the API keeps local development to a single process."""
    return FileResponse(
        pathlib.Path(__file__).resolve().parents[2] / "widget" / "index.html",
        media_type="text/html",
    )


@app.get("/demo", include_in_schema=False)
async def demo_page() -> FileResponse:
    """A host page embedding the widget, to exercise the real iframe path."""
    return FileResponse(
        pathlib.Path(__file__).resolve().parents[2] / "widget" / "demo.html",
        media_type="text/html",
    )


app.include_router(chat_router)
app.include_router(admin_router)
