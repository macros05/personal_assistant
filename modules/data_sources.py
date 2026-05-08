"""Multi-source fundamentals: yfinance + Alpha Vantage + Finviz cross-check.

The goal is to detect *data drift* between sources before it ends up in the
LLM prompt. yfinance is fast and free but often stale on weekends or for
foreign tickers; Alpha Vantage is authoritative but rate-limited; Finviz is a
useful third-opinion (and the only place we get short interest in real time).

If the sources disagree on a key valuation metric by more than 15%, we flag
DATA_DISCREPANCY and reduce confidence. The flag also carries the largest gap
so the operator can sanity-check.
"""
import logging
import os
import re
from typing import Optional

import httpx

log = logging.getLogger("modules.data_sources")

_FINVIZ_URL = "https://finviz.com/quote.ashx"
_FINVIZ_HEADERS = {
    # Finviz blocks default Python user agents; a normal-looking browser UA
    # works for low-volume scraping. We hit at most one page per company per
    # daily run, well below any reasonable threshold.
    "User-Agent":     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Finviz scrape ────────────────────────────────────────────────────────────

# Finviz's snapshot table uses two-column rows. We pull the page once, regex
# the values out. Any field can be absent.
_FINVIZ_KEYS = (
    "P/E",
    "Forward P/E",
    "P/B",
    "EPS (ttm)",
    "ROE",
    "Profit Margin",
    "Oper. Margin",
    "Sales Q/Q",
    "Insider Trans",
    "Insider Own",
    "Inst Trans",
    "Short Float",
    "Short Ratio",
    "Beta",
    "ATR",
    "Earnings",
    "Recom",
    "Target Price",
    "Sales",
    "Avg Volume",
)


def _finviz_clean(value: str) -> Optional[float]:
    """Convert a Finviz cell ('21.45%', '32.10', '-', '12.34B') into a float.

    Returns None for '-' and unparsable values. % values become percentage points
    (32.5% → 32.5), B/M/K suffixes are honored (12.34B → 1.234e10).
    """
    if not value or value.strip() == "-":
        return None
    v = value.strip()
    m = re.match(r"^(-?[0-9]+(?:\.[0-9]+)?)([%KMBT]?)$", v)
    if not m:
        return None
    num = float(m.group(1))
    suffix = m.group(2)
    if suffix == "K":
        return num * 1_000
    if suffix == "M":
        return num * 1_000_000
    if suffix == "B":
        return num * 1_000_000_000
    if suffix == "T":
        return num * 1_000_000_000_000
    if suffix == "%":
        return num
    return num


async def fetch_finviz(ticker: str, *, client: Optional[httpx.AsyncClient] = None) -> dict:
    """Scrape Finviz snapshot for `ticker`. Returns {} on any failure.

    Finviz only carries US tickers reliably — we still try foreign symbols and
    let the parser return {} when the table is missing.
    """
    out: dict = {}
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    try:
        r = await client.get(
            _FINVIZ_URL,
            params={"t": ticker.upper()},
            headers=_FINVIZ_HEADERS,
            follow_redirects=True,
        )
        if r.status_code != 200 or "snapshot-table2" not in r.text:
            return out
        html = r.text
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Finviz fetch failed for %s: %s", ticker, e)
        return out
    finally:
        if own_client:
            await client.aclose()

    # Cells alternate label/value in <td class="snapshot-td2">… cells. The
    # current (2026-Q2) Finviz HTML wraps labels in a label-div and values in
    # a content-div with a <b>. We grab the first <b>...</b> after the label.
    # Note: labels can include slashes and parentheses, so we escape them.
    for label in _FINVIZ_KEYS:
        esc = re.escape(label)
        m = re.search(
            rf">{esc}<.*?<b[^>]*>(.*?)</b>",
            html, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            continue
        raw = re.sub(r"<.*?>", "", m.group(1)).strip()
        # Split things like "21.45 / 19.20" — keep the first number only.
        if "/" in raw:
            raw = raw.split("/")[0].strip()
        out[label] = raw

    # Normalize the relevant fields into a stable schema mirroring our fund dict.
    norm: dict = {"raw": out}
    norm["pe_ratio"]          = _finviz_clean(out.get("P/E", ""))
    norm["forward_pe"]        = _finviz_clean(out.get("Forward P/E", ""))
    norm["pb_ratio"]          = _finviz_clean(out.get("P/B", ""))
    norm["eps"]               = _finviz_clean(out.get("EPS (ttm)", ""))
    roe = _finviz_clean(out.get("ROE", ""))
    norm["roe"]               = roe / 100.0 if isinstance(roe, (int, float)) else None
    pm = _finviz_clean(out.get("Profit Margin", ""))
    norm["profit_margins"]    = pm / 100.0 if isinstance(pm, (int, float)) else None
    om = _finviz_clean(out.get("Oper. Margin", ""))
    norm["operating_margins"] = om / 100.0 if isinstance(om, (int, float)) else None
    rg = _finviz_clean(out.get("Sales Q/Q", ""))
    norm["revenue_growth_yoy"] = rg / 100.0 if isinstance(rg, (int, float)) else None
    sf = _finviz_clean(out.get("Short Float", ""))
    norm["short_pct_float"]   = sf  # already in percent
    it = _finviz_clean(out.get("Insider Trans", ""))
    norm["insider_trans_pct_6m"] = it  # already in percent

    norm["earnings_label"] = out.get("Earnings")  # raw e.g. "Apr 30 BMO"
    norm["analyst_recom"]  = _finviz_clean(out.get("Recom", ""))  # 1.0 best, 5.0 worst
    norm["analyst_target"] = _finviz_clean(out.get("Target Price", ""))
    norm["beta"]           = _finviz_clean(out.get("Beta", ""))
    return norm


# ── Cross-source comparison ──────────────────────────────────────────────────

# Metric → tolerance for "agreement" (relative). 0.15 = 15%.
_DISCREPANCY_TOLERANCE = {
    "pe_ratio":          0.15,
    "forward_pe":        0.15,
    "pb_ratio":          0.15,
    "eps":               0.20,
    "roe":               0.20,
    "profit_margins":    0.20,
    "operating_margins": 0.20,
}

_DISCREPANCY_MIN_ABS = {
    # Don't fire on absolute differences smaller than these — for very small
    # numbers a 15% relative gap is just noise.
    "pe_ratio":          1.5,
    "forward_pe":        1.5,
    "pb_ratio":          0.4,
    "eps":               0.10,
    "roe":               0.02,    # 2 percentage points
    "profit_margins":    0.02,
    "operating_margins": 0.02,
}


def _values_agree(a: float, b: float, *, tol: float, min_abs: float) -> bool:
    if a is None or b is None:
        return True
    a, b = float(a), float(b)
    if abs(a - b) < min_abs:
        return True
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base <= tol


def cross_validate(yf_fund: dict, alpha: dict, finviz: dict) -> dict:
    """Compare the three sources on key metrics. Return discrepancy report.

    Output:
      {
        "available":  bool,
        "agreements": {metric: {sources_compared, max_gap_pct, ok}},
        "max_gap":    {metric, gap_pct, sources},
        "flags":      [],   # ['DATA_DISCREPANCY'] when any disagreement above tolerance
      }
    """
    def _alpha_get(key: str) -> Optional[float]:
        # Alpha Vantage uses different field names; we already merged it into
        # yf_fund in stock_analyzer._merge_alpha. This function is fed the raw
        # alpha dict for an independent comparison.
        mapping = {
            "pe_ratio": "PERatio",
            "forward_pe": "ForwardPE",
            "pb_ratio": "PriceToBookRatio",
            "eps": "EPS",
            "roe": "ReturnOnEquityTTM",
            "profit_margins": "ProfitMargin",
            "operating_margins": "OperatingMarginTTM",
        }
        v = (alpha or {}).get(mapping.get(key, ""))
        try:
            return float(v) if v not in (None, "", "None", "-") else None
        except (TypeError, ValueError):
            return None

    yf = yf_fund or {}
    fv = finviz or {}

    agreements: dict[str, dict] = {}
    max_gap_metric = None
    max_gap_value = 0.0
    max_gap_sources: tuple[str, str] = ("", "")

    triggered = False

    for metric in _DISCREPANCY_TOLERANCE.keys():
        a_yf = yf.get(metric)
        a_av = _alpha_get(metric)
        a_fv = fv.get(metric)

        # Build (source, value) list of present numerics.
        triplets = [(name, val) for name, val in (("yf", a_yf), ("av", a_av), ("fv", a_fv))
                    if isinstance(val, (int, float))]
        if len(triplets) < 2:
            continue

        tol     = _DISCREPANCY_TOLERANCE[metric]
        min_abs = _DISCREPANCY_MIN_ABS[metric]
        ok = True
        gap_pct_max = 0.0
        worst_pair: tuple[str, str] = ("", "")
        for i in range(len(triplets)):
            for j in range(i + 1, len(triplets)):
                n1, v1 = triplets[i]; n2, v2 = triplets[j]
                if not _values_agree(v1, v2, tol=tol, min_abs=min_abs):
                    ok = False
                base = max(abs(v1), abs(v2), 1e-9)
                gap = abs(v1 - v2) / base * 100.0
                if gap > gap_pct_max:
                    gap_pct_max = gap
                    worst_pair = (n1, n2)

        agreements[metric] = {
            "sources":     [n for n, _ in triplets],
            "values":      {n: v for n, v in triplets},
            "max_gap_pct": gap_pct_max,
            "ok":          ok,
        }

        if not ok and gap_pct_max > max_gap_value:
            max_gap_value   = gap_pct_max
            max_gap_metric  = metric
            max_gap_sources = worst_pair
            triggered = True

    flags: list[str] = []
    if triggered:
        flags.append("DATA_DISCREPANCY")

    return {
        "available":  bool(agreements),
        "agreements": agreements,
        "max_gap":    {
            "metric":  max_gap_metric,
            "gap_pct": max_gap_value,
            "sources": list(max_gap_sources),
        } if max_gap_metric else None,
        "flags":      flags,
    }


def format_data_quality_block(cross: dict, finviz: dict) -> str:
    """Compact line for the prompt: which sources we cross-checked + worst gap."""
    if not cross.get("available"):
        return "(no cruzamos métricas con peers de fuentes)"
    parts = []
    n_pairs = len(cross.get("agreements", {}))
    parts.append(f"{n_pairs} métricas comparadas yf/AV/Finviz")
    mg = cross.get("max_gap")
    if mg and mg.get("metric"):
        parts.append(f"máx. divergencia {mg['gap_pct']:.0f}% en {mg['metric']} "
                     f"({'/'.join(mg['sources'])})")
    if "DATA_DISCREPANCY" in cross.get("flags", []):
        parts.append("⚠ DATA_DISCREPANCY")
    if (finviz or {}).get("short_pct_float") is not None:
        parts.append(f"short% (Finviz) {finviz['short_pct_float']:.1f}")
    return " | ".join(parts)
