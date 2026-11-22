"""Tests for replay.py — all expectations computed by hand from the fixtures."""
import math

import pytest

from replay import (DEMO_PATHS, LADDERS, Ladder, ReplayResult, Tranche,
                    compare_ladders, exit_reason_breakdown, first_crossing_time,
                    is_round_tripper, last_exit_time, make_clean_runner,
                    make_round_tripper, make_slow_grinder, make_straight_rug,
                    max_drawdown, peak_multiple, peaked_ge, replay,
                    replay_batch, time_above_multiple, time_to_peak,
                    validate_ladder, weighted_exit_time)

RT = make_round_tripper()
RUG = make_straight_rug()


def test_round_tripper_ladder_beats_hold():
    # user_ladder: 0.50@2.0 (tp) + 0.25 trail@1.82 (2.6*0.7) + 0.25@0.5 (hard stop)
    # = 1.0 + 0.455 + 0.125 = 1.58 ; hold_to_stop: everything @0.5 hard stop.
    user = replay(RT, "user_ladder")
    hold = replay(RT, "hold_to_stop")
    assert user.realized_mult == pytest.approx(1.58)
    assert hold.realized_mult == pytest.approx(0.5)
    assert user.realized_mult > 1.0 > hold.realized_mult  # the proven leak


def test_hard_stop_fires_on_rug():
    for ladder in ("user_ladder", "hold_to_stop", "fast_harvest"):
        r = replay(RUG, ladder)
        stop = 0.6 if ladder == "fast_harvest" else 0.5
        assert r.realized_mult == pytest.approx(stop)
        assert all(leg.reason == "hard_stop" for leg in r.legs)


def test_leg_fills_sum_to_100_percent():
    for path in DEMO_PATHS.values():
        for ladder in ("user_ladder", "fast_harvest", "hold_to_stop"):
            assert replay(path, ladder).filled_fraction == pytest.approx(1.0)


def test_clean_runner_user_ladder():
    # 0.50@2.0 + 0.25@3.0 + 0.25 trail@3.5 (5.0*0.7) = 2.625
    assert replay(make_clean_runner(), "user_ladder").realized_mult == pytest.approx(2.625)


def test_time_cap_enforced():
    # fast_harvest caps at 240 min: the crash at t=300 must never be seen.
    path = [(0, 1.0), (100, 1.2), (240, 1.1), (300, 0.1)]
    r = replay(path, "fast_harvest")
    assert r.realized_mult == pytest.approx(1.1)
    assert all(leg.exit_time <= 240 for leg in r.legs)
    # slow grinder ends exactly at the 1440 cap under user_ladder:
    # 0.50@2.0 (tp at t=1200) + 0.50 remaining @1.8 (time cap) = 1.9
    g = replay(make_slow_grinder(), "user_ladder")
    assert g.realized_mult == pytest.approx(1.9)
    assert max(leg.exit_time for leg in g.legs) <= 1440


def test_time_to_peak_and_peaked_ge():
    assert time_to_peak(RT) == 50
    assert time_to_peak(make_slow_grinder()) == 1200
    assert peaked_ge(RT, 2.0) is True
    assert peaked_ge(RUG, 2.0) is False
    assert peaked_ge(RUG, 1.1) is True


def test_fast_harvest_round_tripper():
    # 0.50@1.5 + 0.25@2.0 + 0.25 trail@2.08 (2.6*0.8, armed at 1.5x) = 1.77
    assert replay(RT, "fast_harvest").realized_mult == pytest.approx(1.77)


def test_batch_stats_on_known_paths():
    stats = replay_batch([RT, RUG], "hold_to_stop")
    assert stats["mean_mult"] == pytest.approx(0.5)  # both hard-stop at 0.5
    assert stats["win_rate"] == 0.0
    assert stats["round_trippers"] == 1  # RT peaked 2.6x, closed < 1.0
    assert stats["median_time_to_peak_min"] == pytest.approx(30.0)  # median(50, 10)
    assert replay_batch([], "user_ladder")["mean_mult"] == 0.0


def test_fail_closed_on_bad_input():
    with pytest.raises(ValueError):
        replay([], "user_ladder")
    with pytest.raises(ValueError):
        replay(RT, "no_such_ladder")
    with pytest.raises(ValueError):
        replay([(5, 1.0), (10, 1.1)], "user_ladder")  # must start at t=0
    with pytest.raises(ValueError):
        replay([(0, 1.0), (0, 1.1)], "user_ladder")  # non-increasing t


