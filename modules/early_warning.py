"""Early-warning signals: insider selling, guidance cuts, short interest."""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("modules.early_warning")

# Headline patterns indicating downward guidance revisions. Conservative — only
# fires on explicit cut/lower wording so we don't flag analyst chatter.
_GUIDANCE_CUT_PATTERNS = (
    re.compile(r"\b(cuts|lowers|trims|reduces|slashes)\s+(its\s+)?(full[- ]?year\s+)?(annual\s+)?(revenue\s+|sales\s+|profit\s+|earnings\s+|eps\s+)?(guidance|outlook|forecast|guide)\b", re.I),
    re.compile(r"\bguidance\s+(cut|lowered|reduced|slashed)\b", re.I),
    re.compile(r"\b(warns|warning)\s+on\s+(profits?|earnings|sales|revenue)\b", re.I),
    re.compile(r"\b(profit|earnings)\s+warning\b", re.I),
    re.compile(r"\b(weaker[- ]than[- ]expected|disappointing)\s+(guidance|outlook|guide)\b", re.I),
)


def _scan_guidance_cuts(headlines: list[dict]) -> int:
    """How many of the recent headlines look like guidance cuts."""
    if not headlines:
        return 0
    count = 0
    for h in headlines:
        title = (h.get("title") or "")
        if any(p.search(title) for p in _GUIDANCE_CUT_PATTERNS):
            count += 1
    return count


def compute_early_warnings(ticker: str, headlines: Optional[list[dict]] = None) -> dict:
    """Insider selling (90d), short interest, recent guidance-cut signals.

    Returns dict with metrics + `flags` list. Always returns a usable shape.
    """
    out: dict = {
        "available":               False,
        "insider_sell_pct_float":  None,
        "insider_net_shares":      None,
        "insider_window_days":     90,
        "short_pct_float":         None,
        "guidance_cut_count":      _scan_guidance_cuts(headlines or []),
        "flags":                   [],
    }

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed; early warnings unavailable")
        return out

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
    except Exception as e:
        log.warning("yfinance info failed for %s: %s", ticker, e)
        info = {}

    short_pct = info.get("shortPercentOfFloat")
    if isinstance(short_pct, (int, float)):
        out["short_pct_float"] = short_pct * 100.0  # yfinance returns 0-1

    floats_shares = info.get("floatShares") or info.get("sharesOutstanding")
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    insider_net = 0.0
    insider_sold = 0.0
    try:
        df = t.insider_transactions
    except Exception as e:
        log.debug("insider_transactions failed for %s: %s", ticker, e)
        df = None

    if df is not None and not df.empty:
        # Column shapes vary; guard accesses. Most commonly: Insider, Position,
        # Transaction, Start Date, Shares, Value.
        for _, row in df.iterrows():
            try:
                d = row.get("Start Date") if hasattr(row, "get") else row["Start Date"]
                if hasattr(d, "to_pydatetime"):
                    d = d.to_pydatetime()
                if isinstance(d, datetime):
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=timezone.utc)
                    if d < cutoff:
                        continue
            except (KeyError, AttributeError, TypeError, ValueError):
                pass

            try:
                shares_raw = row.get("Shares") if hasattr(row, "get") else row["Shares"]
                shares = float(shares_raw)
            except (KeyError, TypeError, ValueError):
                continue

            try:
                txn = (row.get("Transaction") if hasattr(row, "get") else row["Transaction"]) or ""
            except KeyError:
                txn = ""
            txn_l = str(txn).lower()
            if "sale" in txn_l or "sell" in txn_l or "disposition" in txn_l:
                insider_sold += abs(shares)
                insider_net -= abs(shares)
            elif "purchase" in txn_l or "buy" in txn_l or "acquisition" in txn_l:
                insider_net += abs(shares)

    out["insider_net_shares"] = insider_net
    if isinstance(floats_shares, (int, float)) and floats_shares > 0 and insider_sold > 0:
        out["insider_sell_pct_float"] = (insider_sold / floats_shares) * 100.0

    flags: list[str] = []
    if (out["insider_sell_pct_float"] is not None
            and out["insider_sell_pct_float"] >= 2.0):
        flags.append("INSIDER_SELLING")
    if out["guidance_cut_count"] >= 1:
        flags.append("GUIDANCE_CUT")
    if (out["short_pct_float"] is not None
            and out["short_pct_float"] >= 15.0):
        flags.append("HIGH_SHORT_INTEREST")

    out["available"] = True
    out["flags"] = flags
    return out


def format_early_warning_block(ew: dict) -> str:
    """One-line description for prompts."""
    if not ew.get("available"):
        return "(early-warning data no disponible)"

    def pct(v):
        return f"{v:.2f}%" if isinstance(v, (int, float)) else "N/A"

    return (
        f"Insider selling 90d: {pct(ew.get('insider_sell_pct_float'))} | "
        f"Short interest: {pct(ew.get('short_pct_float'))} | "
        f"Cortes guidance recientes: {ew.get('guidance_cut_count', 0)}"
    )
