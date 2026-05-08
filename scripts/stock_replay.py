#!/usr/bin/env python3
"""Run the analyzer once for a given ticker, print the verdict, do NOT alert.

Usage: stock_replay.py TICKER "Display Name"
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from modules import stock_analyzer, macro_context  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("stock_replay")


async def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: stock_replay.py TICKER [\"Display Name\"]")
        return 1
    ticker = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else ticker

    client, model = stock_analyzer.make_gemini_client()
    macro = await macro_context.fetch_macro_context()
    log.info("Macro: %s", macro_context.format_macro_summary(macro))

    analysis = await stock_analyzer.analyze_company(
        ticker, name, gemini_client=client, gemini_model=model, macro=macro,
    )
    if analysis is None:
        print("Analysis returned None — aborting")
        return 2

    summary = {
        "ticker":           analysis["ticker"],
        "name":             analysis["name"],
        "price":            analysis["price"],
        "intrinsic_value":  analysis["intrinsic_value"],
        "intrinsic_method": analysis["intrinsic_method"],
        "margin_of_safety": analysis["margin_of_safety"],
        "score":            analysis["score"],
        "recommendation":   analysis["recommendation"],
        "confidence":       analysis["confidence"],
        "flags":            analysis["flags"],
        "scoring_detail":   analysis.get("scoring_detail"),
        "data_quality":     analysis["data_quality"],
        "dcf_baseline":     analysis["dcf_baseline"],
        "dcf_multiplier":   analysis["dcf_multiplier"],
        "graham_number":    analysis["graham_number"],
        "reason":           analysis["reason"],
        "peer_context":     analysis.get("peer_context"),
        "catalyst":         analysis.get("catalyst"),
        "momentum_summary": {
            "trend":         analysis["momentum"].get("trend"),
            "sma200_ratio":  analysis["momentum"].get("sma200_ratio"),
            "change_90d":    analysis["momentum"].get("change_90d"),
            "high_52w":      analysis["momentum"].get("high_52w"),
            "flags":         analysis["momentum"].get("flags"),
        },
        "quality_summary": {
            "fcf_yield":      analysis["quality"].get("fcf_yield"),
            "debt_to_ebitda": analysis["quality"].get("debt_to_ebitda"),
            "roic":           analysis["quality"].get("roic"),
            "wacc":           analysis["quality"].get("wacc"),
            "margin_trend":   analysis["quality"].get("margin_trend"),
            "leverage_tolerant_sector": analysis["quality"].get("leverage_tolerant_sector"),
            "flags":          analysis["quality"].get("flags"),
        },
        "early_warning_summary": {
            "insider_sell_pct_float": analysis["early_warning"].get("insider_sell_pct_float"),
            "short_pct_float":        analysis["early_warning"].get("short_pct_float"),
            "guidance_cut_count":     analysis["early_warning"].get("guidance_cut_count"),
            "flags":                  analysis["early_warning"].get("flags"),
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
