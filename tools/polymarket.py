"""Polymarket bot tools — read-only sqlite access to the copytrade/arb paper DB."""
import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from tools.base import Tool

log = logging.getLogger("tools.polymarket")

POLYMARKET_DB_PATH = os.getenv(
    "POLYMARKET_DB_PATH", "/root/polymarket-copytrade/data/copytrade.db"
)
POLYMARKET_PAUSED_LOCK = os.getenv(
    "POLYMARKET_PAUSED_LOCK", "/root/polymarket-copytrade/data/paused.lock"
)

_TIMEOUT = 2.0
_QUESTION_MAX_LEN = 80


def _connect(db_path: str) -> sqlite3.Connection:
    """Open the copytrade DB strictly read-only (file URI mode=ro)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def _query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    """Run one read-only query and return rows as plain dicts (sync, use via to_thread)."""
    conn = _connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


async def _safe_query(db_path: str, sql: str, params: tuple = ()) -> list[dict] | dict:
    """Async wrapper: query off the event loop; on any DB failure return an error dict."""
    try:
        return await asyncio.to_thread(_query, db_path, sql, params)
    except (sqlite3.Error, OSError) as exc:
        log.warning("polymarket DB query failed: %s", exc)
        return {"error": "polymarket_data_unavailable", "details": str(exc)}


def _shorten(text: str | None, max_len: int = _QUESTION_MAX_LEN) -> str:
    if not text:
        return ""
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


class GetPolymarketStatusTool(Tool):
    name = "polymarket_status"
    description = (
        "Get Polymarket bot status: copytrade paper summary (bankroll, realized PnL, "
        "open positions, paused state) and NegRisk arb paper experiment summary "
        "(arb bankroll, arb sets, opportunities, last scan cycle). "
        "Use when user asks about the Polymarket bot, copytrade or the arb experiment."
    )

    def __init__(self, db_path: str = POLYMARKET_DB_PATH, paused_lock: str = POLYMARKET_PAUSED_LOCK):
        self._db_path = db_path
        self._paused_lock = paused_lock

    @property
    def schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> dict[str, Any]:
        meta = await _safe_query(self._db_path, "SELECT key, value FROM metadata")
        if isinstance(meta, dict):  # error dict
            return meta
        metadata = {r["key"]: r["value"] for r in meta}

        copytrade_rows = await _safe_query(
            self._db_path,
            "SELECT "
            "  SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_positions, "
            "  SUM(CASE WHEN status != 'OPEN' THEN COALESCE(pnl_usdc, 0) ELSE 0 END) AS realized_pnl_usdc, "
            "  COUNT(*) AS total_trades "
            "FROM paper_trades",
        )
        if isinstance(copytrade_rows, dict):
            return copytrade_rows
        ct = copytrade_rows[0] if copytrade_rows else {}

        arb_sets_rows = await _safe_query(
            self._db_path,
            "SELECT "
            "  SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_sets, "
            "  SUM(CASE WHEN status != 'OPEN' THEN 1 ELSE 0 END) AS resolved_sets, "
            "  SUM(CASE WHEN status != 'OPEN' THEN COALESCE(realized_pnl, 0) ELSE 0 END) AS realized_pnl "
            "FROM arb_sets",
        )
        if isinstance(arb_sets_rows, dict):
            return arb_sets_rows
        arb = arb_sets_rows[0] if arb_sets_rows else {}

        opps_rows = await _safe_query(
            self._db_path, "SELECT COUNT(*) AS n FROM arb_opportunities"
        )
        opportunities_count = opps_rows[0]["n"] if isinstance(opps_rows, list) and opps_rows else None

        scan_rows = await _safe_query(
            self._db_path,
            "SELECT ts, events_scanned, min_sum_asks, median_sum_asks, best_event_title "
            "FROM arb_scan_cycles ORDER BY id DESC LIMIT 1",
        )
        last_scan = scan_rows[0] if isinstance(scan_rows, list) and scan_rows else None

        paused_meta = metadata.get("paused", "0")
        return {
            "copytrade": {
                "paper_bankroll_usdc": float(metadata["paper_bankroll"]) if metadata.get("paper_bankroll") else None,
                "realized_pnl_usdc": ct.get("realized_pnl_usdc"),
                "open_positions": ct.get("open_positions") or 0,
                "total_trades": ct.get("total_trades") or 0,
                "paused": paused_meta in ("1", "true", "True"),
                "paused_lock_present": Path(self._paused_lock).exists(),
                "last_poll_ts": metadata.get("last_poll_ts"),
            },
            "arb": {
                "paper_bankroll_usdc": float(metadata["arb_paper_bankroll"]) if metadata.get("arb_paper_bankroll") else None,
                "paper_bankroll_initial_usdc": float(metadata["arb_paper_bankroll_initial"]) if metadata.get("arb_paper_bankroll_initial") else None,
                "open_sets": arb.get("open_sets") or 0,
                "resolved_sets": arb.get("resolved_sets") or 0,
                "realized_pnl_usdc": arb.get("realized_pnl"),
                "opportunities_count": opportunities_count,
                "last_scan_cycle": last_scan,
            },
        }


class GetPolymarketTradesTool(Tool):
    name = "polymarket_trades"
    description = (
        "Get recent Polymarket copytrade paper trades (market, outcome, side, "
        "entry/exit price, PnL, status). Use when user asks for Polymarket trade history."
    )

    def __init__(self, db_path: str = POLYMARKET_DB_PATH):
        self._db_path = db_path

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of trades. Default 10"},
            },
        }

    async def execute(self, limit: int = 10, **kwargs) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        rows = await _safe_query(
            self._db_path,
            "SELECT market_question, outcome, side, entry_price, exit_price, "
            "pnl_usdc, status, opened_at, closed_at "
            "FROM paper_trades ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        if isinstance(rows, dict):
            return rows
        trades = [
            {
                "market": _shorten(r["market_question"]),
                "outcome": r["outcome"],
                "side": r["side"],
                "entry_price": r["entry_price"],
                "exit_price": r["exit_price"],
                "pnl_usdc": r["pnl_usdc"],
                "status": r["status"],
                "opened_at": r["opened_at"],
                "closed_at": r["closed_at"],
            }
            for r in rows
        ]
        return {"trades": trades, "count": len(trades)}


class GetPolymarketTopWalletsTool(Tool):
    name = "polymarket_top_wallets"
    description = (
        "Get top tracked Polymarket wallets with score, PnL and win rate. "
        "Use when user asks which wallets the copytrade bot follows."
    )

    def __init__(self, db_path: str = POLYMARKET_DB_PATH):
        self._db_path = db_path

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of wallets. Default 10"},
            },
        }

    async def execute(self, limit: int = 10, **kwargs) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        rows = await _safe_query(
            self._db_path,
            "SELECT address, score, pnl_usdc, win_rate, trades_count, is_top, last_seen "
            "FROM wallets ORDER BY score DESC LIMIT ?",
            (limit,),
        )
        if isinstance(rows, dict):
            return rows
        return {"wallets": rows, "count": len(rows)}
