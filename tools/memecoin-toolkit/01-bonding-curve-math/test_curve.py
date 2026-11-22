"""Tests for curve.py -- all values hand-checkable, fixtures synthetic."""
from dataclasses import FrozenInstanceError

import pytest

from curve import Curve


def small_curve(fee_bps=0):
    # k = 1_000_000 * 100 = 100_000_000, spot = 0.0001 SOL/token.
    return Curve(virtual_token_reserves=1_000_000.0, virtual_sol_reserves=100.0,
                 total_supply=1_000_000.0, fee_bps=fee_bps,
                 graduation_target_sol=85.0)


def test_spot_price_from_virtual_reserves():
    assert small_curve().spot_price_sol() == pytest.approx(0.0001)
    assert Curve().spot_price_sol() == pytest.approx(30.0 / 1.073e9)


def test_price_increases_with_buys():
    curve = small_curve()
    prices = [curve.curve_after_buy_sol(x).spot_price_sol()
              for x in (0.5, 5.0, 50.0)]
    assert prices[0] < prices[1] < prices[2]
    assert all(p > curve.spot_price_sol() for p in prices)


def test_price_falls_with_sells():
    curve = small_curve()
    after = curve.curve_after_sell_tokens(10_000.0)
    assert after.spot_price_sol() < curve.spot_price_sol()


def test_cost_to_buy_one_percent_matches_hand_value():
    # Buy 10_000 tokens: new_vt=990_000, new_vs=100_000_000/990_000,
    # cost = 101.010101... - 100 = 1.010101... SOL at zero fee.
    curve = small_curve(fee_bps=0)
    assert curve.cost_to_buy_percent_of_supply(1.0) == pytest.approx(1.01010101, rel=1e-6)
    assert curve.cost_to_buy_tokens(10_000.0) == pytest.approx(1.01010101, rel=1e-6)


def test_tokens_received_is_inverse_of_cost():
    curve = Curve()
    cost = curve.cost_to_buy_tokens(1_000_000.0)
    assert curve.tokens_received_for_sol(cost) == pytest.approx(1_000_000.0, rel=1e-9)


def test_more_sol_buys_more_tokens_but_at_worse_rate():
    curve = Curve()
    small = curve.tokens_received_for_sol(1.0)
    big = curve.tokens_received_for_sol(10.0)
    assert big > small
    assert big / 10.0 < small / 1.0  # per-SOL rate degrades with size


def test_round_trip_loses_only_the_fee():
    curve = Curve(fee_bps=100)  # 1% each side
    tokens = curve.tokens_received_for_sol(5.0)
    # The sell happens on the curve *after* the buy moved it.
    back = curve.curve_after_buy_sol(5.0).sell_proceeds_sol(tokens)
    loss_pct = (5.0 - back) / 5.0 * 100.0
    assert 0 < loss_pct < 2.1  # ~1.99%: fee on the way in + on the way out
    assert back == pytest.approx(5.0 * 0.99 * 0.99, rel=1e-6)


def test_zero_fee_round_trip_is_flat():
    curve = small_curve(fee_bps=0)
    tokens = curve.tokens_received_for_sol(5.0)
    after = curve.curve_after_buy_sol(5.0)
    assert after.sell_proceeds_sol(tokens) == pytest.approx(5.0)


def test_completion_pct_math():
    curve = Curve()
    assert curve.completion_pct(0.0) == 0.0
    assert curve.completion_pct(42.5) == pytest.approx(50.0)
    assert curve.completion_pct(85.0) == pytest.approx(100.0)
    assert curve.completion_pct(170.0) == pytest.approx(100.0)  # clamped


def test_graduation_threshold():
    curve = Curve()
    assert curve.is_graduated(84.999) is False
    assert curve.is_graduated(85.0) is True
    assert curve.is_graduated(200.0) is True


def test_slippage_sign_and_magnitude():
    curve = Curve()
    slip = curve.slippage_buy_tokens(10_000_000.0)
    assert slip > 0  # a buy always fills worse than the spot quote
    # Bigger trade -> bigger slippage.
    assert curve.slippage_buy_tokens(50_000_000.0) > slip
    # A dust trade has near-zero slippage (fee only, ~1% at 100 bps).
    assert curve.slippage_buy_tokens(1_000.0) == pytest.approx(1.0, abs=0.05)
    sell_slip = curve.slippage_sell_tokens(10_000_000.0)
    assert sell_slip > 0


