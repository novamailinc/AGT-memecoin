"""Tests for statslab.py — hand examples and the pinned power calculation."""
import math

import pytest

from statslab import (BASE_RATES, achieved_power, binomial_pvalue_at_least,
                      bootstrap_expectancy_ci, breakeven_wr, expectancy,
                      kelly_fraction, max_consecutive_losses,
                      max_profit_drawdown, median_multiple, profit_factor,
                      required_n, simulate_phacking, standard_error,
                      wilson_interval, win_rate)


def test_expectancy_hand_examples():
    assert expectancy([2.0, 0.0]) == pytest.approx(0.0)
    assert expectancy([3.0, 3.0]) == pytest.approx(2.0)
    assert expectancy([0.5, 0.5, 0.5]) == pytest.approx(-0.5)
    assert expectancy([1.0]) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        expectancy([])


def test_breakeven_wr():
    assert breakeven_wr(2.0) == pytest.approx(0.5)
    assert breakeven_wr(4.0) == pytest.approx(0.25)
    assert breakeven_wr(10.0) == pytest.approx(0.1)
    # consistency: at the breakeven WR, EV of win (W-1) / loss (-1) is 0
    w = breakeven_wr(3.0)
    assert w * (3.0 - 1) - (1 - w) * 1 == pytest.approx(0.0)
    with pytest.raises(ValueError):
        breakeven_wr(0.0)


def test_required_n_pinned_value():
    # (1.644854*sqrt(.21) + 0.841621*sqrt(.2275))^2 / .05^2 = 533.7 -> 534
    n = required_n(0.35, 0.30)
    assert n == 534
    assert 500 <= n <= 700  # sane range for this effect size
    assert required_n(0.40, 0.30) < n  # bigger effect needs fewer closes
    with pytest.raises(ValueError):
        required_n(0.30, 0.35)  # p1 must exceed p0


def test_achieved_power_inverts_required_n():
    n = required_n(0.35, 0.30, power=0.8)
    assert achieved_power(n, 0.30, 0.35) == pytest.approx(0.8, abs=0.01)
    assert achieved_power(50, 0.30, 0.35) < 0.3  # tiny samples are blind
    assert achieved_power(2000, 0.30, 0.35) > 0.99


def test_phacking_best_is_inflated_and_regresses():
    res = simulate_phacking(seed=42)
    assert res["best_in_sample_wr"] > 0.45  # noise, crowned as a discovery
    assert res["best_out_of_sample_wr"] < res["best_in_sample_wr"]
    assert res["mean_in_sample_wr"] == pytest.approx(0.30, abs=0.03)


def test_phacking_determinism():
    assert simulate_phacking(seed=7) == simulate_phacking(seed=7)
    assert simulate_phacking(seed=7) != simulate_phacking(seed=8)


def test_base_rates_constant():
    lo, hi = BASE_RATES["graduation_rate"]
    assert (lo, hi) == (0.0063, 0.017)
    assert BASE_RATES["one_in_launches_to_1m"] == 4200
    assert BASE_RATES["wallets_profitable_90d"] == pytest.approx(0.0625)


# --- Outcome summaries: win rate, median, profit factor, streaks, drawdown ---

def test_win_rate_counts_only_profitable_outcomes():
    assert win_rate([2.0, 0.0, 1.0, 3.0, 0.5]) == pytest.approx(0.4)  # 2 and 3 only
    assert win_rate([1.0, 1.0]) == 0.0  # breakeven is not a win
    assert win_rate([1.01]) == 1.0
    assert win_rate([0.99, 0.0]) == 0.0


def test_win_rate_and_expectancy_fail_closed_on_bad_outcomes():
    for bad in ([], [-0.5, 2.0], [math.nan], [math.inf, 1.0], [None, 1.0]):
        with pytest.raises(ValueError):
            win_rate(bad)
        with pytest.raises(ValueError):
            expectancy(bad)


def test_expectancy_all_losses_and_single_outcomes():
    assert expectancy([0.0, 0.0, 0.0]) == pytest.approx(-1.0)
    assert expectancy([0.0]) == pytest.approx(-1.0)
    assert expectancy([10.0]) == pytest.approx(9.0)
    assert expectancy([1.0, 1.0, 1.0]) == 0.0


