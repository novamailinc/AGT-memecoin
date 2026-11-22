"""Tests for holders.py -- user-only concentration, strict boundaries."""
from holders import analyze, classify_holder, is_user_wallet, verdict_for

SUPPLY = 1_000_000  # 1% of supply == 10_000 units; keeps math readable

REGISTRY = {
    "POOL": {"owner_program": True, "on_curve": False},
    "CURVE": {"owner_program": True, "on_curve": False},
    "ESCROW": {"owner_program": True, "on_curve": False},
}


def registry_with_users(**amounts):
    registry = dict(REGISTRY)
    for name in amounts:
        registry[name] = {"owner_program": False, "on_curve": True}
    return registry


def holders_for(**amounts):
    return [{"address": name, "amount": amount}
            for name, amount in amounts.items()]


def test_pool_excluded_so_user_top1_differs_from_naive():
    registry = registry_with_users(WHALE=100_000, A=50_000, B=50_000)
    holders = holders_for(POOL=600_000, WHALE=100_000, A=50_000, B=50_000)
    result = analyze(holders, SUPPLY, registry)
    naive_top1 = 600_000 / SUPPLY * 100  # the pool, if you counted it
    assert naive_top1 == 60.0
    assert result.top1_user_pct == 10.0
    assert result.top1_user_pct != naive_top1
    assert result.excluded_count == 1
    assert result.verdict == "PASS"


def test_bonding_curve_and_escrow_also_excluded():
    registry = registry_with_users(A=100_000)
    holders = holders_for(CURVE=500_000, ESCROW=200_000, A=100_000)
    result = analyze(holders, SUPPLY, registry)
    assert result.excluded_count == 2
    assert result.top1_user_pct == 10.0
    assert result.user_holder_count == 1


def test_top10_sums_only_users_and_only_first_ten():
    amounts = {f"U{i}": 20_000 for i in range(12)}  # 12 users x 2%
    registry = registry_with_users(**amounts)
    result = analyze(holders_for(**amounts), SUPPLY, registry)
    assert result.top1_user_pct == 2.0
    assert result.top10_user_pct == 20.0  # ten users, not twelve


def test_boundary_top1_exactly_50_is_flag_not_fail():
    assert verdict_for(50.0, 10.0) == "FLAG"
    registry = registry_with_users(WHALE=500_000)
    assert analyze(holders_for(WHALE=500_000), SUPPLY, registry).verdict == "FLAG"


def test_boundary_top1_50_01_fails():
    assert verdict_for(50.01, 10.0) == "FAIL"
    registry = registry_with_users(WHALE=500_100)
    result = analyze(holders_for(WHALE=500_100), SUPPLY, registry)
    assert result.top1_user_pct == 50.01
    assert result.verdict == "FAIL"
    assert result.passed is False


def test_boundary_top10_exactly_70_is_flag_not_fail():
    assert verdict_for(10.0, 70.0) == "FLAG"
    assert verdict_for(10.0, 70.01) == "FAIL"


def test_boundary_flag_lines_are_strict():
    assert verdict_for(25.0, 55.0) == "PASS"    # exactly on flag lines
    assert verdict_for(25.01, 10.0) == "FLAG"
    assert verdict_for(10.0, 55.01) == "FLAG"


def test_top10_alone_can_fail_and_flag():
    many = {f"U{i}": 80_000 for i in range(9)}  # top1 8%, top10 72%
    registry = registry_with_users(**many)
    assert analyze(holders_for(**many), SUPPLY, registry).verdict == "FAIL"
    some = {f"U{i}": 60_000 for i in range(10)}  # top1 6%, top10 60%
    registry = registry_with_users(**some)
    assert analyze(holders_for(**some), SUPPLY, registry).verdict == "FLAG"


def test_empty_holders_is_unknown_and_fail_closed():
    result = analyze([], SUPPLY, REGISTRY)
    assert result.verdict == "unknown"
    assert result.passed is False
    assert result.top1_user_pct is None


def test_missing_or_bad_supply_is_unknown():
    registry = registry_with_users(A=10_000)
    for bad_supply in (0, -5, None, "nope"):
        result = analyze(holders_for(A=10_000), bad_supply, registry)
        assert result.verdict == "unknown"
        assert result.passed is False


def test_no_user_wallets_is_unknown_not_pass():
    holders = holders_for(POOL=900_000, CURVE=100_000)
    result = analyze(holders, SUPPLY, REGISTRY)
    assert result.verdict == "unknown"
    assert result.passed is False


def test_unregistered_accounts_are_not_counted_as_users():
    registry = registry_with_users(A=10_000)
    holders = holders_for(A=10_000, GHOST=800_000)
    result = analyze(holders, SUPPLY, registry)
    assert result.top1_user_pct == 1.0
    assert result.unknown_count == 1


