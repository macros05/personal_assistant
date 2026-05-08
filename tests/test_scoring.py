"""Synthetic tests for the multiplicative scoring + flag rules.

No external dependencies (no pytest, no network). Run with:

    python -m tests.test_scoring

Each test is a small function returning None on pass and raising AssertionError
on failure. Failures abort with a message naming the case.

We exercise the *scoring* layer in isolation by calling
`stock_analyzer._apply_advanced_scoring` directly with synthetic sub-signal
dicts. This avoids any LLM / yfinance / network dependency, which is what we
want for a unit-test surface.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import stock_analyzer  # noqa: E402
from modules import competitors as competitors_mod  # noqa: E402
from modules import business_cycle as cycle_mod  # noqa: E402
from modules import catalysts as catalysts_mod  # noqa: E402
from modules import data_sources as data_sources_mod  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _verdict(score: int = 75, rec: str = "COMPRAR", confidence: str = "HIGH",
             flags: list[str] | None = None) -> dict:
    return {
        "score": score,
        "recommendation": rec,
        "opportunity": rec == "COMPRAR",
        "confidence": confidence,
        "flags": flags or [],
        "intrinsic_value": 100.0,
        "intrinsic_method": "DCF EPS×15",
        "margin_of_safety": 25.0,
        "reason": "synthetic",
        "peer_context": "",
        "data_quality_notes": "",
    }


def _empty(flags: list[str] | None = None) -> dict:
    return {"available": True, "flags": flags or []}


# ── Test cases ───────────────────────────────────────────────────────────────

def test_clean_signal_keeps_score():
    """No flags + clean signals → score barely moves (only phase fit)."""
    v = _verdict(score=75, flags=[])
    cycle = cycle_mod.evaluate("MSFT", {"sector": "Technology"}, {"cycle_phase": "mid_cycle"})
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(), quality=_empty(), early_warning=_empty(),
        fund={}, competitors=_empty(), cycle=cycle, catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    # MSFT (neutral) at mid_cycle → phase_fit = 0.
    assert out["score"] == 75, f"expected 75, got {out['score']} flags={out['flags']}"
    assert out["recommendation"] == "COMPRAR"


def test_high_leverage_caps_to_55():
    v = _verdict(score=82)
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(),
        quality=_empty(["HIGH_LEVERAGE"]),
        early_warning=_empty(), fund={},
        competitors=_empty(), cycle=_empty(), catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    assert out["score"] == 55, f"score={out['score']}"
    assert out["recommendation"] == "ESPERAR"
    assert "HIGH_LEVERAGE" in out["flags"]


def test_debt_stress_caps_to_50():
    v = _verdict(score=80)
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(),
        quality=_empty(["DEBT_STRESS"]),
        early_warning=_empty(), fund={},
        competitors=_empty(), cycle=_empty(), catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    assert out["score"] == 50
    assert out["recommendation"] == "ESPERAR"


def test_debt_stress_with_guidance_cut_downgrades_to_evitar():
    v = _verdict(score=70, rec="ESPERAR")
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(),
        quality=_empty(["DEBT_STRESS"]),
        early_warning=_empty(["GUIDANCE_CUT"]),
        fund={},
        competitors=_empty(), cycle=_empty(), catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    assert out["recommendation"] == "EVITAR", out["recommendation"]


def test_relative_value_bonus_applied():
    v = _verdict(score=72)
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(), quality=_empty(), early_warning=_empty(), fund={},
        competitors=_empty(["RELATIVE_VALUE_OPPORTUNITY"]),
        cycle=_empty(), catalysts=_empty(), options=_empty(), data_cross=_empty(),
    )
    # +8 bonus, no penalties → 80 (clamped at 100).
    assert out["score"] == 80, out["score"]
    assert "RELATIVE_VALUE_OPPORTUNITY" in out["flags"]


def test_premium_vs_peers_caps_to_70():
    v = _verdict(score=85)
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(), quality=_empty(), early_warning=_empty(), fund={},
        competitors=_empty(["PREMIUM_VS_PEERS"]),
        cycle=_empty(), catalysts=_empty(), options=_empty(), data_cross=_empty(),
    )
    assert out["score"] == 70
    assert out["recommendation"] == "ESPERAR"


def test_data_discrepancy_caps_and_drops_confidence():
    v = _verdict(score=82, confidence="HIGH")
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(), quality=_empty(), early_warning=_empty(), fund={},
        competitors=_empty(), cycle=_empty(), catalysts=_empty(),
        options=_empty(),
        data_cross=_empty(["DATA_DISCREPANCY"]),
    )
    assert out["score"] == 70
    assert out["confidence"] == "MEDIUM", out["confidence"]


def test_cycle_phase_fit_adds_to_score():
    """KO (defensive) in contraction → +10 phase fit + DEFENSIVE_TAILWIND bonus +4."""
    v = _verdict(score=70)
    cycle = cycle_mod.evaluate("KO", {"sector": "Consumer Defensive"}, {"cycle_phase": "contraction"})
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(), quality=_empty(), early_warning=_empty(), fund={},
        competitors=_empty(), cycle=cycle, catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    assert "DEFENSIVE_TAILWIND" in out["flags"]
    # Allow a wide window — exact value depends on phase_fit constants.
    assert 80 <= out["score"] <= 100, out["score"]


def test_cyclical_against_phase_penalised():
    """NVDA (cyclical) in recession → CYCLICAL_VS_DOWN_PHASE penalty + phase_fit -12."""
    v = _verdict(score=80)
    cycle = cycle_mod.evaluate("NVDA", {"sector": "Technology"}, {"cycle_phase": "recession"})
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(), quality=_empty(), early_warning=_empty(), fund={},
        competitors=_empty(), cycle=cycle, catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    assert "CYCLICAL_VS_DOWN_PHASE" in out["flags"]
    assert out["score"] < 70, out["score"]


def test_dilution_penalty():
    v = _verdict(score=75)
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(),
        quality=_empty(["SHAREHOLDER_DILUTION"]),
        early_warning=_empty(), fund={},
        competitors=_empty(), cycle=_empty(), catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    assert out["score"] == 65, out["score"]


def test_buyback_bonus_with_phase_neutral():
    v = _verdict(score=70)
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(),
        quality=_empty(["SHARE_BUYBACK"]),
        early_warning=_empty(), fund={},
        competitors=_empty(), cycle=_empty(), catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    assert out["score"] == 75


def test_stacked_negative_caps_dont_double():
    """Multiple cap flags → the LOWEST cap wins, not their sum."""
    v = _verdict(score=90)
    out = stock_analyzer._apply_advanced_scoring(
        v, momentum=_empty(["MOMENTUM_DIVERGENCE"]),
        quality=_empty(["HIGH_LEVERAGE"]),
        early_warning=_empty(), fund={},
        competitors=_empty(), cycle=_empty(), catalysts=_empty(),
        options=_empty(), data_cross=_empty(),
    )
    assert out["score"] == 55, out["score"]


def test_competitor_module_handles_missing_peers():
    """compute_competitor_comparison should never raise on missing data."""
    out = competitors_mod.compute_competitor_comparison("UNKNOWN_TICKER", {})
    assert out["available"] is False
    assert out["flags"] == []


def test_cycle_module_classification():
    assert cycle_mod.classify("NVDA", "Technology") == "cyclical"
    assert cycle_mod.classify("KO",   "Consumer Defensive") == "defensive"
    assert cycle_mod.classify("AAPL", "Technology") == "neutral"


def test_catalyst_detector_picks_up_ma_headline():
    headlines = [
        {"title": "Microsoft to acquire ZeniMax in $7.5 billion deal",
         "source": "WSJ", "published_at": "2026-04-30T10:00:00Z"},
    ]
    out = catalysts_mod.detect_catalysts("MSFT", headlines)
    kinds = {c["kind"] for c in out["headline_catalysts"]}
    assert "M&A" in kinds, kinds
    assert "HARD_CATALYST" in out["flags"]


def test_data_discrepancy_detected():
    yf  = {"pe_ratio": 25.0}
    av  = {"PERatio": "20.0"}
    fv  = {"pe_ratio": 24.0}
    out = data_sources_mod.cross_validate(yf, av, fv)
    assert "DATA_DISCREPANCY" in out["flags"], out
    assert out["max_gap"]["metric"] == "pe_ratio"


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
