"""Tests for grading.py -- follower simulation, packs, shrinkage, mute."""

import pytest

from grading import (
    convergence_count,
    follow,
    grade_weight,
    group_wallets,
    is_muted,
    simulate_follows,
    wallet_stats,
)

# A path that runs hard, then dies: the classic copier trap.
DYING_PATH = [
    (0, 1.00),
    (1, 1.20),
    (2, 1.60),
    (3, 2.10),
    (4, 1.40),
    (5, 0.40),  # last point: where every copier exits
]
EVENT = {"wallet": "W1", "mint": "MINT1", "t": 0, "price": 1.00}


# ------------------------------------------------------------------- follow

def test_follow_enters_at_first_price_reaching_lag_target():
    result = follow(EVENT, DYING_PATH, 0.15)
    assert result["entry_t"] == 1
    assert result["entry_price"] == pytest.approx(1.20)
    assert result["exit_price"] == pytest.approx(0.40)


def test_lag_decay_realized_falls_as_entry_lag_grows():
    early = follow(EVENT, DYING_PATH, 0.15)
    mid = follow(EVENT, DYING_PATH, 0.50)
    late = follow(EVENT, DYING_PATH, 1.00)
    assert early["realized_mult"] > mid["realized_mult"]
    assert mid["realized_mult"] > late["realized_mult"]
    assert early["realized_mult"] == pytest.approx(0.40 / 1.20)
    assert late["realized_mult"] == pytest.approx(0.40 / 2.10)


def test_follow_skipped_when_lag_level_never_reached():
    flat = [(0, 1.00), (1, 1.05), (2, 1.10)]
    assert follow(EVENT, flat, 0.15) is None
    assert follow(EVENT, flat, 1.00) is None


def test_follow_rejects_undocumented_lag_gain():
    with pytest.raises(ValueError):
        follow(EVENT, DYING_PATH, 0.25)


def test_follow_ignores_prices_before_the_wallet_buy():
    path = [(0, 5.00), (10, 1.00), (11, 1.20), (12, 2.00)]
    event = {"wallet": "W1", "mint": "M", "t": 10, "price": 1.00}
    result = follow(event, path, 0.15)
    assert result["entry_t"] == 11  # not the pre-buy spike at t=0


def test_simulate_follows_drops_skips_and_stats_aggregate():
    events = [
        EVENT,
        {"wallet": "W2", "mint": "FLAT", "t": 0, "price": 1.00},
    ]
    paths = {"MINT1": DYING_PATH, "FLAT": [(0, 1.0), (1, 1.01)]}
    results = simulate_follows(events, paths, 0.15)
    assert len(results) == 1
    stats = wallet_stats(results)
    n, avg = stats["W1"]
    assert n == 1
    assert avg == pytest.approx(0.40 / 1.20 - 1.0)


# ---------------------------------------------------------- pack independence

def test_pack_of_three_one_funder_counts_as_one_actor():
    wallets = {"A": "FUNDER-X", "B": "FUNDER-X", "C": "FUNDER-X"}
    assert convergence_count(wallets) == 1
    groups = group_wallets(wallets)
    assert groups == {"FUNDER-X": ["A", "B", "C"]}


def test_two_funders_count_as_two_actors():
    wallets = [
        {"wallet": "A", "funder": "F1"},
        {"wallet": "B", "funder": "F1"},
        {"wallet": "C", "funder": "F2"},
    ]
    assert convergence_count(wallets) == 2


def test_wallet_without_funder_is_its_own_actor():
    wallets = [("A", "F1"), ("B", "F1"), ("C", None)]
    assert convergence_count(wallets) == 2
    assert group_wallets(wallets)["C"] == ["C"]


def test_pairs_input_matches_dict_input():
    as_dict = {"A": "F1", "B": "F1", "C": "F2"}
    as_pairs = [("A", "F1"), ("B", "F1"), ("C", "F2")]
    assert group_wallets(as_dict) == group_wallets(as_pairs)


