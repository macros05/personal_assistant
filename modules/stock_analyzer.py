"""Buffett-style value analysis engine: fundamentals + news → Gemini verdict."""
import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from google import genai
from google.genai import types

from modules import stock_news

log = logging.getLogger("modules.stock_analyzer")

_WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
_ALPHA_URL      = "https://www.alphavantage.co/query"

# Mega caps need extra scrutiny: an 'undervalued by 25%' verdict on a heavily-followed
# $500B+ company is almost always wrong. Threshold and margin used by the sanity check.
_MEGA_CAP_USD               = 200_000_000_000   # $200B+ counts as mega cap
_MEGA_CAP_SUSPICIOUS_MARGIN = 20.0              # margin of safety % above which we doubt

# Static peer/sector reference. Keeps the prompt grounded without needing a live
# sector-data API. Update if watchlist composition changes meaningfully.
_PEER_HINTS: dict[str, dict] = {
    "AAPL":  {"peers": "MSFT, GOOGL, META",      "sector_pe_band": "25-35", "sector": "Mega-cap tech"},
    "MSFT":  {"peers": "AAPL, GOOGL, ORCL",      "sector_pe_band": "28-38", "sector": "Mega-cap tech"},
    "GOOGL": {"peers": "META, MSFT, AAPL",       "sector_pe_band": "20-30", "sector": "Mega-cap tech"},
    "AMZN":  {"peers": "WMT, COST, MSFT",        "sector_pe_band": "30-50", "sector": "E-commerce / cloud"},
    "META":  {"peers": "GOOGL, SNAP, PINS",      "sector_pe_band": "18-28", "sector": "Online advertising"},
    "NVDA":  {"peers": "AMD, AVGO, TSM, INTC",   "sector_pe_band": "30-50", "sector": "Semiconductors"},
    "BRK-B": {"peers": "BRK-A (same issuer)",    "sector_pe_band": "8-12 (insurance/conglomerate)",
              "sector": "Insurance / diversified holding"},
    "KO":    {"peers": "PEP, KDP, MNST",         "sector_pe_band": "20-26", "sector": "Beverages"},
    "JNJ":   {"peers": "PFE, MRK, ABBV",         "sector_pe_band": "12-18", "sector": "Pharma"},
    "V":     {"peers": "MA, AXP, PYPL",          "sector_pe_band": "25-35", "sector": "Payments"},
    "ASML":  {"peers": "AMAT, LRCX, KLAC, TSM",  "sector_pe_band": "30-45", "sector": "Semicap equipment"},
    "SAP":   {"peers": "ORCL, MSFT, CRM",        "sector_pe_band": "22-32", "sector": "Enterprise software"},
    "NVO":   {"peers": "LLY, PFE, MRK",          "sector_pe_band": "20-30", "sector": "Pharma (GLP-1)"},
    "RACE":  {"peers": "POAHY (Porsche), BMWYY", "sector_pe_band": "30-45", "sector": "Luxury auto"},
    "MC.PA": {"peers": "RMS.PA, KER.PA, CFR.SW", "sector_pe_band": "20-28", "sector": "Luxury goods"},
}

