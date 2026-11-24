"""Tests for amm.py -- known x*y=k values, synthetic LP fixtures."""
import pytest

from amm import (InsufficientOutputError, Pool, chained_quote,
                 ensure_min_out, estimate_sandwich, get_amount_in,
                 get_amount_out, impermanent_loss_pct, lp_share_pct,
                 pro_rata_amounts, verify_lp_lock)


def test_known_quote_value_no_fee():
    # out = 1_000_000 * 1 / (100 + 1) = 9900.990099...
    assert get_amount_out(1.0, 100.0, 1_000_000.0, fee_bps=0) == \
        pytest.approx(9900.990099, rel=1e-6)


def test_known_quote_value_with_fee():
    # 25 bps: in_after_fee = 0.9975; out = 1e6*0.9975/100.9975
    expected = 1_000_000.0 * 0.9975 / 100.9975
    assert get_amount_out(1.0, 100.0, 1_000_000.0, fee_bps=25) == \
        pytest.approx(expected, rel=1e-9)


def test_quote_both_directions_and_round_trip_loss():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    tokens = pool.quote_x_for_y(2.0)
    assert tokens > 0
    _out, pool_after_buy = pool.swap_x_for_y(2.0)
    back = pool_after_buy.quote_y_for_x(tokens)
    assert 0 < back < 2.0  # round trip loses the fee + impact, never gains


def test_swap_preserves_direction_and_moves_price():
    pool = Pool(100.0, 1_000_000.0, fee_bps=0)
    _out, after = pool.swap_x_for_y(10.0)
    assert after.reserve_x == pytest.approx(110.0)
    assert after.spot_price_y_per_x() < pool.spot_price_y_per_x()


def test_impact_and_slippage_grow_with_trade_size():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    sizes = (0.1, 1.0, 10.0, 50.0)
    impacts = [pool.price_impact_pct(s) for s in sizes]
    slippages = [pool.slippage_pct(s) for s in sizes]
    assert impacts == sorted(impacts) and all(i > 0 for i in impacts)
    assert slippages == sorted(slippages)
    # Slippage (fee included) is always at least the pure curve impact.
    assert all(s >= i for s, i in zip(slippages, impacts))


def test_min_out_guard_triggers():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    out = pool.quote_x_for_y(1.0)
    with pytest.raises(InsufficientOutputError):
        pool.swap_x_for_y(1.0, min_out=out + 1.0)
    with pytest.raises(InsufficientOutputError):
        ensure_min_out(10.0, 10.5)
    # At or below the quote, the guard lets the swap through.
    got, _ = pool.swap_x_for_y(1.0, min_out=out)
    assert got == pytest.approx(out)
    assert ensure_min_out(10.0, None) == 10.0


def test_lp_95_percent_burned_is_locked():
    verdict = verify_lp_lock(1000.0, burn_balance=950.0,
                             locker_balances=[0.0], deployer_balance=50.0)
    assert verdict.verdict == "locked" and verdict.passed is True
    assert verdict.secured_pct == pytest.approx(95.0)
    assert verdict.burned_pct == pytest.approx(95.0)


def test_lp_burn_plus_locker_counts_together():
    verdict = verify_lp_lock(1000.0, burn_balance=500.0,
                             locker_balances={"locker-a": 300.0,
                                              "locker-b": 150.0},
                             deployer_balance=50.0)
    assert verdict.verdict == "locked"
    assert verdict.secured_pct == pytest.approx(95.0)
    assert verdict.locked_pct == pytest.approx(45.0)


def test_lp_40_percent_burned_is_unlocked():
    verdict = verify_lp_lock(1000.0, burn_balance=400.0,
                             locker_balances=[0.0], deployer_balance=600.0)
    assert verdict.verdict == "unlocked" and verdict.passed is False
    assert verdict.secured_pct == pytest.approx(40.0)
    # Deployer-held LP is reported, never counted as secured.
    assert verdict.deployer_pct == pytest.approx(60.0)