# --- Trailing-arm deviation: arms only at 2.0x (documented modelling choice) ---

def test_single_bar_path_closes_at_path_end():
    r = replay([(0, 1.0)], "user_ladder")
    assert r.realized_mult == pytest.approx(1.0)
    assert len(r.legs) == 3
    assert all(leg.reason == "path_end" and leg.exit_time == 0 for leg in r.legs)


def test_trail_never_fires_below_2x_arm_on_a_dip():
    # Peak 1.9x, then a dip to exactly the would-be trail price (1.9*0.7=1.33).
    # Unarmed (arm is 2.0x), so nothing trails out; all exits at path end 1.5.
    path = [(0, 1.0), (10, 1.9), (20, 1.33), (30, 1.5)]
    r = replay(path, "user_ladder")
    assert r.realized_mult == pytest.approx(1.5)
    assert all(leg.reason == "path_end" for leg in r.legs)


def test_trail_arms_exactly_at_2x_and_fires_at_trail_price():
    # t=10 hits 2.0 exactly: TP1 fires and the trail arms.
    # t=20 dips to 1.39, below trail price 2.0*0.7=1.40: trail fills AT 1.40.
    # Remainder (TP 3x tranche) path-ends at 1.39.
    # 0.50*2.0 + 0.25*1.40 + 0.25*1.39 = 1.0 + 0.35 + 0.3475 = 1.6975
    path = [(0, 1.0), (10, 2.0), (20, 1.39), (30, 1.39)]
    r = replay(path, "user_ladder")
    assert r.realized_mult == pytest.approx(1.6975)
    trail = [leg for leg in r.legs if leg.reason == "trail"]
    assert len(trail) == 1 and trail[0].exit_price == pytest.approx(1.40)


def test_trail_stays_unarmed_at_1_99x():
    path = [(0, 1.0), (10, 1.99), (20, 1.30)]
    r = replay(path, "user_ladder")
    assert not any(leg.reason == "trail" for leg in r.legs)
    assert r.realized_mult == pytest.approx(1.30)


# --- No look-ahead: fills must depend only on bars up to the fill ---

def test_future_spike_after_hard_stop_cannot_change_result():
    base = [(0, 1.0), (10, 0.4)]
    extended = base + [(20, 10.0), (30, 20.0)]
    a, b = replay(base, "user_ladder"), replay(extended, "user_ladder")
    assert b.realized_mult == a.realized_mult == pytest.approx(0.5)
    assert [(l.fraction, l.exit_price, l.exit_time, l.reason) for l in b.legs] == \
           [(l.fraction, l.exit_price, l.exit_time, l.reason) for l in a.legs]


def test_bars_after_the_final_exit_are_never_seen():
    # By t=20 every tranche has closed (TPs at 10, trail at 20).
    path = [(0, 1.0), (10, 3.5), (20, 2.4)]
    for tail in ([(30, 0.05)], [(30, 100.0)]):
        r = replay(path + tail, "user_ladder")
        # 0.50*2.0 + 0.25*3.0 + 0.25*2.45 (trail off the 3.5 peak) = 2.3625
        assert r.realized_mult == pytest.approx(2.3625)
        assert max(leg.exit_time for leg in r.legs) == 20


def test_truncating_at_the_exit_bar_gives_the_same_result():
    full = [(0, 1.0), (10, 3.5), (20, 2.4), (30, 9.0), (40, 0.2)]
    cut = full[:3]
    assert replay(cut, "user_ladder").realized_mult == \
        replay(full, "user_ladder").realized_mult


def test_trail_fill_uses_running_peak_not_future_peak():
    # Trail fires at t=20 off the running peak 2.0 -> 1.40. A look-ahead
    # engine using the future 10x peak would fill it at 7.0 instead.
    path = [(0, 1.0), (10, 2.0), (20, 1.39), (30, 10.0)]
    r = replay(path, "user_ladder")
    trail = [leg for leg in r.legs if leg.reason == "trail"]
    assert trail[0].exit_price == pytest.approx(1.40)
    # TP 3x tranche is still open at t=30 and fills at its 3.0 target.
    assert r.realized_mult == pytest.approx(0.5 * 2.0 + 0.25 * 1.40 + 0.25 * 3.0)


