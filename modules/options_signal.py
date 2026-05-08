"""Options-derived signals: implied volatility vs realized, put/call ratio.

We don't pay for an options-data API. yfinance does expose option chains for
US tickers via `Ticker.option_chain(expiry)` — we use it to:

1. Estimate ATM implied vol (front-month, average of ATM call+put IV).
2. Compute total open-interest put/call ratio for the front-month chain.
3. Compare IV to historical (realized) volatility from the price series.

The output is informational + a flag list. Goal is to flag situations where
the market is pricing in a much bigger move than usual or skewing very
bearish/bullish.
"""
import logging
import math
from typing import Optional

log = logging.getLogger("modules.options_signal")


def _historical_vol(closes) -> Optional[float]:
    """Annualized realized volatility from a daily close series (last ~3 months)."""
    if closes is None or len(closes) < 30:
        return None
    pct = closes.pct_change().dropna().tail(60)
    if pct.empty or pct.std() == 0:
        return None
    return float(pct.std() * math.sqrt(252) * 100.0)  # %


def compute_options_signal(ticker: str) -> dict:
    """Pull front-month chain + recent prices, derive IV/HV and PCR.

    Returns:
      {
        "available":   bool,
        "iv_atm":      float (% annualized) or None,
        "hv":          float or None,
        "iv_hv_ratio": float or None,       # >1 means market expects a bigger move
        "put_call_oi": float or None,
        "put_call_vol": float or None,
        "expiry":      "YYYY-MM-DD" or None,
        "flags":       list,
        "summary":     str,
      }
    """
    out: dict = {"available": False, "flags": []}
    try:
        import yfinance as yf
    except ImportError:
        return out

    try:
        t = yf.Ticker(ticker)
        expiries = list(t.options or [])
    except Exception as e:
        log.debug("options chain unavailable for %s: %s", ticker, e)
        return out

    if not expiries:
        return out

    expiry = expiries[0]
    try:
        chain = t.option_chain(expiry)
    except Exception as e:
        log.debug("option_chain(%s) failed for %s: %s", expiry, ticker, e)
        return out

    calls = chain.calls
    puts  = chain.puts
    if calls is None or puts is None or calls.empty or puts.empty:
        return out

    try:
        hist = t.history(period="6mo", interval="1d", auto_adjust=False)
    except Exception:
        hist = None

    spot = None
    try:
        info = t.fast_info
        spot = float(info.last_price) if info and info.last_price else None
    except Exception:
        pass
    if spot is None and hist is not None and not hist.empty:
        spot = float(hist["Close"].dropna().iloc[-1])

    iv_atm = None
    if spot is not None:
        try:
            calls_sorted = calls.assign(diff=(calls["strike"] - spot).abs()).sort_values("diff")
            puts_sorted  = puts.assign(diff=(puts["strike"] - spot).abs()).sort_values("diff")
            atm_call_iv = float(calls_sorted.iloc[0].get("impliedVolatility") or 0)
            atm_put_iv  = float(puts_sorted.iloc[0].get("impliedVolatility") or 0)
            ivs = [v for v in (atm_call_iv, atm_put_iv) if v and v > 0]
            if ivs:
                iv_atm = sum(ivs) / len(ivs) * 100.0
        except Exception as e:
            log.debug("ATM IV calc failed for %s: %s", ticker, e)

    hv = _historical_vol(hist["Close"]) if hist is not None and not hist.empty else None
    iv_hv_ratio = None
    if iv_atm and hv and hv > 0:
        iv_hv_ratio = iv_atm / hv

    put_call_oi = None
    put_call_vol = None
    try:
        oi_calls = float(calls["openInterest"].fillna(0).sum())
        oi_puts  = float(puts["openInterest"].fillna(0).sum())
        if oi_calls > 0:
            put_call_oi = oi_puts / oi_calls
        v_calls = float(calls["volume"].fillna(0).sum())
        v_puts  = float(puts["volume"].fillna(0).sum())
        if v_calls > 0:
            put_call_vol = v_puts / v_calls
    except Exception as e:
        log.debug("PCR calc failed for %s: %s", ticker, e)

    flags: list[str] = []
    if iv_hv_ratio is not None and iv_hv_ratio >= 1.5:
        flags.append("IV_ELEVATED")
    if put_call_oi is not None and put_call_oi >= 2.0:
        flags.append("PUT_CALL_BEARISH")
    if put_call_oi is not None and put_call_oi <= 0.5:
        flags.append("PUT_CALL_BULLISH")

    summary_parts: list[str] = []
    if iv_atm is not None:
        summary_parts.append(f"IV ATM {iv_atm:.0f}%")
    if hv is not None:
        summary_parts.append(f"HV {hv:.0f}%")
    if iv_hv_ratio is not None:
        summary_parts.append(f"IV/HV {iv_hv_ratio:.2f}")
    if put_call_oi is not None:
        summary_parts.append(f"PCR(OI) {put_call_oi:.2f}")

    return {
        "available":     True,
        "expiry":        expiry,
        "iv_atm":        iv_atm,
        "hv":            hv,
        "iv_hv_ratio":   iv_hv_ratio,
        "put_call_oi":   put_call_oi,
        "put_call_vol":  put_call_vol,
        "flags":         flags,
        "summary":       " | ".join(summary_parts) or "(sin métricas)",
    }


def format_options_block(opt: dict) -> str:
    if not opt or not opt.get("available"):
        return "(opciones no disponibles)"
    return f"Opciones (exp {opt.get('expiry')}): {opt.get('summary', '')}"