def test_lp_exactly_at_threshold_is_locked():
    verdict = verify_lp_lock(1000.0, 900.0, [0.0], 100.0)
    assert verdict.verdict == "locked"


@pytest.mark.parametrize("args", [
    (None, 950.0, [0.0]),
    (1000.0, None, [0.0]),
    (1000.0, 950.0, None),
    (0.0, 0.0, [0.0]),
])
def test_lp_missing_data_is_unverifiable(args):
    verdict = verify_lp_lock(*args)
    assert verdict.verdict == "unverifiable"
    assert verdict.passed is False  # fail-closed: never "locked" on a guess
    assert verdict.secured_pct is None


def test_sandwich_loss_positive():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    result = estimate_sandwich(pool, victim_amount_x_in=1.0,
                               attacker_amount_x_in=5.0)
    assert result.victim_extra_loss_pct > 0
    assert result.victim_out_sandwiched < result.victim_out_normal


def test_sandwich_loss_larger_on_thinner_pool():
    victim, attacker = 1.0, 2.0
    deep = Pool(100.0, 1_000_000.0, fee_bps=25)
    thin = Pool(10.0, 100_000.0, fee_bps=25)  # same spot price, 10x thinner
    deep_loss = estimate_sandwich(deep, victim, attacker).victim_extra_loss_pct
    thin_loss = estimate_sandwich(thin, victim, attacker).victim_extra_loss_pct
    assert thin_loss > deep_loss > 0


@pytest.mark.parametrize("fn", [
    lambda: get_amount_out(0, 100.0, 1000.0),
    lambda: get_amount_out(1.0, 0, 1000.0),
    lambda: Pool(0, 1000.0),
    lambda: Pool(100.0, -1.0),
    lambda: estimate_sandwich(Pool(100.0, 1000.0), 0, 1.0),
])
def test_invalid_inputs_raise(fn):
    with pytest.raises(ValueError):
        fn()


# -- exact-out quotes (get_amount_in) ------------------------------------------


def test_amount_in_round_trips_amount_out():
    for fee in (0, 25, 100):
        out = get_amount_out(2.0, 100.0, 1_000_000.0, fee_bps=fee)
        assert get_amount_in(out, 100.0, 1_000_000.0, fee_bps=fee) == \
            pytest.approx(2.0, rel=1e-9)
    # Hand value at zero fee: in = 100 * 9900.990099 / (1e6 - 9900.990099) = 1
    out = get_amount_out(1.0, 100.0, 1_000_000.0, fee_bps=0)
    assert get_amount_in(out, 100.0, 1_000_000.0, fee_bps=0) == \
        pytest.approx(1.0)


@pytest.mark.parametrize("out,reserve_out,fee", [
    (1_000_000.0, 1_000_000.0, 25),   # exactly the whole reserve: impossible
    (2_000_000.0, 1_000_000.0, 25),   # beyond the reserve
    (0.0, 1_000_000.0, 25),           # non-positive output
    (100.0, 1_000_000.0, 10_000),     # fee out of range
])
def test_amount_in_rejects_impossible_or_bad_input(out, reserve_out, fee):
    with pytest.raises(ValueError):
        get_amount_in(out, 100.0, reserve_out, fee_bps=fee)


def test_pool_exact_out_quotes_invert_the_quotes():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    tokens = pool.quote_x_for_y(2.0)
    assert pool.quote_x_in_for_y_out(tokens) == pytest.approx(2.0, rel=1e-9)
    sol = pool.quote_y_for_x(5_000.0)
    assert pool.quote_y_in_for_x_out(sol) == pytest.approx(5_000.0, rel=1e-9)
    with pytest.raises(ValueError):
        pool.quote_x_in_for_y_out(1_000_000.0)  # the whole y reserve


# -- multi-hop routes ------------------------------------------------------------


