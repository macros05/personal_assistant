"""Trading bot tools — HTTP calls to trading_api service."""
import logging
import os
from typing import Any

import httpx

from tools.base import Tool

log = logging.getLogger("tools.trading_bot")

TRADING_API_URL = os.getenv("TRADING_API_URL", "http://trading_api:8001")
TRADING_API_KEY = os.getenv("TRADING_API_KEY", "")

_HEADERS = {"X-Internal-Key": TRADING_API_KEY}
_TIMEOUT = 5.0


async def _get(path: str, **params) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{TRADING_API_URL}{path}", headers=_HEADERS, params=params)
            r.raise_for_status()
            return r.json()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        log.warning("trading_api GET %s failed: %s", path, exc)
        return {"error": "trading_api_unreachable", "details": str(exc)}


async def _post(path: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{TRADING_API_URL}{path}", headers=_HEADERS)
            r.raise_for_status()
            return r.json()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        log.warning("trading_api POST %s failed: %s", path, exc)
        return {"error": "trading_api_unreachable", "details": str(exc)}


class GetTradingStatusTool(Tool):
    name = "get_trading_status"
    description = (
        "Get current trading bot status, active position, balance and PnL. "
        "Use when user asks about the bot, trades or crypto."
    )

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> dict[str, Any]:
        status = await _get("/internal/status")
        trades = await _get("/internal/trades")
        logs   = await _get("/internal/logs", lines=20)
        return {
            **{k: v for k, v in status.items() if k != "error"},
            "balance_usdt":           trades.get("balance_usdt"),
            "total_pnl_usdt":         trades.get("total_pnl_usdt"),
            "total_trades":           trades.get("total_trades"),
            "win_rate_pct":           trades.get("win_rate_pct"),
            "today_trades":           trades.get("today_trades"),
            "today_pnl_usdt":         trades.get("today_pnl_usdt"),
            "circuit_breaker_active": logs.get("circuit_breaker_active"),
        }


class GetTradingLogsTool(Tool):
    name = "get_trading_logs"
    description = (
        "Get recent trading bot log lines. "
        "Use when user asks what the bot is doing or if there are errors."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "lines": {"type": "integer", "description": "Number of lines. Default 30"},
            },
        }

    async def execute(self, lines: int = 30, **kwargs) -> dict[str, Any]:
        data = await _get("/internal/logs", lines=lines)
        return {"lines": data.get("logs", []), "count": data.get("count", 0)}


class GetTradingPnlTool(Tool):
    name = "get_trading_pnl"
    description = (
        "Get trading bot PnL summary, win rate and full trade history. "
        "Use when user asks about profits, losses or performance."
    )

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> dict[str, Any]:
        trades = await _get("/internal/trades")
        logs   = await _get("/internal/logs", lines=20)
        return {**trades, "circuit_breaker_active": logs.get("circuit_breaker_active")}


class RestartTradingBotTool(Tool):
    name = "restart_trading_bot"
    description = (
        "Reset the trading bot circuit breaker when it trips. "
        "Use when user asks to restart the bot or when the circuit breaker is active."
    )

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> dict[str, Any]:
        return await _post("/internal/restart")
