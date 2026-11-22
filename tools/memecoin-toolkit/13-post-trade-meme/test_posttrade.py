"""Tests for posttrade.py — every number below was computed by hand
from the 10-close fixture in posttrade.demo_fixture() (see module docs).

Fixture realized: degen 2.6, 0.4, 0.0, 1.8, 0.7 (sum 5.5)
                  steady 1.6, 1.1, 0.9, 0.0, 3.2 (sum 6.8) -> total 12.3
"""
import pytest

from posttrade import (breakeven_rate, demo_fixture, expectancy,
                       leakage_report, per_lane_scorecard, render_scorecard,
                       round_trippers, rug_rate, win_rate)

CLOSES = demo_fixture()


def test_overall_stats_hand_computed():
    assert win_rate(CLOSES) == pytest.approx(0.4)        # 2.6, 1.8, 1.6, 3.2 >= 1.5
    assert breakeven_rate(CLOSES) == pytest.approx(0.5)  # plus the 1.1 close
    assert expectancy(CLOSES) == pytest.approx(0.23)     # 12.3/10 - 1
    assert rug_rate(CLOSES) == pytest.approx(0.2)        # 2 rugs in 10


def test_round_trippers_and_leakage():
    rt = round_trippers(CLOSES)
    assert rt["count"] == 2                              # 0.4 (peak 2.6), 0.9 (peak 2.4)
    assert rt["mean_realized"] == pytest.approx(0.65)
    leak = leakage_report(CLOSES)
    assert leak["n_peaked_2x"] == 5                      # peaks 3.1, 2.6, 2.2, 2.4, 4.0
    assert leak["n_closed_win"] == 4
    assert leak["n_round_tripped"] == 2
    assert leak["avg_realized_round_trippers"] == pytest.approx(0.65)


def test_per_lane_scorecard_hand_computed():
    card = per_lane_scorecard(CLOSES)
    assert set(card) == {"degen", "steady"}
    d, s = card["degen"], card["steady"]
    assert d["n"] == 5 and d["wr"] == pytest.approx(0.4)
    assert d["expectancy"] == pytest.approx(0.10)       # 5.5/5 - 1
    assert d["rug_rate"] == pytest.approx(0.2)
    assert d["breakeven_rate"] == pytest.approx(0.4)
    assert s["n"] == 5 and s["wr"] == pytest.approx(0.4)
    assert s["expectancy"] == pytest.approx(0.36)       # 6.8/5 - 1
    assert s["rug_rate"] == pytest.approx(0.2)
    assert s["breakeven_rate"] == pytest.approx(0.6)


def test_empty_ledger_no_crash():
    assert win_rate([]) == 0.0
    assert breakeven_rate([]) == 0.0
    assert expectancy([]) == 0.0
    assert rug_rate([]) == 0.0
    assert round_trippers([]) == {"count": 0, "mean_realized": None}
    assert leakage_report([])["avg_realized_round_trippers"] is None
    assert per_lane_scorecard([]) == {}
    assert "no closes" in render_scorecard([])


def test_render_contains_lanes_and_totals():
    text = render_scorecard(CLOSES)
    assert "degen" in text and "steady" in text and "TOTAL" in text
    assert "leakage" in text


def test_missing_field_is_loud():
    bad = [{"lane": "degen", "mint": "X", "realized_mult": 1.0}]  # fields missing
    with pytest.raises(KeyError):
        win_rate(bad)


from posttrade import (WIN_MULT, PEAK_MULT, best_close, capture_rate,
                       cumulative_pnl_curve, exit_reason_breakdown,
                       filter_by_lane, lane_ranking, longest_loss_streak,
                       longest_win_streak, max_drawdown_usd, median_realized,
                       pnl_usd, profit_factor, verdict_ready, worst_close)


def rec(lane="degen", mint="M", realized=1.0, peak=1.0, reason="tp", rug=False):
    return {"lane": lane, "mint": mint, "realized_mult": realized,
            "peak_mult": peak, "exit_reason": reason, "was_rug": rug}


