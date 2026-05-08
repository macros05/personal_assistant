#!/usr/bin/env python3
"""Replay every historical alert and compute realised alpha vs S&P 500.

Outputs:
  - data/backtest_results.csv   one row per alert with outcome
  - stdout summary suitable for embedding in the weekly digest

Pure read of the predictions table — never mutates rows. Safe to re-run.
"""
import asyncio
import csv
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from modules import predictions  # noqa: E402

log = logging.getLogger("stock_backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_DB_PATH  = ROOT / "data" / "stock_alerts_log.db"
_OUT_CSV  = ROOT / "data" / "backtest_results.csv"


async def _all_predictions(window: int = 30) -> list[dict]:
    return_col = "return_30d_pct" if window == 30 else "return_90d_pct"
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute(f"""
            SELECT id, ticker, name, alert_date, alert_price, recommendation,
                   score, confidence, margin_of_safety, {return_col}
            FROM predictions
            WHERE {return_col} IS NOT NULL
            ORDER BY alert_date ASC
        """) as cur:
            rows = await cur.fetchall()

    return [
        {
            "id":            r[0],
            "ticker":        r[1],
            "name":          r[2] or "",
            "alert_date":    r[3],
            "alert_price":   r[4],
            "recommendation": r[5],
            "score":         r[6],
            "confidence":    r[7],
            "margin":        r[8],
            "return_pct":    r[9],
        }
        for r in rows
    ]


def _bench_return_for(date_iso: str, window: int) -> Optional[float]:
    try:
        alert_dt = datetime.fromisoformat(date_iso)
    except ValueError:
        return None
    return predictions._benchmark_return(alert_dt, alert_dt + timedelta(days=window))


async def _flag_for_alert(alert_id: int) -> str:
    """Best-effort lookup of the flag string for a given prediction.

    We match by (ticker, alert_date) since the analyses table doesn't store the
    prediction id. Multiple analyses can share a date, so we keep the first.
    """
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("""
            SELECT a.flags
            FROM analyses a
            JOIN predictions p ON p.ticker = a.ticker
            WHERE p.id = ?
            ORDER BY ABS(strftime('%s', a.created_at) - strftime('%s', p.alert_date)) ASC
            LIMIT 1
        """, (alert_id,))
        row = await cur.fetchone()
        await cur.close()
    return (row[0] if row else "") or ""


def _bucket_score(score: int) -> str:
    if score >= 85:
        return "85-100"
    if score >= 75:
        return "75-84"
    if score >= 65:
        return "65-74"
    return "<65"


async def run(*, window: int = 30) -> dict:
    rows = await _all_predictions(window=window)
    if not rows:
        log.info("No graded predictions available — nothing to backtest.")
        return {"n": 0}

    enriched: list[dict] = []
    log.info("Backtest: grading %d alerts (window=%dd)", len(rows), window)
    for r in rows:
        bench = await asyncio.to_thread(_bench_return_for, r["alert_date"], window)
        flags = await _flag_for_alert(r["id"])
        alpha = (r["return_pct"] - bench) if isinstance(bench, (int, float)) else None
        enriched.append({**r, "bench_return_pct": bench, "alpha_pct": alpha,
                         "flags": flags})

    _OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with _OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "id", "ticker", "name", "alert_date", "alert_price", "recommendation",
            "score", "confidence", "margin", "return_pct", "bench_return_pct",
            "alpha_pct", "flags",
        ])
        writer.writeheader()
        writer.writerows(enriched)
    log.info("Wrote %s", _OUT_CSV)

    # Aggregates
    by_rec: dict[str, list[dict]] = {}
    by_flag: dict[str, list[dict]] = {}
    by_bucket: dict[str, list[dict]] = {}
    for e in enriched:
        by_rec.setdefault((e.get("recommendation") or "").upper(), []).append(e)
        by_bucket.setdefault(_bucket_score(e.get("score") or 0), []).append(e)
        for f in (e.get("flags") or "").split(","):
            f = f.strip()
            if f:
                by_flag.setdefault(f, []).append(e)

    def _stats(group: list[dict]) -> dict:
        if not group:
            return {"n": 0}
        rets = [g["return_pct"] for g in group if isinstance(g.get("return_pct"), (int, float))]
        alphas = [g["alpha_pct"] for g in group if isinstance(g.get("alpha_pct"), (int, float))]
        wins = sum(1 for r in rets if r > 0)
        return {
            "n":           len(group),
            "avg_return":  sum(rets) / len(rets) if rets else None,
            "avg_alpha":   sum(alphas) / len(alphas) if alphas else None,
            "win_rate":    (wins / len(rets) * 100.0) if rets else None,
        }

    summary = {
        "window":       window,
        "total":        len(enriched),
        "by_recommendation": {k: _stats(v) for k, v in by_rec.items()},
        "by_score_band":     {k: _stats(v) for k, v in sorted(by_bucket.items())},
        "by_flag":           {k: _stats(v) for k, v in sorted(by_flag.items())},
    }

    if enriched:
        sorted_by_alpha = [e for e in enriched if isinstance(e.get("alpha_pct"), (int, float))]
        sorted_by_alpha.sort(key=lambda x: x["alpha_pct"])
        summary["worst_call"] = sorted_by_alpha[0] if sorted_by_alpha else None
        summary["best_call"]  = sorted_by_alpha[-1] if sorted_by_alpha else None

    return summary


def render_summary(summary: dict) -> str:
    if summary.get("total", 0) == 0:
        return "📊 Backtest: aún sin alertas graduadas (esperando ventana de 30d)."
    lines = [f"📊 Backtest histórico — ventana {summary['window']}d (n={summary['total']})"]
    rec = summary.get("by_recommendation", {}).get("COMPRAR")
    if rec:
        a = rec.get("avg_alpha")
        ar = rec.get("avg_return")
        wr = rec.get("win_rate")
        lines.append(
            f"  COMPRAR: n={rec['n']}, avg ret {ar:+.2f}% , alpha {a:+.2f}pp, win {wr:.0f}%"
            if isinstance(a, (int, float)) and isinstance(ar, (int, float)) and isinstance(wr, (int, float))
            else f"  COMPRAR: n={rec['n']} (datos parciales)"
        )
    if summary.get("best_call"):
        bc = summary["best_call"]
        lines.append(f"  Mejor: {bc['ticker']} {bc.get('alpha_pct'):+.1f}pp ({bc['alert_date'][:10]})")
    if summary.get("worst_call"):
        wc = summary["worst_call"]
        lines.append(f"  Peor: {wc['ticker']} {wc.get('alpha_pct'):+.1f}pp ({wc['alert_date'][:10]})")
    bb = summary.get("by_score_band", {})
    if bb:
        for band in ("85-100", "75-84", "65-74", "<65"):
            stats = bb.get(band)
            if not stats or stats.get("n", 0) == 0:
                continue
            wr = stats.get("win_rate")
            a = stats.get("avg_alpha")
            wr_str = f"{wr:.0f}%" if isinstance(wr, (int, float)) else "—"
            a_str = f"{a:+.1f}pp" if isinstance(a, (int, float)) else "—"
            lines.append(f"  Score {band}: n={stats['n']}, alpha {a_str}, win {wr_str}")
    return "\n".join(lines)


async def main() -> int:
    summary = await run(window=30)
    print(render_summary(summary))
    print("CSV:", _OUT_CSV)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