def test_median_multiple_odd_even_and_single():
    assert median_multiple([0.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert median_multiple([0.0, 1.0, 2.0, 3.0]) == pytest.approx(1.5)
    assert median_multiple([5.0]) == pytest.approx(5.0)
    assert median_multiple([3.0, 0.0, 2.0]) == pytest.approx(2.0)  # order-free


def test_median_exposes_what_one_outlier_hides_from_the_mean():
    outcomes = [0.0] * 9 + [20.0]
    assert expectancy(outcomes) == pytest.approx(1.0)  # mean looks healthy
    assert median_multiple(outcomes) == 0.0  # the typical trade lost everything
    assert win_rate(outcomes) == pytest.approx(0.1)


def test_profit_factor_hand_values_and_edges():
    assert profit_factor([2.0, 0.0]) == pytest.approx(1.0)  # gain 1, loss 1
    assert profit_factor([3.0, 0.5, 0.5]) == pytest.approx(2.0)  # gain 2, loss 1
    assert profit_factor([2.0, 3.0]) == math.inf  # gains, no losses
    assert profit_factor([1.0, 1.0]) == 0.0  # no movement at all
    assert profit_factor([0.0, 0.0]) == 0.0  # losses, no gains
    assert profit_factor([1.0, 0.5]) == 0.0  # breakeven adds to neither side


def test_max_consecutive_losses_runs_and_breakeven_break():
    assert max_consecutive_losses([2.0, 0.0, 0.5, 3.0, 0.0, 0.0, 0.0, 2.0]) == 3
    assert max_consecutive_losses([2.0, 3.0]) == 0
    assert max_consecutive_losses([0.0, 1.0, 0.0]) == 1  # 1.0 breaks the streak
    assert max_consecutive_losses([0.0, 0.0, 0.0]) == 3


def test_max_profit_drawdown_hand_values():
    # Cumulative profit: 0, +1, 0, -1, +1 -> peak +1 to trough -1 = 2.0.
    assert max_profit_drawdown([2.0, 0.0, 0.0, 3.0]) == pytest.approx(2.0)
    assert max_profit_drawdown([2.0, 2.0]) == 0.0  # never falls
    assert max_profit_drawdown([0.0, 0.0]) == pytest.approx(2.0)
    # 0, -0.5, +0.5, -0.5: running peak reaches +0.5, final fall is 1.0.
    assert max_profit_drawdown([0.5, 2.0, 0.0]) == pytest.approx(1.0)


def test_standard_error_hand_value_and_failures():
    # [1, 3]: sample std sqrt(2), divided by sqrt(2) -> exactly 1.0.
    assert standard_error([1.0, 3.0]) == pytest.approx(1.0)
    assert standard_error([2.0, 2.0, 2.0]) == 0.0
    with pytest.raises(ValueError):
        standard_error([2.0])  # one trade has no knowable spread
    with pytest.raises(ValueError):
        standard_error([])


# --- Breakeven / Kelly consistency ---

def test_breakeven_wr_edges_and_invalid_payoffs():
    assert breakeven_wr(1.0) == pytest.approx(1.0)  # must win every time
    assert breakeven_wr(0.5) == pytest.approx(2.0)  # > 1: impossible to break even
    for bad in (0.0, -2.0, math.nan, math.inf, None):
        with pytest.raises(ValueError):
            breakeven_wr(bad)


def test_kelly_fraction_hand_values():
    assert kelly_fraction(0.5, 2.0) == pytest.approx(0.0)
    assert kelly_fraction(0.6, 2.0) == pytest.approx(0.2)
    assert kelly_fraction(0.5, 3.0) == pytest.approx(0.25)
    assert kelly_fraction(0.0, 2.0) == pytest.approx(-1.0)  # never wins: bet nothing, raw -1
    assert kelly_fraction(1.0, 4.0) == pytest.approx(1.0)  # certain win: full stake


def test_kelly_is_zero_exactly_at_breakeven_for_many_payoffs():
    for w in (1.5, 2.0, 3.0, 5.0, 10.0):
        assert kelly_fraction(breakeven_wr(w), w) == pytest.approx(0.0)
        assert kelly_fraction(breakeven_wr(w) + 0.05, w) > 0
        assert kelly_fraction(breakeven_wr(w) - 0.05, w) < 0


def test_kelly_rejects_unprofitable_payoffs_and_bad_win_rates():
    for bad_wr, bad_w in ((-0.1, 2.0), (1.1, 2.0), (0.5, 1.0), (0.5, 0.5), (0.5, 0.0)):
        with pytest.raises(ValueError):
            kelly_fraction(bad_wr, bad_w)


def test_win_rate_above_breakeven_matches_positive_expectancy():
    # 35 wins at 3x and 65 total losses: WR .35 > breakeven 1/3, EV +0.05.
    outcomes = [3.0] * 35 + [0.0] * 65
    assert win_rate(outcomes) == pytest.approx(0.35)
    assert expectancy(outcomes) == pytest.approx(0.05)
    assert win_rate(outcomes) > breakeven_wr(3.0)
    # At exactly 30% the same payoff loses: WR < breakeven, EV -0.10.
    losing = [3.0] * 30 + [0.0] * 70
    assert expectancy(losing) == pytest.approx(-0.10)
    assert win_rate(losing) < breakeven_wr(3.0)


# --- Required n / achieved power, extended ---

def test_required_n_monotonic_in_effect_alpha_and_power():
    base = required_n(0.35, 0.30)
    assert required_n(0.32, 0.30) == 3286  # halving-ish the gap explodes n
    assert required_n(0.32, 0.30) > 5 * base
    assert required_n(0.35, 0.30, alpha=0.10) == 392  # laxer alpha: fewer closes
    assert required_n(0.35, 0.30, alpha=0.10) < base
    assert required_n(0.35, 0.30, power=0.9) == 746  # more power: more closes
    assert required_n(0.35, 0.30, power=0.9) > base
    assert isinstance(base, int)


def test_required_n_rejects_every_bad_parameter_region():
    for args in ((0.30, 0.30), (0.29, 0.30), (0.35, 0.0), (1.0, 0.30)):
        with pytest.raises(ValueError):
            required_n(*args)
    for kwargs in ({"alpha": 0.0}, {"alpha": 1.0}, {"power": 0.0}, {"power": 1.0}):
        with pytest.raises(ValueError):
            required_n(0.35, 0.30, **kwargs)


def test_achieved_power_monotonic_and_bounded():
    powers = [achieved_power(n, 0.30, 0.35) for n in (10, 50, 100, 534, 1000)]
    assert powers == sorted(powers)
    assert all(0.0 < p < 1.0 for p in powers)
    assert powers[0] == pytest.approx(0.106, abs=0.01)
    assert powers[-1] == pytest.approx(0.959, abs=0.01)


def test_achieved_power_inverts_required_n_for_a_second_effect():
    n = required_n(0.40, 0.30, power=0.9)
    assert n == 191
    assert achieved_power(n, 0.30, 0.40) == pytest.approx(0.9, abs=0.01)


def test_achieved_power_rejects_bad_alpha_and_n():
    for bad_alpha in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            achieved_power(100, 0.30, 0.35, alpha=bad_alpha)
    for bad_n in (0, -5):
        with pytest.raises(ValueError):
            achieved_power(bad_n, 0.30, 0.35)
    with pytest.raises(ValueError):
        achieved_power(100, 0.35, 0.30)  # p1 must exceed p0


# --- Wilson interval and exact binomial p-values ---

def test_wilson_interval_boundary_hand_values():
    lo, hi = wilson_interval(0, 10)
    assert lo == pytest.approx(0.0, abs=1e-9)
    assert hi == pytest.approx(0.2775, abs=0.001)
    lo, hi = wilson_interval(10, 10)
    assert lo == pytest.approx(0.7225, abs=0.001)
    assert hi == pytest.approx(1.0)
    lo, hi = wilson_interval(50, 100)
    assert (lo, hi) == (pytest.approx(0.4038, abs=0.001), pytest.approx(0.5962, abs=0.001))
    assert lo < 0.5 < hi


def test_wilson_interval_narrows_with_n_and_widens_with_confidence():
    w100 = wilson_interval(50, 100)
    w1000 = wilson_interval(500, 1000)
    assert (w1000[1] - w1000[0]) < (w100[1] - w100[0]) / 3  # ~sqrt(10) narrower
    narrow = wilson_interval(30, 100, alpha=0.10)
    wide = wilson_interval(30, 100, alpha=0.05)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])
    assert narrow[0] > wide[0] and narrow[1] < wide[1]  # strictly nested


