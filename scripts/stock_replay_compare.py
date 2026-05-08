#!/usr/bin/env python3
"""Run analyze_company for a list of tickers and print a before/after table.

`Before` = LLM raw score adjusted only by the legacy hard caps + legacy
additive penalties (the rules that already existed at commit 698b770).
`After`  = the score that comes out of the upgraded scoring layer.

This is a coherence check, not a regression test — the goal is to spot
unexpected swings introduced by the new signal sources.
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
log = logging.getLogger("stock_replay_compare")


# Legacy constants — exactly what existed at HEAD before this patch.
_LEGACY_HARD_CAPS = {
    "ROIC_BELOW_WACC":          50,
    "HIGH_LEVERAGE":            55,
    "MOMENTUM_DIVERGENCE":      60,
    "PRICED_FOR_PERFECTION":    65,
}
_LEGACY_PENALTIES = {
    "INSIDER_SELLING":          15,
    "GUIDANCE_CUT":             20,
    "MARGIN_DETERIORATION":      8,
    "NEGATIVE_FCF":              5,
}


def _legacy_score(verdict_score: int, all_flags: list[str]) -> tuple[int, list[str]]:
    """Apply only the legacy rules to an LLM score and a flag list."""
    legacy_flags = [f for f in all_flags if f in _LEGACY_HARD_CAPS or f in _LEGACY_PENALTIES
                    or f in {"SUSPICIOUS_MEGACAP_UNDERVALUATION", "INTRINSIC_FAR_FROM_ANCHOR"}]
    score = max(0, verdict_score - sum(_LEGACY_PENALTIES.get(f, 0) for f in legacy_flags))
    cap_flags = [f for f in legacy_flags if f in _LEGACY_HARD_CAPS]
    if cap_flags:
        cap = min(_LEGACY_HARD_CAPS[f] for f in cap_flags)
        score = min(score, cap)
    return score, legacy_flags


async def replay(tickers: list[tuple[str, str]]) -> list[dict]:
    client, model = stock_analyzer.make_gemini_client()
    macro = await macro_context.fetch_macro_context()
    log.info("Macro: %s", macro_context.format_macro_summary(macro))

    rows: list[dict] = []
    for ticker, name in tickers:
        try:
            analysis = await stock_analyzer.analyze_company(
                ticker, name,
                gemini_client=client, gemini_model=model, macro=macro,
            )
        except Exception as e:
            log.exception("analyze_company failed for %s: %s", ticker, e)
            continue
        if analysis is None:
            log.warning("Skipping %s — analyzer returned None", ticker)
            continue
        try:
            raw_payload = json.loads(analysis.get("raw_payload") or "{}")
        except json.JSONDecodeError:
            raw_payload = {}

        llm_score = int(raw_payload.get("score") or 0)
        all_flags = analysis.get("flags") or []
        legacy_score, legacy_flags = _legacy_score(llm_score, all_flags)

        new_flags = [f for f in all_flags if f not in legacy_flags]

        rows.append({
            "ticker":         ticker,
            "name":           name,
            "price":          analysis.get("price"),
            "llm_score":      llm_score,
            "legacy_score":   legacy_score,
            "new_score":      analysis.get("score"),
            "delta":          (analysis.get("score") or 0) - legacy_score,
            "legacy_rec":     raw_payload.get("recommendation") or "ESPERAR",
            "new_rec":        analysis.get("recommendation"),
            "confidence":     analysis.get("confidence"),
            "new_flags":      new_flags,
            "all_flags":      all_flags,
            "scoring_detail": analysis.get("scoring_detail"),
            "competitors":    (analysis.get("competitors") or {}).get("flags"),
            "cycle":          (analysis.get("cycle") or {}).get("cyclicality"),
            "phase_fit":      (analysis.get("cycle") or {}).get("phase_fit"),
            "data_cross":     (analysis.get("data_cross") or {}).get("flags"),
            "options_flags":  (analysis.get("options") or {}).get("flags"),
            "catalysts":      (analysis.get("catalysts") or {}).get("flags"),
        })
        await asyncio.sleep(13)
    return rows


def render_table(rows: list[dict]) -> str:
    headers = ["Ticker", "LLM", "Legacy", "Nuevo", "Δ", "Rec", "Conf", "Phase fit", "New flags"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for r in rows:
        lines.append(
            "| {ticker} | {llm} | {legacy} | {new} | {delta:+d} | {rec} | {conf} | {pf} | {flags} |".format(
                ticker=r["ticker"],
                llm=r["llm_score"],
                legacy=r["legacy_score"],
                new=r["new_score"],
                delta=r["delta"],
                rec=r["new_rec"],
                conf=r["confidence"],
                pf=r.get("phase_fit"),
                flags=", ".join(r["new_flags"]) or "—",
            )
        )
    return "\n".join(lines)


async def main() -> int:
    cohort = [
        ("NVDA",  "Nvidia"),
        ("BRK-B", "Berkshire Hathaway"),
        ("AMD",   "AMD"),
        ("PG",    "Procter & Gamble"),
    ]
    rows = await replay(cohort)
    print("\n=== REPLAY COMPARISON ===\n")
    print(render_table(rows))
    print()
    out_path = ROOT / "data" / "replay_compare.json"
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
    print(f"Detalle JSON: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
