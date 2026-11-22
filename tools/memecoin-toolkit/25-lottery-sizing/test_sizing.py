"""Tests for sizing.py -- hand-checked distributions, seeded Monte Carlo."""
import pytest

from sizing import (expectancy, expected_log_growth,
                    growth_comparison, kelly_fraction, prob_of_loss,
                    prob_of_ruin_outcome, ruin_probability,
                    simulate_bankroll, validate_distribution, variance)

LOTTERY = [(0.90, 0.0), (0.10, 15.0)]   # expectancy +0.50, Kelly = 0.5/14
BINARY = [(0.60, 0.0), (0.40, 3.0)]     # expectancy +0.20, Kelly = 0.10
NEGATIVE = [(0.90, 0.0), (0.10, 5.0)]   # expectancy -0.50


# -- validation -------------------------------------------------------------------

def test_valid_distribution_round_trips():
    assert validate_distribution(LOTTERY) == [(0.90, 0.0), (0.10, 15.0)]


def test_probabilities_summing_below_one_rejected():
    with pytest.raises(ValueError):
        validate_distribution([(0.5, 0.0), (0.4, 15.0)])


def test_probabilities_summing_above_one_rejected():
    with pytest.raises(ValueError):
        validate_distribution([(0.6, 0.0), (0.6, 15.0)])


def test_probability_sum_within_tolerance_accepted():
    dist = [(0.3333333, 0.0), (0.3333333, 1.0), (0.3333334, 2.0)]
    assert len(validate_distribution(dist)) == 3


def test_empty_and_none_distributions_rejected():
    with pytest.raises(ValueError):
        validate_distribution([])
    with pytest.raises(ValueError):
        validate_distribution(None)


@pytest.mark.parametrize("bad_prob", [0.0, 1.5])
def test_bad_probability_rejected(bad_prob):
    with pytest.raises(ValueError):
        validate_distribution([(bad_prob, 2.0), (0.5, 1.0)])


@pytest.mark.parametrize("bad_multiple", [-1.0, "15"])
def test_bad_multiple_rejected(bad_multiple):
    with pytest.raises(ValueError):
        validate_distribution([(0.5, bad_multiple), (0.5, 1.0)])


def test_malformed_pairs_rejected():
    with pytest.raises(ValueError):
        validate_distribution([(0.5,), (0.5, 1.0)])
    with pytest.raises(ValueError):
        validate_distribution([0.5, 0.5])


def test_zero_multiple_is_valid_total_loss():
    assert validate_distribution([(1.0, 0.0)]) == [(1.0, 0.0)]


def test_single_certain_outcome_is_valid():
    assert expectancy([(1.0, 2.0)]) == pytest.approx(1.0)


# -- expectancy ---------------------------------------------------------------------

def test_expectancy_lottery_and_binary():
    assert expectancy(LOTTERY) == pytest.approx(0.50)
    assert expectancy(BINARY) == pytest.approx(0.20)


def test_expectancy_negative_distribution():
    assert expectancy(NEGATIVE) == pytest.approx(-0.50)


def test_expectancy_break_even_distribution():
    assert expectancy([(0.5, 0.0), (0.5, 2.0)]) == pytest.approx(0.0)


def test_variance_lottery():
    # Mean 1.5; 0.9*(1.5)^2 + 0.1*(13.5)^2 = 2.025 + 18.225 = 20.25.
    assert variance(LOTTERY) == pytest.approx(20.25)


def test_prob_of_loss_and_ruin_outcome():
    assert prob_of_loss(LOTTERY) == pytest.approx(0.90)
    assert prob_of_ruin_outcome(LOTTERY) == pytest.approx(0.90)
    assert prob_of_loss([(0.5, 0.5), (0.5, 2.0)]) == pytest.approx(0.5)
    assert prob_of_ruin_outcome([(0.5, 0.5), (0.5, 2.0)]) == 0.0


# -- expected log growth ----------------------------------------------------------------

def test_log_growth_at_zero_fraction_is_zero():
    assert expected_log_growth(0.0, LOTTERY) == 0.0


def test_log_growth_positive_below_kelly():
    assert expected_log_growth(0.01, LOTTERY) > 0


def test_log_growth_at_kelly_exceeds_flat_and_double():
    comp = growth_comparison(LOTTERY)
    assert comp["kelly"][1] > comp["flat"][1]
    assert comp["kelly"][1] > comp["double_kelly"][1]


def test_pinned_overbetting_grows_slower():
    # Pinned: 2x Kelly on a +50%-expectancy lottery grows slower.
    kelly_growth = expected_log_growth(kelly_fraction(LOTTERY), LOTTERY)
    double_growth = expected_log_growth(2 * kelly_fraction(LOTTERY), LOTTERY)
    assert kelly_growth == pytest.approx(0.007816, abs=1e-5)
    assert double_growth == pytest.approx(0.002618, abs=1e-5)
    assert double_growth < kelly_growth


def test_pinned_far_overbet_turns_growth_negative():
    # f=0.50: same +0.50 expectancy, but expected log growth < 0.
    assert expectancy(LOTTERY) > 0
    assert expected_log_growth(0.50, LOTTERY) < 0


def test_log_growth_full_bankroll_with_total_loss_raises():
    with pytest.raises(ValueError):
        expected_log_growth(1.0, LOTTERY)  # factor 0 on the 0x outcome


def test_log_growth_full_bankroll_certain_win_is_fine():
    assert expected_log_growth(1.0, [(1.0, 2.0)]) == pytest.approx(0.6931, abs=1e-3)


@pytest.mark.parametrize("bad_f", [-0.1, 1.01])
def test_bad_fraction_rejected(bad_f):
    with pytest.raises(ValueError):
        expected_log_growth(bad_f, LOTTERY)


