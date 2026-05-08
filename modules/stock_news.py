"""News fetcher for watchlist companies. Uses NewsAPI with GNews fallback."""
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger("modules.stock_news")

_NEWSAPI_URL = "https://newsapi.org/v2/everything"
_GNEWS_URL   = "https://gnews.io/api/v4/search"

# Reputable financial publications. Headlines from these sources get a relevance bonus.
_TRUSTED_SOURCES = {
    "reuters", "bloomberg", "wall street journal", "wsj", "financial times", "ft",
    "cnbc", "marketwatch", "yahoo finance", "seeking alpha", "investor's business daily",
    "barrons", "barron's", "morningstar", "the motley fool", "investing.com",
    "forbes", "business insider", "the economist", "associated press", "ap",
    "zacks", "benzinga", "thestreet", "investorplace", "kiplinger",
}

# Vocabulary that signals a headline is genuinely about company finances/operations.
_FINANCE_KEYWORDS = (
    "earnings", "revenue", "profit", "loss", "guidance", "forecast", "outlook",
    "eps", "margin", "dividend", "buyback", "share", "stock", "shares",
    "analyst", "upgrade", "downgrade", "target price", "price target",
    "lawsuit", "regulator", "regulators", "sec ", "ftc", "antitrust",
    "merger", "acquisition", "acquires", "acquired", "deal", "stake",
    "ceo", "cfo", "executive", "resigns", "appoints",
    "beats", "misses", "raises", "cuts", "halts", "recall", "fraud",
    "investigation", "settlement", "fine", "penalty",
    "quarterly", "q1", "q2", "q3", "q4", "fiscal", "annual report",
    "ipo", "split", "spinoff", "listing",
)

# Hard blacklist — patterns that, when matched, drop the headline regardless of
# anything else. Built directly from real false positives observed in the analyses
# DB (PyPI package indexing, CVE feeds in non-English, sports, retail promos).
_NOISE_PATTERNS = (
    re.compile(r"\badded to (pypi|npm|crates\.io|maven|packagist|rubygems)\b", re.I),
    re.compile(r"\b(jvndb|cve)-\d{4}-\d+", re.I),
    re.compile(r"\bnpm install\b", re.I),
    re.compile(r"\b(super[- ]?saver|free shipping|woot|coupon|deal of the day)\b", re.I),
    re.compile(r"\b(nba|nfl|mlb|ufc|playoffs?|spring camp|spurs|lakers|wembanyama)\b", re.I),
    re.compile(r"\b(travel insurance|ultimate guide|things to do|top \d+ recipes)\b", re.I),
    re.compile(r"\b(horoscope|zodiac|celebrity|red carpet|reality show)\b", re.I),
    # Mostly-Japanese / non-Latin titles tend to be CVE feeds or scrapes; safer to skip.
    re.compile(r"[぀-ヿ一-鿿]{4,}"),
)


async def fetch_news(
    query: str,
    *,
    ticker: Optional[str] = None,
    limit: int = 5,
    hours_back: int = 48,
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Return up to `limit` recent, *relevance-filtered* headlines about `query`.

    Each item: {title, url, source, published_at, relevance}. The list is sorted
    by relevance descending; clearly irrelevant items are dropped before return.
    Pass `ticker` (e.g. "BRK-B") so headlines can be matched against the symbol
    too — fixes the case where a generic company name pulls unrelated articles.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15.0)

    # Pull a wider set than asked for, since we drop noise after fetching.
    pull = max(limit * 3, 10)
    try:
        items = await _try_newsapi(client, query, pull, hours_back)
        if not items:
            items = await _try_gnews(client, query, pull, hours_back)
    finally:
        if own_client:
            await client.aclose()

    return _filter_and_rank(items, query=query, ticker=ticker, limit=limit)


def _is_noise(title: str) -> bool:
    if not title:
        return True
    return any(p.search(title) for p in _NOISE_PATTERNS)


