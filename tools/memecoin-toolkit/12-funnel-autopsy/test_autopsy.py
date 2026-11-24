"""Tests for autopsy.py -- one fixture per class, plus precedence."""

from autopsy import LIQ_MIN, MCAP_MIN, classify, meets_bar
from fixtures import (
    CAUGHT_CASE,
    DACITY_CASE,
    DOOMSDAY_CASE,
    NEVER_MET_CASE,
    RUG_CASE,
    SAFETY_UNKNOWN_CASE,
)


def test_bar_constants():
    assert LIQ_MIN == 25_000
    assert MCAP_MIN == 50_000


def test_meets_bar_needs_all_three_conditions():
    base = {"liq": 30_000, "mcap": 60_000, "momentum": True}
    assert meets_bar(base) is True
    assert meets_bar({**base, "liq": 24_999}) is False
    assert meets_bar({**base, "mcap": 49_999}) is False
    assert meets_bar({**base, "momentum": False}) is False


def test_dacity_like_is_burned_one_shot_convergence():
    assert classify(DACITY_CASE) == "burned-one-shot-convergence"


def test_doomsday_like_is_invented_ceiling_block():
    assert DOOMSDAY_CASE["name"] == "Doomsday-like"
    assert classify(DOOMSDAY_CASE) == "invented-ceiling-block"


def test_never_met_bar():
    assert classify(NEVER_MET_CASE) == "never-met-bar"


def test_safety_unknown_lag():
    assert classify(SAFETY_UNKNOWN_CASE) == "safety-unknown-lag"


def test_gate_correctly_blocked_rug_by_failed_safety():
    assert classify(RUG_CASE) == "gate-correctly-blocked-rug"


def test_gate_correctly_blocked_rug_by_outcome_alone():
    case = {
        "timeline": [(5, 60_000, 300_000, True, "known_ok")],
        "gate_log": [],
        "outcome": "rug",
    }
    assert classify(case) == "gate-correctly-blocked-rug"


def test_caught_or_other():
    assert classify(CAUGHT_CASE) == "caught-or-other"


def test_precedence_never_met_bar_beats_rug_outcome():
    case = {
        "timeline": [(0, 1_000, 5_000, True, "failed")],
        "gate_log": [],
        "outcome": "rug",
    }
    assert classify(case) == "never-met-bar"


def test_precedence_rug_beats_invented_ceiling():
    case = {
        "timeline": [(5, 60_000, 300_000, True, "failed")],
        "gate_log": [(5, "momentum", "blocked", "gain exceeds 300% ceiling")],
        "outcome": "runner",
    }
    assert classify(case) == "gate-correctly-blocked-rug"


def test_precedence_ceiling_beats_burned_convergence():
    case = {
        "timeline": [
            (1, 8_000, 30_000, True, "unknown"),
            (10, 80_000, 500_000, True, "known_ok"),
        ],
        "gate_log": [
            (1, "convergence", "blocked", "safety unknown"),
            (10, "momentum", "blocked", "h24 gain exceeds 300% ceiling"),
        ],
        "outcome": "runner",
    }
    assert classify(case) == "invented-ceiling-block"


def test_convergence_retried_is_not_burned():
    case = {
        "timeline": [
            (1, 8_000, 30_000, True, "unknown"),
            (10, 80_000, 500_000, True, "known_ok"),
        ],
        "gate_log": [
            (1, "convergence", "blocked", "safety unknown"),
            (12, "convergence", "carded", "retry passed all gates"),
        ],
        "outcome": "runner",
    }
    assert classify(case) == "caught-or-other"


def test_dict_shaped_rows_are_accepted():
    case = {
        "timeline": [
            {
                "t_min": 20,
                "liq_usd": 96_000,
                "mcap_usd": 1_140_000,
                "momentum_positive": True,
                "safety": "known_ok",
            }
        ],
        "gate_log": [
            {
                "t_min": 20,
                "lane": "momentum",
                "action": "blocked",
                "reason": "h24 gain 2540% exceeds 300% ceiling",
            }
        ],
        "outcome": "runner",
    }
    assert classify(case) == "invented-ceiling-block"


def test_empty_case_is_never_met_bar():
    assert classify({"timeline": [], "gate_log": []}) == "never-met-bar"


# ------------------------------------------------------------ bar boundaries

def test_meets_bar_at_exact_thresholds():
    snap = {"liq": LIQ_MIN, "mcap": MCAP_MIN, "momentum": True}
    assert meets_bar(snap) is True  # the bar is inclusive on both numbers
    assert meets_bar({**snap, "liq": LIQ_MIN - 1}) is False
    assert meets_bar({**snap, "mcap": MCAP_MIN - 1}) is False


