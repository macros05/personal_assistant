"""Business-quality metrics: FCF yield, margin trend, ROIC vs WACC, debt/EBITDA."""
import logging
from typing import Optional

log = logging.getLogger("modules.quality")

# Sectors where high leverage is normal and 'debt > 3x EBITDA' is not a red flag.
# Match yfinance Sector strings (case-insensitive substring).
_LEVERAGE_TOLERANT_SECTORS = (
    "utilities", "communication services", "telecom", "real estate",
    "financial services", "banks", "insurance",
)


def _is_leverage_tolerant(sector: Optional[str]) -> bool:
    if not sector:
        return False
    s = sector.lower()
    return any(tag in s for tag in _LEVERAGE_TOLERANT_SECTORS)


def compute_quality(ticker: str, fundamentals: dict, *, macro: Optional[dict] = None) -> dict:
    """Pull deeper quality signals via yfinance and combine with passed fundamentals.

    Returns a dict with computed metrics + a `flags` list. Always returns; the
    `available` key indicates whether yfinance returned usable data.
    """
    out: dict = {"available": False, "flags": []}
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed; quality unavailable")
        return out

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
    except Exception as e:
        log.warning("yfinance info failed for %s: %s", ticker, e)
        info = {}

    market_cap   = fundamentals.get("market_cap") or info.get("marketCap")
    fcf          = info.get("freeCashflow") or info.get("operatingCashflow")
    ebitda       = info.get("ebitda")
    total_debt   = info.get("totalDebt")
    total_cash   = info.get("totalCash") or info.get("cash") or 0
    enterprise_v = info.get("enterpriseValue")
    op_margin    = info.get("operatingMargins") or fundamentals.get("operating_margins")
    profit_margin = info.get("profitMargins") or fundamentals.get("profit_margins")
    roe          = info.get("returnOnEquity") or fundamentals.get("roe")
    roa          = info.get("returnOnAssets")
    beta         = info.get("beta") or 1.0
    sector       = info.get("sector") or fundamentals.get("sector")
    pretax_income = info.get("pretaxIncome")
    income       = info.get("netIncomeToCommon") or info.get("netIncome")

    fcf_yield = None
    if isinstance(fcf, (int, float)) and isinstance(market_cap, (int, float)) and market_cap > 0:
        fcf_yield = (fcf / market_cap) * 100.0

    debt_to_ebitda = None
    if isinstance(total_debt, (int, float)) and isinstance(ebitda, (int, float)) and ebitda > 0:
        net_debt = total_debt - (total_cash if isinstance(total_cash, (int, float)) else 0)
        debt_to_ebitda = max(0.0, net_debt) / ebitda

    # ROIC ≈ NOPAT / Invested Capital, where Invested Capital = Total Debt + Book Equity.
    # We pull Book Equity from t.balance_sheet (stockholders' equity), since yfinance's
    # info dict doesn't expose it reliably and using market_cap as a stand-in massively
    # overstates the denominator for high-multiple stocks (NVDA → fake 2% ROIC).
    book_equity: Optional[float] = None
    try:
        bs = t.balance_sheet
        if bs is not None and not bs.empty:
            for key in ("Stockholders Equity", "Total Stockholder Equity",
                        "Common Stock Equity"):
                if key in bs.index:
                    val = bs.loc[key].iloc[0]
                    if val is not None:
                        book_equity = float(val)
                        break
    except Exception as e:
        log.debug("balance_sheet lookup failed for %s: %s", ticker, e)

    roic = None
    invested_capital = None
    if (isinstance(total_debt, (int, float))
            and isinstance(book_equity, (int, float)) and book_equity > 0
            and isinstance(income, (int, float))):
        invested_capital = max(1.0, total_debt + book_equity)
        tax_rate = 0.21
        if isinstance(pretax_income, (int, float)) and pretax_income > 0:
            tax_rate = max(0.0, min(0.5, 1.0 - (income / pretax_income)))
        ebit = ebitda  # rough; we don't reliably get D&A separately
        if isinstance(ebit, (int, float)):
            nopat = ebit * (1.0 - tax_rate)
            roic = (nopat / invested_capital) * 100.0
    elif isinstance(roe, (int, float)) and isinstance(total_debt, (int, float)):
        # Fallback: when book equity is missing but we have ROE, use it iff leverage
        # is low (debt/equity < 0.5) — under that assumption ROIC ≈ ROE within noise.
        de = fundamentals.get("debt_to_equity")
        if isinstance(de, (int, float)) and de < 50.0:  # yfinance D/E in % (e.g. 30 = 30%)
            roic = float(roe) * 100.0 if roe < 5 else float(roe)

    # WACC from macro (risk-free) + ERP (by cycle phase) + debt cost. We don't try
    # to be precise — the goal is "is the company creating value" (ROIC vs WACC).
    wacc = None
    if macro:
        rf = macro.get("ust_10y") or macro.get("fed_funds_rate") or 4.5
        rf_dec = float(rf) / 100.0
        from modules.macro_context import _equity_risk_premium_for_phase  # local import to avoid cycle
        erp = _equity_risk_premium_for_phase(macro.get("cycle_phase", "mid_cycle"))
        ke = rf_dec + float(beta) * erp
        kd = rf_dec + 0.015  # 150bp spread proxy for IG corporate
        if isinstance(market_cap, (int, float)) and isinstance(total_debt, (int, float)) \
                and (market_cap + total_debt) > 0:
            we = market_cap / (market_cap + total_debt)
            wd = total_debt / (market_cap + total_debt)
            wacc = (we * ke + wd * kd * 0.79) * 100.0  # 0.79 = (1 - 21% tax)
        else:
            wacc = ke * 100.0

    # Margin trend: prefer t.quarterly_financials if available; fall back to None.
    margin_trend = None
    margins_quarterly: list[float] = []
    try:
        qf = t.quarterly_financials
        if qf is not None and not qf.empty:
            # Operating margin per quarter = OperatingIncome / TotalRevenue
            if "Operating Income" in qf.index and "Total Revenue" in qf.index:
                opi = qf.loc["Operating Income"]
                rev = qf.loc["Total Revenue"]
                # Most recent first in yfinance quarterly_financials
                for col in qf.columns[:4]:
                    try:
                        o = float(opi[col]); r = float(rev[col])
                        if r > 0:
                            margins_quarterly.append((o / r) * 100.0)
                    except (TypeError, ValueError, KeyError):
                        continue
            if len(margins_quarterly) >= 2:
                # margins_quarterly is most-recent first; reverse to oldest-first for trend
                ordered = list(reversed(margins_quarterly))
                if ordered[-1] > ordered[0] + 0.5:
                    margin_trend = "improving"
                elif ordered[-1] < ordered[0] - 0.5:
                    margin_trend = "deteriorating"
                else:
                    margin_trend = "stable"
    except Exception as e:
        log.debug("quarterly margin trend unavailable for %s: %s", ticker, e)

    flags: list[str] = []

    if (debt_to_ebitda is not None and debt_to_ebitda > 3.0
            and not _is_leverage_tolerant(sector)):
        flags.append("HIGH_LEVERAGE")

    if (roic is not None and wacc is not None and roic < wacc):
        flags.append("ROIC_BELOW_WACC")

    if margin_trend == "deteriorating":
        flags.append("MARGIN_DETERIORATION")

    if fcf_yield is not None and fcf_yield < 0:
        flags.append("NEGATIVE_FCF")

    out.update({
        "available":        True,
        "fcf_yield":        fcf_yield,
        "ebitda":           ebitda,
        "total_debt":       total_debt,
        "total_cash":       total_cash,
        "debt_to_ebitda":   debt_to_ebitda,
        "roic":             roic,
        "wacc":             wacc,
        "operating_margin": op_margin,
        "profit_margin":    profit_margin,
        "roe":              roe,
        "roa":              roa,
        "beta":             beta,
        "sector":           sector,
        "leverage_tolerant_sector": _is_leverage_tolerant(sector),
        "margin_trend":     margin_trend,
        "margins_quarterly": margins_quarterly,
        "enterprise_value": enterprise_v,
        "flags":            flags,
    })
    return out


def format_quality_block(q: dict) -> str:
    """One-line description for prompts."""
    if not q.get("available"):
        return "(datos de calidad no disponibles)"

    def pct(v):
        return f"{v:.2f}%" if isinstance(v, (int, float)) else "N/A"

    def num(v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else "N/A"

    return (
        f"FCF yield: {pct(q.get('fcf_yield'))} | "
        f"Deuda neta/EBITDA: {num(q.get('debt_to_ebitda'))} | "
        f"ROIC: {pct(q.get('roic'))} | WACC: {pct(q.get('wacc'))} | "
        f"Tendencia márgenes: {q.get('margin_trend') or 'N/A'} | "
        f"Sector tolerante deuda: {q.get('leverage_tolerant_sector')}"
    )
