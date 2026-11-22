"""Tests for liqvelocity.py -- synthetic flows, hand-checkable rates."""
import pytest

from liqvelocity import (MIN_TRADES, ROUND_TRIP_COST, Flow,
                         breakeven_gross_edge, calibration_accuracy,
                         calibration_samples, calibrated_gross_edge,
                         clears_breakeven, duration_minutes, edge_after_costs,
                         graduation_score, inflow_rate, mean_trade_size,
                         minutes_to_graduation, predict_graduation,
                         progress_rate, total_inflow, trade_rate,
                         trades_normalized_velocity, velocity_band)


def flow(events):
    return [Flow(ts=float(t), sol_in=float(s)) for t, s in events]


def steady(n=20, per=1.0, gap=30.0):
    # n events, `per` SOL each, one every `gap` seconds.
    return [Flow(ts=i * gap, sol_in=per) for i in range(n)]


def fast(n=20):
    return [Flow(ts=i * 10.0, sol_in=3.0) for i in range(n)]


# -- validation ----------------------------------------------------------------
def test_flow_validation():
    with pytest.raises(ValueError):
        Flow(ts=-1.0, sol_in=1.0)
    with pytest.raises(ValueError):
        Flow(ts=0.0, sol_in=0.0)
    with pytest.raises(ValueError):
        Flow(ts=0.0, sol_in=None)


def test_rate_functions_raise_on_empty_and_none():
    for fn in (total_inflow, inflow_rate, trade_rate, progress_rate):
        with pytest.raises(ValueError):
            fn([])
        with pytest.raises(ValueError):
            fn(None)


def test_zero_duration_stream_raises():
    same_instant = [Flow(ts=5.0, sol_in=1.0), Flow(ts=5.0, sol_in=2.0)]
    with pytest.raises(ValueError):
        inflow_rate(same_instant)
    with pytest.raises(ValueError):
        duration_minutes(same_instant)


def test_single_event_has_no_duration():
    with pytest.raises(ValueError):
        duration_minutes([Flow(ts=0.0, sol_in=1.0)])


# -- basic rates -----------------------------------------------------------------
def test_total_inflow_sums():
    assert total_inflow(flow([(0, 1.5), (10, 2.5), (20, 1.0)])) == pytest.approx(5.0)


def test_duration_minutes_from_first_to_last():
    assert duration_minutes(flow([(0, 1), (120, 1)])) == pytest.approx(2.0)
    assert duration_minutes(flow([(60, 1), (180, 1)])) == pytest.approx(2.0)


def test_duration_uses_sorted_order():
    # Out-of-order input is sorted before measuring.
    assert duration_minutes(flow([(180, 1), (60, 1)])) == pytest.approx(2.0)


def test_inflow_rate_hand_value():
    # 20 events x 1 SOL over 19*30s = 9.5 min -> 20/9.5 SOL/min.
    assert inflow_rate(steady()) == pytest.approx(20 / 9.5)


def test_trade_rate_hand_value():
    assert trade_rate(steady()) == pytest.approx(20 / 9.5)


def test_mean_trade_size():
    assert mean_trade_size(steady(per=2.5)) == pytest.approx(2.5)


def test_trades_normalized_velocity_hand_value():
    # inflow_rate / count * 100 = (20/9.5)/20*100 = 100/9.5.
    assert trades_normalized_velocity(steady()) == pytest.approx(100 / 9.5)


def test_normalized_velocity_rewards_bigger_trades():
    small = trades_normalized_velocity(steady(per=0.5))
    big = trades_normalized_velocity(steady(per=2.0))
    assert big > small


def test_faster_stream_has_higher_inflow_rate():
    assert inflow_rate(fast()) > inflow_rate(steady())


def test_progress_rate_hand_value():
    # rate 20/9.5 SOL/min against an 85 SOL target.
    assert progress_rate(steady()) == pytest.approx((20 / 9.5) / 85 * 100)


def test_progress_rate_rejects_bad_target():
    with pytest.raises(ValueError):
        progress_rate(steady(), target_sol=0)


def test_minutes_to_graduation_hand_value():
    # Collected 20 of 85 at 20/9.5 SOL/min -> 65 / (20/9.5) minutes.
    assert minutes_to_graduation(steady()) == pytest.approx(65 / (20 / 9.5))


def test_minutes_to_graduation_none_when_reached():
    rich = [Flow(ts=i * 10.0, sol_in=10.0) for i in range(10)]  # 100 SOL in
    assert minutes_to_graduation(rich) is None