_ANALYSIS_PROMPT = """\
Actúa como un analista de valor estilo Warren Buffett. Tu tarea es decidir si la empresa \
representa una oportunidad de compra clara con margen de seguridad. Sé escéptico: las \
empresas mega-cap muy seguidas raramente están "infravaloradas un 25%"; si tu cálculo \
indica eso, casi seguro tu valor intrínseco es erróneo o los datos están incompletos.

Reglas estrictas:
1. NO confundas tickers. {ticker} aquí es {ticker_disambiguation}.
2. El valor intrínseco debe estar respaldado por al menos dos métodos: (a) DCF EPS×múltiplo \
   razonable para el sector, (b) Graham number cuando aplique (sqrt(22.5 × EPS × BookValue)), \
   (c) comparación con P/E sector. Si EPS o BookValue son negativos, NO uses Graham.
3. Compara P/E vs el rango sectorial proporcionado. Una mega-cap con P/E dentro del rango \
   sectorial NO está infravalorada significativamente.
4. Ignora noticias irrelevantes (paquetes en PyPI/npm, deportes, ofertas retail, CVEs en \
   japonés). Solo trata como catalizador noticias claramente sobre la empresa: resultados, \
   guidance, regulación, M&A, cambios de CEO, demandas materiales, dividendos, recompras.
5. Si los datos fundamentales tienen huecos (P/E o EPS o BookValue ausentes), refleja eso \
   en confidence (LOW) y reduce score.
6. Tu intrinsic_value debe estar denominado en la MISMA moneda que el precio actual.

Responde EXCLUSIVAMENTE con un objeto JSON válido (sin markdown, sin texto adicional):
{{
  "opportunity": true|false,
  "score": 0-100,
  "confidence": "HIGH"|"MEDIUM"|"LOW",
  "reason": "2-3 frases en español que justifiquen la conclusión y citen datos clave",
  "recommendation": "COMPRAR"|"ESPERAR"|"EVITAR",
  "intrinsic_value": número o null,
  "intrinsic_method": "1-2 frases describiendo cómo lo calculaste (DCF, Graham, etc.)",
  "margin_of_safety": número (porcentaje, positivo si infravalorada),
  "peer_context": "1 frase comparando con peers/sector",
  "data_quality_notes": "1 frase: qué datos faltan o son dudosos"
}}

Empresa: {name} ({ticker})
Sector / negocio: {sector}
Peers: {peers}
Rango P/E sectorial típico: {sector_pe_band}

Precio actual: {price}
Datos fundamentales:
{fundamentals}

Estimación inicial (ancla, NO definitiva) DCF EPS×15: {dcf_baseline}
Graham number (referencia, solo si EPS y BookValue > 0): {graham}

Últimas noticias relevantes (ya filtradas por relevancia):
{news_block}

Sentimiento general inferido de las noticias: {sentiment}
"""

# Disambiguation hints for tickers that get confused with other things.
_TICKER_DISAMBIGUATION: dict[str, str] = {
    "BRK-B": "Berkshire Hathaway clase B (holding diversificado de Warren Buffett, NYSE). "
             "NO confundir con criptomonedas, bonos ni con Brookfield Renewable.",
    "BRK-A": "Berkshire Hathaway clase A.",
    "META":  "Meta Platforms (Facebook/Instagram/WhatsApp), NO Metavante ni proyectos cripto.",
    "V":     "Visa Inc. (red de pagos), NO Visa Equity Holdings ni otros tickers genéricos.",
    "MC.PA": "LVMH Moët Hennessy Louis Vuitton (París), NO McDonald's (MCD).",
    "RACE":  "Ferrari NV (NYSE), NO ningún otro vehículo financiero con ese símbolo.",
    "KO":    "The Coca-Cola Company (NYSE), NO empresas con nombre similar en otros mercados.",
}


