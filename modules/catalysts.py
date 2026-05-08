"""Detect real, business-relevant catalysts for a ticker.

Sources (cheap, no extra API keys):
- yfinance: next earnings date, calendar
- Headlines: scan for explicit M&A / spin-off / CEO-change / activist patterns
- Macro calendar (manual): third Friday of every month is options expiry

Output is a list of catalysts each tagged with `kind`, `eta_days`, `confidence`,
and `evidence`. The scoring layer can use these to lift the score modestly when
a near-term, business-related catalyst is real.
"""
from __future__ import annotations
import calendar as _cal
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("modules.catalysts")


# ── Headline patterns ────────────────────────────────────────────────────────

_PATTERNS: dict[str, list[re.Pattern]] = {
    "M&A": [
        re.compile(r"\b(to acquire|acquires|acquisition of|merger with|to merge|takeover bid|in talks to buy|approached.*to buy)\b", re.I),
        re.compile(r"\b(buyout|leveraged buyout|lbo|hostile bid)\b", re.I),
    ],
    "SPIN_OFF": [
        re.compile(r"\b(spin[- ]?off|spinoff|to separate|tax[- ]?free distribution|carve[- ]?out)\b", re.I),
    ],
    "CEO_CHANGE": [
        re.compile(r"\b(new ceo|appoints.*ceo|names.*ceo|ceo to step down|ceo resigns|ceo (departs|exits)|ceo replaced)\b", re.I),
    ],
    "ACTIVIST": [
        re.compile(r"\b(activist|elliott management|carl icahn|nelson peltz|trian|starboard|engine no\.? 1)\b", re.I),
    ],
    "BUYBACK_RAISE": [
        re.compile(r"\b(announces|expands|raises|increases) .{0,30}(buyback|share repurchase|repurchase program)\b", re.I),
    ],
    "DIVIDEND_CHANGE": [
        re.compile(r"\b(raises|increases|hikes|cuts|suspends) .{0,20}dividend\b", re.I),
    ],
    "REGULATORY": [
        re.compile(r"\b(antitrust|doj|ftc|sec investigation|class action|lawsuit filed|fda (approval|rejection))\b", re.I),
    ],
}


def scan_headlines(headlines: list[dict]) -> list[dict]:
    """Tag headlines that match a catalyst pattern. Returns a list of catalysts."""
    catalysts: list[dict] = []
    if not headlines:
        return catalysts
    seen_kinds: set[str] = set()
    for h in headlines:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        for kind, patterns in _PATTERNS.items():
            if any(p.search(title) for p in patterns):
                # Dedup by kind so we don't list the same catalyst 5 times.
                if kind in seen_kinds:
                    continue
                seen_kinds.add(kind)
                catalysts.append({
                    "kind":       kind,
                    "evidence":   title,
                    "source":     h.get("source", ""),
                    "published":  (h.get("published_at") or "")[:19],
                    "confidence": "MEDIUM",
                })
                break
    return catalysts


# ── Earnings calendar (yfinance) ─────────────────────────────────────────────

