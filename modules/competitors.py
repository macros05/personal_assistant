"""Auto-discover peer competitors and compare key valuation/quality metrics.

Strategy:
1. Map ticker → curated peer list (~3-5 competitors). Curated, not heuristic, so
   that comparing AAPL against NVDA doesn't happen unless we explicitly say so.
2. Pull cheap fundamentals (P/E, P/B, profit margin, revenue growth, ROE, EV/EBITDA)
   for each peer via yfinance .info — single round-trip per peer.
3. Compute the company's percentile within the peer group on each metric, then
   roll that up into RELATIVE_VALUE_OPPORTUNITY (cheaper + better) or
   PREMIUM_VS_PEERS (richer with no clear quality edge).

Designed to be safe: missing data falls through silently, peer fetch errors do
not break the analysis, and the flag list is empty if we don't have at least 2
peers with comparable data.
"""
import logging
import statistics
from typing import Optional

log = logging.getLogger("modules.competitors")

# Curated peer map. Same companies that show up in _PEER_HINTS in stock_analyzer
# but parsed into actual ticker lists so we can fetch metrics. Keep the list
# tight (<=5) — more peers introduce noise without adding signal.
_PEER_MAP: dict[str, list[str]] = {
    "AAPL":  ["MSFT", "GOOGL", "META", "AMZN"],
    "MSFT":  ["AAPL", "GOOGL", "ORCL", "CRM"],
    "GOOGL": ["META", "MSFT", "AAPL", "AMZN"],
    "AMZN":  ["WMT", "COST", "MSFT", "GOOGL"],
    "META":  ["GOOGL", "SNAP", "PINS", "AAPL"],
    "NVDA":  ["AMD", "AVGO", "TSM", "INTC"],
    "BRK-B": ["JPM", "BAC", "PGR", "TRV"],
    "KO":    ["PEP", "KDP", "MNST"],
    "JNJ":   ["PFE", "MRK", "ABBV", "BMY"],
    "V":     ["MA", "AXP", "PYPL"],
    "ASML":  ["AMAT", "LRCX", "KLAC", "TSM"],
    "SAP":   ["ORCL", "MSFT", "CRM"],
    "NVO":   ["LLY", "PFE", "MRK"],
    "RACE":  ["BMWYY", "POAHY"],
    "MC.PA": ["RMS.PA", "KER.PA", "CFR.SW"],
    # Dynamic-watchlist newcomers can add more entries here at runtime via
    # add_peers_for_ticker(); see modules.watchlist.
}


def add_peers_for_ticker(ticker: str, peers: list[str]) -> None:
    """Allow the dynamic watchlist to register peers for newly-tracked tickers."""
    if not ticker or not peers:
        return
    _PEER_MAP[ticker.upper()] = [p.upper() for p in peers if p]


def get_peers(ticker: str) -> list[str]:
    return _PEER_MAP.get((ticker or "").upper(), [])


_METRIC_KEYS = (
    "trailingPE", "forwardPE", "priceToBook",
    "profitMargins", "operatingMargins", "returnOnEquity",
    "revenueGrowth", "enterpriseToEbitda",
)


def _fetch_peer_metrics(peer_tickers: list[str]) -> dict[str, dict]:
    """Pull a small bundle of valuation/quality metrics per peer via yfinance.

    Returns {peer: {metric: value or None}}. Failures are silent so a single
    flaky peer cannot break the whole comparison.
    """
    out: dict[str, dict] = {}
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed; competitor comparison disabled")
        return out

    for p in peer_tickers:
        try:
            info = yf.Ticker(p).info or {}
        except Exception as e:
            log.debug("peer fetch failed for %s: %s", p, e)
            continue

        bundle: dict = {}
        for k in _METRIC_KEYS:
            v = info.get(k)
            if isinstance(v, (int, float)) and v not in (0, 0.0):
                bundle[k] = float(v)
            else:
                bundle[k] = None
        if any(bundle.values()):
            out[p] = bundle
    return out


def _percentile_rank(values: list[float], target: float) -> Optional[float]:
    """Return the % of `values` strictly below `target`. None if too few values."""
    cleaned = [v for v in values if isinstance(v, (int, float))]
    if not cleaned:
        return None
    below = sum(1 for v in cleaned if v < target)
    return below / len(cleaned) * 100.0


