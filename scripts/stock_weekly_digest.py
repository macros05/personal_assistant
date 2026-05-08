#!/usr/bin/env python3
"""Monday 08:00 weekly Telegram digest.

Sends: top 3 watchlist by latest score, current macro snapshot, model accuracy
last week, and any new flag changes on companies that already received alerts.
"""
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from modules import (  # noqa: E402
    stock_alerts, stock_analyzer, macro_context, predictions, watchlist,
)
from scripts import stock_backtest  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "data" / "stock_weekly_digest.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("stock_weekly_digest")

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stock_alerts_log.db"


async def _latest_per_ticker() -> list[dict]:
    """Pull the most recent analysis per ticker from the analyses table."""
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute("""
            SELECT a.ticker, a.name, a.score, a.recommendation, a.confidence,
                   a.margin_of_safety, a.flags, a.price, a.created_at
            FROM analyses a
            JOIN (
              SELECT ticker, MAX(created_at) AS max_ts
              FROM analyses
              GROUP BY ticker
            ) m ON a.ticker = m.ticker AND a.created_at = m.max_ts
            ORDER BY a.score DESC NULLS LAST
        """) as cur:
            rows = await cur.fetchall()
    return [
        {
            "ticker": r[0], "name": r[1], "score": r[2] or 0,
            "recommendation": r[3] or "", "confidence": r[4] or "",
            "margin": r[5], "flags": r[6] or "", "price": r[7], "ts": r[8],
        }
        for r in rows
    ]


async def _flag_changes_last_week() -> list[str]:
    """For tickers alerted in the past 30d, list any flag added in the past 7d."""
    cutoff_recent = (datetime.utcnow() - timedelta(days=7)).isoformat()
    cutoff_window = (datetime.utcnow() - timedelta(days=30)).isoformat()

    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute("""
            SELECT DISTINCT ticker FROM alerts_sent WHERE sent_at >= ?
        """, (cutoff_window,)) as cur:
            recent_tickers = [r[0] for r in await cur.fetchall()]

        changes: list[str] = []
        for tk in recent_tickers:
            async with db.execute("""
                SELECT flags, created_at FROM analyses
                WHERE ticker = ?
                ORDER BY created_at DESC
                LIMIT 5
            """, (tk,)) as cur:
                rows = await cur.fetchall()
            if not rows:
                continue
            current = set((rows[0][0] or "").split(",")) - {""}
            recent_flags = current
            older_flags: set[str] = set()
            for r in rows[1:]:
                if r[1] < cutoff_recent:
                    older_flags = set((r[0] or "").split(",")) - {""}
                    break
            new_flags = recent_flags - older_flags
            if new_flags:
                changes.append(f"{tk}: nuevos flags {', '.join(sorted(new_flags))}")
    return changes


def _format_digest(top3: list[dict], macro: dict, accuracy: dict,
                   flag_changes: list[str], *,
                   alpha: dict | None = None,
                   backtest_summary: dict | None = None,
                   watchlist_changes: dict | None = None) -> str:
    lines = ["🗓️ RESUMEN SEMANAL — ASISTENTE DE BOLSA", ""]
    lines.append("📈 Top 3 watchlist (por puntuación más reciente):")
    if not top3:
        lines.append("  (sin análisis recientes)")
    for i, e in enumerate(top3[:3], 1):
        margin = f"{e['margin']:+.1f}%" if isinstance(e.get("margin"), (int, float)) else "N/A"
        flags = e.get("flags") or ""
        flag_part = f" | flags: {flags}" if flags else ""
        lines.append(
            f"  {i}. {e['name']} ({e['ticker']}) — {e['score']}/100, "
            f"{e['recommendation']}, margen {margin}, conf {e['confidence']}{flag_part}"
        )
    lines.append("")

    lines.append("🌍 Macro:")
    lines.append("  " + macro_context.format_macro_summary(macro))
    lines.append("")

    lines.append("🎯 Precisión del modelo (COMPRAR, último año):")
    lines.append("  " + predictions.format_accuracy_line(accuracy))
    if isinstance(accuracy.get("avg_return_30d_pct"), (int, float)):
        lines.append(f"  Retorno medio 30d: {accuracy['avg_return_30d_pct']:+.1f}%")
    if isinstance(accuracy.get("avg_return_90d_pct"), (int, float)):
        lines.append(f"  Retorno medio 90d: {accuracy['avg_return_90d_pct']:+.1f}%")
    if alpha is not None:
        lines.append("  " + predictions.format_alpha_line(alpha))
    lines.append("")

    if backtest_summary is not None and backtest_summary.get("total", 0) > 0:
        lines.append(stock_backtest.render_summary(backtest_summary))
        lines.append("")

    if watchlist_changes is not None:
        lines.append(watchlist.format_evolve_summary(watchlist_changes))
        lines.append("")

    if flag_changes:
        lines.append("⚑ Cambios de flags en empresas ya alertadas (últimos 7d):")
        for c in flag_changes:
            lines.append(f"  - {c}")
    else:
        lines.append("⚑ Sin nuevos flags relevantes esta semana")

    lines.append("")
    lines.append("⚠️ EXPERIMENTO TÉCNICO — NO ES ASESORAMIENTO FINANCIERO.")
    return "\n".join(lines)


async def main() -> int:
    await stock_alerts.init_db()
    await predictions.init_predictions_table()

    macro = await macro_context.fetch_macro_context()
    top3 = (await _latest_per_ticker())[:3]
    accuracy = await predictions.model_accuracy()
    flag_changes = await _flag_changes_last_week()
    try:
        alpha = await predictions.alpha_vs_spy(window=30)
    except Exception as e:
        log.warning("alpha_vs_spy failed: %s", e)
        alpha = None
    try:
        backtest_summary = await stock_backtest.run(window=30)
    except Exception as e:
        log.warning("backtest failed: %s", e)
        backtest_summary = None
    try:
        watchlist_changes = await watchlist.auto_evolve()
    except Exception as e:
        log.warning("watchlist auto_evolve failed: %s", e)
        watchlist_changes = None

    digest = _format_digest(
        top3, macro, accuracy, flag_changes,
        alpha=alpha, backtest_summary=backtest_summary,
        watchlist_changes=watchlist_changes,
    )
    log.info("Digest:\n%s", digest)
    sent = await stock_alerts.send_telegram(digest)
    log.info("Weekly digest sent=%s", sent)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