def test_post_cap_crash_is_never_seen_and_exit_is_last_seen_price():
    path = [(0, 1.0), (100, 1.3), (200, 1.4), (2000, 0.05)]
    r = replay(path, "fast_harvest")  # cap 240 min
    assert r.realized_mult == pytest.approx(1.4)
    assert all(leg.reason == "time_cap" and leg.exit_time == 200 for leg in r.legs)


# --- Fill conventions: targets, gaps, boundaries ---

def test_tp_fills_at_target_not_at_the_overshooting_bar_price():
    r = replay([(0, 1.0), (10, 2.7)], "user_ladder")
    tp = [leg for leg in r.legs if leg.reason == "tp"]
    assert len(tp) == 1 and tp[0].exit_price == pytest.approx(2.0)
    assert r.realized_mult == pytest.approx(0.5 * 2.0 + 0.5 * 2.7)


def test_gap_down_fills_hard_stop_at_stop_price_not_bar_price():
    r = replay([(0, 1.0), (10, 1.9), (20, 0.4)], "user_ladder")
    assert r.realized_mult == pytest.approx(0.5)
    assert all(leg.exit_price == pytest.approx(0.5) for leg in r.legs)
    assert all(leg.reason == "hard_stop" for leg in r.legs)


def test_gap_up_over_both_tps_fills_each_at_its_own_target():
    r = replay([(0, 1.0), (10, 3.2)], "user_ladder")
    prices = sorted(leg.exit_price for leg in r.legs if leg.reason == "tp")
    assert prices == [pytest.approx(2.0), pytest.approx(3.0)]
    assert r.realized_mult == pytest.approx(0.5 * 2.0 + 0.25 * 3.0 + 0.25 * 3.2)


def test_hard_stop_boundary_is_inclusive_and_just_above_is_not():
    hit = replay([(0, 1.0), (10, 0.5)], "user_ladder")
    assert all(leg.reason == "hard_stop" for leg in hit.legs)
    miss = replay([(0, 1.0), (10, 0.51)], "user_ladder")
    assert all(leg.reason == "path_end" for leg in miss.legs)
    assert miss.realized_mult == pytest.approx(0.51)


def test_bar_at_exactly_the_time_cap_fills_as_time_cap():
    r = replay([(0, 1.0), (240, 1.2)], "fast_harvest")
    assert r.realized_mult == pytest.approx(1.2)
    assert all(leg.reason == "time_cap" for leg in r.legs)
    # The same ladder on a path ending before the cap is a path_end instead.
    early = replay([(0, 1.0), (10, 1.2)], "fast_harvest")
    assert all(leg.reason == "path_end" for leg in early.legs)
    # A TP at exactly its target fires (inclusive), one tick below does not.
    assert any(leg.reason == "tp" for leg in replay([(0, 1.0), (10, 2.0)], "user_ladder").legs)
    assert not any(leg.reason == "tp" for leg in replay([(0, 1.0), (10, 1.99)], "user_ladder").legs)


# --- Entry-price scaling ---

def test_realized_multiple_is_invariant_to_entry_price_scaling():
    scaled = [(t, p * 2.5) for t, p in make_clean_runner()]
    assert replay(scaled, "user_ladder").realized_mult == pytest.approx(2.625)


def test_hard_stop_price_scales_with_entry_price():
    # p0 = 4.0, so the 0.5x stop is a price of 2.0, not 0.5.
    r = replay([(0, 4.0), (10, 1.9)], "user_ladder")
    assert all(leg.exit_price == pytest.approx(2.0) for leg in r.legs)
    assert r.realized_mult == pytest.approx(0.5)


# --- Path analytics: peak, crossing, duration, drawdown ---

def test_peak_multiple_on_fixtures_and_scaled_path():
    assert peak_multiple(RT) == pytest.approx(2.6)
    assert peak_multiple(RUG) == pytest.approx(1.1)
    assert peak_multiple([(0, 2.0), (10, 5.0)]) == pytest.approx(2.5)
    assert peak_multiple([(0, 1.0)]) == pytest.approx(1.0)


def test_first_crossing_time_first_hit_and_miss():
    assert first_crossing_time(RT, 2.0) == 30  # first bar >= 2.0x
    assert first_crossing_time(RT, 2.6) == 50
    assert first_crossing_time(RT, 3.0) is None
    assert first_crossing_time(RT, 1.0) == 0  # entry bar itself