def compute_competitor_comparison(ticker: str, fund: dict, *,
                                  quality: Optional[dict] = None) -> dict:
    """Compare `ticker`'s metrics vs peers and emit RELATIVE_VALUE / PREMIUM flags.

    Returns:
      {
        "available": bool,
        "peers": [tickers],
        "metrics": {metric: {self, peer_median, percentile_self, peer_count}},
        "flags": [...],
        "summary": "1-line human-readable",
      }

    Designed for the prompt and the scoring layer — values are normalized to
    percentages where applicable.
    """
    out: dict = {"available": False, "peers": [], "metrics": {}, "flags": [], "summary": ""}
    peers = get_peers(ticker)
    if not peers:
        out["summary"] = "(sin peers configurados para este ticker)"
        return out

    peer_data = _fetch_peer_metrics(peers)
    if len(peer_data) < 2:
        out["summary"] = "(no se pudieron leer métricas de al menos 2 peers)"
        return out

    quality = quality or {}

    self_metrics: dict[str, Optional[float]] = {
        "trailingPE":         fund.get("pe_ratio"),
        "forwardPE":          fund.get("forward_pe"),
        "priceToBook":        fund.get("pb_ratio"),
        "profitMargins":      fund.get("profit_margins"),
        "operatingMargins":   fund.get("operating_margins") or quality.get("operating_margin"),
        "returnOnEquity":     fund.get("roe") or quality.get("roe"),
        "revenueGrowth":      fund.get("revenue_growth_yoy"),
        "enterpriseToEbitda": None,  # rarely in our fund dict
    }

    metrics_summary: dict[str, dict] = {}
    cheaper_count = 0
    cheaper_total = 0
    quality_better_count = 0
    quality_total = 0

    # Categorize: lower-is-cheaper for valuation; higher-is-better for quality.
    LOWER_IS_BETTER = ("trailingPE", "forwardPE", "priceToBook", "enterpriseToEbitda")
    HIGHER_IS_BETTER = ("profitMargins", "operatingMargins", "returnOnEquity", "revenueGrowth")

    for key in _METRIC_KEYS:
        peer_values = [d.get(key) for d in peer_data.values() if isinstance(d.get(key), (int, float))]
        if len(peer_values) < 2:
            continue
        peer_median = statistics.median(peer_values)
        self_v = self_metrics.get(key)
        if not isinstance(self_v, (int, float)):
            metrics_summary[key] = {
                "self":            None,
                "peer_median":     peer_median,
                "percentile_self": None,
                "peer_count":      len(peer_values),
            }
            continue

        pct = _percentile_rank(peer_values, self_v)
        metrics_summary[key] = {
            "self":            self_v,
            "peer_median":     peer_median,
            "percentile_self": pct,
            "peer_count":      len(peer_values),
        }

        if key in LOWER_IS_BETTER:
            cheaper_total += 1
            if self_v < peer_median:
                cheaper_count += 1
        elif key in HIGHER_IS_BETTER:
            quality_total += 1
            if self_v > peer_median:
                quality_better_count += 1

    flags: list[str] = []

    # Need both signals to declare RELATIVE_VALUE — cheaper *and* better quality
    # in at least 2 valuation and 2 quality dimensions.
    if (cheaper_total >= 2 and cheaper_count >= 2
            and quality_total >= 2 and quality_better_count >= 2):
        flags.append("RELATIVE_VALUE_OPPORTUNITY")

    # PREMIUM_VS_PEERS: more expensive on at least 2 valuation metrics AND
    # quality metrics aren't beating peers (≤1 better).
    if (cheaper_total >= 2 and (cheaper_total - cheaper_count) >= 2
            and quality_total >= 2 and quality_better_count <= 1):
        flags.append("PREMIUM_VS_PEERS")

    out.update({
        "available": True,
        "peers": list(peer_data.keys()),
        "metrics": metrics_summary,
        "flags": flags,
        "cheaper_dims": cheaper_count,
        "cheaper_total": cheaper_total,
        "quality_dims_beating": quality_better_count,
        "quality_total": quality_total,
        "summary": _format_summary(ticker, metrics_summary, flags, list(peer_data.keys())),
    })
    return out


_LABELS = {
    "trailingPE":         "P/E",
    "forwardPE":          "Fwd P/E",
    "priceToBook":        "P/B",
    "profitMargins":      "Margen neto",
    "operatingMargins":   "Margen op.",
    "returnOnEquity":     "ROE",
    "revenueGrowth":      "Crec. ingresos",
    "enterpriseToEbitda": "EV/EBITDA",
}


def _format_summary(ticker: str, metrics: dict, flags: list[str], peers: list[str]) -> str:
    parts = [f"vs {','.join(peers[:4])}"]
    for k in ("trailingPE", "profitMargins", "returnOnEquity", "revenueGrowth"):
        m = metrics.get(k)
        if not m or m.get("self") is None:
            continue
        parts.append(f"{_LABELS[k]} {m['self']:.2f} vs mediana {m['peer_median']:.2f}")
    if flags:
        parts.append(f"flags: {', '.join(flags)}")
    return " | ".join(parts)


def format_competitors_block(comp: dict) -> str:
    """Compact block for the LLM prompt and Telegram messages."""
    if not comp.get("available"):
        return f"(comparativa peers: {comp.get('summary', 'no disponible')})"
    return f"Peers ({len(comp.get('peers', []))}): {comp.get('summary', '')}"