# ------------------------------------------------------------- fixture integrity

def test_fixture_shape_and_constants():
    assert WIN_MULT == 1.5 and PEAK_MULT == 2.0
    assert len(CLOSES) == 10
    required = {"lane", "mint", "realized_mult", "peak_mult", "exit_reason", "was_rug"}
    assert all(set(c) == required for c in CLOSES)
    assert {c["lane"] for c in CLOSES} == {"degen", "steady"}


def test_pnl_usd_scales_with_shot_size():
    assert pnl_usd(CLOSES) == pytest.approx(2.30)        # 12.3 in, 10 shots out
    assert pnl_usd(CLOSES, shot_usd=5.0) == pytest.approx(11.50)
    assert pnl_usd([]) == 0.0
    with pytest.raises(ValueError):
        pnl_usd(CLOSES, shot_usd=0)


def test_median_realized_fixture_and_odd_count():
    assert median_realized(CLOSES) == pytest.approx(1.0)  # (0.9 + 1.1) / 2
    odd = [rec(realized=0.2), rec(realized=3.0), rec(realized=1.0)]
    assert median_realized(odd) == pytest.approx(1.0)
    assert median_realized([]) is None


def test_best_and_worst_close_with_first_on_ties():
    assert best_close(CLOSES)["mint"] == "MINT_J"         # 3.2x
    assert worst_close(CLOSES)["mint"] == "MINT_C"        # first of the two 0.0x
    tied = [rec(mint="FIRST", realized=2.0), rec(mint="SECOND", realized=2.0)]
    assert best_close(tied)["mint"] == "FIRST"
    assert best_close([]) is None and worst_close([]) is None


# --------------------------------------------------------------- win boundaries

def test_win_and_breakeven_thresholds_are_inclusive():
    at_win = [rec(realized=1.5)]
    just_under = [rec(realized=1.4999)]
    assert win_rate(at_win) == 1.0
    assert win_rate(just_under) == 0.0
    assert breakeven_rate([rec(realized=1.0)]) == 1.0
    assert breakeven_rate([rec(realized=0.9999)]) == 0.0


def test_round_tripper_boundaries_are_exact():
    edge = [rec(realized=0.999, peak=2.0)]     # peak exactly 2x, closed < 1x
    assert round_trippers(edge)["count"] == 1
    saved = [rec(realized=1.0, peak=2.0)]      # closed AT breakeven: not a tripper
    assert round_trippers(saved)["count"] == 0
    shy_peak = [rec(realized=0.5, peak=1.999)]  # never actually saw 2x
    assert round_trippers(shy_peak)["count"] == 0


def test_capture_rate_counts_only_peaked_closes():
    assert capture_rate(CLOSES) == pytest.approx(0.6)  # A, D, J won of 5 peaked
    no_peaks = [rec(realized=3.0, peak=1.9), rec(realized=0.5, peak=1.0)]
    assert capture_rate(no_peaks) is None  # nothing to capture != captured nothing
    whiffed = [rec(realized=1.2, peak=2.5)]
    assert capture_rate(whiffed) == 0.0    # peaked, but 1.2x is not a win


def test_profit_factor_hand_computed():
    # Gains: 1.6+0.8+0.6+0.1+2.2 = 5.3 ; losses: 0.6+1.0+0.3+0.1+1.0 = 3.0
    assert profit_factor(CLOSES) == pytest.approx(5.3 / 3.0)


def test_profit_factor_edge_shapes():
    import math
    winners = [rec(realized=2.0), rec(realized=1.5)]
    assert math.isinf(profit_factor(winners))       # gains, zero losses
    flat = [rec(realized=1.0), rec(realized=1.0)]
    assert profit_factor(flat) is None              # neither side exists
    assert profit_factor([]) is None
    losers = [rec(realized=0.5), rec(realized=0.0)]
    assert profit_factor(losers) == 0.0             # no gains over real losses


# ------------------------------------------------------------------ pnl curve