def test_owner_key_alias_and_classification():
    registry = {"W": {"owner_program": False, "on_curve": True},
                "P": {"owner_program": True, "on_curve": False}}
    assert classify_holder("W", registry) == "user"
    assert classify_holder("P", registry) == "program"
    assert classify_holder("MISSING", registry) == "unknown"
    assert is_user_wallet("W", registry) is True
    assert is_user_wallet("P", registry) is False
    result = analyze([{"owner": "W", "amount": 300_000}], SUPPLY, registry)
    assert result.top1_user_pct == 30.0
    assert result.verdict == "FLAG"


# -- classification edges -------------------------------------------------

import pytest

from holders import (aggregate_user_balances, effective_holder_count,
                     herfindahl_index, nakamoto_coefficient,
                     top_n_user_pct, user_holders_sorted)


def test_classify_on_curve_but_program_owned_is_program():
    registry = {"VAULT": {"owner_program": True, "on_curve": True}}
    assert classify_holder("VAULT", registry) == "program"
    assert is_user_wallet("VAULT", registry) is False


def test_classify_off_curve_even_when_not_program_owned_is_program():
    registry = {"PDA": {"owner_program": False, "on_curve": False}}
    assert classify_holder("PDA", registry) == "program"


def test_classify_missing_owner_program_key_defaults_to_user():
    registry = {"W": {"on_curve": True}}  # owner_program absent -> False
    assert classify_holder("W", registry) == "user"


def test_classify_malformed_registry_entries_are_unknown():
    registry = {
        "NONE_ENTRY": None,
        "STRING_ENTRY": "user",
        "INT_CURVE": {"owner_program": False, "on_curve": 1},
        "STRING_CURVE": {"owner_program": False, "on_curve": "true"},
        "NO_CURVE_KEY": {"owner_program": False},
    }
    for address in registry:
        assert classify_holder(address, registry) == "unknown", address
        assert is_user_wallet(address, registry) is False


def test_classify_none_or_empty_registry_is_unknown_for_everything():
    assert classify_holder("ANYTHING", None) == "unknown"
    assert classify_holder("ANYTHING", {}) == "unknown"
    result = analyze(holders_for(A=100_000), SUPPLY, None)
    assert result.verdict == "unknown"
    assert result.unknown_count == 1


# -- user_holders_sorted edges ---------------------------------------------


def test_sorted_descending_with_address_tiebreak():
    registry = registry_with_users(B=50_000, A=50_000, C=80_000)
    rows = user_holders_sorted(holders_for(B=50_000, A=50_000, C=80_000),
                                registry)
    assert rows == [("C", 80_000.0), ("A", 50_000.0), ("B", 50_000.0)]


def test_sorted_skips_every_kind_of_malformed_row():
    registry = registry_with_users(A=10_000)
    holders = [
        {"address": "A", "amount": 10_000},
        {"amount": 99_000},                       # no address
        {"address": "A"},                         # no amount
        {"address": "A", "amount": "not-a-num"},  # non-numeric amount
        {"address": "A", "amount": None},         # explicit None amount
        "not-a-dict-row",
        None,
    ]
    assert user_holders_sorted(holders, registry) == [("A", 10_000.0)]


def test_sorted_parses_string_amounts_and_address_beats_owner():
    registry = {"W": {"owner_program": False, "on_curve": True},
                "P": {"owner_program": True, "on_curve": False}}
    rows = user_holders_sorted(
        [{"address": "W", "owner": "P", "amount": "50000"}], registry)
    assert rows == [("W", 50_000.0)]


def test_sorted_empty_address_falls_back_to_owner_key():
    registry = {"W": {"owner_program": False, "on_curve": True}}
    rows = user_holders_sorted(
        [{"address": "", "owner": "W", "amount": 5_000}], registry)
    assert rows == [("W", 5_000.0)]


def test_negative_and_nonfinite_amounts_are_malformed():
    registry = registry_with_users(A=10_000, B=10_000, C=10_000, D=10_000)
    holders = [
        {"address": "A", "amount": -50_000},          # negative "balance"
        {"address": "B", "amount": float("inf")},
        {"address": "C", "amount": float("nan")},
        {"address": "D", "amount": 10_000},
    ]
    assert user_holders_sorted(holders, registry) == [("D", 10_000.0)]
    result = analyze(holders, SUPPLY, registry)
    assert result.top1_user_pct == 1.0
    assert result.unknown_count == 3  # the three malformed rows


# -- analyze degenerate inputs ----------------------------------------------


def test_analyze_none_holders_is_unknown():
    result = analyze(None, SUPPLY, REGISTRY)
    assert result.verdict == "unknown"
    assert result.passed is False
    assert result.top10_user_pct is None