def test_chained_quote_composes_hops():
    hops = [(100.0, 1_000_000.0, 25), (50_000.0, 200.0, 25)]
    step1 = get_amount_out(1.0, 100.0, 1_000_000.0, fee_bps=25)
    manual = get_amount_out(step1, 50_000.0, 200.0, fee_bps=25)
    assert chained_quote(1.0, hops) == pytest.approx(manual)
    # A one-hop route is exactly the module-level quote.
    assert chained_quote(1.0, hops[:1]) == pytest.approx(step1)
    # Two fee-taking hops deliver less than the second hop's spot quote.
    assert manual < step1 * (200.0 / 50_000.0)
    with pytest.raises(ValueError):
        chained_quote(1.0, [])
    with pytest.raises(ValueError):
        chained_quote(1.0, None)


# -- sell-direction impact / slippage ---------------------------------------------


def test_y_direction_impact_and_slippage():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    sizes = (10_000.0, 100_000.0, 500_000.0)
    impacts = [pool.price_impact_pct_y_in(s) for s in sizes]
    slippages = [pool.slippage_pct_y_in(s) for s in sizes]
    assert impacts == sorted(impacts) and all(i > 0 for i in impacts)
    assert all(s >= i for s, i in zip(slippages, impacts))
    # Hand value: selling the whole y reserve size moves price 50%.
    assert pool.price_impact_pct_y_in(1_000_000.0) == pytest.approx(50.0)


def test_price_impact_hand_values_x_direction():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    # impact = in / (reserve_in + in), independent of the fee.
    assert pool.price_impact_pct(100.0) == pytest.approx(50.0)
    assert pool.price_impact_pct(25.0) == pytest.approx(20.0)


# -- pool-state invariants ----------------------------------------------------------


def test_invariant_k_preserved_at_zero_fee_and_grows_with_fee():
    zero = Pool(100.0, 1_000_000.0, fee_bps=0)
    _out, after_zero = zero.swap_x_for_y(10.0)
    assert after_zero.invariant_k == pytest.approx(zero.invariant_k)
    # With a fee, part of the input stays in the pool: k strictly grows.
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    _out, after = pool.swap_x_for_y(10.0)
    assert after.invariant_k > pool.invariant_k


def test_quotes_and_swaps_do_not_mutate_original_pool():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    pool.quote_x_for_y(5.0)
    _out, after = pool.swap_x_for_y(5.0)
    assert pool.reserve_x == 100.0 and pool.reserve_y == 1_000_000.0
    assert after is not pool and after.reserve_x == pytest.approx(105.0)


def test_swap_y_for_x_moves_price_and_respects_min_out():
    pool = Pool(100.0, 1_000_000.0, fee_bps=25)
    out, after = pool.swap_y_for_x(100_000.0)
    assert out == pytest.approx(pool.quote_y_for_x(100_000.0))
    assert after.reserve_y == pytest.approx(1_100_000.0)
    # Tokens got cheaper: more tokens per SOL than before the sale.
    assert after.spot_price_y_per_x() > pool.spot_price_y_per_x()
    with pytest.raises(InsufficientOutputError):
        pool.swap_y_for_x(1_000.0, min_out=pool.quote_y_for_x(1_000.0) + 1.0)


def test_ensure_min_out_edges():
    assert ensure_min_out(10.0, 0.0) == 10.0    # zero guard never blocks
    assert ensure_min_out(10.0, 10.0) == 10.0   # exactly at the guard passes
    with pytest.raises(ValueError):
        ensure_min_out(10.0, -0.5)              # a negative guard is a bug


# -- LP share math + impermanent loss -------------------------------------------------