def load_watchlist() -> list[dict]:
    """Read watchlist.json and return a flat list of {ticker, name, region}."""
    try:
        raw = json.loads(_WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("Cannot read watchlist: %s", e)
        return []

    items: list[dict] = []
    for region in ("us", "eu"):
        for entry in raw.get(region, []) or []:
            ticker = entry.get("ticker")
            name   = entry.get("name") or ticker
            if ticker:
                items.append({"ticker": ticker, "name": name, "region": region})
    return items


def _safe(v):
    """Convert NaN / None / pandas-ish blanks to None for JSON-friendly output."""
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _fmt(label: str, value) -> str:
    if value is None:
        return f"- {label}: N/A"
    if isinstance(value, float):
        return f"- {label}: {value:.4g}"
    return f"- {label}: {value}"


def _fundamentals_from_yf(ticker: str) -> dict:
    """Fetch fundamentals via yfinance. Synchronous — call via asyncio.to_thread."""
    import yfinance as yf  # imported here to avoid top-level cost when unused

    t = yf.Ticker(ticker)
    info: dict = {}
    try:
        info = t.info or {}
    except Exception as e:  # yfinance raises various errors
        log.warning("yfinance .info failed for %s: %s", ticker, e)

    price = (
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
    )

    return {
        "price":              _safe(price),
        "currency":           _safe(info.get("currency")),
        "pe_ratio":           _safe(info.get("trailingPE")),
        "forward_pe":         _safe(info.get("forwardPE")),
        "pb_ratio":           _safe(info.get("priceToBook")),
        "eps":                _safe(info.get("trailingEps")),
        "book_value":         _safe(info.get("bookValue")),
        "roe":                _safe(info.get("returnOnEquity")),
        "debt_to_equity":     _safe(info.get("debtToEquity")),
        "profit_margins":     _safe(info.get("profitMargins")),
        "operating_margins":  _safe(info.get("operatingMargins")),
        "revenue_growth_yoy": _safe(info.get("revenueGrowth")),
        "market_cap":         _safe(info.get("marketCap")),
        "dividend_yield":     _safe(info.get("dividendYield")),
        "sector":             _safe(info.get("sector")),
    }


async def _alpha_overview(client: httpx.AsyncClient, ticker: str) -> dict:
    """Optional Alpha Vantage OVERVIEW. Quietly returns {} if unavailable."""
    api_key = os.getenv("ALPHA_VANTAGE_KEY")
    if not api_key:
        return {}
    params = {"function": "OVERVIEW", "symbol": ticker, "apikey": api_key}
    try:
        r = await client.get(_ALPHA_URL, params=params, timeout=15.0)
        r.raise_for_status()
        data = r.json() or {}
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Alpha Vantage OVERVIEW failed for %s: %s", ticker, e)
        return {}
    if not isinstance(data, dict) or "Symbol" not in data:
        return {}
    # Alpha Vantage returns the symbol it actually resolved. Reject mismatches —
    # they happen with non-US exchanges (e.g. MC.PA → empty / wrong company).
    returned_symbol = (data.get("Symbol") or "").upper()
    if returned_symbol and returned_symbol != ticker.upper():
        log.warning("Alpha Vantage symbol mismatch: requested %s, got %s — ignoring",
                    ticker, returned_symbol)
        return {}
    return data


def _merge_alpha(fundamentals: dict, alpha: dict) -> dict:
    """Fill in any missing yfinance fields from Alpha Vantage."""
    if not alpha:
        return fundamentals

    def f(key):
        v = alpha.get(key)
        if v in (None, "", "None", "-"):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    mapping = {
        "pe_ratio":          f("PERatio"),
        "forward_pe":        f("ForwardPE"),
        "pb_ratio":          f("PriceToBookRatio"),
        "eps":               f("EPS"),
        "book_value":        f("BookValue"),
        "roe":               f("ReturnOnEquityTTM"),
        "profit_margins":    f("ProfitMargin"),
        "operating_margins": f("OperatingMarginTTM"),
        "revenue_growth_yoy": f("QuarterlyRevenueGrowthYOY"),
    }
    for k, v in mapping.items():
        if fundamentals.get(k) is None and v is not None:
            fundamentals[k] = v
    return fundamentals


def _infer_sentiment(headlines: list[dict]) -> str:
    """Rough lexicon-based sentiment label."""
    if not headlines:
        return "neutral"
    pos = ("beats", "surge", "record", "growth", "upgrade", "strong", "rally", "raises")
    neg = ("misses", "plunge", "fraud", "lawsuit", "downgrade", "weak", "cut", "drops",
           "investigation", "recall", "bankruptcy")
    score = 0
    for h in headlines:
        title = (h.get("title") or "").lower()
        score += sum(1 for k in pos if k in title)
        score -= sum(1 for k in neg if k in title)
    if score >= 2:
        return "positive"
    if score <= -2:
        return "negative"
    return "neutral"


def _format_news_block(headlines: list[dict]) -> str:
    if not headlines:
        return "- (sin noticias relevantes en las últimas 48h)"
    lines = []
    for h in headlines[:5]:
        date = (h.get("published_at") or "")[:10]
        src  = h.get("source") or "?"
        title = (h.get("title") or "").strip()
        lines.append(f"- [{date}] {src}: {title}")
    return "\n".join(lines)


def _graham_number(eps, book_value) -> Optional[float]:
    """Classic Graham number: sqrt(22.5 * EPS * BookValue). Returns None if invalid."""
    if not isinstance(eps, (int, float)) or not isinstance(book_value, (int, float)):
        return None
    if eps <= 0 or book_value <= 0:
        return None
    try:
        return math.sqrt(22.5 * eps * book_value)
    except ValueError:
        return None


def _data_quality(fund: dict, headlines: list[dict]) -> tuple[str, list[str]]:
    """Return ('HIGH'|'MEDIUM'|'LOW', notes). Used as a floor for confidence."""
    required_fields = ("price", "pe_ratio", "eps", "book_value", "market_cap")
    missing = [k for k in required_fields if fund.get(k) is None]
    notes: list[str] = []
    if missing:
        notes.append(f"Faltan campos: {', '.join(missing)}")
    if not headlines:
        notes.append("Sin noticias relevantes recientes")

    if not missing and len(headlines) >= 3:
        return "HIGH", notes
    if len(missing) <= 2:
        return "MEDIUM", notes
    return "LOW", notes


def _build_prompt(name: str, ticker: str, fund: dict, headlines: list[dict]) -> tuple[str, str, Optional[float], Optional[float]]:
    eps         = fund.get("eps")
    book_value  = fund.get("book_value")
    dcf_baseline = (eps * 15.0) if isinstance(eps, (int, float)) else None
    graham       = _graham_number(eps, book_value)
    sentiment    = _infer_sentiment(headlines)
    hint         = _PEER_HINTS.get(ticker, {})
    peers        = hint.get("peers")          or "(no disponibles en hint estático)"
    sector_pe    = hint.get("sector_pe_band") or "(no disponible)"
    sector_label = hint.get("sector")         or fund.get("sector") or "(desconocido)"

    fund_block = "\n".join([
        _fmt("Precio",              fund.get("price")),
        _fmt("Moneda",              fund.get("currency")),
        _fmt("P/E (trailing)",      fund.get("pe_ratio")),
        _fmt("P/E (forward)",       fund.get("forward_pe")),
        _fmt("P/B",                 fund.get("pb_ratio")),
        _fmt("EPS",                 eps),
        _fmt("Book value",          book_value),
        _fmt("ROE",                 fund.get("roe")),
        _fmt("Debt/Equity",         fund.get("debt_to_equity")),
        _fmt("Profit margins",      fund.get("profit_margins")),
        _fmt("Operating margins",   fund.get("operating_margins")),
        _fmt("Revenue growth YoY",  fund.get("revenue_growth_yoy")),
        _fmt("Market cap",          fund.get("market_cap")),
        _fmt("Sector (yfinance)",   fund.get("sector")),
    ])

    prompt = _ANALYSIS_PROMPT.format(
        name=name,
        ticker=ticker,
        ticker_disambiguation=_TICKER_DISAMBIGUATION.get(ticker, f"el símbolo bursátil de {name}"),
        sector=sector_label,
        peers=peers,
        sector_pe_band=sector_pe,
        price=(f"{fund['price']:.2f} {fund.get('currency') or ''}".strip()
               if isinstance(fund.get("price"), (int, float)) else "N/A"),
        fundamentals=fund_block,
        dcf_baseline=(f"{dcf_baseline:.2f}" if dcf_baseline is not None else "N/A"),
        graham=(f"{graham:.2f}" if graham is not None else "N/A"),
        news_block=_format_news_block(headlines),
        sentiment=sentiment,
    )
    return prompt, sentiment, dcf_baseline, graham


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of Gemini's response."""
    if not text:
        return None
    match = _JSON_OBJ_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


async def _ask_gemini(prompt: str, client: genai.Client, model: str) -> Optional[dict]:
    config = types.GenerateContentConfig(
        temperature=0.3,
        response_mime_type="application/json",
    )
    try:
        resp = await client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=config,
        )
    except Exception as e:
        log.error("Gemini call failed: %s", e)
        return None

    text = (resp.text or "").strip()
    parsed = _extract_json(text)
    if parsed is None:
        log.warning("Gemini returned non-JSON: %s", text[:200])
    return parsed