def test_first_crossing_time_and_peaked_ge_reject_bad_mult():
    # Each bad multiple is a genuinely different failure: zero, negative,
    # NaN, and infinite thresholds must all fail closed, on both helpers.
    for bad in (0, -2.0, math.nan, math.inf):
        with pytest.raises(ValueError):
            first_crossing_time(RT, bad)
        with pytest.raises(ValueError):
            peaked_ge(RT, bad)
    assert peaked_ge(RUG, 1.1) is True  # inclusive boundary still works
    assert peaked_ge(RUG, 1.100001) is False


def test_time_above_multiple_counts_intervals_from_qualifying_bars():
    # RT is >= 2.0x on bars t=30,40,50,60 -> four 10-min intervals = 40.
    assert time_above_multiple(RT, 2.0) == pytest.approx(40.0)
    assert time_above_multiple(RUG, 2.0) == 0.0
    path = [(0, 1.0), (10, 2.0), (25, 2.5), (40, 1.0)]
    assert time_above_multiple(path, 2.0) == pytest.approx(15.0 + 15.0)


def test_max_drawdown_hand_values():
    assert max_drawdown([(0, 1.0), (10, 2.0), (20, 1.0)]) == pytest.approx(0.5)
    assert max_drawdown([(0, 1.0), (10, 1.5), (20, 2.0)]) == 0.0  # never falls
    assert max_drawdown(RT) == pytest.approx((2.6 - 0.3) / 2.6)


def test_max_drawdown_uses_running_peak_not_the_future_peak():
    # Early halving from 1.0 counts 0.5; pricing it off the later 2.0 peak
    # (look-ahead) would claim 0.75.
    path = [(0, 1.0), (10, 0.5), (20, 2.0), (30, 1.9)]
    assert max_drawdown(path) == pytest.approx(0.5)


def test_time_to_peak_reports_the_first_peak_on_a_tie():
    assert time_to_peak([(0, 1.0), (10, 2.0), (20, 2.0), (30, 1.5)]) == 10


# --- Result / batch analytics ---

def test_exit_reason_breakdown_sums_to_the_whole_position():
    assert exit_reason_breakdown(replay(make_clean_runner(), "user_ladder")) == {
        "tp": pytest.approx(0.75), "trail": pytest.approx(0.25)}
    bd = exit_reason_breakdown(replay(RT, "user_ladder"))
    assert bd == {"tp": pytest.approx(0.5), "trail": pytest.approx(0.25),
                  "hard_stop": pytest.approx(0.25)}
    assert sum(bd.values()) == pytest.approx(1.0)


def test_weighted_and_last_exit_time_on_clean_runner():
    r = replay(make_clean_runner(), "user_ladder")
    # Legs close at t=30 (50%), t=40 (25%), t=100 (25%): 15 + 10 + 25 = 50.
    assert weighted_exit_time(r) == pytest.approx(50.0)
    assert last_exit_time(r) == 100


def test_exit_time_helpers_fail_closed_on_an_empty_result():
    empty = ReplayResult(ladder="x", realized_mult=0.0, peak_mult=0.0, legs=[])
    with pytest.raises(ValueError):
        weighted_exit_time(empty)
    with pytest.raises(ValueError):
        last_exit_time(empty)


def test_is_round_tripper_classification():
    assert is_round_tripper(replay(RT, "hold_to_stop")) is True
    assert is_round_tripper(replay(RT, "user_ladder")) is False  # harvested 1.58x
    assert is_round_tripper(replay(make_clean_runner(), "hold_to_stop")) is False
    assert is_round_tripper(replay(RUG, "hold_to_stop")) is False  # never doubled


def test_compare_ladders_matches_individual_replays():
    comp = compare_ladders(RT)
    assert list(comp) == list(LADDERS)
    for name, res in comp.items():
        assert res.realized_mult == pytest.approx(replay(RT, name).realized_mult)
    subset = compare_ladders(RT, ["hold_to_stop"])
    assert list(subset) == ["hold_to_stop"]


def test_compare_ladders_fail_closed():
    with pytest.raises(ValueError):
        compare_ladders(RT, [])
    with pytest.raises(ValueError):
        compare_ladders(RT, ["nope"])


