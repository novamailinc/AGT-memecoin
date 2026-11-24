"""Tests for copyecon.py -- the lag result is simulated, then pinned."""
import pytest

from copyecon import (LAG_BUCKETS, LEADER_SLIPPAGE, expectancy,
                      expectancy_by_lag, generate_price_path, is_copying_viable,
                      lag_penalty, lag_to_steps, simulate_copy,
                      simulate_follower, simulate_leader, win_rate)

SEEDS = range(200)
TABLE = expectancy_by_lag(SEEDS)


# -- price path ------------------------------------------------------------------
def test_path_starts_at_one_and_has_full_length():
    p = generate_price_path(1)
    assert p[0] == 1.0 and len(p) == 400
    assert all(x > 0 for x in p)


def test_path_is_deterministic_per_seed():
    assert generate_price_path(42) == generate_price_path(42)
    assert generate_price_path(42) != generate_price_path(43)


def test_path_pumps_then_bleeds_on_average():
    peaks = []
    finals = []
    for s in range(50):
        p = generate_price_path(s)
        peaks.append(max(p))
        finals.append(p[-1])
    assert sum(peaks) / len(peaks) > 1.5       # the pump is real
    assert sum(finals) / len(finals) < 1.0     # and so is the bleed


def test_path_validation():
    with pytest.raises(ValueError):
        generate_price_path(None)
    with pytest.raises(ValueError):
        generate_price_path(1, steps=5)


def test_custom_path_length():
    assert len(generate_price_path(3, steps=50)) == 50


# -- lag plumbing -------------------------------------------------------------------
def test_lag_to_steps_uses_five_second_steps():
    assert lag_to_steps(0) == 0
    assert lag_to_steps(15) == 3
    assert lag_to_steps(60) == 12
    assert lag_to_steps(300) == 60
    assert lag_to_steps(900) == 180


def test_lag_to_steps_rejects_negative():
    with pytest.raises(ValueError):
        lag_to_steps(-5)


def test_lag_buckets_cover_the_four_required_lags():
    assert set(LAG_BUCKETS) == {"15s", "1m", "5m", "15m"}
    secs = [LAG_BUCKETS[k][0] for k in ("15s", "1m", "5m", "15m")]
    assert secs == [15, 60, 300, 900]


def test_slippage_grows_with_lag():
    slips = [LAG_BUCKETS[k][1] for k in ("15s", "1m", "5m", "15m")]
    assert slips == sorted(slips) and slips[0] > LEADER_SLIPPAGE


# -- single-trade ladder --------------------------------------------------------------
def test_flat_path_loses_only_slippage():
    flat = [1.0] * 100
    assert simulate_copy(flat, 0, 0.0) == pytest.approx(0.0)
    assert simulate_copy(flat, 0, 0.10) == pytest.approx(1 / 1.10 - 1)


def test_clean_double_then_hold_pays_the_ladder():
    # 1 -> 2 (sell half at 2x) -> stays 2 (rest exits at end at 2x).
    path = [1.0, 1.5, 2.0] + [2.0] * 50
    ret = simulate_copy(path, 0, 0.0)
    assert ret == pytest.approx(0.5 * 2.0 + 0.5 * 2.0 - 1.0)  # +100%


def test_trailing_stop_exits_the_rest_after_a_peak():
    # Double, peak 3, then a >30% fall from peak trails out the rest.
    path = [1.0, 2.0, 3.0, 2.0] + [2.0] * 20
    ret = simulate_copy(path, 0, 0.0)
    assert ret == pytest.approx(0.5 * 2.0 + 0.5 * 2.0 - 1.0)


def test_hard_stop_cuts_a_trade_that_never_doubles():
    path = [1.0, 0.8, 0.5] + [0.5] * 20
    assert simulate_copy(path, 0, 0.0) == pytest.approx(-0.5)


def test_hard_stop_includes_slippage_drag():
    path = [1.0, 0.5] + [0.5] * 20
    assert simulate_copy(path, 0, 0.15) == pytest.approx(0.5 / 1.15 - 1)


def test_no_lookahead_entry_at_last_index_rejected():
    with pytest.raises(ValueError):
        simulate_copy([1.0] * 10, 9, 0.0)


def test_simulate_copy_validation():
    with pytest.raises(ValueError):
        simulate_copy([], 0, 0.0)
    with pytest.raises(ValueError):
        simulate_copy([1.0, -1.0], 0, 0.0)
    with pytest.raises(ValueError):
        simulate_copy([1.0] * 10, -1, 0.0)
    with pytest.raises(ValueError):
        simulate_copy([1.0] * 10, 0, 1.0)
    with pytest.raises(ValueError):
        simulate_copy([1.0] * 10, 0, -0.1)


def test_simulate_follower_rejects_unknown_bucket():
    with pytest.raises(ValueError):
        simulate_follower(generate_price_path(1), "30s")