def _coerce_verdict(raw: dict, fund: dict) -> dict:
    """Normalize Gemini's JSON into stable types/keys."""
    def num(v) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    intrinsic = num(raw.get("intrinsic_value"))
    price     = fund.get("price") if isinstance(fund.get("price"), (int, float)) else None

    margin = num(raw.get("margin_of_safety"))
    if margin is None and intrinsic and price and intrinsic > 0:
        margin = (intrinsic - price) / intrinsic * 100.0

    score = num(raw.get("score")) or 0
    score = max(0, min(100, int(round(score))))

    rec = (raw.get("recommendation") or "").strip().upper()
    if rec not in {"COMPRAR", "ESPERAR", "EVITAR"}:
        rec = "ESPERAR"

    confidence = (raw.get("confidence") or "").strip().upper()
    if confidence not in {"HIGH", "MEDIUM", "LOW"}:
        confidence = "LOW"

    return {
        "opportunity":      bool(raw.get("opportunity")),
        "score":            score,
        "confidence":       confidence,
        "reason":           (raw.get("reason") or "").strip(),
        "recommendation":   rec,
        "intrinsic_value":  intrinsic,
        "intrinsic_method": (raw.get("intrinsic_method") or "").strip(),
        "margin_of_safety": margin,
        "peer_context":     (raw.get("peer_context") or "").strip(),
        "data_quality_notes": (raw.get("data_quality_notes") or "").strip(),
    }