def test_analyze_nonfinite_supply_is_unknown():
    registry = registry_with_users(A=10_000)
    for bad in (float("inf"), float("nan")):
        result = analyze(holders_for(A=10_000), bad, registry)
        assert result.verdict == "unknown"
        assert result.passed is False


def test_analyze_numeric_string_supply_is_accepted():
    registry = registry_with_users(A=250_000)
    result = analyze(holders_for(A=250_000), "1000000", registry)
    assert result.top1_user_pct == 25.0
    assert result.verdict == "PASS"  # exactly on the flag line


def test_analyze_counts_excluded_unknown_and_malformed_separately():
    registry = registry_with_users(A=10_000)
    holders = [
        {"address": "POOL", "amount": 500_000},   # program -> excluded
        {"address": "GHOST", "amount": 300_000},  # unregistered -> unknown
        {"address": "A"},                          # malformed -> unknown
        {"address": "A", "amount": 10_000},       # the one real user
    ]
    result = analyze(holders, SUPPLY, registry)
    assert result.excluded_count == 1
    assert result.unknown_count == 2
    assert result.user_holder_count == 1
    assert result.top1_user_pct == 1.0


def test_analyze_top10_with_fewer_than_ten_users_sums_all():
    registry = registry_with_users(A=30_000, B=20_000, C=10_000)
    result = analyze(holders_for(A=30_000, B=20_000, C=10_000),
                     SUPPLY, registry)
    assert result.top1_user_pct == 3.0
    assert result.top10_user_pct == 6.0
    assert result.user_holder_count == 3
    # Degenerate case of the same rule: one user -> top1 == top10.
    solo = registry_with_users(ONLY=123_456)
    single = analyze(holders_for(ONLY=123_456), SUPPLY, solo)
    assert single.top1_user_pct == single.top10_user_pct == pytest.approx(12.3456)


def test_flag_passes_but_fail_and_unknown_do_not():
    registry = registry_with_users(W=300_000)
    flagged = analyze(holders_for(W=300_000), SUPPLY, registry)
    assert flagged.verdict == "FLAG"
    assert flagged.passed is True


def test_verdict_for_extreme_corners():
    assert verdict_for(0.0, 0.0) == "PASS"
    assert verdict_for(100.0, 100.0) == "FAIL"
    assert verdict_for(0.0, 70.01) == "FAIL"   # top10 alone, zero top1
    assert verdict_for(50.01, 0.0) == "FAIL"   # top1 alone, zero top10


def test_analyze_reason_and_payload_carry_the_numbers():
    registry = registry_with_users(A=60_000, B=40_000)
    result = analyze(holders_for(B=40_000, A=60_000), SUPPLY, registry)
    assert "6.00%" in result.reason and "10.00%" in result.reason
    assert result.user_holders == [("A", 60_000.0), ("B", 40_000.0)]


# -- aggregate_user_balances ---------------------------------------------------


def test_aggregate_sums_duplicate_rows_for_one_address():
    registry = registry_with_users(A=0, B=0)
    holders = [{"address": "A", "amount": 30_000},
               {"address": "A", "amount": 25_000},  # second token account
               {"address": "B", "amount": 40_000}]
    assert aggregate_user_balances(holders, registry) == [
        ("A", 55_000.0), ("B", 40_000.0)]


def test_aggregate_excludes_programs_unknowns_and_malformed():
    registry = registry_with_users(A=0)
    holders = [{"address": "POOL", "amount": 900_000},
               {"address": "GHOST", "amount": 800_000},
               {"address": "A", "amount": "junk"},
               {"address": "A", "amount": 10_000}]
    assert aggregate_user_balances(holders, registry) == [("A", 10_000.0)]
    assert aggregate_user_balances([], registry) == []
    assert aggregate_user_balances(None, registry) == []


# -- top_n_user_pct -------------------------------------------------------------


def test_top_n_reproduces_top1_and_top10_and_generalises():
    amounts = {f"U{i}": 20_000 for i in range(12)}  # 12 users x 2%
    registry = registry_with_users(**amounts)
    holders = holders_for(**amounts)
    base = analyze(holders, SUPPLY, registry)
    assert top_n_user_pct(holders, SUPPLY, registry, 1) == base.top1_user_pct
    assert top_n_user_pct(holders, SUPPLY, registry, 10) == base.top10_user_pct
    assert top_n_user_pct(holders, SUPPLY, registry, 3) == 6.0
    assert top_n_user_pct(holders, SUPPLY, registry, 99) == 24.0  # all twelve


