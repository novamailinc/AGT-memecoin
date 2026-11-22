"""Tests for lifecycle.py -- synthetic fixtures only."""
import pytest

from lifecycle import (
    Classification, Phase, Snapshot, TimelinePoint, classify,
    classify_timeline, classify_with_reasons, dac_timeline,
    fast_rug_timeline, first_phase_at, is_legal_transition,
    is_runner_phase, phase_counts, slow_steady_runner_timeline,
    snapshot_issues, time_in_phase, transition_flag,
    validate_timeline_phases, zombie_timeline,
)


def snap(curve=100, age=60, liq=50_000, mcap=120_000, mom=30, holders=10):
    return Snapshot(curve_completion_pct=curve, age_minutes=age,
                    liquidity_usd=liq, mcap_usd=mcap,
                    momentum_pct=mom, holder_trend_pct=holders)


# -- single-snapshot classification -----------------------------------------
def test_healthy_graduated_snapshot_is_runner():
    assert classify(snap()) is Phase.RUNNER


def test_partial_curve_is_bonding():
    assert classify(snap(curve=42)) is Phase.BONDING


def test_curve_exactly_100_is_not_bonding():
    assert classify(snap(curve=100, mom=5)) is Phase.GRADUATED


def test_curve_99_9_is_still_bonding():
    assert classify(snap(curve=99.9)) is Phase.BONDING


def test_graduated_calm_snapshot_is_graduated():
    assert classify(snap(mom=5, holders=2)) is Phase.GRADUATED


def test_runner_needs_liquidity_floor():
    assert classify(snap(liq=24_999)) is not Phase.RUNNER


def test_runner_liquidity_boundary_is_runner():
    assert classify(snap(liq=25_000)) is Phase.RUNNER


def test_runner_needs_mcap_floor():
    assert classify(snap(mcap=49_999)) is Phase.GRADUATED


def test_runner_needs_momentum():
    assert classify(snap(mom=14.9, liq=90_000, mcap=200_000)) is Phase.GRADUATED


def test_runner_momentum_boundary():
    assert classify(snap(mom=15.0)) is Phase.RUNNER


def test_runner_rejects_collapsing_holders():
    # Holders at -20% is past both the runner floor and the fading line.
    assert classify(snap(holders=-20)) is Phase.FADING


def test_fading_on_negative_momentum():
    assert classify(snap(mom=-10.0)) is Phase.FADING


def test_fading_on_thin_post_grad_liquidity():
    assert classify(snap(mom=0, holders=0, liq=9_999)) is Phase.FADING


def test_dead_on_drained_liquidity():
    assert classify(snap(liq=999)) is Phase.DEAD


def test_dead_on_collapse_even_with_liquidity():
    assert classify(snap(liq=80_000, mom=-90, holders=-40)) is Phase.DEAD


def test_collapse_needs_holders_too():
    # Price crashed but holders have not fled: fading, not proven dead.
    assert classify(snap(mom=-90, holders=0)) is Phase.FADING


def test_dead_beats_bonding():
    assert classify(snap(curve=20, liq=100, mom=-95, holders=-60)) is Phase.DEAD


def test_bonding_beats_runner_numbers():
    # Enormous momentum pre-graduation is still just bonding.
    assert classify(snap(curve=80, mom=500, liq=200_000, mcap=900_000)) is Phase.BONDING


# -- fail-closed UNKNOWN -------------------------------------------------------
@pytest.mark.parametrize("field", ["curve_completion_pct", "age_minutes",
                                   "liquidity_usd", "mcap_usd",
                                   "momentum_pct", "holder_trend_pct"])
def test_each_missing_metric_is_unknown(field):
    values = dict(curve_completion_pct=100, age_minutes=60, liquidity_usd=50_000,
                  mcap_usd=120_000, momentum_pct=30, holder_trend_pct=10)
    values[field] = None
    assert classify(Snapshot(**values)) is Phase.UNKNOWN


def test_empty_snapshot_is_unknown():
    assert classify(Snapshot()) is Phase.UNKNOWN


def test_none_snapshot_is_unknown():
    assert classify(None) is Phase.UNKNOWN


def test_nan_metric_is_unknown():
    assert classify(snap(mom=float("nan"))) is Phase.UNKNOWN


def test_negative_liquidity_is_unknown_not_dead():
    assert classify(snap(liq=-5)) is Phase.UNKNOWN


def test_curve_over_100_is_unknown():
    assert classify(snap(curve=100.1)) is Phase.UNKNOWN


def test_negative_age_is_unknown():
    assert classify(snap(age=-1)) is Phase.UNKNOWN


def test_reasons_explain_unknown():
    result = classify_with_reasons(Snapshot())
    assert isinstance(result, Classification)
    assert result.phase is Phase.UNKNOWN
    assert len(result.reasons) == 6


def test_reasons_present_for_runner():
    assert classify_with_reasons(snap()).reasons


def test_snapshot_issues_empty_for_valid():
    assert snapshot_issues(snap()) == ()


def test_unknown_is_not_a_huntable_runner():
    assert not is_runner_phase(classify(Snapshot()))
    assert is_runner_phase(Phase.RUNNER)
    assert not is_runner_phase(Phase.FADING)