def _score_headline(item: dict, *, query: str, ticker: Optional[str]) -> int:
    """Higher = more likely to be a real, finance-relevant headline about the company."""
    title  = (item.get("title") or "").lower()
    source = (item.get("source") or "").lower()
    if not title or _is_noise(title):
        return -100

    score = 0
    q_lower = query.lower()
    # Strip ' Inc', ' Corp', ' Holdings' etc. for matching the company name root.
    q_root  = re.sub(r"\b(inc|corp|holdings?|company|co|plc|sa|nv|ag|ltd)\.?$", "",
                     q_lower).strip()

    if q_root and q_root in title:
        score += 5
    if ticker and re.search(rf"\b{re.escape(ticker.lower())}\b", title):
        score += 4

    if any(k in title for k in _FINANCE_KEYWORDS):
        score += 3

    if any(s in source for s in _TRUSTED_SOURCES):
        score += 2

    return score


def _filter_and_rank(
    items: list[dict],
    *,
    query: str,
    ticker: Optional[str],
    limit: int,
) -> list[dict]:
    scored: list[tuple[int, dict]] = []
    for it in items:
        s = _score_headline(it, query=query, ticker=ticker)
        if s <= 0:
            continue
        it = dict(it)
        it["relevance"] = s
        scored.append((s, it))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[:limit]]


async def _try_newsapi(client: httpx.AsyncClient, query: str, limit: int, hours_back: int) -> list[dict]:
    api_key = os.getenv("NEWS_API_KEY")
    if not api_key:
        return []

    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat(timespec="seconds")
    params = {
        "q":        f'"{query}"',  # quote the query to reduce stray matches
        "from":     since,
        "language": "en",
        "sortBy":   "publishedAt",
        "pageSize": limit,
        "apiKey":   api_key,
    }
    try:
        r = await client.get(_NEWSAPI_URL, params=params)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("NewsAPI fetch failed for %s: %s", query, e)
        return []

    return [
        {
            "title":        a.get("title", ""),
            "url":          a.get("url", ""),
            "source":       (a.get("source") or {}).get("name", ""),
            "published_at": a.get("publishedAt", ""),
        }
        for a in (data.get("articles") or [])[:limit]
    ]


async def _try_gnews(client: httpx.AsyncClient, query: str, limit: int, hours_back: int) -> list[dict]:
    api_key = os.getenv("GNEWS_API_KEY")
    if not api_key:
        return []
    # Set GNEWS_ENABLED=1 in .env to re-enable. Disabled by default because the free
    # tier's daily quota is regularly exhausted, producing 429s with no headlines.
    if os.getenv("GNEWS_ENABLED", "0").strip() not in ("1", "true", "yes"):
        return []

    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "q":     query,
        "from":  since,
        "lang":  "en",
        "max":   limit,
        "token": api_key,
    }
    try:
        r = await client.get(_GNEWS_URL, params=params)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("GNews fetch failed for %s: %s", query, e)
        return []

    return [
        {
            "title":        a.get("title", ""),
            "url":          a.get("url", ""),
            "source":       (a.get("source") or {}).get("name", ""),
            "published_at": a.get("publishedAt", ""),
        }
        for a in (data.get("articles") or [])[:limit]
    ]


def detect_breaking(headlines: list[dict]) -> bool:
    """Heuristic: headline within last 2 h with strong-signal keywords."""
    if not headlines:
        return False
    keywords = (
        "earnings", "guidance", "lawsuit", "acquisition", "merger",
        "downgrade", "upgrade", "halts", "investigation", "recall",
        "ceo", "fraud", "bankruptcy", "split", "dividend cut",
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    for h in headlines:
        ts = h.get("published_at") or ""
        try:
            published = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published < cutoff:
            continue
        title = (h.get("title") or "").lower()
        if any(k in title for k in keywords):
            return True
    return False