def test_wilson_interval_rejects_impossible_counts():
    for wins, n in ((11, 10), (-1, 10), (0, 0), (5, -3)):
        with pytest.raises(ValueError):
            wilson_interval(wins, n)
    for bad_alpha in (0.0, 1.0):
        with pytest.raises(ValueError):
            wilson_interval(5, 10, alpha=bad_alpha)


def test_binomial_pvalue_hand_values():
    assert binomial_pvalue_at_least(10, 10, 0.5) == pytest.approx(1 / 1024)
    assert binomial_pvalue_at_least(9, 10, 0.5) == pytest.approx(11 / 1024)
    assert binomial_pvalue_at_least(3, 3, 0.3) == pytest.approx(0.027)
    assert binomial_pvalue_at_least(0, 10, 0.3) == 1.0  # at least zero wins: certain
    assert binomial_pvalue_at_least(1, 1, 0.3) == pytest.approx(0.3)


def test_binomial_pvalue_decreases_in_k_and_detects_a_real_gap():
    vals = [binomial_pvalue_at_least(k, 20, 0.3) for k in (6, 10, 14, 18)]
    assert vals == sorted(vals, reverse=True)
    assert binomial_pvalue_at_least(40, 100, 0.30) == pytest.approx(0.021, abs=0.002)
    assert binomial_pvalue_at_least(40, 100, 0.30) < 0.05  # 40/100 beats a 30% null
    assert binomial_pvalue_at_least(60, 100, 0.30) < 1e-8


