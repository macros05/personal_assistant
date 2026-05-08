"""Track every alert/recommendation and grade it 30/90 days later for calibration."""
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

log = logging.getLogger("modules.predictions")

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stock_alerts_log.db"

# What counts as a "win" depends on the recommendation.
# COMPRAR  → expected positive move; >+5% within window = win.
# EVITAR   → expected flat-or-down; >-5% within window = win (we avoided a fall).
# ESPERAR  → no directional bet; not graded (kept for completeness, never counted).
_WIN_THRESHOLD_PCT = 5.0


async def init_predictions_table() -> None:
    """Create the predictions table if needed (idempotent)."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL,
                name            TEXT,
                alert_date      TIMESTAMP NOT NULL,
                alert_price     REAL,
                recommendation  TEXT NOT NULL,
                score           INTEGER,
                confidence      TEXT,
                margin_of_safety REAL,
                price_30d       REAL,
                price_30d_at    TIMESTAMP,
                return_30d_pct  REAL,
                hit_30d         INTEGER,
                price_90d       REAL,
                price_90d_at    TIMESTAMP,
                return_90d_pct  REAL,
                hit_90d         INTEGER,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_predictions_ticker_date "
            "ON predictions(ticker, alert_date)"
        )
        await db.commit()


async def record_prediction(analysis: dict) -> Optional[int]:
    """Append a prediction row when an alert is sent. Returns row id."""
    await init_predictions_table()
    if not analysis.get("ticker") or analysis.get("price") is None:
        return None
    async with aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO predictions
                (ticker, name, alert_date, alert_price, recommendation,
                 score, confidence, margin_of_safety)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis.get("ticker"),
                analysis.get("name", ""),
                datetime.utcnow().isoformat(),
                analysis.get("price"),
                analysis.get("recommendation", ""),
                analysis.get("score"),
                analysis.get("confidence", ""),
                analysis.get("margin_of_safety"),
            ),
        )
        await db.commit()
        return cur.lastrowid


def _evaluate_hit(recommendation: str, return_pct: float) -> int:
    """Did the prediction work out? 1 = yes, 0 = no."""
    rec = (recommendation or "").upper()
    if rec == "COMPRAR":
        return 1 if return_pct >= _WIN_THRESHOLD_PCT else 0
    if rec == "EVITAR":
        return 1 if return_pct <= -_WIN_THRESHOLD_PCT else 0
    return 0


def _fetch_close_on(ticker: str, target_date: datetime) -> Optional[float]:
    """Look up the closing price near `target_date`. Synchronous (yfinance)."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    start = (target_date - timedelta(days=5)).strftime("%Y-%m-%d")
    end   = (target_date + timedelta(days=5)).strftime("%Y-%m-%d")
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
    except Exception as e:
        log.warning("history near %s for %s failed: %s", target_date.date(), ticker, e)
        return None
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    closes = hist["Close"].dropna()
    if closes.empty:
        return None
    # Find the close on or after target_date; if none, fall back to the latest before.
    same_or_after = closes[closes.index.tz_localize(None) >= target_date.replace(tzinfo=None)] \
        if hasattr(closes.index, "tz_localize") else closes
    if not same_or_after.empty:
        return float(same_or_after.iloc[0])
    return float(closes.iloc[-1])


