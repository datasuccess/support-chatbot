"""In-process sliding-window rate limiter for the public widget endpoints.

Deliberately in-memory: at this traffic level a Redis dependency would cost more
in operational surface than it buys. The trade-off is that limits are per-process,
so a multi-worker deployment multiplies the effective ceiling — revisit this when
the API runs more than one worker. See docs/ARCHITECTURE.md.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status


class SlidingWindowLimiter:
    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            retry = int(self.window - (now - bucket[0])) + 1
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down.",
                headers={"Retry-After": str(retry)},
            )
        bucket.append(now)

    def prune(self) -> None:
        """Drop empty buckets so idle sessions do not accumulate forever."""
        now = time.monotonic()
        for key in list(self._hits):
            bucket = self._hits[key]
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if not bucket:
                del self._hits[key]


chat_limiter = SlidingWindowLimiter(max_requests=20, window_seconds=60)
login_limiter = SlidingWindowLimiter(max_requests=5, window_seconds=300)


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"