def test_binomial_pvalue_rejects_bad_arguments():
    for k, n, p0 in ((11, 10, 0.5), (-1, 10, 0.5), (5, 0, 0.5),
                     (5, 10, 0.0), (5, 10, 1.0)):
        with pytest.raises(ValueError):
            binomial_pvalue_at_least(k, n, p0)


def test_wilson_and_binomial_tell_the_same_story():
    # 60 wins in 100 against a 30% null: interval clears the null, p-value tiny.
    lo, _ = wilson_interval(60, 100)
    assert lo > 0.30
    assert binomial_pvalue_at_least(60, 100, 0.30) < 0.001
    # 32 in 100: interval straddles the null, p-value nowhere near significant.
    lo, hi = wilson_interval(32, 100)
    assert lo < 0.30 < hi
    assert binomial_pvalue_at_least(32, 100, 0.30) > 0.3


# --- Bootstrap confidence interval ---

def test_bootstrap_ci_collapses_on_constant_outcomes():
    assert bootstrap_expectancy_ci([2.0, 2.0, 2.0], seed=1) == (1.0, 1.0)
    assert bootstrap_expectancy_ci([0.0, 0.0], seed=99) == (-1.0, -1.0)


def test_bootstrap_ci_deterministic_and_brackets_the_estimate():
    sample = [2.5, 0.0, 1.2, 0.4, 3.0, 0.0, 0.8]
    lo, hi = bootstrap_expectancy_ci(sample, seed=42)
    assert (lo, hi) == (pytest.approx(-0.657, abs=0.001), pytest.approx(0.929, abs=0.001))
    assert lo < expectancy(sample) < hi  # the interval straddles 0: no proven edge
    assert lo < 0 < hi
    assert bootstrap_expectancy_ci(sample, seed=42) == (lo, hi)  # same seed, same CI
    assert bootstrap_expectancy_ci(sample, seed=2)[1] != hi  # different seed, different draw


def test_bootstrap_ci_widens_with_level_and_fails_closed():
    sample = [2.5, 0.0, 1.2, 0.4, 3.0, 0.0, 0.8]
    lo80, hi80 = bootstrap_expectancy_ci(sample, seed=42, level=0.80)
    lo95, hi95 = bootstrap_expectancy_ci(sample, seed=42, level=0.95)
    assert lo95 <= lo80 and hi80 <= hi95
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci(sample, n_boot=50)
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci(sample, level=1.0)
    with pytest.raises(ValueError):
        bootstrap_expectancy_ci([], seed=42)


# --- P-hacking simulator, extended ---

def test_phacking_result_echoes_parameters_and_bounds():
    res = simulate_phacking(seed=5, n_strategies=1)
    assert res["seed"] == 5 and res["n_strategies"] == 1 and res["true_wr"] == 0.30
    assert res["best_index"] == 0
    assert res["best_in_sample_wr"] == res["mean_in_sample_wr"]  # one strategy: best is mean
    assert 0.0 <= res["best_out_of_sample_wr"] <= 1.0
    res50 = simulate_phacking(seed=42, true_wr=0.5)
    assert res50["mean_in_sample_wr"] == pytest.approx(0.5, abs=0.03)


def test_phacking_best_is_never_below_the_mean_for_any_seed():
    for seed in (1, 2, 3, 123):
        res = simulate_phacking(seed=seed, n_strategies=50, n_in_sample=30)
        assert res["best_in_sample_wr"] >= res["mean_in_sample_wr"]
        assert 0 <= res["best_index"] < 50
        assert res["inflation"] == pytest.approx(
            res["best_in_sample_wr"] - res["best_out_of_sample_wr"])


def test_phacking_rejects_bad_counts_and_rates():
    for kwargs in ({"n_strategies": 0}, {"n_in_sample": 0}, {"n_out_sample": -1},
                   {"true_wr": 0.0}, {"true_wr": 1.0}):
        with pytest.raises(ValueError):
            simulate_phacking(**kwargs)


def test_base_rates_are_probability_shaped():
    lo, hi = BASE_RATES["graduation_rate"]
    assert 0.0 < lo < hi < 1.0
    assert 0.0 < BASE_RATES["wallets_profitable_90d"] < 1.0
    assert BASE_RATES["one_in_launches_to_1m"] > 1
    # The profitable-wallet share sits inside the graduation range's order
    # of magnitude story: most launches and most wallets both lose.
    assert BASE_RATES["wallets_profitable_90d"] < 0.10