def test_cumulative_pnl_curve_matches_hand_running_total():
    curve = cumulative_pnl_curve(CLOSES)
    assert curve == pytest.approx([1.6, 1.0, 0.0, 0.8, 0.5, 1.1, 1.2, 1.1, 0.1, 2.3])
    assert cumulative_pnl_curve([]) == []
    assert cumulative_pnl_curve(CLOSES, shot_usd=2.0)[-1] == pytest.approx(4.6)


def test_max_drawdown_from_the_curve_peak():
    # Peak +1.6 after close 1, trough 0.0 after close 3 -> drawdown 1.6.
    assert max_drawdown_usd(CLOSES) == pytest.approx(1.6)
    opening_losses = [rec(realized=0.5), rec(realized=0.5), rec(realized=3.0)]
    assert max_drawdown_usd(opening_losses) == pytest.approx(1.0)  # from the 0 start
    only_winners = [rec(realized=1.5), rec(realized=2.0)]
    assert max_drawdown_usd(only_winners) == 0.0
    assert max_drawdown_usd([]) == 0.0


# --------------------------------------------------------------------- streaks

def test_streaks_on_the_fixture():
    assert longest_win_streak(CLOSES) == 1   # no two fixture wins are adjacent
    assert longest_loss_streak(CLOSES) == 2  # B,C early and H,I late


def test_breakeven_band_breaks_both_streaks():
    closes = [rec(realized=2.0), rec(realized=1.2), rec(realized=2.0),
              rec(realized=0.5), rec(realized=1.0), rec(realized=0.5)]
    assert longest_win_streak(closes) == 1   # the 1.2x close breaks the run
    assert longest_loss_streak(closes) == 1  # the exactly-1.0x close breaks it too
    assert longest_win_streak([]) == 0 and longest_loss_streak([]) == 0


def test_all_loss_ledger_has_full_length_loss_streak():
    closes = [rec(realized=0.5), rec(realized=0.0), rec(realized=0.9)]
    assert longest_loss_streak(closes) == 3
    assert win_rate(closes) == 0.0
    assert expectancy(closes) == pytest.approx(-0.5333, abs=1e-3)


# --------------------------------------------------------------- breakdowns

def test_exit_reason_breakdown_hand_computed():
    out = exit_reason_breakdown(CLOSES)
    assert list(out) == sorted(out)  # deterministic key order
    assert out["trail"]["n"] == 3
    assert out["trail"]["wr"] == pytest.approx(2 / 3)
    assert out["trail"]["expectancy"] == pytest.approx(6.7 / 3 - 1)
    assert out["tp"] == {"n": 2, "wr": 1.0, "expectancy": pytest.approx(0.7)}
    assert out["rug"]["expectancy"] == pytest.approx(-1.0)
    assert out["hard_stop"]["n"] == 1 and out["stop"]["n"] == 1
    assert exit_reason_breakdown([]) == {}


def test_filter_by_lane_preserves_order():
    degen = filter_by_lane(CLOSES, "degen")
    assert [c["mint"] for c in degen] == ["MINT_A", "MINT_B", "MINT_C", "MINT_D", "MINT_E"]
    assert filter_by_lane(CLOSES, "no-such-lane") == []


def test_lane_ranking_orders_by_expectancy():
    ranked = lane_ranking(CLOSES)
    assert [lane for lane, _ in ranked] == ["steady", "degen"]  # +0.36 beats +0.10
    assert lane_ranking([]) == []


def test_single_close_ledger_is_fully_consistent():
    one = [rec(lane="solo", mint="ONE", realized=2.0, peak=2.5, reason="tp")]
    assert win_rate(one) == 1.0 and breakeven_rate(one) == 1.0
    assert expectancy(one) == pytest.approx(1.0)
    assert rug_rate(one) == 0.0
    assert per_lane_scorecard(one)["solo"]["n"] == 1
    assert leakage_report(one)["n_peaked_2x"] == 1
    assert capture_rate(one) == 1.0


def test_render_with_no_round_trippers_shows_na_average():
    text = render_scorecard([rec(realized=2.0, peak=2.5)])
    assert "avg realized n/a" in text
    assert "TOTAL" in text and "1" in text