# -- velocity bands -----------------------------------------------------------------
def test_velocity_band_boundaries():
    assert velocity_band(0.0) == "slow"
    assert velocity_band(0.49) == "slow"
    assert velocity_band(0.5) == "steady"
    assert velocity_band(1.99) == "steady"
    assert velocity_band(2.0) == "fast"
    assert velocity_band(4.99) == "fast"
    assert velocity_band(5.0) == "igniting"
    assert velocity_band(50.0) == "igniting"


def test_velocity_band_rejects_negative_and_none():
    with pytest.raises(ValueError):
        velocity_band(-0.1)
    with pytest.raises(ValueError):
        velocity_band(None)


# -- predictor: sparse data fails closed to unknown ---------------------------------
def test_score_is_none_below_min_trades():
    assert graduation_score(steady(n=MIN_TRADES - 1)) is None
    assert graduation_score([]) is None
    assert graduation_score(None) is None


def test_score_exists_at_exactly_min_trades():
    assert graduation_score(steady(n=MIN_TRADES)) is not None


def test_predict_sparse_is_unknown_never_a_call():
    out = predict_graduation(steady(n=3))
    assert out["status"] == "unknown"
    assert out["score"] is None and out["will_graduate"] is None


def test_predict_none_is_unknown():
    out = predict_graduation(None)
    assert out["status"] == "unknown" and out["trade_count"] == 0


def test_predict_fast_stream_scores_high_and_calls_graduation():
    out = predict_graduation(fast())
    assert out["status"] == "scored"
    assert out["score"] >= 60 and out["will_graduate"] is True


def test_predict_slow_stream_scores_low():
    trickle = [Flow(ts=i * 60.0, sol_in=0.1) for i in range(20)]
    out = predict_graduation(trickle)
    assert out["will_graduate"] is False
    assert out["score"] < 60


def test_score_in_range_and_monotonic_in_pace():
    slow = graduation_score(steady(per=0.2, gap=60.0))
    mid = graduation_score(steady())
    high = graduation_score(fast())
    assert 0 <= slow < mid < high <= 100


def test_predict_rejects_bad_threshold():
    with pytest.raises(ValueError):
        predict_graduation(steady(), threshold=101)
    with pytest.raises(ValueError):
        predict_graduation(steady(), threshold=-1)


def test_predict_threshold_boundary():
    out = predict_graduation(fast(), threshold=predict_graduation(fast())["score"])
    assert out["will_graduate"] is True


# -- calibration -----------------------------------------------------------------------
def test_calibration_samples_shape_and_determinism():
    a, b = calibration_samples(), calibration_samples()
    assert a == b and len(a) == 120
    grads = [r for r, g in a if g]
    nongrads = [r for r, g in a if not g]
    assert len(grads) == len(nongrads) == 60
    assert sum(grads) / len(grads) > sum(nongrads) / len(nongrads)


def test_calibration_accuracy_is_strong_for_graduation():
    # Velocity genuinely predicts the *event* of graduation.
    assert calibration_accuracy() >= 0.85


def test_calibration_accuracy_rejects_negative_threshold():
    with pytest.raises(ValueError):
        calibration_accuracy(-1.0)


# -- the honest cost math: pinned, not prose --------------------------------------------
def test_round_trip_cost_constant_is_four_percent():
    assert ROUND_TRIP_COST == pytest.approx(0.04)


def test_calibrated_gross_edge_hand_value():
    # 55 winners at +30%, 45 losers at -30% -> +3.0%.
    assert calibrated_gross_edge() == pytest.approx(0.03)


def test_edge_after_costs_is_negative_pinned():
    assert edge_after_costs() == pytest.approx(-0.01)
    assert edge_after_costs() < 0


def test_predictor_does_not_clear_breakeven_pinned():
    assert clears_breakeven() is False


def test_breakeven_gross_edge_equals_the_cost():
    assert breakeven_gross_edge() == pytest.approx(0.04)
    assert calibrated_gross_edge() < breakeven_gross_edge()


def test_edge_after_costs_with_explicit_numbers():
    assert edge_after_costs(0.10) == pytest.approx(0.06)
    assert edge_after_costs(0.04) == pytest.approx(0.0)
    assert clears_breakeven(0.10) is True
    assert clears_breakeven(0.04) is False  # exactly at cost is not clearing


def test_edge_after_costs_custom_cost():
    assert edge_after_costs(0.03, round_trip_cost=0.02) == pytest.approx(0.01)
    assert clears_breakeven(0.03, round_trip_cost=0.02) is True


def test_edge_after_costs_rejects_bad_cost():
    with pytest.raises(ValueError):
        edge_after_costs(0.05, round_trip_cost=1.0)
    with pytest.raises(ValueError):
        edge_after_costs(0.05, round_trip_cost=-0.01)
    with pytest.raises(ValueError):
        breakeven_gross_edge(None)