def test_bar_conditions_must_coincide_in_one_snapshot():
    # Liquidity in one print, mcap in another, momentum in a third:
    # the bar is per-snapshot, so this token never met it.
    case = {
        "timeline": [
            (0, 90_000, 10_000, False, "known_ok"),
            (5, 5_000, 900_000, False, "known_ok"),
            (9, 5_000, 10_000, True, "known_ok"),
        ],
        "gate_log": [],
        "outcome": "runner",
    }
    assert classify(case) == "never-met-bar"


def test_classes_tuple_is_exactly_the_six_verdicts():
    from autopsy import CLASSES
    assert len(CLASSES) == 6
    assert set(CLASSES) == {
        "never-met-bar", "gate-correctly-blocked-rug",
        "invented-ceiling-block", "burned-one-shot-convergence",
        "safety-unknown-lag", "caught-or-other",
    }
    for case in (DACITY_CASE, DOOMSDAY_CASE, NEVER_MET_CASE,
                 SAFETY_UNKNOWN_CASE, RUG_CASE, CAUGHT_CASE):
        assert classify(case) in CLASSES


# --------------------------------------------------------------- rug variants

import pytest


@pytest.mark.parametrize("outcome", ["rugged", "RUG", "flagged_rug"])
def test_rug_outcome_synonyms_all_count(outcome):
    case = {
        "timeline": [(5, 60_000, 300_000, True, "known_ok")],
        "gate_log": [],
        "outcome": outcome,
    }
    assert classify(case) == "gate-correctly-blocked-rug"


@pytest.mark.parametrize("outcome", [{"rug": True}, {"status": "rug"}, {"status": "runner", "rug": True}])
def test_rug_outcome_dict_shapes(outcome):
    case = {
        "timeline": [(5, 60_000, 300_000, True, "known_ok")],
        "gate_log": [],
        "outcome": outcome,
    }
    assert classify(case) == "gate-correctly-blocked-rug"


def test_failed_safety_before_the_bar_does_not_count_as_rug_block():
    # Safety failed only at a dust snapshot that never met the bar;
    # at the bar it was known-ok and nothing else blocked it.
    case = {
        "timeline": [
            (0, 2_000, 8_000, True, "failed"),
            (6, 60_000, 300_000, True, "known_ok"),
        ],
        "gate_log": [],
        "outcome": "runner",
    }
    assert classify(case) == "caught-or-other"


def test_missing_outcome_key_is_not_a_rug():
    case = {
        "timeline": [(6, 60_000, 300_000, True, "known_ok")],
        "gate_log": [(6, "momentum", "carded", "all gates passed")],
    }
    assert classify(case) == "caught-or-other"


# ------------------------------------------------------------ ceiling variants

@pytest.mark.parametrize("reason", [
    "24h gain above the max gain allowed",
    "blocked by max_gain rule",
    "gain cap hit at 300%",
    "exceeds the gain ceiling",
])
def test_ceiling_reason_spellings_all_classify(reason):
    case = {
        "timeline": [(5, 60_000, 300_000, True, "known_ok")],
        "gate_log": [(5, "momentum", "blocked", reason)],
        "outcome": "runner",
    }
    assert classify(case) == "invented-ceiling-block"


def test_ceiling_reason_matching_is_case_insensitive():
    case = {
        "timeline": [(5, 60_000, 300_000, True, "known_ok")],
        "gate_log": [(5, "momentum", "blocked", "GAIN CEILING exceeded")],
        "outcome": "runner",
    }
    assert classify(case) == "invented-ceiling-block"


def test_ordinary_block_reason_is_not_a_ceiling():
    case = {
        "timeline": [(5, 60_000, 300_000, True, "known_ok")],
        "gate_log": [(5, "momentum", "blocked", "liquidity dropped intraday")],
        "outcome": "runner",
    }
    assert classify(case) == "caught-or-other"


# ---------------------------------------------------------- convergence rules

def test_burned_requires_safety_ok_somewhere():
    # Convergence blocked once and never retried, but safety was never
    # known-ok either -- that is the safety-lag story, not a burned shot.
    case = {
        "timeline": [
            (1, 8_000, 30_000, True, "unknown"),
            (10, 80_000, 500_000, True, "unknown"),
        ],
        "gate_log": [(1, "convergence", "blocked", "safety unknown")],
        "outcome": "runner",
    }
    assert classify(case) == "safety-unknown-lag"


def test_same_minute_retry_does_not_unburn():
    case = {
        "timeline": [
            (1, 8_000, 30_000, True, "unknown"),
            (10, 80_000, 500_000, True, "known_ok"),
        ],
        "gate_log": [
            (1, "convergence", "blocked", "safety unknown"),
            (1, "convergence", "retry", "same-minute retry"),
        ],
        "outcome": "runner",
    }
    assert classify(case) == "burned-one-shot-convergence"