async def update_returns(*, max_per_run: int = 50) -> dict:
    """Backfill 30d/90d returns for predictions whose windows have elapsed."""
    await init_predictions_table()
    now = datetime.utcnow()
    cutoff_30d = (now - timedelta(days=30)).isoformat()
    cutoff_90d = (now - timedelta(days=90)).isoformat()

    updated_30 = 0
    updated_90 = 0

    async with aiosqlite.connect(_DB_PATH) as db:
        # 30d updates first
        async with db.execute(
            """
            SELECT id, ticker, alert_date, alert_price, recommendation
            FROM predictions
            WHERE price_30d IS NULL AND alert_date <= ?
            ORDER BY alert_date ASC
            LIMIT ?
            """,
            (cutoff_30d, max_per_run),
        ) as cur:
            rows_30 = await cur.fetchall()

        for row in rows_30:
            row_id, ticker, alert_date_s, alert_price, rec = row
            try:
                alert_dt = datetime.fromisoformat(alert_date_s)
            except ValueError:
                continue
            target = alert_dt + timedelta(days=30)
            price = await asyncio.to_thread(_fetch_close_on, ticker, target)
            if price is None or alert_price in (None, 0):
                continue
            ret_pct = (price / alert_price - 1.0) * 100.0
            hit = _evaluate_hit(rec, ret_pct)
            await db.execute(
                """
                UPDATE predictions
                SET price_30d = ?, price_30d_at = ?, return_30d_pct = ?, hit_30d = ?
                WHERE id = ?
                """,
                (price, target.isoformat(), ret_pct, hit, row_id),
            )
            updated_30 += 1

        async with db.execute(
            """
            SELECT id, ticker, alert_date, alert_price, recommendation
            FROM predictions
            WHERE price_90d IS NULL AND alert_date <= ?
            ORDER BY alert_date ASC
            LIMIT ?
            """,
            (cutoff_90d, max_per_run),
        ) as cur:
            rows_90 = await cur.fetchall()

        for row in rows_90:
            row_id, ticker, alert_date_s, alert_price, rec = row
            try:
                alert_dt = datetime.fromisoformat(alert_date_s)
            except ValueError:
                continue
            target = alert_dt + timedelta(days=90)
            price = await asyncio.to_thread(_fetch_close_on, ticker, target)
            if price is None or alert_price in (None, 0):
                continue
            ret_pct = (price / alert_price - 1.0) * 100.0
            hit = _evaluate_hit(rec, ret_pct)
            await db.execute(
                """
                UPDATE predictions
                SET price_90d = ?, price_90d_at = ?, return_90d_pct = ?, hit_90d = ?
                WHERE id = ?
                """,
                (price, target.isoformat(), ret_pct, hit, row_id),
            )
            updated_90 += 1

        await db.commit()

    log.info("Predictions updated: 30d=%d, 90d=%d", updated_30, updated_90)
    return {"updated_30d": updated_30, "updated_90d": updated_90}


_BENCHMARK_TICKER = "SPY"