def test_price_impact_grows_with_size():
    curve = Curve()
    impacts = [curve.price_impact_of_buy_tokens(x)
               for x in (1e5, 1e6, 1e7, 5e7)]
    assert impacts == sorted(impacts)
    assert all(i > 0 for i in impacts)
    assert curve.price_impact_of_sell_tokens(1e7) > 0


@pytest.mark.parametrize("bad", [0, -1.0, None])
def test_invalid_amounts_raise(bad):
    curve = Curve()
    with pytest.raises(ValueError):
        curve.tokens_received_for_sol(bad)
    with pytest.raises(ValueError):
        curve.cost_to_buy_tokens(bad)
    with pytest.raises(ValueError):
        curve.sell_proceeds_sol(bad)


def test_invalid_curve_and_percent_raise():
    with pytest.raises(ValueError):
        Curve(virtual_sol_reserves=0)
    with pytest.raises(ValueError):
        Curve(virtual_token_reserves=-5)
    with pytest.raises(ValueError):
        Curve(fee_bps=10_000)
    curve = Curve()
    with pytest.raises(ValueError):
        curve.cost_to_buy_percent_of_supply(0)
    with pytest.raises(ValueError):
        curve.cost_to_buy_percent_of_supply(101)
    with pytest.raises(ValueError):
        curve.completion_pct(-1.0)
    with pytest.raises(ValueError):
        curve.cost_to_buy_tokens(curve.virtual_token_reserves)


# -- invariants and state handling -------------------------------------------


def test_invariant_k_preserved_after_buys_and_sells():
    curve = Curve()
    after_buy = curve.curve_after_buy_sol(7.5)
    assert after_buy.invariant_k == pytest.approx(curve.invariant_k, rel=1e-9)
    tokens = curve.tokens_received_for_sol(7.5)
    after_sell = after_buy.curve_after_sell_tokens(tokens)
    assert after_sell.invariant_k == pytest.approx(curve.invariant_k, rel=1e-9)
    small = small_curve(fee_bps=25)
    assert small.curve_after_sell_tokens(50_000.0).invariant_k == \
        pytest.approx(small.invariant_k, rel=1e-9)


def test_curve_states_are_immutable_snapshots():
    curve = small_curve()
    after = curve.curve_after_buy_sol(5.0)
    # Deriving a new state never moves the original.
    assert curve.virtual_sol_reserves == 100.0
    assert curve.virtual_token_reserves == 1_000_000.0
    assert after.virtual_sol_reserves > 100.0
    with pytest.raises(FrozenInstanceError):
        after.virtual_sol_reserves = 1.0


def test_spot_quotes_are_exact_reciprocals():
    for curve in (Curve(), small_curve(), small_curve(fee_bps=250)):
        assert curve.spot_price_sol() * \
            curve.spot_price_tokens_per_sol() == pytest.approx(1.0)


def test_zero_fee_buy_then_sell_all_restores_state():
    curve = small_curve(fee_bps=0)
    tokens = curve.tokens_received_for_sol(5.0)
    restored = curve.curve_after_buy_sol(5.0).curve_after_sell_tokens(tokens)
    assert restored.virtual_sol_reserves == pytest.approx(100.0)
    assert restored.virtual_token_reserves == pytest.approx(1_000_000.0)
    assert restored.spot_price_sol() == pytest.approx(curve.spot_price_sol())


# -- monotonicity, convexity, extremes ----------------------------------------


def test_percent_cost_strictly_increasing_and_convex():
    curve = Curve()
    costs = [curve.cost_to_buy_percent_of_supply(p)
             for p in (0.5, 1.0, 2.0, 5.0, 10.0)]
    assert costs == sorted(costs) and len(set(costs)) == len(costs)
    # Convex: the second 1% costs more than the first 1% did.
    assert curve.cost_to_buy_percent_of_supply(2.0) > \
        2 * curve.cost_to_buy_percent_of_supply(1.0)


def test_monotonic_tokens_and_cost():
    curve = Curve()
    token_amounts = [curve.tokens_received_for_sol(s)
                     for s in (0.1, 1.0, 5.0, 25.0)]
    assert token_amounts == sorted(token_amounts)
    costs = [curve.cost_to_buy_tokens(t)
             for t in (1e5, 1e6, 1e7, 1e8)]
    assert costs == sorted(costs) and len(set(costs)) == len(costs)


