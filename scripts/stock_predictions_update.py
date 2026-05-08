#!/usr/bin/env python3
"""Daily backfill of price_30d / price_90d for past predictions."""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from modules import predictions  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "data" / "stock_predictions_update.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("stock_predictions_update")


async def main() -> int:
    result = await predictions.update_returns()
    log.info("Done: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
