"""Synthetic tests for the Polymarket read-only tools.

No external dependencies (no pytest, no network). Run with:

    python -m tests.test_polymarket_tool

Each test builds a throwaway sqlite DB mirroring the copytrade schema subset
the tools actually query, then exercises `execute()` via asyncio.run.
"""
import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.polymarket import (  # noqa: E402
    GetPolymarketStatusTool,
    GetPolymarketTradesTool,
    GetPolymarketTopWalletsTool,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_question TEXT, outcome TEXT, side TEXT NOT NULL,
    entry_price REAL NOT NULL, exit_price REAL, pnl_usdc REAL,
    status TEXT NOT NULL DEFAULT 'OPEN', opened_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE wallets (
    address TEXT PRIMARY KEY, score REAL NOT NULL DEFAULT 0,
    pnl_usdc REAL NOT NULL DEFAULT 0, win_rate REAL NOT NULL DEFAULT 0,
    trades_count INTEGER NOT NULL DEFAULT 0, is_top INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT
);
CREATE TABLE arb_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, title TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN', realized_pnl REAL
);
CREATE TABLE arb_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE arb_scan_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    events_scanned INTEGER NOT NULL DEFAULT 0, min_sum_asks REAL,
    median_sum_asks REAL, best_event_title TEXT
);
"""


def _make_db(tmpdir: str) -> str:
    """Create a synthetic copytrade DB and return its path."""
    path = str(Path(tmpdir) / "copytrade.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        [
            ("paper_bankroll", "988.16"),
            ("paused", "1"),
            ("last_poll_ts", "2026-06-11T11:45:51+00:00"),
            ("arb_paper_bankroll", "500.0"),
            ("arb_paper_bankroll_initial", "500.0"),
        ],
    )
    conn.executemany(
        "INSERT INTO paper_trades "
        "(market_question, outcome, side, entry_price, exit_price, pnl_usdc, status, opened_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("Will BTC hit 200k in 2026?", "Yes", "BUY", 0.40, 0.35, -1.25, "CLOSED", "2026-06-01", "2026-06-02"),
            ("Will ETH flip BTC?", "No", "BUY", 0.80, None, None, "OPEN", "2026-06-10", None),
            ("X" * 200, "Yes", "BUY", 0.50, 0.60, 2.0, "CLOSED", "2026-06-03", "2026-06-04"),
        ],
    )
    conn.executemany(
        "INSERT INTO wallets (address, score, pnl_usdc, win_rate, trades_count, is_top, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("0xaaa", 0.9, 1500.0, 0.66, 120, 1, "2026-06-10"),
            ("0xbbb", 0.5, 200.0, 0.55, 40, 0, "2026-06-09"),
            ("0xccc", 0.2, -50.0, 0.40, 10, 0, "2026-06-01"),
        ],
    )
    conn.executemany(
        "INSERT INTO arb_sets (event_id, title, status, realized_pnl) VALUES (?, ?, ?, ?)",
        [
            ("ev1", "Election A", "RESOLVED", 3.5),
            ("ev2", "Election B", "OPEN", None),
        ],
    )
    conn.execute("INSERT INTO arb_opportunities (event_id, ts) VALUES ('ev1', '2026-06-11')")
    conn.execute(
        "INSERT INTO arb_scan_cycles (ts, events_scanned, min_sum_asks, median_sum_asks, best_event_title) "
        "VALUES ('2026-06-11T11:44:57', 25, 0.999, 1.029, 'Balance of Power')"
    )
    conn.commit()
    conn.close()
    return path


# ── Tests ────────────────────────────────────────────────────────────────────

def test_status_happy_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        lock = str(Path(tmp) / "paused.lock")
        Path(lock).touch()
        tool = GetPolymarketStatusTool(db_path=db, paused_lock=lock)
        out = asyncio.run(tool.execute())
        ct, arb = out["copytrade"], out["arb"]
        assert ct["paper_bankroll_usdc"] == 988.16, ct
        assert ct["open_positions"] == 1, ct
        assert ct["total_trades"] == 3, ct
        assert abs(ct["realized_pnl_usdc"] - 0.75) < 1e-9, ct
        assert ct["paused"] is True and ct["paused_lock_present"] is True, ct
        assert arb["paper_bankroll_usdc"] == 500.0, arb
        assert arb["open_sets"] == 1 and arb["resolved_sets"] == 1, arb
        assert arb["realized_pnl_usdc"] == 3.5, arb
        assert arb["opportunities_count"] == 1, arb
        assert arb["last_scan_cycle"]["events_scanned"] == 25, arb
        assert arb["last_scan_cycle"]["best_event_title"] == "Balance of Power", arb


def test_status_no_paused_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        tool = GetPolymarketStatusTool(db_path=db, paused_lock=str(Path(tmp) / "paused.lock"))
        out = asyncio.run(tool.execute())
        assert out["copytrade"]["paused_lock_present"] is False, out


def test_trades_limit_and_shortening() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        tool = GetPolymarketTradesTool(db_path=db)
        out = asyncio.run(tool.execute(limit=2))
        assert out["count"] == 2, out
        # Most recent first; long question shortened to <= 80 chars
        assert len(out["trades"][0]["market"]) <= 80, out["trades"][0]
        assert out["trades"][0]["market"].endswith("…"), out["trades"][0]
        assert out["trades"][1]["status"] == "OPEN", out["trades"][1]


def test_top_wallets_sorted_by_score() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        tool = GetPolymarketTopWalletsTool(db_path=db)
        out = asyncio.run(tool.execute(limit=2))
        assert out["count"] == 2, out
        assert out["wallets"][0]["address"] == "0xaaa", out
        assert out["wallets"][1]["address"] == "0xbbb", out


def test_missing_db_returns_error_dict() -> None:
    missing = "/nonexistent/dir/copytrade.db"
    for tool in (
        GetPolymarketStatusTool(db_path=missing, paused_lock="/nonexistent/paused.lock"),
        GetPolymarketTradesTool(db_path=missing),
        GetPolymarketTopWalletsTool(db_path=missing),
    ):
        out = asyncio.run(tool.execute())
        assert out.get("error") == "polymarket_data_unavailable", (tool.name, out)


def test_read_only_connection_cannot_write() -> None:
    from tools.polymarket import _connect
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        conn = _connect(db)
        try:
            try:
                conn.execute("INSERT INTO metadata (key, value) VALUES ('x', 'y')")
                raise AssertionError("write succeeded on a mode=ro connection")
            except sqlite3.OperationalError:
                pass  # expected: attempt to write a readonly database
        finally:
            conn.close()


def test_registry_exposes_polymarket_tools() -> None:
    from tools.registry import get_tool
    for name in ("polymarket_status", "polymarket_trades", "polymarket_top_wallets"):
        assert get_tool(name) is not None, f"{name} not registered"


# ── Runner ───────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [obj for name, obj in globals().items()
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {t.__name__}: unexpected {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