def _next_earnings_date(ticker: str) -> Optional[date]:
    """Best-effort next earnings date via yfinance .calendar / .earnings_dates."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        t = yf.Ticker(ticker)
    except Exception:
        return None

    candidates: list[date] = []

    try:
        cal = t.calendar
        if cal is not None:
            # yfinance returns either a DataFrame or a dict depending on version
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date") or cal.get("Earnings Date ")
                if isinstance(ed, list):
                    for v in ed:
                        if isinstance(v, (datetime, date)):
                            candidates.append(v.date() if hasattr(v, "date") else v)
                elif isinstance(ed, (datetime, date)):
                    candidates.append(ed.date() if hasattr(ed, "date") else ed)
            else:
                # DataFrame fallback
                try:
                    if "Earnings Date" in cal.index:
                        ed = cal.loc["Earnings Date"]
                        for v in ed.values.flatten():
                            try:
                                d = v.to_pydatetime().date() if hasattr(v, "to_pydatetime") else v
                                if isinstance(d, (datetime, date)):
                                    candidates.append(d.date() if hasattr(d, "date") else d)
                            except Exception:
                                continue
                except Exception:
                    pass
    except Exception as e:
        log.debug("yfinance .calendar failed for %s: %s", ticker, e)

    try:
        ed = t.earnings_dates
        if ed is not None and not ed.empty:
            today = datetime.now(timezone.utc).date()
            for ts in ed.index:
                try:
                    d = ts.to_pydatetime().date() if hasattr(ts, "to_pydatetime") else ts
                    if isinstance(d, datetime):
                        d = d.date()
                    if d >= today:
                        candidates.append(d)
                except Exception:
                    continue
    except Exception as e:
        log.debug("yfinance .earnings_dates failed for %s: %s", ticker, e)

    today = datetime.now(timezone.utc).date()
    future = sorted({c for c in candidates if c >= today})
    return future[0] if future else None


# ── Options expiry (third Friday) ────────────────────────────────────────────

def _third_friday(year: int, month: int) -> date:
    """Return the third-Friday OpEx for a given month."""
    cal = _cal.Calendar()
    fridays = [d for d in cal.itermonthdates(year, month)
               if d.month == month and d.weekday() == 4]
    return fridays[2]


def days_to_next_opex(today: Optional[date] = None) -> tuple[date, int]:
    """Return (next_opex_date, days_until) — useful as a context catalyst."""
    today = today or datetime.now(timezone.utc).date()
    candidate = _third_friday(today.year, today.month)
    if candidate < today:
        nm = today.replace(day=1) + timedelta(days=32)
        candidate = _third_friday(nm.year, nm.month)
    return candidate, (candidate - today).days


# ── Public API ───────────────────────────────────────────────────────────────

def detect_catalysts(ticker: str, headlines: list[dict],
                     *, today: Optional[date] = None) -> dict:
    """Run all detectors and return the consolidated catalyst dict.

    Output:
      {
        "available": bool,
        "next_earnings_date": "YYYY-MM-DD" or None,
        "days_to_earnings":   int or None,
        "next_opex_date":     "YYYY-MM-DD",
        "days_to_opex":       int,
        "headline_catalysts": [ {kind, evidence, ...}, ... ],
        "flags":              [ "CATALYST_NEAR" | "EARNINGS_IMMINENT" | ... ],
        "summary":            "1-line",
      }
    """
    today = today or datetime.now(timezone.utc).date()
    next_e = _next_earnings_date(ticker)
    days_to_e = (next_e - today).days if next_e else None

    opex_date, days_to_opex = days_to_next_opex(today)
    headline_cats = scan_headlines(headlines or [])

    flags: list[str] = []
    if days_to_e is not None and 0 <= days_to_e <= 7:
        flags.append("EARNINGS_IMMINENT")
    elif days_to_e is not None and 0 <= days_to_e <= 21:
        flags.append("EARNINGS_NEAR")
    if any(c["kind"] in ("M&A", "SPIN_OFF", "ACTIVIST") for c in headline_cats):
        flags.append("HARD_CATALYST")
    if days_to_opex <= 5 and headline_cats:
        flags.append("OPEX_PROXIMITY")

    summary_parts = []
    if next_e:
        summary_parts.append(f"earnings {next_e.isoformat()} (+{days_to_e}d)")
    if headline_cats:
        kinds = ",".join(sorted({c["kind"] for c in headline_cats}))
        summary_parts.append(f"señales: {kinds}")
    summary_parts.append(f"OpEx en {days_to_opex}d")

    return {
        "available":          True,
        "next_earnings_date": next_e.isoformat() if next_e else None,
        "days_to_earnings":   days_to_e,
        "next_opex_date":     opex_date.isoformat(),
        "days_to_opex":       days_to_opex,
        "headline_catalysts": headline_cats,
        "flags":              flags,
        "summary":            " | ".join(summary_parts) or "(sin catalizadores claros)",
    }


def format_catalysts_block(c: dict) -> str:
    """Compact block for the LLM prompt."""
    if not c.get("available"):
        return "(catalizadores no disponibles)"
    return f"Catalizadores: {c.get('summary', '—')}"