# ------------------------------------------------------------ grade weighting

def test_grade_weight_neutral_for_no_history():
    assert grade_weight(0, 0.0) == pytest.approx(1.0)


def test_shrinkage_pulls_small_n_toward_prior():
    small = grade_weight(1, 1.00, prior=0.0, m=10)
    large = grade_weight(100, 1.00, prior=0.0, m=10)
    assert small == pytest.approx(1.0 + 1.0 / 11.0)
    assert abs(small - 1.0) < abs(large - 1.0)


def test_grade_weight_exact_shrunk_value():
    # (10 * 0.5 + 10 * 0.0) / 20 = 0.25 shrunk -> weight 1.25
    assert grade_weight(10, 0.5, prior=0.0, m=10) == pytest.approx(1.25)


def test_grade_weight_clamped_at_both_ends():
    assert grade_weight(1000, 5.0) == 1.5
    assert grade_weight(1000, -5.0) == 0.5


def test_grade_weight_respects_nonzero_prior():
    # n=0 means only the prior speaks: weight = 1 + prior.
    assert grade_weight(0, 9.9, prior=0.2, m=10) == pytest.approx(1.2)


# ------------------------------------------------------------------ auto-mute

def test_mute_boundary_exactly_10_follows_at_minus_20_percent():
    assert is_muted(10, -0.20) is True


def test_no_mute_at_9_follows_however_bad():
    assert is_muted(9, -0.99) is False


def test_no_mute_when_average_above_threshold():
    assert is_muted(10, -0.19) is False
    assert is_muted(50, 0.10) is False


def test_mute_with_more_follows_and_worse_average():
    assert is_muted(25, -0.35) is True


# ------------------------------------------------------------- follow extras

def test_follow_entry_at_exact_target_price_counts():
    path = [(0, 1.00), (1, 1.15), (2, 2.30)]
    result = follow(EVENT, path, 0.15)
    assert result["entry_t"] == 1  # >= target, so exactly 1.15 enters
    assert result["realized_mult"] == pytest.approx(2.0)


def test_follow_empty_path_is_a_skip_not_a_crash():
    assert follow(EVENT, [], 0.15) is None


def test_follow_entering_on_the_last_point_realizes_exactly_one():
    path = [(0, 1.00), (1, 1.05), (2, 1.20)]
    result = follow(EVENT, path, 0.15)
    assert result["entry_t"] == result["exit_t"] == 2
    assert result["realized_mult"] == pytest.approx(1.0)
    assert result["realized_fraction"] == pytest.approx(0.0)


def test_follow_overshoot_is_charged_to_the_copier():
    # The +15% target is 1.15, but the first print at/above it is 1.90:
    # the copier pays 1.90, not the target.
    path = [(0, 1.00), (1, 1.90), (2, 1.90)]
    result = follow(EVENT, path, 0.15)
    assert result["entry_price"] == pytest.approx(1.90)
    assert result["realized_mult"] == pytest.approx(1.0)


def test_follow_all_lags_keys_and_skips():
    from grading import follow_all_lags
    results = follow_all_lags(EVENT, DYING_PATH)
    assert list(results) == [0.15, 0.50, 1.00]
    assert all(r is not None for r in results.values())
    flat = follow_all_lags(EVENT, [(0, 1.0), (1, 1.02)])
    assert flat == {0.15: None, 0.50: None, 1.00: None}


def test_wallet_outcome_fraction_is_the_baseline():
    from grading import wallet_outcome_fraction
    assert wallet_outcome_fraction(EVENT, DYING_PATH) == pytest.approx(-0.60)
    assert wallet_outcome_fraction(EVENT, []) is None
    late = {"wallet": "W", "mint": "M", "t": 99, "price": 1.0}
    assert wallet_outcome_fraction(late, DYING_PATH) is None