# -- Kelly ---------------------------------------------------------------------------------

def test_kelly_lottery_matches_analytic_value():
    # Maximise 0.9*log(1-f) + 0.1*log(1+14f) -> f = 0.5/14 = 1/28.
    assert kelly_fraction(LOTTERY) == pytest.approx(1 / 28, abs=1e-6)


def test_kelly_binary_matches_analytic_value():
    # Maximise 0.6*log(1-f) + 0.4*log(1+2f) -> f = 0.10 exactly.
    assert kelly_fraction(BINARY) == pytest.approx(0.10, abs=1e-6)


def test_kelly_binary_growth_at_optimum():
    assert expected_log_growth(0.10, BINARY) == pytest.approx(0.009712, abs=1e-5)


def test_kelly_negative_expectancy_is_zero():
    assert kelly_fraction(NEGATIVE) == 0.0


def test_kelly_break_even_is_zero():
    assert kelly_fraction([(0.5, 0.0), (0.5, 2.0)]) == 0.0


def test_kelly_certain_win_is_full_bankroll():
    assert kelly_fraction([(1.0, 2.0)]) == pytest.approx(1.0, abs=1e-6)


def test_kelly_never_exceeds_one_and_never_negative():
    dists = [LOTTERY, BINARY, [(0.99, 0.0), (0.01, 500.0)],
             [(0.5, 0.5), (0.5, 3.0)]]
    for dist in dists:
        assert 0.0 <= kelly_fraction(dist) <= 1.0


def test_kelly_high_multiple_longshot():
    # 0.99*log(1-f) + 0.01*log(1+499f): optimum f = 3.99/499... check
    # via the defining property: growth at Kelly >= growth nearby.
    dist = [(0.99, 0.0), (0.01, 500.0)]
    kelly = kelly_fraction(dist)
    assert kelly > 0
    assert expected_log_growth(kelly, dist) >= \
        expected_log_growth(kelly * 0.5, dist)
    assert expected_log_growth(kelly, dist) >= \
        expected_log_growth(min(kelly * 1.5, 0.99), dist)


def test_growth_comparison_structure():
    comp = growth_comparison(LOTTERY)
    assert set(comp) == {"flat", "kelly", "double_kelly"}
    assert comp["flat"][0] == 0.01
    assert comp["double_kelly"][0] == pytest.approx(2 * comp["kelly"][0])


# -- Monte Carlo ruin ---------------------------------------------------------------------------

def test_ruin_probability_reproducible_for_same_seed():
    first = ruin_probability(LOTTERY, n_bets=100, n_sims=100, seed=7)
    second = ruin_probability(LOTTERY, n_bets=100, n_sims=100, seed=7)
    assert first == second


def test_ruin_certain_win_is_zero():
    assert ruin_probability([(1.0, 2.0)], n_bets=50, n_sims=50, seed=1) == 0.0


def test_ruin_certain_loss_is_one():
    # 150 straight $1 losses against a $100 bankroll must ruin;
    # a 50-bet horizon could only lose $50, so it cannot show ruin.
    assert ruin_probability([(1.0, 0.0)], n_bets=150, n_sims=20, seed=1) == 1.0


def test_ruin_negative_distribution_thin_bankroll_is_one():
    # Pinned seeded result: NEGATIVE on $10 over 200 shots always ruins.
    assert ruin_probability(NEGATIVE, bankroll=10.0, bet=1.0,
                            n_bets=200, n_sims=200, seed=42) == 1.0


def test_ruin_lottery_full_bankroll_is_zero_over_200_shots():
    # Pinned seeded result: $100 vs $1 shots survives 200 shots here.
    assert ruin_probability(LOTTERY, bankroll=100.0, bet=1.0,
                            n_bets=200, n_sims=200, seed=42) == 0.0


def test_ruin_thinner_bankroll_is_riskier():
    thick = ruin_probability(LOTTERY, bankroll=100.0, n_bets=200,
                             n_sims=200, seed=42)
    thin = ruin_probability(LOTTERY, bankroll=10.0, n_bets=200,
                            n_sims=200, seed=42)
    assert thin > thick


def test_larger_bets_ruin_more_than_smaller_bets():
    small = ruin_probability(LOTTERY, bankroll=100.0, bet=1.0,
                             n_bets=200, n_sims=200, seed=42)
    big = ruin_probability(LOTTERY, bankroll=100.0, bet=10.0,
                           n_bets=200, n_sims=200, seed=42)
    assert big > small


def test_simulate_bankroll_seeded_path_is_deterministic():
    first = simulate_bankroll(LOTTERY, seed=7, n_bets=10)
    assert first == simulate_bankroll(LOTTERY, seed=7, n_bets=10)
    assert first == 90.0  # pinned: ten straight losses on seed 7's stream


def test_simulate_bankroll_certain_win_grows():
    assert simulate_bankroll([(1.0, 2.0)], bankroll=100.0, bet=1.0,
                             n_bets=10, seed=0) == 110.0


def test_ruin_rejects_bad_arguments():
    with pytest.raises(ValueError):
        ruin_probability(LOTTERY, bankroll=0)
    with pytest.raises(ValueError):
        ruin_probability(LOTTERY, bet=0)
    with pytest.raises(ValueError):
        ruin_probability(LOTTERY, bet=200.0, bankroll=100.0)
    with pytest.raises(ValueError):
        ruin_probability(LOTTERY, n_sims=0)
    with pytest.raises(ValueError):
        ruin_probability(LOTTERY, n_bets=0)
    with pytest.raises(ValueError):
        simulate_bankroll(LOTTERY, bankroll=-5.0)
