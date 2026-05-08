#!/usr/bin/env python3
"""Daily full-watchlist analysis. Runs 07:00 Mon–Fri via cron.

Skips market price on weekends (still updates fundamentals + news).
On any per-company API failure, the company is skipped silently and logged.
"""
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from modules import stock_alerts, stock_analyzer  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "data" / "stock_daily.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("stock_daily")


async def main() -> int:
    weekend = datetime.utcnow().weekday() >= 5
    watchlist = stock_analyzer.load_watchlist()
    if not watchlist:
        log.error("Empty watchlist — aborting")
        return 1

    await stock_alerts.init_db()

    client, model = stock_analyzer.make_gemini_client()

    sent = 0
    analyzed = 0
    log.info("Starting daily analysis: %d companies (weekend=%s)", len(watchlist), weekend)

    for entry in watchlist:
        ticker, name = entry["ticker"], entry["name"]
        try:
            analysis = await stock_analyzer.analyze_company(
                ticker, name,
                gemini_client=client,
                gemini_model=model,
                weekend_skip_market=weekend,
            )
        except Exception as e:
            log.exception("Unexpected failure analysing %s: %s", ticker, e)
            continue

        if analysis is None:
            log.info("Skipped %s — analysis unavailable", ticker)
            continue

        analyzed += 1
        await stock_alerts.log_analysis(analysis)

        if await stock_alerts.maybe_alert(analysis):
            sent += 1

        # Alpha Vantage free tier: 5 req/min — small breather between companies
        await asyncio.sleep(13)

    log.info("Daily run done: analyzed=%d alerts_sent=%d", analyzed, sent)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
