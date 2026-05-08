"""Retry helper around Gemini calls — 3 attempts with 1s, 5s, 30s backoff.

Distinguishes transient failures (network, 429, 5xx) from permanent ones (4xx
auth, schema errors). Permanent errors fail fast; transient errors retry.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

log = logging.getLogger("gemini_retry")

T = TypeVar("T")

BACKOFF_S = (1, 5, 30)


def _is_transient(exc: BaseException) -> bool:
    """Heuristic: treat network errors, timeouts, 429, and 5xx as transient."""
    msg = (str(exc) or type(exc).__name__).lower()
    if any(s in msg for s in ("timeout", "timed out", "connection", "reset", "broken pipe")):
        return True
    if any(s in msg for s in ("429", "503", "502", "500", "internal error", "unavailable", "deadline")):
        return True
    return False


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    label: str = "gemini",
) -> T:
    """Call ``fn`` up to ``len(BACKOFF_S)+1`` times with exponential backoff."""
    last_exc: BaseException | None = None
    for attempt in range(len(BACKOFF_S) + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt == len(BACKOFF_S) or not _is_transient(exc):
                log.warning("%s: giving up after %d attempt(s): %s", label, attempt + 1, exc)
                raise
            delay = BACKOFF_S[attempt]
            log.warning("%s: attempt %d failed (%s) — retrying in %ds", label, attempt + 1, exc, delay)
            await asyncio.sleep(delay)
    # unreachable, but mypy/static checkers like a fallthrough
    raise last_exc  # type: ignore[misc]
