"""
ASGI middleware for the Context Recall API.

Contains:

- :class:`BodySizeLimitMiddleware` — rejects requests whose declared
  ``Content-Length`` exceeds ``max_bytes`` (default 5 MB) so the daemon
  does not spend memory buffering oversized payloads.

- :class:`RateLimitMiddleware` — per-IP token-bucket limiter. Even
  though the API binds to ``127.0.0.1`` only, a runaway browser tab or
  buggy script can still flood the daemon. Token bucket keeps the
  steady-state ceiling at ``rate`` requests/second/IP and allows a
  short ``burst`` of traffic.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("contextrecall.api.middleware")

DEFAULT_MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared Content-Length exceeds ``max_bytes``.

    Returns HTTP 413 (Payload Too Large) when the limit is exceeded so the
    daemon does not spend memory buffering oversized payloads.
    """

    def __init__(self, app, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return JSONResponse(
                    {"detail": "Invalid Content-Length header"},
                    status_code=400,
                )
            if declared > self.max_bytes:
                logger.warning(
                    "Rejecting request with Content-Length=%d > limit=%d",
                    declared,
                    self.max_bytes,
                )
                return JSONResponse(
                    {"detail": "Request body too large"},
                    status_code=413,
                )
        return await call_next(request)


class _TokenBucket:
    """Simple thread-safe token bucket.

    Tokens regenerate at ``rate`` per second up to ``capacity``.
    ``try_consume`` returns whether one token was available.
    """

    __slots__ = ("rate", "capacity", "_tokens", "_last", "_lock")

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def try_consume(self, now: float | None = None) -> tuple[bool, float]:
        """Attempt to consume one token.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds``
        is ``0`` when the request was allowed; otherwise it is the wait
        time until the next token becomes available.
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True, 0.0
            needed = 1.0 - self._tokens
            retry = needed / self.rate if self.rate > 0 else 1.0
            return False, retry


class RateLimitMiddleware:
    """Per-IP token-bucket rate limiter.

    Returns a 429 response with ``Retry-After`` (seconds, rounded up) when
    a client exceeds ``rate`` requests/sec, optionally absorbing bursts up
    to ``burst`` tokens.
    """

    def __init__(
        self,
        app: ASGIApp,
        rate: float = 30.0,
        burst: float | None = None,
    ) -> None:
        self.app = app
        self.rate = float(rate)
        self.capacity = float(burst) if burst is not None else self.rate
        self._buckets: dict[str, _TokenBucket] = {}
        self._buckets_lock = threading.Lock()

    def _bucket_for(self, key: str) -> _TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is not None:
            return bucket
        with self._buckets_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _TokenBucket(self.rate, self.capacity)
                self._buckets[key] = bucket
            return bucket

    @staticmethod
    def _client_key(scope: Scope) -> str:
        client = scope.get("client")
        if client and isinstance(client, (tuple, list)) and client:
            return str(client[0])
        # Fallback so multiple anonymous clients still share one bucket
        # rather than bypassing the limit entirely.
        return "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only rate-limit HTTP requests. WebSocket upgrades have their
        # own auth handshake; lifespan and other events should pass
        # through untouched.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        key = self._client_key(scope)
        allowed, retry_after = self._bucket_for(key).try_consume()
        if allowed:
            await self.app(scope, receive, send)
            return

        # Round up so the client never retries earlier than the next
        # token actually becomes available.
        retry_seconds = max(1, math.ceil(retry_after))
        await _send_429(send, retry_seconds)


async def _send_429(send: Send, retry_after_seconds: int) -> None:
    """Send a minimal 429 Too Many Requests response."""
    body = b'{"detail":"Too Many Requests"}'
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"retry-after", str(retry_after_seconds).encode("ascii")),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


__all__ = ["BodySizeLimitMiddleware", "RateLimitMiddleware"]