def test_lag_cost_fraction_quantifies_the_delay():
    from grading import lag_cost_fraction
    # Wallet held to -60%; the +15% copier realised -66.7%: lag cost 6.7pts.
    assert lag_cost_fraction(EVENT, DYING_PATH, 0.15) == pytest.approx(1 / 15)
    # The +100% copier realised 0.4/2.1 - 1, so the delay cost far more.
    assert lag_cost_fraction(EVENT, DYING_PATH, 1.00) == pytest.approx(
        -0.60 - (0.40 / 2.10 - 1.0)
    )


def test_lag_cost_is_none_when_the_copier_never_entered():
    from grading import lag_cost_fraction
    flat = [(0, 1.00), (1, 1.10)]
    assert lag_cost_fraction(EVENT, flat, 0.15) is None


def test_follow_with_stop_exits_on_the_first_breach():
    from grading import follow_with_stop
    result = follow_with_stop(EVENT, DYING_PATH, 0.15, 0.25)
    # Entry 1.20 at t=1; stop is 0.90. Price first breaches at t=5 (0.40).
    assert result["stopped_out"] is True
    assert result["exit_t"] == 5
    assert result["realized_mult"] == pytest.approx(0.40 / 1.20)


def test_follow_with_stop_can_beat_holding_to_the_end():
    from grading import follow_with_stop
    path = [(0, 1.00), (1, 1.20), (2, 0.95), (3, 0.50), (4, 0.10)]
    stopped = follow_with_stop(EVENT, path, 0.15, 0.20)
    held = follow(EVENT, path, 0.15)
    assert stopped["exit_t"] == 2  # 0.95 <= 1.20 * 0.80 = 0.96
    assert stopped["realized_mult"] == pytest.approx(0.95 / 1.20)
    assert stopped["realized_mult"] > held["realized_mult"]


def test_follow_with_stop_not_triggered_matches_plain_follow():
    from grading import follow_with_stop
    rising = [(0, 1.00), (1, 1.20), (2, 1.30), (3, 1.50)]
    result = follow_with_stop(EVENT, rising, 0.15, 0.50)
    assert result["stopped_out"] is False
    assert result["exit_t"] == 3
    assert result["realized_mult"] == pytest.approx(1.50 / 1.20)


def test_follow_with_stop_validates_stop_and_still_skips():
    from grading import follow_with_stop
    with pytest.raises(ValueError):
        follow_with_stop(EVENT, DYING_PATH, 0.15, 0.0)
    with pytest.raises(ValueError):
        follow_with_stop(EVENT, DYING_PATH, 0.15, 1.0)
    assert follow_with_stop(EVENT, [(0, 1.0), (1, 1.01)], 0.15, 0.3) is None


def test_simulate_follows_propagates_bad_lag_gain():
    with pytest.raises(ValueError):
        simulate_follows([EVENT], {"MINT1": DYING_PATH}, 0.33)


def test_wallet_stats_multiple_wallets_and_empty():
    results = [
        {"wallet": "A", "realized_fraction": 0.5},
        {"wallet": "A", "realized_fraction": -0.1},
        {"wallet": "B", "realized_fraction": 2.0},
    ]
    stats = wallet_stats(results)
    assert stats["A"] == (2, pytest.approx(0.2))
    assert stats["B"] == (1, pytest.approx(2.0))
    assert wallet_stats([]) == {}


def test_median_realized_odd_even_and_empty():
    from grading import median_realized
    odd = [{"realized_fraction": v} for v in (0.9, -0.5, 0.1)]
    assert median_realized(odd) == pytest.approx(0.1)
    even = [{"realized_fraction": v} for v in (1.0, 0.0, -1.0, 0.5)]
    assert median_realized(even) == pytest.approx(0.25)
    assert median_realized([]) is None


# ---------------------------------------------------------- pack extras