_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_CONFIDENCE_BY_RANK = {0: "LOW", 1: "MEDIUM", 2: "HIGH"}


def _apply_sanity_checks(verdict: dict, fund: dict, ticker: str) -> dict:
    """Mark suspicious mega-cap valuations and ratchet down confidence/score."""
    flags: list[str] = []

    market_cap = fund.get("market_cap") if isinstance(fund.get("market_cap"), (int, float)) else None
    margin     = verdict.get("margin_of_safety") if isinstance(verdict.get("margin_of_safety"), (int, float)) else None

    # Mega-cap "deeply undervalued" verdict — almost always wrong; flag and downgrade.
    if (market_cap is not None and market_cap >= _MEGA_CAP_USD
            and margin is not None and margin >= _MEGA_CAP_SUSPICIOUS_MARGIN):
        flags.append("SUSPICIOUS_MEGACAP_UNDERVALUATION")
        log.warning(
            "%s flagged: mega-cap (~$%.0fB) reportedly %.1f%% undervalued — downgrading",
            ticker, market_cap / 1e9, margin,
        )
        if verdict.get("recommendation") == "COMPRAR":
            verdict["recommendation"] = "ESPERAR"
        verdict["score"] = min(verdict.get("score", 0), 60)
        verdict["opportunity"] = False
        # Knock confidence down by one notch (LOW stays LOW).
        rank = _CONFIDENCE_RANK.get(verdict.get("confidence", "LOW"), 0)
        verdict["confidence"] = _CONFIDENCE_BY_RANK[max(0, rank - 1)]

    # Intrinsic deviates >100% from the simple DCF anchor → likely fabrication.
    eps = fund.get("eps")
    intrinsic = verdict.get("intrinsic_value")
    if (isinstance(eps, (int, float)) and eps > 0
            and isinstance(intrinsic, (int, float)) and intrinsic > 0):
        anchor = eps * 15.0
        if intrinsic > anchor * 3 or intrinsic < anchor * 0.25:
            flags.append("INTRINSIC_FAR_FROM_ANCHOR")
            verdict["score"] = min(verdict.get("score", 0), 65)

    verdict["flags"] = flags
    return verdict