# ---------------------------------------------------------------- verdict gate

def test_verdict_ready_needs_both_size_and_expectancy():
    assert verdict_ready(CLOSES, min_closes=10) is True
    assert verdict_ready(CLOSES, min_closes=11) is False       # not enough closes
    negative = [rec(realized=0.5) for _ in range(30)]
    assert verdict_ready(negative, min_closes=20) is False      # size without edge
    assert verdict_ready([], min_closes=1) is False


def test_verdict_ready_expectancy_bar_is_strict():
    even = [rec(realized=1.5), rec(realized=1.5)]  # expectancy exactly +0.5
    assert verdict_ready(even, min_closes=2, min_expectancy=0.5) is False
    assert verdict_ready(even, min_closes=2, min_expectancy=0.49) is True
    with pytest.raises(ValueError):
        verdict_ready(even, min_closes=0)


# ------------------------------------------------------------ corrupt records

@pytest.mark.parametrize("field,func", [
    ("realized_mult", win_rate),
    ("peak_mult", round_trippers),
    ("was_rug", rug_rate),
    ("lane", per_lane_scorecard),
    ("exit_reason", exit_reason_breakdown),
    ("mint", win_rate),
])
def test_each_missing_field_is_loud(field, func):
    bad = rec()
    del bad[field]
    with pytest.raises(KeyError):
        func([bad])


def test_leakage_report_when_nothing_peaked():
    closes = [rec(realized=1.8, peak=1.9), rec(realized=0.5, peak=1.2)]
    leak = leakage_report(closes)
    assert leak == {"n_peaked_2x": 0, "n_closed_win": 1,
                    "n_round_tripped": 0, "avg_realized_round_trippers": None}


def test_rug_rate_concentrated_in_one_lane():
    closes = [rec(lane="wild", realized=0.0, peak=0.9, reason="rug", rug=True),
              rec(lane="wild", realized=0.0, peak=1.0, reason="rug", rug=True),
              rec(lane="calm", realized=1.6, peak=1.8),
              rec(lane="calm", realized=1.1, peak=1.2)]
    assert rug_rate(closes) == pytest.approx(0.5)
    card = per_lane_scorecard(closes)
    assert card["wild"]["rug_rate"] == 1.0
    assert card["calm"]["rug_rate"] == 0.0


def test_zero_sum_ledger_has_exactly_zero_expectancy():
    closes = [rec(realized=1.7), rec(realized=0.3), rec(realized=1.0)]
    assert expectancy(closes) == pytest.approx(0.0)
    assert pnl_usd(closes) == pytest.approx(0.0)
    assert breakeven_rate(closes) == pytest.approx(2 / 3)


def test_render_total_row_carries_the_fixture_numbers():
    total_line = [ln for ln in render_scorecard(CLOSES).splitlines()
                  if ln.startswith("TOTAL")][0]
    assert "0.40" in total_line and "+0.230" in total_line and "0.20" in total_line
    lane_lines = [ln.split()[0] for ln in render_scorecard(CLOSES).splitlines()
                  if ln.startswith(("degen", "steady"))]
    assert lane_lines == ["degen", "steady"]  # alphabetical lane order


def test_max_drawdown_and_curve_scale_with_shot_size():
    assert max_drawdown_usd(CLOSES, shot_usd=3.0) == pytest.approx(4.8)
    with pytest.raises(ValueError):
        cumulative_pnl_curve(CLOSES, shot_usd=-1.0)


def test_median_of_single_and_pair():
    assert median_realized([rec(realized=0.42)]) == pytest.approx(0.42)
    pair = [rec(realized=0.0), rec(realized=2.0)]
    assert median_realized(pair) == pytest.approx(1.0)


def test_filtered_lane_matches_scorecard_row():
    degen = filter_by_lane(CLOSES, "degen")
    row = per_lane_scorecard(CLOSES)["degen"]
    assert expectancy(degen) == pytest.approx(row["expectancy"])
    assert win_rate(degen) == pytest.approx(row["wr"])
    assert len(degen) == row["n"]
