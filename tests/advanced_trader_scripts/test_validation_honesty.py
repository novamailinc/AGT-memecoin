"""Regression tests for the validation honesty gate — run:
python3 tests/advanced_trader_scripts/test_validation_honesty.py

Covers the fix for the upstream harness's incoherent fixture: the edge
gate was fed preloaded stats its own exit mechanics could not produce,
so the backtest "passed" while realizing negative expectancy. The fix
calibrates lanes with replayed closes and adds check_expectancy_honesty.
These tests pin the gate's semantics: it must BITE on incoherent
fixtures and stay quiet on coherent ones."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from advanced_trader import validation


def _result(entries, calibration, lanes, admitted_lanes, matched_replay,
            closes=None, admitted_closes=None, expectancy_admitted=None):
    if closes is None:
        closes = sum(entries.values())
    if admitted_closes is None:
        admitted_closes = sum(entries.values())
    if expectancy_admitted is None and admitted_closes:
        expectancy_admitted = 1.0  # coherent positive unless overridden
    return {
        "entries": entries,
        "probe_entries": 0,
        "calibration": calibration,
        "lanes": lanes,
        "admitted_lanes": admitted_lanes,
        "matched_replay": matched_replay,
        "closes": closes,
        "admitted_closes": admitted_closes,
        "expectancy_admitted": expectancy_admitted,
    }


def test_negative_calibration_lane_with_entries_bites():
    r = _result(
        entries={"bad": 3},
        calibration={"bad": {"expectancy": -0.5}},
        lanes={"bad": {"drift": -0.01, "vol": 0.12}},
        admitted_lanes=[],
        matched_replay={},
    )
    failures = validation.check_expectancy_honesty(r)
    assert failures, "gate must fail when a negative-expectancy lane traded"
    assert "bad" in failures[0]
    print("PASS test_negative_calibration_lane_with_entries_bites")


def test_mechanics_negative_lane_admitted_bites():
    r = _result(
        entries={"bad": 0},
        calibration={"bad": {"expectancy": 0.1}},
        lanes={"bad": {"drift": -0.01, "vol": 0.12}},
        admitted_lanes=["bad"],
        matched_replay={"bad": 0.1},
    )
    failures = validation.check_expectancy_honesty(r)
    assert any("mechanics-negative" in f for f in failures), \
        f"gate must fail when a non-positive-drift lane is admitted: {failures}"
    print("PASS test_mechanics_negative_lane_admitted_bites")


def test_admitted_lane_with_negative_matched_replay_bites():
    r = _result(
        entries={"good": 2},
        calibration={"good": {"expectancy": 1.0}},
        lanes={"good": {"drift": 0.01, "vol": 0.12}},
        admitted_lanes=["good"],
        matched_replay={"good": -0.4},
    )
    failures = validation.check_expectancy_honesty(r)
    assert any("matched 100-trade replay" in f for f in failures), \
        f"gate must fail when an admitted lane's matched replay loses: {failures}"
    print("PASS test_admitted_lane_with_negative_matched_replay_bites")


def test_coherent_fixture_is_clean():
    r = _result(
        entries={"good": 4, "bad": 0},
        calibration={"good": {"expectancy": 1.3}, "bad": {"expectancy": -0.9}},
        lanes={"good": {"drift": 0.01, "vol": 0.12}, "bad": {"drift": -0.01, "vol": 0.12}},
        admitted_lanes=["good"],
        matched_replay={"good": 1.0},
        admitted_closes=4, expectancy_admitted=1.1,
    )
    failures = validation.check_expectancy_honesty(r)
    assert failures == [], f"coherent fixture must be clean, got: {failures}"
    print("PASS test_coherent_fixture_is_clean")


def test_negative_admitted_expectancy_bites():
    """The missing clause the reviewer caught: realized admitted
    expectancy <= 0 must fail even when every other gate is green."""
    r = _result(
        entries={"good": 4},
        calibration={"good": {"expectancy": 1.3}},
        lanes={"good": {"drift": 0.01, "vol": 0.12}},
        admitted_lanes=["good"],
        matched_replay={"good": 1.0},
        admitted_closes=4, expectancy_admitted=-5.1035,
    )
    failures = validation.check_expectancy_honesty(r)
    assert any("admitted trades realized expectancy" in f
               for f in failures), f"negative admitted expectancy must bite: {failures}"
    print("PASS test_negative_admitted_expectancy_bites")


def test_admitted_entries_without_closes_bites():
    r = _result(
        entries={"good": 2},
        calibration={"good": {"expectancy": 1.3}},
        lanes={"good": {"drift": 0.01, "vol": 0.12}},
        admitted_lanes=["good"],
        matched_replay={"good": 1.0},
        admitted_closes=0, expectancy_admitted=None,
    )
    failures = validation.check_expectancy_honesty(r)
    assert any("zero admitted closes" in f for f in failures), failures
    print("PASS test_admitted_entries_without_closes_bites")


def test_zero_closes_vacuous_bites():
    r = _result(
        entries={"good": 0},
        calibration={"good": {"expectancy": 1.3}},
        lanes={"good": {"drift": 0.01, "vol": 0.12}},
        admitted_lanes=[],
        matched_replay={},
        closes=0, admitted_closes=0,
    )
    failures = validation.check_expectancy_honesty(r)
    assert any("vacuous" in f for f in failures), failures
    print("PASS test_zero_closes_vacuous_bites")


def test_real_fixture_run_fails_expectancy_gate():
    """Anti-tuning tripwire: the actual synthetic run currently realizes
    NEGATIVE admitted expectancy (-5.1035/trade) — the gate MUST fail
    on it. If this test ever passes silently, someone tuned the run to
    green instead of fixing the strategy or the fixture."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = validation.synthetic_backtest(tmp)
    failures = validation.check_expectancy_honesty(result)
    assert any("admitted trades realized expectancy" in f
               for f in failures), (
        "the real fixture run must currently fail the admitted-"
        f"expectancy clause; got failures={failures} and admitted "
        f"expectancy={result['expectancy_admitted']}")
    assert result["expectancy_admitted"] is not None \
        and result["expectancy_admitted"] <= 0
    print("PASS test_real_fixture_run_fails_expectancy_gate")


def test_calibrated_preload_produces_gate_visible_stats():
    """End-to-end: a positive-drift lane calibrated through replay must
    become tradable on the real gate; a negative-drift lane must not."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        trader = validation.make_trader(tmp)
        good = validation.calibrated_preload(tmp, trader, "good", 0.010, 0.12, seed=101)
        assert good["n"] >= 30, f"calibration needs a real sample, got {good['n']}"
        assert good["expectancy"] > 0, good
        assert good["tradable"] is True, \
            f"positive-drift lane must be gate-admissible after calibration: {good}"
        bad = validation.calibrated_preload(tmp, trader, "bad", -0.010, 0.12, seed=102)
        assert bad["tradable"] is False, \
            f"negative-drift lane must stay dark after calibration: {bad}"
    print("PASS test_calibrated_preload_produces_gate_visible_stats")


if __name__ == "__main__":
    test_negative_calibration_lane_with_entries_bites()
    test_mechanics_negative_lane_admitted_bites()
    test_admitted_lane_with_negative_matched_replay_bites()
    test_coherent_fixture_is_clean()
    test_negative_admitted_expectancy_bites()
    test_admitted_entries_without_closes_bites()
    test_zero_closes_vacuous_bites()
    test_real_fixture_run_fails_expectancy_gate()
    test_calibrated_preload_produces_gate_visible_stats()
    print("ALL VALIDATION-HONESTY TESTS PASSED")
