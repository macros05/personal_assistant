"""Dynamic watchlist management.

Wraps `data/watchlist.json` with rules-based auto-add / auto-remove + manual
commands (`/watchlist add TICKER`, `/watchlist remove TICKER`).

Auto-add rules (any one is enough):
  • a ticker shows up in ≥3 analyses with INSIDER_BUYING in the last 30 days
  • a ticker's score improved ≥20 points vs. its previous analysis

Auto-remove rule:
  • the ticker has been in EVITAR for ≥90 days with no fundamentals delta

The module is conservative — it never silently writes; the daily script calls
`auto_evolve()` which returns the proposed changes plus a Telegram message to
send so the user always sees what changed.
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

log = logging.getLogger("modules.watchlist")

_WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
_DB_PATH        = Path(__file__).resolve().parent.parent / "data" / "stock_alerts_log.db"

_AUTO_ADD_INSIDER_BUY_COUNT = 3
_AUTO_ADD_SCORE_DELTA_POINTS = 20
_AUTO_REMOVE_EVITAR_DAYS = 90


# ── Read / write watchlist file ──────────────────────────────────────────────

def _load_raw() -> dict:
    if not _WATCHLIST_PATH.exists():
        return {"us": [], "eu": []}
    try:
        return json.loads(_WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("Cannot read watchlist: %s", e)
        return {"us": [], "eu": []}


def _save_raw(data: dict) -> None:
    _WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the trailing comment line and pretty-print so manual edits remain
    # reasonable.
    data.setdefault("_comment",
                    "Edit this file to add/remove companies. Restart not required - re-read on each run.")
    _WATCHLIST_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def list_tickers() -> list[dict]:
    """Flat list of {ticker, name, region}."""
    raw = _load_raw()
    out: list[dict] = []
    for region in ("us", "eu"):
        for entry in raw.get(region, []) or []:
            tk = entry.get("ticker")
            if tk:
                out.append({"ticker": tk, "name": entry.get("name") or tk, "region": region})
    return out


def _region_for(ticker: str) -> str:
    """Best-effort regional bucketing — '.PA'/'.SW'/'.AS' style → eu, else us."""
    t = (ticker or "").upper()
    return "eu" if "." in t else "us"


def add_ticker(ticker: str, name: Optional[str] = None,
               region: Optional[str] = None) -> dict:
    """Add ticker to watchlist if not already present. Returns {added, region, name}."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"added": False, "reason": "empty ticker"}
    raw = _load_raw()
    region = region or _region_for(ticker)
    bucket = raw.setdefault(region, [])
    if any((entry.get("ticker") or "").upper() == ticker for entry in bucket):
        return {"added": False, "reason": "already present", "region": region}
    bucket.append({"ticker": ticker, "name": name or ticker})
    _save_raw(raw)
    log.info("watchlist: added %s to %s", ticker, region)
    return {"added": True, "region": region, "name": name or ticker}


def remove_ticker(ticker: str) -> dict:
    """Remove ticker from any region in the watchlist. Returns {removed, region}."""
    ticker = (ticker or "").upper().strip()
    raw = _load_raw()
    for region in ("us", "eu"):
        bucket = raw.get(region) or []
        new_bucket = [e for e in bucket if (e.get("ticker") or "").upper() != ticker]
        if len(new_bucket) != len(bucket):
            raw[region] = new_bucket
            _save_raw(raw)
            log.info("watchlist: removed %s from %s", ticker, region)
            return {"removed": True, "region": region}
    return {"removed": False, "reason": "not found"}


# ── Auto-evolve rules ────────────────────────────────────────────────────────

