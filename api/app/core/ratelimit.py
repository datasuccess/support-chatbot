"""In-process sliding-window rate limiter for the public widget endpoints.

Deliberately in-memory: at this traffic level a Redis dependency would cost more
in operational surface than it buys. The trade-off is that limits are per-process,
so a multi-worker deployment multiplies the effective ceiling — revisit this when
the API runs more than one worker. See docs/ARCHITECTURE.md.
"""
import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.net import client_ip


class SlidingWindowLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> int | None:
        """Return seconds to wait if the caller is over the limit, else None.

        Returns rather than raises on purpose. This is called from HTTP
        middleware, which runs outside FastAPI's routing layer — an HTTPException
        raised there never reaches the exception handlers and surfaces as a bare
        500, so the client sees a server error instead of a 429 and never gets the
        Retry-After header.
        """
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return int(self.window - (now - bucket[0])) + 1
        bucket.append(now)
        return None

    def prune(self) -> None:
        """Drop empty buckets so idle sessions do not accumulate forever."""
        now = time.monotonic()
        for key in list(self._hits):
            bucket = self._hits[key]
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if not bucket:
                del self._hits[key]


# Sending a message is the expensive path (retrieval + reranking + an LLM call).
chat_limiter = SlidingWindowLimiter(max_requests=20, window_seconds=60)
# Starting a conversation is cheap, and a page reload mints one — so it gets its
# own budget rather than eating into the user's message allowance.
session_limiter = SlidingWindowLimiter(max_requests=30, window_seconds=60)
# Deliberately loose. Ministry staff share office NAT gateways, so a tight per-IP
# login limit locks out a whole floor after a handful of typos. The real
# brute-force control is the per-account lockout in core/security.py
# (MAX_FAILED_LOGINS); this only blunts naive scripted attacks from one address.
login_limiter = SlidingWindowLimiter(max_requests=30, window_seconds=300)


def client_key(request: Request) -> str:
    """Rate-limit bucket. Proxy-aware: see core/net.py for why this is not just
    `request.client.host`."""
    return client_ip(request)