def test_leader_uses_zero_lag_and_lowest_slippage():
    p = generate_price_path(5)
    assert simulate_leader(p) == simulate_copy(p, 0, LEADER_SLIPPAGE)


def test_return_is_bounded_below_by_total_loss():
    for s in range(30):
        p = generate_price_path(s)
        for label in LAG_BUCKETS:
            assert simulate_follower(p, label) > -1.0


# -- statistics helpers ------------------------------------------------------------------
def test_expectancy_and_win_rate_hand_values():
    rs = [0.5, -0.5, 1.0, -1.0]
    assert expectancy(rs) == pytest.approx(0.0)
    assert win_rate(rs) == pytest.approx(0.5)
    assert expectancy([0.2, 0.4]) == pytest.approx(0.3)


def test_expectancy_helpers_reject_empty():
    with pytest.raises(ValueError):
        expectancy([])
    with pytest.raises(ValueError):
        win_rate(None)


# -- the headline result: simulated across 200 seeded paths, then pinned -------------------
def test_leader_expectancy_is_positive():
    assert TABLE["leader"] > 0


def test_every_follower_bucket_is_negative():
    for label in LAG_BUCKETS:
        assert TABLE[label] < 0, label


def test_expectancy_worsens_monotonically_with_lag():
    order = [TABLE["leader"], TABLE["15s"], TABLE["1m"], TABLE["5m"], TABLE["15m"]]
    assert order == sorted(order, reverse=True)
    assert all(a > b for a, b in zip(order, order[1:]))


def test_even_the_fastest_copy_loses_to_the_leader_by_a_wide_margin():
    assert TABLE["leader"] - TABLE["15s"] > 0.20


def test_fifteen_minute_copy_is_catastrophic():
    assert TABLE["15m"] < -0.40


def test_lag_penalty_is_negative_and_grows():
    pen = lag_penalty(SEEDS)
    assert all(v < 0 for v in pen.values())
    vals = [pen[k] for k in ("15s", "1m", "5m", "15m")]
    assert vals == sorted(vals, reverse=True)  # strictly more negative with lag


def test_copying_is_not_viable_at_any_lag():
    for label in LAG_BUCKETS:
        assert is_copying_viable(SEEDS, label) is False


def test_is_copying_viable_rejects_unknown_bucket():
    with pytest.raises(ValueError):
        is_copying_viable(SEEDS, "2h")


def test_expectancy_table_has_leader_plus_all_buckets():
    assert set(TABLE) == {"leader", "15s", "1m", "5m", "15m"}


def test_expectancy_by_lag_subset_and_empty_seeds():
    sub = expectancy_by_lag(range(20), lags=["1m"])
    assert set(sub) == {"leader", "1m"}
    with pytest.raises(ValueError):
        expectancy_by_lag([])
    with pytest.raises(ValueError):
        expectancy_by_lag(range(5), lags=["bogus"])


def test_follower_win_rate_collapses_with_lag():
    paths = [generate_price_path(s) for s in range(100)]
    early = win_rate([simulate_follower(p, "15s") for p in paths])
    late = win_rate([simulate_follower(p, "15m") for p in paths])
    assert late < early
    assert late <= 0.05


def test_zero_slippage_follower_still_loses_to_leader():
    # Lag alone -- with slippage removed -- already costs the follower.
    paths = [generate_price_path(s) for s in range(100)]
    leader = expectancy([simulate_copy(p, 0, 0.0) for p in paths])
    delayed = expectancy([simulate_copy(p, 12, 0.0) for p in paths])
    assert delayed < leader


def test_more_slippage_always_hurts_the_same_entry():
    p = generate_price_path(9)
    low = simulate_copy(p, 12, 0.0)
    high = simulate_copy(p, 12, 0.20)
    assert high <= low


def test_later_entry_loses_in_aggregate_at_equal_slippage():
    # Equal slippage for both entries, so only the lag differs: across
    # many paths the 1m entry still earns far less than the t0 entry.
    # (Per-path dominance is false -- trail quirks let a follower win
    # individual paths -- so this is asserted in aggregate, honestly.)
    paths = [generate_price_path(s) for s in range(100)]
    early = expectancy([simulate_copy(p, 0, 0.02) for p in paths])
    late = expectancy([simulate_copy(p, 12, 0.02) for p in paths])
    assert late < early - 0.20


def test_follower_returns_are_mostly_losses_at_five_minutes():
    paths = [generate_price_path(s) for s in range(100)]
    rs = [simulate_follower(p, "5m") for p in paths]
    assert win_rate(rs) <= 0.05
    assert expectancy(rs) < -0.40


def test_leader_win_rate_beats_every_follower_bucket():
    paths = [generate_price_path(s) for s in range(100)]
    leader_wr = win_rate([simulate_leader(p) for p in paths])
    for label in LAG_BUCKETS:
        assert win_rate([simulate_follower(p, label) for p in paths]) < leader_wr


def test_expectancy_by_lag_is_deterministic():
    assert expectancy_by_lag(range(30)) == expectancy_by_lag(range(30))
