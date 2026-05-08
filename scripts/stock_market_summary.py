#!/usr/bin/env python3
"""08:00 Madrid daily market snapshot — sent to Telegram.

Pulls quotes for S&P 500, Nasdaq 100, VIX, DXY, US 10y yield. Builds 5-line
summary: levels + day change + 1 frase de contexto generada por Gemini.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from modules import stock_alerts, macro_context  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "data" / "stock_market_summary.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("stock_market_summary")

# yfinance symbols and a friendly label.
_INSTRUMENTS = [
    ("S&P 500",   "^GSPC",  "%.2f"),
    ("Nasdaq",    "^IXIC",  "%.2f"),
    ("Russell 2000", "^RUT", "%.2f"),
    ("VIX",       "^VIX",   "%.2f"),
    ("DXY",       "DX-Y.NYB", "%.2f"),
    ("UST 10y",   "^TNX",   "%.3f"),
]


def _fetch_quotes() -> list[dict]:
    """Pull last 2 daily closes per instrument; compute change %."""
    try:
        import yfinance as yf
    except ImportError:
        return []

    out: list[dict] = []
    for label, symbol, fmt in _INSTRUMENTS:
        try:
            hist = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
            if hist is None or hist.empty or len(hist) < 2:
                continue
            closes = hist["Close"].dropna()
            last  = float(closes.iloc[-1])
            prev  = float(closes.iloc[-2])
            change_pct = (last / prev - 1.0) * 100.0 if prev > 0 else 0.0
            out.append({"label": label, "symbol": symbol, "price": last,
                        "change_pct": change_pct, "fmt": fmt})
        except Exception as e:
            log.warning("failed to fetch %s: %s", symbol, e)
    return out


async def _gemini_blurb(quotes: list[dict], macro: dict) -> Optional[str]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=api_key)

    quote_lines = "\n".join(
        f"- {q['label']}: {q['fmt'] % q['price']} ({q['change_pct']:+.2f}%)"
        for q in quotes
    )
    macro_line = macro_context.format_macro_summary(macro)
    prompt = (
        "Genera UNA frase en español (≤200 caracteres) describiendo el contexto "
        "macro/bursátil actual. Sé directo, sin disclaimers, sin emojis. Datos:\n\n"
        f"{quote_lines}\n\nMacro: {macro_line}"
    )
    try:
        resp = await client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.3),
        )
        text = (resp.text or "").strip()
        return text[:300] if text else None
    except Exception as e:
        log.warning("Gemini blurb failed: %s", e)
        return None


def _format_message(quotes: list[dict], macro: dict, blurb: Optional[str]) -> str:
    today = datetime.now().strftime("%a %d %b %Y")
    lines = [f"☀️ Mercado al abrir — {today}"]
    for q in quotes:
        lines.append(f"  {q['label']}: {q['fmt'] % q['price']} ({q['change_pct']:+.2f}%)")
    phase = macro.get("cycle_phase", "?")
    bias  = macro.get("valuation_bias", "?")
    lines.append(f"  Macro: ciclo {phase}, sesgo {bias}")
    if blurb:
        lines.append("")
        lines.append(blurb)
    return "\n".join(lines)


async def main() -> int:
    quotes = await asyncio.to_thread(_fetch_quotes)
    if not quotes:
        log.error("No quotes available — aborting")
        return 1
    macro = await macro_context.fetch_macro_context()
    blurb = await _gemini_blurb(quotes, macro)
    msg = _format_message(quotes, macro, blurb)
    log.info("Message:\n%s", msg)
    sent = await stock_alerts.send_telegram(msg)
    log.info("Daily market summary sent=%s", sent)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
