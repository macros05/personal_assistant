"""Health-check probes and aggregator for the personal assistant.

Each probe issues a real call against the dependency it covers and returns a
small dict with at least `status` ("ok" | "degraded" | "error") and `latency_ms`.
The aggregator caches results for ``CACHE_TTL`` seconds so /health can be polled
cheaply (e.g. by a systemd timer) without hammering Gemini's quota.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite
import httpx

import tools.calendar as calendar_tool
from database import DB_PATH, bridge_get, oauth_get_state

log = logging.getLogger("health")

CACHE_TTL = 30          # seconds
GEMINI_TIMEOUT = 8      # seconds
TELEGRAM_TIMEOUT = 5
BRIDGE_STALE_SECONDS = 15 * 60

_cache: dict[str, Any] = {"ts": 0.0, "data": None}


# ── Individual probes ────────────────────────────────────────────────────────

async def check_database() -> dict:
    t0 = time.monotonic()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1") as cur:
                await cur.fetchone()
        return {"status": "ok", "latency_ms": int((time.monotonic() - t0) * 1000)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


async def check_calendar() -> dict:
    """Real probe: try refreshing the token and listing one event."""
    t0 = time.monotonic()
    if not calendar_tool.credentials_file_exists():
        return {"status": "error", "error": "credentials.json missing"}
    if not calendar_tool.is_authenticated():
        state = await oauth_get_state("google_calendar")
        return {
            "status": "error",
            "error": "refresh_token rejected or absent — visit /auth/google",
            "last_valid_at": (state or {}).get("last_valid_at"),
            "reauth_url": "/auth/google",
        }
    try:
        result = await asyncio.to_thread(calendar_tool._fetch_events, 1)
        if "error" in result:
            return {"status": "error", "error": str(result["error"])[:200]}
        return {
            "status": "ok",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "events_today": result.get("count", 0),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


async def check_gemini() -> dict:
    """Real probe: one-token generation against the configured Gemini model."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        return {"status": "error", "error": "GEMINI_API_KEY not set"}
    t0 = time.monotonic()
    try:
        # Use REST directly for the smallest possible health request — avoids
        # constructing the full SDK schema + tool decls just for a ping.
        async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT) as c:
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                    "generationConfig": {"maxOutputTokens": 1, "temperature": 0},
                },
            )
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code >= 400:
            return {"status": "error", "code": r.status_code, "error": r.text[:200], "latency_ms": latency}
        return {"status": "ok", "latency_ms": latency, "model": model}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


async def check_telegram() -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"status": "error", "error": "TELEGRAM_BOT_TOKEN not set"}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return {"status": "error", "code": r.status_code, "error": r.text[:200], "latency_ms": latency}
        data = r.json()
        return {
            "status": "ok",
            "latency_ms": latency,
            "username": data.get("result", {}).get("username"),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


async def check_bridge() -> dict:
    """Bridge = seobot's _forward_to_assistant + the /telegram/heartbeat ping.

    Status:
      - 'ok'        if either heartbeat or last forwarded message was within BRIDGE_STALE_SECONDS
      - 'degraded'  if both stale but at least one was ever observed
      - 'error'     if no heartbeat has ever been received
    """
    hb = await bridge_get("seobot_heartbeat_at")
    msg = await bridge_get("last_telegram_webhook_at")
    now = datetime.now(timezone.utc)

    def _seconds_since(rec: Optional[dict]) -> Optional[float]:
        if not rec or not rec.get("value"):
            return None
        try:
            then = datetime.fromisoformat(rec["value"])
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            return (now - then).total_seconds()
        except Exception:
            return None

    hb_age = _seconds_since(hb)
    msg_age = _seconds_since(msg)
    fresh = min((a for a in (hb_age, msg_age) if a is not None), default=None)

    if fresh is None:
        return {"status": "error", "error": "no heartbeat or webhook ever received"}
    if fresh <= BRIDGE_STALE_SECONDS:
        return {
            "status": "ok",
            "last_heartbeat_age_s": int(hb_age) if hb_age is not None else None,
            "last_message_age_s": int(msg_age) if msg_age is not None else None,
        }
    return {
        "status": "degraded",
        "last_heartbeat_age_s": int(hb_age) if hb_age is not None else None,
        "last_message_age_s": int(msg_age) if msg_age is not None else None,
    }


# ── Aggregator ───────────────────────────────────────────────────────────────

CRITICAL = {"calendar", "gemini", "telegram", "bridge", "database"}


async def get_health(force: bool = False) -> dict:
    now = time.monotonic()
    if not force and _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        cached = dict(_cache["data"])
        cached["cache"] = "hit"
        return cached

    cal, gem, tg, br, db = await asyncio.gather(
        check_calendar(), check_gemini(), check_telegram(), check_bridge(), check_database(),
        return_exceptions=True,
    )

    def _safe(x: Any) -> dict:
        if isinstance(x, Exception):
            return {"status": "error", "error": f"{type(x).__name__}: {str(x)[:150]}"}
        return x

    services = {
        "calendar": _safe(cal),
        "gemini":   _safe(gem),
        "telegram": _safe(tg),
        "bridge":   _safe(br),
        "database": _safe(db),
    }

    overall = "ok"
    for name, s in services.items():
        if s.get("status") == "error":
            overall = "error"
            break
        if s.get("status") == "degraded" and overall == "ok":
            overall = "degraded"

    data = {
        "status":    overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "services":  services,
        "cache":     "miss",
    }
    _cache["ts"] = now
    _cache["data"] = data
    return data