# -- transitions -----------------------------------------------------------------
def test_legal_forward_transitions():
    assert is_legal_transition(Phase.BONDING, Phase.GRADUATED)
    assert is_legal_transition(Phase.GRADUATED, Phase.RUNNER)
    assert is_legal_transition(Phase.RUNNER, Phase.FADING)
    assert is_legal_transition(Phase.FADING, Phase.DEAD)


def test_staying_in_phase_is_legal():
    for phase in Phase:
        assert is_legal_transition(phase, phase)


def test_illegal_backwards_transitions():
    assert not is_legal_transition(Phase.RUNNER, Phase.BONDING)
    assert not is_legal_transition(Phase.RUNNER, Phase.GRADUATED)
    assert not is_legal_transition(Phase.DEAD, Phase.BONDING)
    assert not is_legal_transition(Phase.BONDING, Phase.RUNNER)


def test_bonding_can_die_without_graduating():
    assert is_legal_transition(Phase.BONDING, Phase.DEAD)


def test_fading_can_revive_to_runner_legally():
    assert is_legal_transition(Phase.FADING, Phase.RUNNER)
    assert transition_flag(Phase.FADING, Phase.RUNNER) == "ok"


def test_dead_to_runner_is_suspicious_but_legal():
    assert is_legal_transition(Phase.DEAD, Phase.RUNNER)
    assert transition_flag(Phase.DEAD, Phase.RUNNER) == "suspicious"


def test_transition_flag_illegal():
    assert transition_flag(Phase.DEAD, Phase.GRADUATED) == "illegal"


def test_transition_rejects_non_phases():
    with pytest.raises(ValueError):
        is_legal_transition("RUNNER", Phase.DEAD)


def test_validate_timeline_reports_only_bad_steps():
    phases = [Phase.BONDING, Phase.GRADUATED, Phase.RUNNER, Phase.BONDING]
    bad = validate_timeline_phases(phases)
    assert len(bad) == 1 and bad[0][0] == 3 and bad[0][3] == "illegal"


def test_validate_clean_timeline_is_empty():
    phases = [Phase.BONDING, Phase.GRADUATED, Phase.RUNNER, Phase.FADING, Phase.DEAD]
    assert validate_timeline_phases(phases) == []


# -- fixtures / timelines -----------------------------------------------------------
def test_fast_rug_fixture_shape():
    phases = classify_timeline(fast_rug_timeline())
    assert phases[0] is Phase.BONDING
    assert phases[-1] is Phase.DEAD
    assert Phase.GRADUATED not in phases and Phase.RUNNER not in phases


def test_dac_fixture_full_arc_under_two_hours():
    points = dac_timeline()
    phases = classify_timeline(points)
    assert phases == [Phase.BONDING, Phase.BONDING, Phase.GRADUATED,
                      Phase.RUNNER, Phase.FADING, Phase.DEAD]
    assert points[-1].timestamp_minutes < 120
    assert validate_timeline_phases(phases) == []


def test_slow_steady_runner_fixture():
    phases = classify_timeline(slow_steady_runner_timeline())
    assert phases[-1] is Phase.RUNNER
    assert Phase.DEAD not in phases and Phase.FADING not in phases
    assert first_phase_at(slow_steady_runner_timeline(), Phase.RUNNER) == 60.0


def test_zombie_fixture_fades_forever():
    phases = classify_timeline(zombie_timeline())
    assert phases[0] is Phase.GRADUATED
    assert all(p is Phase.FADING for p in phases[1:])
    assert Phase.DEAD not in phases


def test_time_in_phase_fast_rug():
    totals = time_in_phase(fast_rug_timeline(), end_minutes=9.0)
    assert totals[Phase.BONDING] == pytest.approx(5.0)
    assert totals[Phase.DEAD] == pytest.approx(4.0)


def test_time_in_phase_last_point_owns_nothing_without_end():
    totals = time_in_phase(fast_rug_timeline())
    assert totals[Phase.DEAD] == pytest.approx(4.0)  # 5->9 owned by 3rd point
    assert sum(totals.values()) == pytest.approx(9.0)


def test_time_in_phase_rejects_end_before_last():
    with pytest.raises(ValueError):
        time_in_phase(fast_rug_timeline(), end_minutes=1.0)


def test_non_monotonic_timeline_rejected():
    points = dac_timeline()
    bad = [points[1], points[0]]
    with pytest.raises(ValueError):
        classify_timeline(bad)


def test_phase_counts_zombie():
    counts = phase_counts(zombie_timeline())
    assert counts[Phase.FADING] == 4 and counts[Phase.GRADUATED] == 1


def test_first_phase_at_missing_phase_is_none():
    assert first_phase_at(fast_rug_timeline(), Phase.RUNNER) is None


def test_timeline_point_validation():
    with pytest.raises(ValueError):
        TimelinePoint(-1.0, snap())
    with pytest.raises(ValueError):
        TimelinePoint(0.0, None)


def test_dead_revival_timeline_flagged_suspicious():
    phases = [Phase.DEAD, Phase.RUNNER]
    flagged = validate_timeline_phases(phases)
    assert len(flagged) == 1 and flagged[0][3] == "suspicious"