def test_independence_ratio_values():
    from grading import independence_ratio
    assert independence_ratio({"A": "F", "B": "F", "C": "F", "D": "F"}) == pytest.approx(0.25)
    assert independence_ratio({"A": "F1", "B": "F2"}) == pytest.approx(1.0)
    assert independence_ratio([]) == 0.0
    assert convergence_count([]) == 0
    assert group_wallets([]) == {}


def test_meets_independence_threshold_and_validation():
    from grading import meets_independence
    wallets = {"A": "F1", "B": "F1", "C": "F2"}
    assert meets_independence(wallets, 2) is True
    assert meets_independence(wallets, 3) is False
    with pytest.raises(ValueError):
        meets_independence(wallets, 0)


def test_largest_pack_picks_biggest_then_alphabetical():
    from grading import largest_pack
    wallets = {"A": "F2", "B": "F2", "C": "F1", "D": "F1", "E": "F1"}
    assert largest_pack(wallets) == ("F1", ["C", "D", "E"])
    tied = {"A": "F2", "B": "F2", "C": "F1", "D": "F1"}
    assert largest_pack(tied)[0] == "F1"  # tie: alphabetically first funder
    assert largest_pack([]) is None


def test_duplicate_wallet_rows_do_not_double_count():
    rows = [("A", "F1"), ("A", "F1"), ("B", "F1")]
    assert group_wallets(rows) == {"F1": ["A", "B"]}
    assert convergence_count(rows) == 1


def test_empty_string_funder_means_own_actor():
    rows = [("A", ""), ("B", "F1"), ("C", "F1")]
    assert convergence_count(rows) == 2
    assert group_wallets(rows)["A"] == ["A"]


# ------------------------------------------------------------ grade extras

def test_grade_weight_rejects_negative_counts():
    with pytest.raises(ValueError):
        grade_weight(-1, 0.5)
    with pytest.raises(ValueError):
        grade_weight(5, 0.5, m=-1)


def test_grade_weight_with_zero_strength_prior_is_the_wallet_itself():
    # m=0 removes the prior entirely: weight = 1 + avg, clamped.
    assert grade_weight(4, 0.3, m=0) == pytest.approx(1.3)
    # n=0 and m=0: nothing speaks, so the prior itself is returned.
    assert grade_weight(0, 0.9, prior=-0.2, m=0) == pytest.approx(0.8)


def test_grade_weight_monotonic_in_average_and_in_n():
    assert grade_weight(20, 0.4) > grade_weight(20, 0.1) > grade_weight(20, -0.1)
    # Same average: more follows move the weight further from neutral.
    assert grade_weight(50, 0.4) > grade_weight(5, 0.4) > 1.0


def test_grade_all_and_rank_wallets_ordering():
    from grading import grade_all, rank_wallets
    stats = {"W-LOW": (50, -0.1), "W-HIGH": (50, 0.4), "W-MID": (50, 0.1)}
    graded = grade_all(stats)
    assert graded["W-HIGH"]["weight"] > graded["W-MID"]["weight"]
    assert graded["W-MID"]["weight"] > graded["W-LOW"]["weight"]
    ranked = rank_wallets(stats)
    assert [w for w, _ in ranked] == ["W-HIGH", "W-MID", "W-LOW"]


def test_rank_wallets_tie_breaks_on_evidence_then_name():
    from grading import rank_wallets
    # Identical (n, avg) for ZED/ANN: name decides. MORE has the same
    # average over more follows, so shrinkage gives it a higher weight.
    stats = {"ZED": (10, 0.2), "ANN": (10, 0.2), "MORE": (40, 0.2)}
    ranked = [w for w, _ in rank_wallets(stats)]
    assert ranked == ["MORE", "ANN", "ZED"]


def test_grade_wallet_bundle_mutes_and_weights_consistently():
    from grading import grade_wallet
    grade = grade_wallet(12, -0.30)
    assert grade == {
        "n_follows": 12,
        "avg_realized": -0.30,
        "weight": grade_weight(12, -0.30),
        "muted": True,
    }