def test_retry_by_another_lane_still_counts_as_retried():
    case = {
        "timeline": [
            (1, 8_000, 30_000, True, "unknown"),
            (10, 80_000, 500_000, True, "known_ok"),
        ],
        "gate_log": [
            (1, "convergence", "blocked", "safety unknown"),
            (12, "momentum", "allowed", "momentum lane picked it up"),
        ],
        "outcome": "runner",
    }
    assert classify(case) == "caught-or-other"


def test_convergence_lane_and_action_matching_is_case_insensitive():
    case = {
        "timeline": [
            (1, 8_000, 30_000, True, "unknown"),
            (10, 80_000, 500_000, True, "known_ok"),
        ],
        "gate_log": [(1, "Convergence", "Convergence_Blocked", "safety unknown")],
        "outcome": "runner",
    }
    assert classify(case) == "burned-one-shot-convergence"


def test_a_second_block_is_still_burned_not_retried():
    case = {
        "timeline": [
            (1, 8_000, 30_000, True, "unknown"),
            (10, 80_000, 500_000, True, "known_ok"),
        ],
        "gate_log": [
            (1, "convergence", "blocked", "safety unknown"),
            (9, "convergence", "blocked", "still blocked"),
        ],
        "outcome": "runner",
    }
    assert classify(case) == "burned-one-shot-convergence"


def test_safety_unknown_only_counts_at_bar_meeting_snapshots():
    # One bar-meeting print had known-ok safety, so the lag story fails
    # even though an earlier bar-meeting print was unknown.
    case = {
        "timeline": [
            (3, 30_000, 90_000, True, "unknown"),
            (9, 55_000, 300_000, True, "known_ok"),
        ],
        "gate_log": [(3, "momentum", "blocked", "safety unknown")],
        "outcome": "runner",
    }
    assert classify(case) == "caught-or-other"


def test_missing_timeline_and_gate_log_keys_fail_closed():
    assert classify({}) == "never-met-bar"
    assert classify({"outcome": "rug"}) == "never-met-bar"


# ------------------------------------------------------------- reporting helpers

def test_explain_dacity_evidence():
    from autopsy import explain
    info = explain(DACITY_CASE)
    assert info["classification"] == "burned-one-shot-convergence"
    assert info["bar_met"] is True
    assert info["n_bar_snapshots"] == 3
    assert info["first_bar_t"] == 5
    assert info["bar_duration_minutes"] == pytest.approx(35.0)
    assert info["safety_at_first_bar"] == "unknown"
    assert info["n_blocking_events"] == 1
    assert info["has_ceiling_block"] is False
    assert info["retried_after_block"] is False


def test_explain_doomsday_and_never_met():
    from autopsy import explain
    doom = explain(DOOMSDAY_CASE)
    assert doom["classification"] == "invented-ceiling-block"
    assert doom["has_ceiling_block"] is True
    assert doom["safety_at_first_bar"] == "known_ok"
    assert doom["bar_duration_minutes"] == pytest.approx(15.0)
    never = explain(NEVER_MET_CASE)
    assert never["bar_met"] is False
    assert never["first_bar_t"] is None
    assert never["safety_at_first_bar"] is None
    assert never["bar_duration_minutes"] == 0.0


def test_first_bar_time_and_duration_edges():
    from autopsy import bar_duration_minutes, first_bar_time
    assert first_bar_time(CAUGHT_CASE) == 6
    assert bar_duration_minutes(CAUGHT_CASE) == 0.0  # a single print
    assert first_bar_time({"timeline": []}) is None


def test_blocking_events_and_retry_helpers():
    from autopsy import blocking_events, has_ceiling_block, retry_after_first_block
    assert len(blocking_events(DOOMSDAY_CASE)) == 1
    assert blocking_events(CAUGHT_CASE) == []
    assert has_ceiling_block(DOOMSDAY_CASE) is True
    assert has_ceiling_block(DACITY_CASE) is False
    assert retry_after_first_block(DACITY_CASE) is False


def test_summarise_counts_all_six_fixtures():
    from autopsy import summarise
    from fixtures import ALL_CASES
    counts = summarise(ALL_CASES)
    assert counts == {
        "never-met-bar": 1,
        "gate-correctly-blocked-rug": 1,
        "invented-ceiling-block": 1,
        "burned-one-shot-convergence": 1,
        "safety-unknown-lag": 1,
        "caught-or-other": 1,
    }
    assert summarise([]) == {name: 0 for name in counts}
