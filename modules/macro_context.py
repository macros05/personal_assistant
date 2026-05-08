"""Macro-economic context: rates, inflation, cycle phase. Refreshed weekly."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("modules.macro_context")

_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "macro_cache.json"
_CACHE_TTL_DAYS = 7
_FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# FRED series IDs we read when an API key is configured. Each is a single value,
# we just take the most recent observation.
_FRED_SERIES = {
    "fed_funds_rate":   "FEDFUNDS",        # Effective Federal Funds Rate (monthly)
    "ust_10y":          "DGS10",           # 10-Year Treasury Constant Maturity (daily)
    "us_cpi_yoy":       "CPIAUCSL",        # CPI All Urban — we compute YoY from last 13 months
    "ecb_main_rate":    "ECBDFR",          # ECB Deposit Facility Rate
    "ea_hicp_yoy":      "CP0000EZ19M086NEST",  # Euro Area HICP — fallback if missing
    "us_unemployment":  "UNRATE",          # used to infer cycle phase
    "ust_2y":           "DGS2",            # for yield curve inversion check
}


async def _fetch_fred_series(client: httpx.AsyncClient, series_id: str, api_key: str,
                             limit: int = 13) -> list[dict]:
    """Pull recent observations from FRED. Returns [] on any error."""
    params = {
        "series_id":     series_id,
        "api_key":       api_key,
        "file_type":     "json",
        "sort_order":    "desc",
        "limit":         limit,
    }
    try:
        r = await client.get(_FRED_URL, params=params, timeout=15.0)
        r.raise_for_status()
        return (r.json() or {}).get("observations") or []
    except (httpx.HTTPError, ValueError) as e:
        log.warning("FRED %s failed: %s", series_id, e)
        return []


def _latest_value(observations: list[dict]) -> Optional[float]:
    for obs in observations:
        v = obs.get("value")
        if v in (None, "", "."):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _cpi_yoy(observations: list[dict]) -> Optional[float]:
    """Compute YoY % change from a series of monthly CPI observations (desc)."""
    cleaned: list[tuple[str, float]] = []
    for o in observations:
        v = o.get("value")
        d = o.get("date")
        if v in (None, "", ".") or not d:
            continue
        try:
            cleaned.append((d, float(v)))
        except (TypeError, ValueError):
            continue
    if len(cleaned) < 13:
        return None
    latest = cleaned[0][1]
    year_ago = cleaned[12][1]
    if year_ago == 0:
        return None
    return (latest / year_ago - 1.0) * 100.0


def _yfinance_proxy() -> dict:
    """Last-resort proxy when FRED unavailable: yfinance ^IRX, ^TNX, ^FVX."""
    try:
        import yfinance as yf
    except ImportError:
        return {}

    out: dict = {}
    for key, symbol in (("ust_3m", "^IRX"), ("ust_10y", "^TNX"), ("ust_2y", "^FVX")):
        try:
            hist = yf.Ticker(symbol).history(period="5d", interval="1d")
            if not hist.empty:
                out[key] = float(hist["Close"].iloc[-1])
        except Exception as e:  # yfinance throws many things
            log.warning("yfinance proxy %s failed: %s", symbol, e)
    # ^IRX is the 13-week T-bill yield → near-Fed-funds proxy
    if "ust_3m" in out:
        out.setdefault("fed_funds_rate", out["ust_3m"])
    return out


def _classify_cycle(unemployment: Optional[float], curve_inverted: Optional[bool],
                    inflation: Optional[float]) -> str:
    """Rough cycle phase. Conservative — only declares recession on clear signals."""
    if unemployment is not None and unemployment >= 5.5:
        return "recession" if (curve_inverted or False) else "contraction"
    if curve_inverted:
        return "late_cycle"
    if unemployment is not None and unemployment < 4.0:
        if inflation is not None and inflation > 3.5:
            return "late_cycle"
        return "expansion"
    return "mid_cycle"


def _equity_risk_premium_for_phase(phase: str) -> float:
    """Equity risk premium by cycle phase (decimals). Higher during stress."""
    return {
        "expansion":    0.045,
        "mid_cycle":    0.050,
        "late_cycle":   0.055,
        "contraction":  0.065,
        "recession":    0.075,
    }.get(phase, 0.050)


def discount_rate_for(macro: dict, *, beta: float = 1.0, growth_company: bool = False) -> float:
    """Compute a sensible DCF discount rate (decimal) given macro context.

    Floor of 7%, ceiling of 14%. Growth companies get a 1pp premium because their
    cashflows are further out; cyclically late-stage gets a small extra cushion.
    """
    rf = macro.get("ust_10y") or macro.get("fed_funds_rate") or 4.5
    rf_dec = float(rf) / 100.0
    erp = _equity_risk_premium_for_phase(macro.get("cycle_phase", "mid_cycle"))
    rate = rf_dec + beta * erp
    if growth_company:
        rate += 0.01
    return max(0.07, min(0.14, rate))


def valuation_bias(macro: dict) -> str:
    """Return 'aggressive' | 'neutral' | 'conservative' based on rates regime."""
    rate = macro.get("fed_funds_rate")
    if rate is None:
        return "neutral"
    if rate >= 4.0:
        return "conservative"
    if rate <= 2.0:
        return "aggressive"
    return "neutral"


async def fetch_macro_context(*, force_refresh: bool = False,
                              client: Optional[httpx.AsyncClient] = None) -> dict:
    """Return cached macro snapshot, refreshing if older than _CACHE_TTL_DAYS.

    Keys: fed_funds_rate, ecb_main_rate, ust_10y, ust_2y, us_cpi_yoy, ea_hicp_yoy,
    us_unemployment, cycle_phase, valuation_bias, fetched_at, source.
    """
    if not force_refresh and _CACHE_PATH.exists():
        try:
            cached = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(cached.get("fetched_at", "").replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - fetched < timedelta(days=_CACHE_TTL_DAYS):
                return cached
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    snapshot: dict = {}
    fred_key = os.getenv("FRED_API_KEY")
    try:
        if fred_key:
            tasks = {
                key: _fetch_fred_series(client, sid, fred_key,
                                        limit=13 if "cpi" in key or "hicp" in key else 5)
                for key, sid in _FRED_SERIES.items()
            }
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            data = dict(zip(tasks.keys(), results))
            for k, obs in data.items():
                if isinstance(obs, Exception) or not obs:
                    continue
                if k in ("us_cpi_yoy", "ea_hicp_yoy"):
                    snapshot[k] = _cpi_yoy(obs)
                else:
                    snapshot[k] = _latest_value(obs)
            snapshot["source"] = "fred"
        else:
            log.info("FRED_API_KEY not set — using yfinance proxy for rates")
            snapshot.update(await asyncio.to_thread(_yfinance_proxy))
            snapshot["source"] = "yfinance_proxy"
    finally:
        if own_client:
            await client.aclose()

    curve_inverted = None
    if snapshot.get("ust_2y") is not None and snapshot.get("ust_10y") is not None:
        curve_inverted = snapshot["ust_2y"] > snapshot["ust_10y"]
    snapshot["yield_curve_inverted"] = curve_inverted

    snapshot["cycle_phase"] = _classify_cycle(
        snapshot.get("us_unemployment"),
        curve_inverted,
        snapshot.get("us_cpi_yoy"),
    )
    snapshot["valuation_bias"] = valuation_bias(snapshot)
    snapshot["fetched_at"] = datetime.now(timezone.utc).isoformat()

    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write macro cache: %s", e)

    return snapshot


def format_macro_summary(macro: dict) -> str:
    """Human-readable one-paragraph summary for prompts and Telegram digests."""
    def pct(v):
        return f"{v:.2f}%" if isinstance(v, (int, float)) else "N/A"

    return (
        f"Fed: {pct(macro.get('fed_funds_rate'))} | UST10y: {pct(macro.get('ust_10y'))} | "
        f"BCE: {pct(macro.get('ecb_main_rate'))} | "
        f"CPI EE.UU.: {pct(macro.get('us_cpi_yoy'))} | "
        f"Paro EE.UU.: {pct(macro.get('us_unemployment'))} | "
        f"Curva invertida: {macro.get('yield_curve_inverted')} | "
        f"Fase ciclo: {macro.get('cycle_phase')} | "
        f"Sesgo valoración: {macro.get('valuation_bias')}"
    )