def _benchmark_return(start_dt: datetime, end_dt: datetime) -> Optional[float]:
    """Return SPY's % return between two dates. Synchronous; call via to_thread."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        hist = yf.Ticker(_BENCHMARK_TICKER).history(
            start=(start_dt - timedelta(days=5)).strftime("%Y-%m-%d"),
            end=(end_dt + timedelta(days=5)).strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False,
        )
    except Exception as e:
        log.warning("benchmark history failed: %s", e)
        return None
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    closes = hist["Close"].dropna()
    if closes.empty:
        return None

    def _on_or_after(d: datetime) -> Optional[float]:
        try:
            after = closes[closes.index.tz_localize(None) >= d.replace(tzinfo=None)]
        except Exception:
            after = closes[closes.index >= d]
        return float(after.iloc[0]) if not after.empty else None

    p_start = _on_or_after(start_dt) or float(closes.iloc[0])
    p_end   = _on_or_after(end_dt) or float(closes.iloc[-1])
    if p_start <= 0:
        return None
    return (p_end / p_start - 1.0) * 100.0


async def alpha_vs_spy(*, recommendation: str = "COMPRAR",
                       lookback_days: int = 365,
                       window: int = 30) -> dict:
    """Compare avg return of `recommendation` calls vs SPY in the same windows.

    Skill-detection logic:
      - Sample mean alpha = mean(stock_return - benchmark_return) per call
      - One-sample t-test approximation (no scipy): t = mean / (std/sqrt(n))
        We treat |t| ≥ 2 as 'evidence of skill'.
      - alpha < 0 with n ≥ 30 → flag NEEDS_RECALIBRATION.
    """
    await init_predictions_table()
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

    return_col = "return_30d_pct" if window == 30 else "return_90d_pct"

    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute(
            f"""
            SELECT alert_date, ticker, alert_price, {return_col}
            FROM predictions
            WHERE recommendation = ? AND alert_date >= ? AND {return_col} IS NOT NULL
            """,
            (recommendation, cutoff),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        return {
            "n":             0,
            "avg_stock":     None,
            "avg_benchmark": None,
            "avg_alpha":     None,
            "alpha_std":     None,
            "t_stat":        None,
            "needs_recalibration": False,
            "skill_evidence": False,
            "window":        window,
        }

    alphas: list[float] = []
    stock_returns: list[float] = []
    bench_returns: list[float] = []
    for alert_date_s, ticker, alert_price, ret in rows:
        try:
            alert_dt = datetime.fromisoformat(alert_date_s)
        except (TypeError, ValueError):
            continue
        target_dt = alert_dt + timedelta(days=window)
        bench_ret = await asyncio.to_thread(_benchmark_return, alert_dt, target_dt)
        if bench_ret is None:
            continue
        stock_ret = float(ret)
        alpha = stock_ret - bench_ret
        alphas.append(alpha)
        stock_returns.append(stock_ret)
        bench_returns.append(bench_ret)

    if not alphas:
        return {
            "n":             0,
            "avg_stock":     None,
            "avg_benchmark": None,
            "avg_alpha":     None,
            "alpha_std":     None,
            "t_stat":        None,
            "needs_recalibration": False,
            "skill_evidence": False,
            "window":        window,
        }

    n = len(alphas)
    mean_alpha = sum(alphas) / n
    mean_stock = sum(stock_returns) / n
    mean_bench = sum(bench_returns) / n
    variance = sum((x - mean_alpha) ** 2 for x in alphas) / max(1, n - 1)
    std = variance ** 0.5
    t_stat = (mean_alpha / (std / (n ** 0.5))) if std > 0 and n > 1 else None

    needs_recal = (n >= 30 and recommendation == "COMPRAR" and mean_alpha < 0)
    skill = (t_stat is not None and abs(t_stat) >= 2.0)

    return {
        "n":              n,
        "window":         window,
        "avg_stock":      mean_stock,
        "avg_benchmark":  mean_bench,
        "avg_alpha":      mean_alpha,
        "alpha_std":      std,
        "t_stat":         t_stat,
        "skill_evidence": skill,
        "needs_recalibration": needs_recal,
    }


def format_alpha_line(alpha: dict) -> str:
    """One-line description of alpha vs benchmark."""
    n = alpha.get("n") or 0
    if n == 0:
        return "Alpha vs SPY: muestra insuficiente"
    a = alpha.get("avg_alpha")
    b = alpha.get("avg_benchmark")
    s = alpha.get("avg_stock")
    t = alpha.get("t_stat")
    parts = [
        f"Alpha {alpha.get('window', 30)}d: {a:+.2f}pp (n={n}, modelo {s:+.2f}% vs SPY {b:+.2f}%)"
    ]
    if isinstance(t, (int, float)):
        parts.append(f"t={t:+.2f}")
    if alpha.get("skill_evidence"):
        parts.append("✅ skill significativo")
    if alpha.get("needs_recalibration"):
        parts.append("⚠️ RECALIBRAR")
    return " | ".join(parts)


async def model_accuracy(*, recommendation: str = "COMPRAR",
                         lookback_days: int = 365) -> dict:
    """Precision of the model: % of `recommendation` calls whose return beat the threshold.

    Returns counts and percentages for both 30d and 90d windows over the last
    `lookback_days`. Predictions still inside their evaluation window are excluded.
    """
    await init_predictions_table()
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()

    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute(
            """
            SELECT
              SUM(CASE WHEN hit_30d IS NOT NULL THEN 1 ELSE 0 END) AS graded_30,
              SUM(CASE WHEN hit_30d = 1 THEN 1 ELSE 0 END) AS wins_30,
              SUM(CASE WHEN hit_90d IS NOT NULL THEN 1 ELSE 0 END) AS graded_90,
              SUM(CASE WHEN hit_90d = 1 THEN 1 ELSE 0 END) AS wins_90,
              AVG(return_30d_pct) AS avg_ret_30,
              AVG(return_90d_pct) AS avg_ret_90,
              COUNT(*) AS total
            FROM predictions
            WHERE recommendation = ? AND alert_date >= ?
            """,
            (recommendation, cutoff),
        ) as cur:
            row = await cur.fetchone()

    graded_30 = row[0] or 0
    wins_30   = row[1] or 0
    graded_90 = row[2] or 0
    wins_90   = row[3] or 0
    avg_30    = row[4]
    avg_90    = row[5]
    total     = row[6] or 0

    def pct(num, den):
        return (num / den * 100.0) if den else None

    return {
        "recommendation":     recommendation,
        "lookback_days":      lookback_days,
        "total_alerts":       total,
        "graded_30d":         graded_30,
        "wins_30d":           wins_30,
        "precision_30d_pct":  pct(wins_30, graded_30),
        "avg_return_30d_pct": avg_30,
        "graded_90d":         graded_90,
        "wins_90d":           wins_90,
        "precision_90d_pct":  pct(wins_90, graded_90),
        "avg_return_90d_pct": avg_90,
    }


def format_accuracy_line(acc: dict) -> str:
    """Compact one-liner used in alert footers and weekly digests."""
    def pct(v):
        return f"{v:.0f}%" if isinstance(v, (int, float)) else "—"

    p30, p90 = acc.get("precision_30d_pct"), acc.get("precision_90d_pct")
    g30, g90 = acc.get("graded_30d") or 0, acc.get("graded_90d") or 0
    if g30 == 0 and g90 == 0:
        return "Precisión histórica: aún sin muestra suficiente"
    return (f"Precisión histórica COMPRAR: {pct(p30)} a 30d (n={g30}) | "
            f"{pct(p90)} a 90d (n={g90})")