def test_batch_single_path_and_median_with_odd_count():
    stats = replay_batch([RUG], "user_ladder")
    assert stats["mean_mult"] == pytest.approx(0.5)
    assert stats["win_rate"] == 0.0
    assert stats["round_trippers"] == 0
    assert stats["median_time_to_peak_min"] == pytest.approx(10.0)
    empty = replay_batch([], "user_ladder")
    assert empty == {"mean_mult": 0.0, "win_rate": 0.0, "round_trippers": 0,
                     "median_time_to_peak_min": 0.0}
    # Passing the Ladder object instead of its name gives identical stats.
    assert replay_batch([RT], LADDERS["user_ladder"]) == replay_batch([RT], "user_ladder")


# --- Custom ladders: validation is fail-closed ---

def test_custom_ladder_replays_with_hand_math():
    lad = Ladder(name="half_at_150",
                 tranches=(Tranche(0.5, "tp", target_mult=1.5), Tranche(0.5, "hold")),
                 hard_stop_mult=0.5, time_cap_min=1440.0)
    r = replay([(0, 1.0), (10, 1.6), (20, 1.2)], lad)
    # 0.50*1.5 (tp) + 0.50*1.2 (path end) = 1.35
    assert r.realized_mult == pytest.approx(1.35)
    assert r.ladder == "half_at_150"


def test_ladder_fractions_must_sum_to_one():
    lad = Ladder(name="short", tranches=(Tranche(0.5, "hold"), Tranche(0.25, "hold")),
                 hard_stop_mult=0.5, time_cap_min=100.0)
    with pytest.raises(ValueError):
        validate_ladder(lad)
    with pytest.raises(ValueError):
        replay([(0, 1.0), (10, 1.1)], lad)


def test_validate_ladder_rejects_malformed_tranches_and_fields():
    # Six distinct malformations, each breaking a different invariant.
    bad_ladders = [
        Ladder(name="bad_tp", tranches=(Tranche(1.0, "tp", target_mult=0.9),),
               hard_stop_mult=0.5, time_cap_min=100.0),       # tp target <= 1
        Ladder(name="bad_trail", tranches=(Tranche(1.0, "trail", trail_pct=1.5),),
               hard_stop_mult=0.5, time_cap_min=100.0),       # trail_pct >= 1
        Ladder(name="bad_stop", tranches=(Tranche(1.0, "hold"),),
               hard_stop_mult=1.0, time_cap_min=100.0),       # stop not below 1
        Ladder(name="bad_cap", tranches=(Tranche(1.0, "hold"),),
               hard_stop_mult=0.5, time_cap_min=0.0),         # zero time cap
        Ladder(name="bad_kind", tranches=(Tranche(1.0, "yolo"),),
               hard_stop_mult=0.5, time_cap_min=100.0),       # unknown kind
        Ladder(name="", tranches=(Tranche(1.0, "hold"),),
               hard_stop_mult=0.5, time_cap_min=100.0),       # empty name
    ]
    for lad in bad_ladders:
        with pytest.raises(ValueError):
            validate_ladder(lad)
    for lad in LADDERS.values():  # the builtins all survive the same check
        assert validate_ladder(lad) is lad


def test_replay_rejects_a_non_ladder_non_string():
    with pytest.raises(ValueError):
        replay(RT, 42)


# --- Fail-closed path validation, extended ---

def test_fail_closed_on_malformed_paths():
    # Eight distinct malformations: non-finite prices/entry, non-positive
    # prices/entry, and decreasing timestamps.
    bad_paths = [
        [(0, 1.0), (10, math.nan)], [(0, 1.0), (10, math.inf)],
        [(0, math.inf), (10, 1.1)], [(0, 1.0), (10, 0.0)],
        [(0, 1.0), (10, -0.5)], [(0, 1.0), (20, 1.1), (10, 1.2)],
        [(0, 0.0), (10, 1.1)], [(0, -1.0), (10, 1.1)],
    ]
    for path in bad_paths:
        with pytest.raises(ValueError):
            replay(path, "user_ladder")
        with pytest.raises(ValueError):
            time_to_peak(path)
    # The demo fixtures are the well-formed control group.
    for path in DEMO_PATHS.values():
        assert path[0] == (0, 1.0)
        ts = [t for t, _ in path]
        assert ts == sorted(ts) and len(set(ts)) == len(ts)