async def _recent_insider_buying_tickers() -> list[str]:
    """Tickers that appear in ≥N analyses with INSIDER_BUYING in last 30 days."""
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()
    if not _DB_PATH.exists():
        return []
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute("""
            SELECT ticker, COUNT(*) AS c
            FROM analyses
            WHERE created_at >= ? AND flags LIKE '%INSIDER_BUYING%'
            GROUP BY ticker
            HAVING c >= ?
        """, (cutoff, _AUTO_ADD_INSIDER_BUY_COUNT)) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def _score_jumpers() -> list[tuple[str, int, int]]:
    """Tickers whose latest score improved by ≥THRESHOLD vs the previous analysis.

    Returns list of (ticker, previous_score, current_score).
    """
    if not _DB_PATH.exists():
        return []
    out: list[tuple[str, int, int]] = []
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute("""
            SELECT DISTINCT ticker FROM analyses
            WHERE created_at >= datetime('now', '-30 days')
        """) as cur:
            tickers = [r[0] for r in await cur.fetchall()]

        for tk in tickers:
            async with db.execute("""
                SELECT score FROM analyses
                WHERE ticker = ?
                ORDER BY created_at DESC
                LIMIT 2
            """, (tk,)) as cur:
                rows = await cur.fetchall()
            if len(rows) < 2:
                continue
            current = rows[0][0] or 0
            previous = rows[1][0] or 0
            if current - previous >= _AUTO_ADD_SCORE_DELTA_POINTS:
                out.append((tk, previous, current))
    return out


async def _stale_evitar_tickers() -> list[str]:
    """Tickers in EVITAR for ≥90 days without a recommendation change."""
    if not _DB_PATH.exists():
        return []
    cutoff = (datetime.utcnow() - timedelta(days=_AUTO_REMOVE_EVITAR_DAYS)).isoformat()
    out: list[str] = []
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute("""
            SELECT ticker, MIN(created_at) AS first_seen
            FROM analyses
            WHERE recommendation = 'EVITAR'
            GROUP BY ticker
        """) as cur:
            evitar_rows = await cur.fetchall()

        for ticker, first_seen in evitar_rows:
            if first_seen and first_seen <= cutoff:
                # Confirm the most recent analysis is also EVITAR — otherwise the
                # name has improved and shouldn't be auto-removed.
                async with db.execute("""
                    SELECT recommendation FROM analyses
                    WHERE ticker = ? ORDER BY created_at DESC LIMIT 1
                """, (ticker,)) as cur:
                    last = await cur.fetchone()
                if last and (last[0] or "").upper() == "EVITAR":
                    out.append(ticker)
    return out


async def auto_evolve(*, dry_run: bool = False) -> dict:
    """Run all auto rules and apply (unless dry_run). Return a summary."""
    insider_candidates = await _recent_insider_buying_tickers()
    score_jumps        = await _score_jumpers()
    stale_evitar       = await _stale_evitar_tickers()

    current = {t["ticker"].upper() for t in list_tickers()}

    add_set: dict[str, str] = {}
    for tk in insider_candidates:
        if tk.upper() not in current:
            add_set[tk.upper()] = "insiders comprando en últimos 30d"
    for tk, prev, cur_score in score_jumps:
        if tk.upper() not in current:
            add_set[tk.upper()] = f"score subió {cur_score - prev:+d}pts ({prev}→{cur_score})"

    remove_set: dict[str, str] = {}
    for tk in stale_evitar:
        if tk.upper() in current:
            remove_set[tk.upper()] = f"EVITAR ≥{_AUTO_REMOVE_EVITAR_DAYS}d sin cambios"

    if not dry_run:
        for tk, _reason in add_set.items():
            add_ticker(tk)
        for tk in remove_set.keys():
            remove_ticker(tk)

    return {
        "added":   add_set,
        "removed": remove_set,
        "dry_run": dry_run,
    }


def format_evolve_summary(result: dict) -> str:
    """Telegram-ready summary of auto-evolve changes."""
    added = result.get("added") or {}
    removed = result.get("removed") or {}
    if not added and not removed:
        return "🔄 Watchlist sin cambios automáticos esta vez."
    lines = ["🔄 Watchlist actualizada"]
    if added:
        lines.append("➕ Añadidas:")
        for tk, reason in added.items():
            lines.append(f"  • {tk} — {reason}")
    if removed:
        lines.append("➖ Quitadas:")
        for tk, reason in removed.items():
            lines.append(f"  • {tk} — {reason}")
    return "\n".join(lines)
