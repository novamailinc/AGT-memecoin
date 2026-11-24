"""Tests for sniperwindow.py -- hand-checkable math, seeded synthetics."""
import pytest

from sniperwindow import (
    break_even_delay, copy_window_expectancy, delay_bucket, entry_multiple,
    expectancy_by_delay, expectancy_table, fraction_of_move_remaining,
    gross_return_pct, late_entry_return_pct, multiple_at_time,
    net_return_at_peak_pct,
)


# -- price path ---------------------------------------------------------------------
def test_multiple_at_launch_is_one():
    assert multiple_at_time(0.0) == pytest.approx(1.0)


def test_multiple_at_peak_time_is_peak():
    assert multiple_at_time(120.0) == pytest.approx(10.0)


def test_multiple_halfway_is_sqrt_of_peak():
    # Geometric rise: halfway in time is sqrt(10)x, not 5.5x.
    assert multiple_at_time(60.0) == pytest.approx(10 ** 0.5)


def test_multiple_quarter_way():
    assert multiple_at_time(30.0) == pytest.approx(10 ** 0.25)


def test_multiple_decays_after_peak_by_half_life():
    assert multiple_at_time(120.0 + 180.0) == pytest.approx(5.0)
    assert multiple_at_time(120.0 + 360.0) == pytest.approx(2.5)


def test_multiple_rises_monotonically_during_rise():
    values = [multiple_at_time(t) for t in (0, 10, 30, 60, 119)]
    assert values == sorted(values)


def test_multiple_falls_monotonically_after_peak():
    values = [multiple_at_time(t) for t in (120, 200, 400, 900)]
    assert values == sorted(values, reverse=True)


def test_custom_peak_and_rise():
    assert multiple_at_time(50.0, peak_multiple=100.0, rise_seconds=100.0) == pytest.approx(10.0)


def test_negative_time_rejected():
    with pytest.raises(ValueError):
        multiple_at_time(-1.0)


def test_bad_parameters_rejected():
    with pytest.raises(ValueError):
        multiple_at_time(10.0, peak_multiple=0)
    with pytest.raises(ValueError):
        multiple_at_time(10.0, rise_seconds=0)
    with pytest.raises(ValueError):
        multiple_at_time(10.0, peak_multiple=0.5)  # a 'peak' below launch


def test_entry_multiple_matches_path():
    assert entry_multiple(45.0) == pytest.approx(multiple_at_time(45.0))


# -- fraction of the move remaining ------------------------------------------------------
def test_fraction_remaining_at_launch_is_all():
    assert fraction_of_move_remaining(0.0) == pytest.approx(1.0)


def test_fraction_remaining_at_peak_is_zero():
    assert fraction_of_move_remaining(120.0) == 0.0
    assert fraction_of_move_remaining(600.0) == 0.0


def test_fraction_remaining_halfway():
    assert fraction_of_move_remaining(60.0) == pytest.approx(0.5)


def test_sniper_delay_keeps_almost_everything():
    assert fraction_of_move_remaining(2.0) > 0.98


def test_polling_delay_has_already_lost_a_chunk():
    # 30s into a 120s rise: a quarter of the log-move is already gone,
    # i.e. the entrant pays 10**0.25 = 1.78x the launch price.
    assert fraction_of_move_remaining(30.0) == pytest.approx(0.75)
    assert entry_multiple(30.0) == pytest.approx(1.78, abs=0.01)


def test_fraction_remaining_rejects_bad_inputs():
    with pytest.raises(ValueError):
        fraction_of_move_remaining(-5.0)
    with pytest.raises(ValueError):
        fraction_of_move_remaining(10.0, peak_multiple=1.0)


# -- returns --------------------------------------------------------------------------------
def test_gross_return_from_launch_to_peak():
    assert gross_return_pct(0.0, 10.0) == pytest.approx(900.0)


def test_net_at_peak_instant_entry_is_gross_minus_costs():
    # (10x) * 0.96 - 1 = 860%.
    assert net_return_at_peak_pct(0.0) == pytest.approx(860.0)


def test_net_at_peak_falls_with_delay():
    values = [net_return_at_peak_pct(d) for d in (0, 30, 60, 90, 119)]
    assert values == sorted(values, reverse=True)


def test_net_at_peak_at_the_peak_is_just_costs():
    assert net_return_at_peak_pct(120.0) == pytest.approx(-4.0)


def test_net_at_peak_after_peak_raises():
    with pytest.raises(ValueError):
        net_return_at_peak_pct(121.0)


def test_late_entry_during_decay_loses():
    # Buy at 300s, sell 60s later: price only fell, plus 4% costs.
    result = late_entry_return_pct(300.0, hold_seconds=60.0)
    assert result < -4.0


