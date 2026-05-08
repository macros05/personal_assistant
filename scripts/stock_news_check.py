#!/usr/bin/env python3
"""News-only sweep. Runs every 4 hours during market hours via cron.

Pulls news for each watchlist company. If a breaking-news signal is detected,
re-runs full analysis for that company only and may send an alert.
"""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

import httpx  # noqa: E402

from modules import stock_alerts, stock_analyzer, stock_news  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "data" / "stock_news_check.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("stock_news_check")


async def main() -> int:
    watchlist = stock_analyzer.load_watchlist()
    if not watchlist:
        log.error("Empty watchlist — aborting")
        return 1

    await stock_alerts.init_db()
    client, model = stock_analyzer.make_gemini_client()

    breaking: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as http:
        for entry in watchlist:
            try:
                items = await stock_news.fetch_news(entry["name"], limit=5, hours_back=6, client=http)
            except Exception as e:
                log.warning("News fetch failed for %s: %s", entry["ticker"], e)
                continue
            if stock_news.detect_breaking(items):
                log.info("Breaking news for %s: %s", entry["ticker"], items[0].get("title", ""))
                breaking.append(entry)

    log.info("News scan done — breaking signals: %d", len(breaking))

    sent = 0
    for entry in breaking:
        try:
            analysis = await stock_analyzer.analyze_company(
                entry["ticker"], entry["name"],
                gemini_client=client,
                gemini_model=model,
            )
        except Exception as e:
            log.exception("Re-analysis failed for %s: %s", entry["ticker"], e)
            continue

        if analysis is None:
            continue

        await stock_alerts.log_analysis(analysis)
        if await stock_alerts.maybe_alert(analysis):
            sent += 1

        await asyncio.sleep(13)

    log.info("News-check run done: re-analyzed=%d alerts_sent=%d", len(breaking), sent)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
