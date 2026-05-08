"""Classify a company as cyclical / defensive / neutral and score its fit
against the current macro phase.

This is intentionally simple: most of the value comes from the *flag*, not from
the magnitude. Use it as a tiebreaker — boost defensive names in
contraction/recession, penalize cyclicals in the same phase, and the inverse
when expansion is clearly underway.
"""
from typing import Optional

# Sector → cyclicality. Substring match (case-insensitive) against yfinance sector.
_CYCLICAL_TAGS = (
    "consumer cyclical", "consumer discretionary", "automotive", "auto",
    "materials", "basic materials", "energy", "oil", "industrial",
    "industrials", "semiconductor", "semiconductors", "transportation",
    "construction", "homebuilders", "luxury", "leisure",
)

_DEFENSIVE_TAGS = (
    "consumer defensive", "consumer staples", "utilities", "healthcare",
    "health care", "food", "beverage", "household", "telecom",
    "communication services", "pharma", "drug",
)

# Per-ticker overrides for the cases where the yfinance sector is ambiguous
# (e.g. NVDA listed under "Technology" — really cyclical because of GPU cycles;
# BRK-B listed under "Financial Services" — operates as a defensive holding).
_TICKER_OVERRIDES: dict[str, str] = {
    "NVDA":  "cyclical",
    "AMD":   "cyclical",
    "TSM":   "cyclical",
    "ASML":  "cyclical",
    "RACE":  "cyclical",
    "MC.PA": "cyclical",
    "AAPL":  "neutral",     # blue-chip with cyclical exposure but resilient
    "MSFT":  "neutral",
    "GOOGL": "neutral",
    "META":  "cyclical",    # ad spend is cyclical
    "AMZN":  "neutral",
    "BRK-B": "defensive",
    "KO":    "defensive",
    "JNJ":   "defensive",
    "NVO":   "defensive",
    "V":     "neutral",
    "SAP":   "neutral",
}


def classify(ticker: str, sector: Optional[str]) -> str:
    """Return 'cyclical' | 'defensive' | 'neutral'."""
    override = _TICKER_OVERRIDES.get((ticker or "").upper())
    if override:
        return override
    s = (sector or "").lower()
    if any(tag in s for tag in _DEFENSIVE_TAGS):
        return "defensive"
    if any(tag in s for tag in _CYCLICAL_TAGS):
        return "cyclical"
    return "neutral"


# Phase-fit scoring: positive => good moment to lean in, negative => unfavorable.
_FIT_TABLE: dict[tuple[str, str], int] = {
    ("cyclical",  "expansion"):    8,
    ("cyclical",  "mid_cycle"):    4,
    ("cyclical",  "late_cycle"):  -3,
    ("cyclical",  "contraction"): -10,
    ("cyclical",  "recession"):   -12,
    ("defensive", "expansion"):   -2,
    ("defensive", "mid_cycle"):    2,
    ("defensive", "late_cycle"):   6,
    ("defensive", "contraction"):  10,
    ("defensive", "recession"):    12,
    # neutral: 0 across the board (no adjustment).
}


def phase_fit_score(cyclicality: str, phase: str) -> int:
    """How well a name's profile matches the current cycle phase. Range ~[-12, 12]."""
    return _FIT_TABLE.get((cyclicality, phase), 0)


def evaluate(ticker: str, fund: dict, macro: dict) -> dict:
    """Return classification + phase fit + a flag list for the scoring layer."""
    sector = (fund or {}).get("sector")
    cyclicality = classify(ticker, sector)
    phase = (macro or {}).get("cycle_phase", "mid_cycle")
    fit = phase_fit_score(cyclicality, phase)

    flags: list[str] = []
    if cyclicality == "cyclical" and phase in ("contraction", "recession"):
        flags.append("CYCLICAL_VS_DOWN_PHASE")
    if cyclicality == "defensive" and phase in ("contraction", "recession"):
        flags.append("DEFENSIVE_TAILWIND")
    if cyclicality == "cyclical" and phase == "expansion":
        flags.append("CYCLICAL_TAILWIND")

    return {
        "available":   True,
        "cyclicality": cyclicality,
        "phase":       phase,
        "phase_fit":   fit,         # int, additive bonus/penalty for scoring
        "flags":       flags,
    }


def format_cycle_block(c: dict) -> str:
    """Compact one-line summary for the prompt and digests."""
    if not c.get("available"):
        return "(ciclo no disponible)"
    return (f"Tipo: {c.get('cyclicality')} | Fase macro: {c.get('phase')} | "
            f"Phase fit: {c.get('phase_fit'):+d}")