def test_buying_full_supply_needs_reserve_headroom():
    # Default curve: reserves (1.073B) exceed supply (1B), so 100% is
    # buyable at a finite, hand-checkable cost (~415.11 SOL).
    cost = Curve().cost_to_buy_percent_of_supply(100.0)
    assert cost == pytest.approx(415.11, rel=1e-3)
    # Small fixture: supply == reserves, so 100% would drain the curve.
    with pytest.raises(ValueError):
        small_curve().cost_to_buy_percent_of_supply(100.0)


def test_huge_buy_approaches_but_never_drains_reserve():
    curve = Curve()
    tokens = curve.tokens_received_for_sol(1e9)
    assert tokens < curve.virtual_token_reserves
    # Only ~32.5 virtual tokens remain: the reserve is an asymptote.
    assert curve.virtual_token_reserves - tokens < 100.0


def test_sell_proceeds_bounded_by_virtual_sol_reserve():
    curve = Curve()  # 30 virtual SOL, 1% fee: proceeds can never reach 29.7
    huge = curve.sell_proceeds_sol(1e12)
    assert 29.0 < huge < 30.0 * 0.99
    # Still strictly increasing in size even near the bound.
    assert curve.sell_proceeds_sol(1e9) < huge


# -- fee behaviour -------------------------------------------------------------


def test_fee_strictly_raises_cost_and_lowers_output():
    amounts = [Curve(fee_bps=f).cost_to_buy_tokens(1_000_000.0)
               for f in (0, 100, 500)]
    assert amounts[0] < amounts[1] < amounts[2]
    received = [Curve(fee_bps=f).tokens_received_for_sol(2.0)
                for f in (0, 100, 500)]
    assert received[0] > received[1] > received[2]


def test_max_fee_still_quotes_and_negative_fee_rejected():
    curve = Curve(fee_bps=9999)  # 99.99%: legal, just brutal
    # Net SOL in is 0.0001, so tokens = 1.073e9 * 0.0001 / 30.0001.
    assert curve.tokens_received_for_sol(1.0) == pytest.approx(3576.65,
                                                               rel=1e-3)
    with pytest.raises(ValueError):
        Curve(fee_bps=-1)


def test_dust_trade_impact_is_fee_only():
    curve = Curve()  # 100 bps: a dust trade's impact is ~the fee alone
    assert curve.price_impact_of_buy_tokens(1_000.0) == \
        pytest.approx(1.0, abs=0.05)
    assert curve.price_impact_of_sell_tokens(1_000.0) == \
        pytest.approx(1.0, abs=0.05)


def test_slippage_is_price_impact_under_another_name():
    curve = Curve()
    for amount in (1e5, 1e7, 5e7):
        assert curve.slippage_buy_tokens(amount) == \
            pytest.approx(curve.price_impact_of_buy_tokens(amount))
        assert curve.slippage_sell_tokens(amount) == \
            pytest.approx(curve.price_impact_of_sell_tokens(amount))


# -- derived quotes (new helpers) -----------------------------------------------


def test_market_cap_sol():
    # Small fixture: supply == token reserves, so mcap == virtual SOL.
    assert small_curve().market_cap_sol() == pytest.approx(100.0)
    assert Curve().market_cap_sol() == pytest.approx(30.0 / 1.073e9 * 1e9)
    # A buy raises the market cap (price up, supply fixed).
    curve = Curve()
    assert curve.curve_after_buy_sol(10.0).market_cap_sol() > \
        curve.market_cap_sol()


def test_effective_prices_bracket_spot_and_worsen_with_size():
    curve = Curve()
    spot = curve.spot_price_sol()
    assert curve.effective_buy_price_sol(1.0) > spot
    assert curve.effective_buy_price_sol(10.0) > \
        curve.effective_buy_price_sol(1.0)
    assert curve.effective_sell_price_sol(10_000_000.0) < spot
    # Consistency: effective price times quantity returns the input.
    tokens = curve.tokens_received_for_sol(3.0)
    assert curve.effective_buy_price_sol(3.0) * tokens == pytest.approx(3.0)


def test_round_trip_loss_pct_helper():
    curve = Curve(fee_bps=100)
    assert curve.round_trip_loss_pct(5.0) == pytest.approx(1.99, abs=0.01)
    # Same number as the manual buy-then-sell-on-new-state computation.
    tokens = curve.tokens_received_for_sol(2.0)
    back = curve.curve_after_buy_sol(2.0).sell_proceeds_sol(tokens)
    assert curve.round_trip_loss_pct(2.0) == \
        pytest.approx((2.0 - back) / 2.0 * 100.0)
    assert small_curve(fee_bps=0).round_trip_loss_pct(5.0) == \
        pytest.approx(0.0, abs=1e-9)