def test_top_n_rejects_bad_n():
    registry = registry_with_users(A=10_000)
    holders = holders_for(A=10_000)
    for bad_n in (0, -2, 2.5, "3", None, True):
        with pytest.raises(ValueError):
            top_n_user_pct(holders, SUPPLY, registry, bad_n)


def test_top_n_unmeasurable_inputs_return_none():
    registry = registry_with_users(A=10_000)
    assert top_n_user_pct(holders_for(A=10_000), 0, registry, 1) is None
    assert top_n_user_pct(holders_for(POOL=10_000), SUPPLY, registry, 1) is None
    assert top_n_user_pct([], SUPPLY, registry, 1) is None


# -- herfindahl / effective count ------------------------------------------------


def test_herfindahl_monopoly_scores_10000():
    registry = registry_with_users(ONLY=1_000_000)
    assert herfindahl_index(holders_for(ONLY=1_000_000), SUPPLY,
                            registry) == pytest.approx(10_000.0)


def test_herfindahl_equal_splits_score_by_formula():
    two = registry_with_users(A=500_000, B=500_000)
    assert herfindahl_index(holders_for(A=500_000, B=500_000), SUPPLY,
                            two) == pytest.approx(5_000.0)  # 50^2 + 50^2
    ten_amounts = {f"U{i}": 100_000 for i in range(10)}
    ten = registry_with_users(**ten_amounts)
    assert herfindahl_index(holders_for(**ten_amounts), SUPPLY,
                            ten) == pytest.approx(1_000.0)  # 10 x 10%^2
    skewed = registry_with_users(A=600_000, B=400_000)
    assert herfindahl_index(holders_for(A=600_000, B=400_000), SUPPLY,
                            skewed) == pytest.approx(5_200.0)  # 60^2+40^2


def test_herfindahl_excludes_pools_and_fails_closed():
    registry = registry_with_users(A=100_000)
    holders = holders_for(POOL=900_000, A=100_000)
    # Only the user's 10% counts: 10^2 = 100, not ~8200 with the pool.
    assert herfindahl_index(holders, SUPPLY, registry) == pytest.approx(100.0)
    assert herfindahl_index(holders, 0, registry) is None
    assert herfindahl_index(holders_for(POOL=900_000), SUPPLY,
                            registry) is None


def test_effective_holder_count_known_values():
    two = registry_with_users(A=100_000, B=100_000)  # equal, any total
    assert effective_holder_count(holders_for(A=100_000, B=100_000),
                                  SUPPLY, two) == pytest.approx(2.0)
    one = registry_with_users(A=700_000)
    assert effective_holder_count(holders_for(A=700_000), SUPPLY,
                                  one) == pytest.approx(1.0)
    zero = registry_with_users(A=0, B=0)
    assert effective_holder_count(holders_for(A=0, B=0), SUPPLY,
                                  zero) is None  # HHI 0 is unmeasurable


# -- nakamoto_coefficient ---------------------------------------------------------


def test_nakamoto_single_majority_whale_is_one():
    registry = registry_with_users(W=510_000, A=10_000)
    assert nakamoto_coefficient(holders_for(W=510_000, A=10_000),
                                SUPPLY, registry) == 1


def test_nakamoto_exactly_fifty_percent_is_not_a_majority():
    amounts = {name: 250_000 for name in ("A", "B", "C", "D")}  # 25% each
    registry = registry_with_users(**amounts)
    # Cumulative: 25, 50 (not >50), 75 -> the coefficient is 3.
    assert nakamoto_coefficient(holders_for(**amounts), SUPPLY,
                                registry) == 3


def test_nakamoto_none_when_threshold_never_exceeded_and_custom_threshold():
    registry = registry_with_users(A=200_000, B=200_000)  # users total 40%
    holders = holders_for(A=200_000, B=200_000)
    assert nakamoto_coefficient(holders, SUPPLY, registry) is None
    assert nakamoto_coefficient(holders, SUPPLY, registry,
                                threshold_pct=30.0) == 2  # 20, then 40 > 30


def test_nakamoto_uses_aggregated_balances():
    registry = registry_with_users(A=0, B=300_000)
    holders = [{"address": "A", "amount": 260_000},
               {"address": "A", "amount": 260_000},  # A totals 52%
               {"address": "B", "amount": 300_000}]
    assert nakamoto_coefficient(holders, SUPPLY, registry) == 1


def test_nakamoto_rejects_bad_threshold_and_bad_supply_returns_none():
    registry = registry_with_users(A=600_000)
    holders = holders_for(A=600_000)
    for bad in (0, -5, 100.01, None):
        with pytest.raises(ValueError):
            nakamoto_coefficient(holders, SUPPLY, registry,
                                 threshold_pct=bad)
    assert nakamoto_coefficient(holders, 0, registry) is None
    assert nakamoto_coefficient([], SUPPLY, registry) is None