def _reconcile_confidence(verdict: dict, data_quality: str) -> dict:
    """Confidence cannot exceed data quality. Otherwise we'd over-trust thin data."""
    cap = _CONFIDENCE_RANK[data_quality]
    cur = _CONFIDENCE_RANK.get(verdict.get("confidence", "LOW"), 0)
    verdict["confidence"] = _CONFIDENCE_BY_RANK[min(cap, cur)]
    return verdict


async def analyze_company(
    ticker: str,
    name: str,
    *,
    gemini_client: genai.Client,
    gemini_model: str,
    http_client: Optional[httpx.AsyncClient] = None,
    weekend_skip_market: bool = False,
) -> Optional[dict]:
    """Run the full pipeline for one company. Returns analysis dict or None on failure."""
    own_http = http_client is None
    if own_http:
        http_client = httpx.AsyncClient(timeout=15.0)

    try:
        fund = await asyncio.to_thread(_fundamentals_from_yf, ticker)
    except Exception as e:
        log.error("yfinance failed hard for %s: %s — skipping", ticker, e)
        if own_http:
            await http_client.aclose()
        return None

    if weekend_skip_market:
        fund["price"] = None  # weekends: skip stale market price; still analyze fundamentals

    try:
        alpha = await _alpha_overview(http_client, ticker)
        fund = _merge_alpha(fund, alpha)

        try:
            headlines = await stock_news.fetch_news(
                name, ticker=ticker, limit=5, hours_back=72, client=http_client,
            )
        except Exception as e:
            log.warning("News fetch failed for %s: %s", ticker, e)
            headlines = []

        prompt, sentiment, dcf_baseline, graham = _build_prompt(name, ticker, fund, headlines)
        raw = await _ask_gemini(prompt, gemini_client, gemini_model)
        if raw is None:
            return None

        verdict = _coerce_verdict(raw, fund)
        verdict = _apply_sanity_checks(verdict, fund, ticker)

        data_quality, dq_notes = _data_quality(fund, headlines)
        verdict = _reconcile_confidence(verdict, data_quality)

        catalyst = headlines[0]["title"] if headlines else ""

        return {
            "ticker":            ticker,
            "name":              name,
            "price":             fund.get("price"),
            "intrinsic_value":   verdict["intrinsic_value"],
            "intrinsic_method":  verdict.get("intrinsic_method", ""),
            "margin_of_safety":  verdict["margin_of_safety"],
            "score":             verdict["score"],
            "opportunity":       verdict["opportunity"],
            "recommendation":    verdict["recommendation"],
            "confidence":        verdict["confidence"],
            "flags":             verdict.get("flags", []),
            "reason":            verdict["reason"],
            "peer_context":      verdict.get("peer_context", ""),
            "data_quality":      data_quality,
            "data_quality_notes": "; ".join(filter(None, [verdict.get("data_quality_notes", ""), *dq_notes])),
            "catalyst":          catalyst,
            "sentiment":         sentiment,
            "dcf_baseline":      dcf_baseline,
            "graham_number":     graham,
            "fundamentals":      fund,
            "headlines":         headlines,
            "analyzed_at":       datetime.utcnow().isoformat(),
            "raw_payload":       json.dumps(raw, ensure_ascii=False),
        }
    finally:
        if own_http:
            await http_client.aclose()


def make_gemini_client() -> tuple[genai.Client, str]:
    """Construct a Gemini client + model name from env."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    return genai.Client(api_key=api_key), model
