"""Price momentum vs fundamentals. SMA200, 52-week extremes, divergence flags."""
import logging
from typing import Optional

log = logging.getLogger("modules.momentum")


def compute_momentum(ticker: str) -> dict:
    """Pull 1y of daily prices and derive momentum signals.

    Returns a dict with: price, sma50, sma200, sma200_ratio, sma50_ratio,
    high_52w, low_52w, distance_from_high_pct, distance_from_low_pct, change_30d,
    change_90d, change_ytd, trend ('uptrend'|'downtrend'|'sideways'), and the
    flag list (MOMENTUM_DIVERGENCE / PRICED_FOR_PERFECTION when applicable).

    If the data is unavailable, returns {"available": False, "flags": []} so
    callers can fall back without exceptions.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed; momentum unavailable")
        return {"available": False, "flags": []}

    try:
        hist = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=False)
    except Exception as e:  # yfinance raises many things
        log.warning("history failed for %s: %s", ticker, e)
        return {"available": False, "flags": []}

    if hist is None or hist.empty or "Close" not in hist.columns:
        return {"available": False, "flags": []}

    closes = hist["Close"].dropna()
    if len(closes) < 50:
        return {"available": False, "flags": []}

    price    = float(closes.iloc[-1])
    sma50    = float(closes.tail(50).mean())
    sma200   = float(closes.tail(200).mean()) if len(closes) >= 200 else None
    high_52w = float(closes.max())
    low_52w  = float(closes.min())

    def pct_change(periods: int) -> Optional[float]:
        if len(closes) <= periods:
            return None
        old = float(closes.iloc[-periods - 1])
        if old <= 0:
            return None
        return (price / old - 1.0) * 100.0

    distance_from_high_pct = (price / high_52w - 1.0) * 100.0 if high_52w > 0 else None
    distance_from_low_pct  = (price / low_52w  - 1.0) * 100.0 if low_52w  > 0 else None

    sma50_ratio  = (price / sma50  - 1.0) * 100.0 if sma50  > 0 else None
    sma200_ratio = (price / sma200 - 1.0) * 100.0 if sma200 and sma200 > 0 else None

    trend = "sideways"
    if sma200_ratio is not None:
        if sma200_ratio > 5 and sma50_ratio and sma50_ratio > 0:
            trend = "uptrend"
        elif sma200_ratio < -5:
            trend = "downtrend"

    return {
        "available":               True,
        "price":                   price,
        "sma50":                   sma50,
        "sma200":                  sma200,
        "sma50_ratio":             sma50_ratio,
        "sma200_ratio":            sma200_ratio,
        "high_52w":                high_52w,
        "low_52w":                 low_52w,
        "distance_from_high_pct":  distance_from_high_pct,
        "distance_from_low_pct":   distance_from_low_pct,
        "change_30d":              pct_change(30),
        "change_90d":              pct_change(90),
        "trend":                   trend,
        "flags":                   [],  # filled in by classify_momentum_flags
    }


def classify_momentum_flags(momentum: dict, fundamentals_strong: bool) -> list[str]:
    """Decide MOMENTUM_DIVERGENCE / PRICED_FOR_PERFECTION based on context.

    - MOMENTUM_DIVERGENCE: fundamentals look good but trend is clearly down — the
      thesis may be right but the market disagrees; do not chase the falling knife.
    - PRICED_FOR_PERFECTION: price near 52w high and stretched above SMA200 — even
      decent fundamentals can disappoint at these levels.
    """
    if not momentum.get("available"):
        return []
    flags: list[str] = []
    sma200_ratio = momentum.get("sma200_ratio")
    distance_from_high = momentum.get("distance_from_high_pct")
    change_90d = momentum.get("change_90d")

    if (fundamentals_strong
            and sma200_ratio is not None and sma200_ratio < -8
            and change_90d is not None and change_90d < -10):
        flags.append("MOMENTUM_DIVERGENCE")

    if (distance_from_high is not None and distance_from_high > -3
            and sma200_ratio is not None and sma200_ratio > 20):
        flags.append("PRICED_FOR_PERFECTION")

    return flags


def format_momentum_block(momentum: dict) -> str:
    """One-line description for prompts."""
    if not momentum.get("available"):
        return "(datos de momentum no disponibles)"

    def pct(v):
        return f"{v:+.1f}%" if isinstance(v, (int, float)) else "N/A"

    return (
        f"Tendencia: {momentum.get('trend')} | "
        f"vs SMA200: {pct(momentum.get('sma200_ratio'))} | "
        f"vs SMA50: {pct(momentum.get('sma50_ratio'))} | "
        f"Distancia a máx 52s: {pct(momentum.get('distance_from_high_pct'))} | "
        f"Cambio 90d: {pct(momentum.get('change_90d'))}"
    )