def test_sol_to_reach_spot_price_round_trips():
    small = small_curve(fee_bps=0)  # spot 0.0001; doubling needs sqrt(k*p)-100
    needed = small.sol_to_reach_spot_price(0.0002)
    assert needed == pytest.approx(41.42135623730951, rel=1e-9)
    assert small.curve_after_buy_sol(needed).spot_price_sol() == \
        pytest.approx(0.0002, rel=1e-9)
    # With a fee, more gross SOL is needed for the same target.
    default = Curve()
    target = default.spot_price_sol() * 2
    gross = default.sol_to_reach_spot_price(target)
    assert default.curve_after_buy_sol(gross).spot_price_sol() == \
        pytest.approx(target, rel=1e-6)


@pytest.mark.parametrize("target", [0.0001, 0.00005, 0, None])
def test_sol_to_reach_spot_price_rejects_bad_targets(target):
    # At/below spot is not reachable by buying; non-positive is invalid.
    with pytest.raises(ValueError):
        small_curve().sol_to_reach_spot_price(target)


def test_remaining_sol_to_graduation():
    curve = Curve()
    assert curve.remaining_sol_to_graduation(0.0) == pytest.approx(85.0)
    assert curve.remaining_sol_to_graduation(42.5) == pytest.approx(42.5)
    assert curve.remaining_sol_to_graduation(85.0) == 0.0
    assert curve.remaining_sol_to_graduation(500.0) == 0.0  # never negative
    with pytest.raises(ValueError):
        curve.remaining_sol_to_graduation(-0.1)


def test_custom_graduation_target():
    curve = Curve(graduation_target_sol=10.0)
    assert curve.completion_pct(2.5) == pytest.approx(25.0)
    assert curve.is_graduated(9.999) is False
    assert curve.is_graduated(10.0) is True
    assert curve.remaining_sol_to_graduation(4.0) == pytest.approx(6.0)


# -- trade sequences -------------------------------------------------------------


def test_simulate_buys_matches_single_combined_buy():
    curve = Curve()
    total, final = curve.simulate_buys([1.0, 1.0])
    # Fees are linear in SOL, so splitting a buy changes nothing.
    assert total == pytest.approx(curve.tokens_received_for_sol(2.0),
                                  rel=1e-6)
    expected_final = curve.curve_after_buy_sol(2.0)
    assert final.virtual_sol_reserves == \
        pytest.approx(expected_final.virtual_sol_reserves)
    assert final.spot_price_sol() > curve.spot_price_sol()
    assert final.invariant_k == pytest.approx(curve.invariant_k, rel=1e-9)


def test_simulate_buys_empty_and_invalid():
    curve = Curve()
    total, final = curve.simulate_buys([])
    assert total == 0.0 and final is curve
    with pytest.raises(ValueError):
        curve.simulate_buys(None)
    with pytest.raises(ValueError):
        curve.simulate_buys([1.0, 0.0])


def test_simulate_sells_matches_manual_sequence():
    curve = Curve()
    legs = [1_000_000.0, 2_000_000.0, 500_000.0]
    total, final = curve.simulate_sells(legs)
    manual_total, manual_curve = 0.0, curve
    for amount in legs:
        manual_total += manual_curve.sell_proceeds_sol(amount)
        manual_curve = manual_curve.curve_after_sell_tokens(amount)
    assert total == pytest.approx(manual_total)
    assert final.virtual_token_reserves == \
        pytest.approx(manual_curve.virtual_token_reserves)
    assert final.spot_price_sol() < curve.spot_price_sol()
    with pytest.raises(ValueError):
        curve.simulate_sells(None)


# -- fail-closed surface of the derived methods -----------------------------------


def test_derived_methods_reject_bad_input():
    curve = Curve()
    with pytest.raises(ValueError):
        curve.curve_after_buy_sol(0)
    with pytest.raises(ValueError):
        curve.curve_after_sell_tokens(-5.0)
    with pytest.raises(ValueError):
        curve.price_impact_of_buy_tokens(curve.virtual_token_reserves)
    with pytest.raises(ValueError):
        curve.completion_pct(None)
    with pytest.raises(ValueError):
        curve.is_graduated(None)
    with pytest.raises(ValueError):
        curve.cost_to_buy_percent_of_supply(None)
    with pytest.raises(ValueError):
        curve.effective_buy_price_sol(0)
    with pytest.raises(ValueError):
        Curve(total_supply=0)
    with pytest.raises(ValueError):
        Curve(graduation_target_sol=-1)