def test_lp_share_and_pro_rata_amounts():
    assert lp_share_pct(250.0, 1000.0) == pytest.approx(25.0)
    assert lp_share_pct(0.0, 1000.0) == 0.0
    x, y = pro_rata_amounts(250.0, 1000.0, 100.0, 1_000_000.0)
    assert x == pytest.approx(25.0) and y == pytest.approx(250_000.0)
    # Redeeming everything returns the whole reserves.
    x, y = pro_rata_amounts(1000.0, 1000.0, 100.0, 1_000_000.0)
    assert x == pytest.approx(100.0) and y == pytest.approx(1_000_000.0)
    with pytest.raises(ValueError):
        lp_share_pct(1000.1, 1000.0)  # more than exist: contradiction
    with pytest.raises(ValueError):
        lp_share_pct(-1.0, 1000.0)


def test_impermanent_loss_hand_values_and_symmetry():
    assert impermanent_loss_pct(1.0) == pytest.approx(0.0, abs=1e-12)
    # 2*sqrt(4)/(1+4) - 1 = -20%; a 4x fall costs exactly the same.
    assert impermanent_loss_pct(4.0) == pytest.approx(-20.0)
    assert impermanent_loss_pct(0.25) == pytest.approx(-20.0)
    assert impermanent_loss_pct(2.0) < 0  # any move away from 1 loses
    with pytest.raises(ValueError):
        impermanent_loss_pct(0.0)


# -- LP verdict: thresholds, edges, inconsistent fixtures ------------------------------


def test_lp_custom_threshold_flips_verdict():
    args = (1000.0, 600.0, [0.0], 400.0)  # 60% secured
    assert verify_lp_lock(*args).verdict == "unlocked"          # default 90
    assert verify_lp_lock(*args, threshold_pct=50.0).verdict == "locked"
    assert verify_lp_lock(*args, threshold_pct=60.0).verdict == "locked"
    assert verify_lp_lock(*args, threshold_pct=61.0).verdict == "unlocked"


def test_lp_locker_balance_tips_exact_boundary():
    just_below = verify_lp_lock(1000.0, 899.0, [0.0])
    assert just_below.verdict == "unlocked"
    assert just_below.secured_pct == pytest.approx(89.9)
    tipped = verify_lp_lock(1000.0, 899.0, [1.0])
    assert tipped.verdict == "locked"
    assert tipped.secured_pct == pytest.approx(90.0)
    assert tipped.locked_pct == pytest.approx(0.1)


def test_lp_empty_lockers_and_default_deployer():
    verdict = verify_lp_lock(1000.0, 950.0, [])  # deployer arg omitted
    assert verdict.verdict == "locked"
    assert verdict.deployer_pct == pytest.approx(0.0)
    verdict = verify_lp_lock(1000.0, 950.0, {})  # empty dict of lockers
    assert verdict.verdict == "locked" and verdict.locked_pct == 0.0


def test_lp_negative_or_missing_locker_entries_raise():
    with pytest.raises(ValueError):
        verify_lp_lock(1000.0, -1.0, [0.0])
    with pytest.raises(ValueError):
        verify_lp_lock(1000.0, 500.0, [-5.0])
    with pytest.raises(ValueError):
        verify_lp_lock(1000.0, 500.0, [None])
    with pytest.raises(ValueError):
        verify_lp_lock(1000.0, 500.0, [100.0], deployer_balance=-1.0)


def test_lp_secured_over_supply_is_unverifiable():
    verdict = verify_lp_lock(1000.0, 600.0, [500.0])  # 110% "secured"
    assert verdict.verdict == "unverifiable" and verdict.passed is False
    assert verdict.secured_pct is None and verdict.reasons


def test_lp_total_balances_over_supply_is_unverifiable():
    # Neither secured (60%) nor deployer (60%) alone exceeds the supply,
    # but together they claim 120% of it: the fixture contradicts itself.
    verdict = verify_lp_lock(1000.0, 600.0, [], deployer_balance=600.0)
    assert verdict.verdict == "unverifiable" and verdict.passed is False


@pytest.mark.parametrize("threshold", [0.0, 101.0, None])
def test_lp_invalid_threshold_raises(threshold):
    with pytest.raises(ValueError):
        verify_lp_lock(1000.0, 950.0, [0.0], threshold_pct=threshold)