def test_late_entry_hold_zero_is_exactly_costs_when_flat():
    # Entering exactly at the peak and exiting instantly at the peak
    # price (the decay has not started within the same instant is not
    # flat, so instead use two post-peak points a half-life apart):
    result = late_entry_return_pct(300.0, hold_seconds=180.0)
    # Price halves over one half-life: 0.5 * 0.96 - 1 = -52%.
    assert result == pytest.approx(-52.0)


def test_early_entry_short_hold_can_win():
    # In at 10s on the rise, out at 70s still on the rise: price rose.
    assert late_entry_return_pct(10.0, hold_seconds=60.0) > 0


def test_higher_costs_always_hurt():
    cheap = late_entry_return_pct(300.0, round_trip_cost_pct=1.0)
    dear = late_entry_return_pct(300.0, round_trip_cost_pct=10.0)
    assert dear < cheap


# -- buckets --------------------------------------------------------------------------------------
@pytest.mark.parametrize("delay,bucket", [
    (0.0, "sniper"), (2.0, "sniper"), (5.0, "fast_polling"),
    (15.0, "polling"), (30.0, "polling"), (119.0, "polling"),
    (120.0, "copy_window_2_10min"), (600.0, "copy_window_2_10min"),
    (601.0, "late"),
])
def test_delay_bucket_boundaries(delay, bucket):
    assert delay_bucket(delay) == bucket


def test_delay_bucket_rejects_negative():
    with pytest.raises(ValueError):
        delay_bucket(-0.1)


# -- seeded expectancy: the structural verdict --------------------------------------------------------
def test_expectancy_is_deterministic_per_seed():
    a = expectancy_by_delay(20.0, seed=42, n_launches=100)
    b = expectancy_by_delay(20.0, seed=42, n_launches=100)
    assert a == b


def test_expectancy_records_trades_and_bucket():
    result = expectancy_by_delay(300.0, n_launches=50)
    assert result.trades == 50 and result.bucket == "copy_window_2_10min"


def test_copy_window_expectancy_is_negative():
    # THE point of the lab: even granting early entrants the peak exit,
    # 2-10 minute entries are structurally negative in expectation.
    assert copy_window_expectancy() < 0


@pytest.mark.parametrize("delay", [120.0, 300.0, 600.0])
def test_each_copy_window_delay_is_negative(delay):
    assert expectancy_by_delay(delay).mean_net_return_pct < 0


def test_sniper_delay_beats_copy_window():
    sniper = expectancy_by_delay(1.0)
    copy = expectancy_by_delay(300.0)
    assert sniper.mean_net_return_pct > copy.mean_net_return_pct


def test_polling_delay_beats_copy_window():
    polling = expectancy_by_delay(30.0)
    copy = expectancy_by_delay(300.0)
    assert polling.mean_net_return_pct > copy.mean_net_return_pct


def test_expectancy_win_rate_falls_with_delay():
    early = expectancy_by_delay(1.0)
    late = expectancy_by_delay(600.0)
    assert late.win_rate_pct < early.win_rate_pct


def test_expectancy_table_shape():
    table = expectancy_table()
    assert len(table) == 6
    assert [r.delay_seconds for r in table] == [1.0, 20.0, 60.0, 180.0, 300.0, 600.0]


def test_expectancy_rejects_zero_launches():
    with pytest.raises(ValueError):
        expectancy_by_delay(10.0, n_launches=0)


# -- break-even solver -----------------------------------------------------------------------------------
def test_break_even_delay_matches_closed_form():
    # net >= 0 iff peak**(1 - d/rise) * (1 - cost) >= 1
    # d = rise * (1 - ln(1/(1-cost)) / ln(peak)); cost 4%, peak 10, rise 120.
    import math
    expected = 120.0 * (1 - math.log(1 / 0.96) / math.log(10.0))
    assert break_even_delay() == pytest.approx(expected, abs=0.05)


def test_break_even_delay_is_before_the_peak():
    result = break_even_delay()
    assert 0 < result < 120.0


def test_break_even_net_at_solution_is_near_zero():
    delay = break_even_delay()
    assert net_return_at_peak_pct(delay) == pytest.approx(0.0, abs=0.1)


def test_break_even_none_when_costs_eat_the_whole_peak():
    # Peak 1.02x with a 4% round trip: even t=0 loses.
    assert break_even_delay(peak_multiple=1.02) is None


def test_break_even_falls_when_costs_rise():
    cheap = break_even_delay(round_trip_cost_pct=1.0)
    dear = break_even_delay(round_trip_cost_pct=20.0)
    assert dear < cheap


def test_break_even_rises_for_bigger_peaks():
    small = break_even_delay(peak_multiple=3.0)
    big = break_even_delay(peak_multiple=100.0)
    assert big > small


def test_break_even_rejects_bad_tolerance():
    with pytest.raises(ValueError):
        break_even_delay(tolerance_seconds=0)
