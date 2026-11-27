"""Tests for DemoExecutionBridge — run: python3 tests/test_demo_bridge.py

The bridge must translate adapter fill vocabulary into executor swaps
and back without lossy error mapping: every skip reaches the caller
with its original reason."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution import sim_executor
from broker.memecoin.execution.demo_bridge import (
    DemoExecutionBridge, LAMPORTS_PER_SOL)
from broker.memecoin.execution.sim_executor import (
    BalanceBook, SimulationExecutor)

SOL = sim_executor.SOL_MINT
TOK = "TokenMint1111111111111111111111111111111111"


class FakeJupiter:
    def __init__(self, quote=None, http_status=None):
        self._quote = quote
        self._http_status = http_status

    def get_quote(self, input_mint, output_mint, amount_lamports,
                  slippage_bps=100):
        self._seen = (input_mint, output_mint, amount_lamports)
        if self._http_status is not None:
            raise BrokerError("unavailable", "quote failed",
                              detail={"reason": "jupiter_http_error",
                                      "http_status": self._http_status})
        return self._quote


def make_bridge(quote=None, book=None, seed=7, decimals=6, **kw):
    ex = SimulationExecutor(
        jupiter=FakeJupiter(quote if quote is not None else
                            {"outAmount": "2000000",
                             "priceImpactPct": "0.004"}),
        book=book if book is not None else BalanceBook(10**9),
        seed=seed, **kw)
    return DemoExecutionBridge(ex, lambda mint: decimals), ex


def test_buy_fill_math():
    book = BalanceBook(10**9)
    bridge, _ = make_bridge(book=book, hourly_vol=0.0)
    fill = bridge.buy(TOK, 0.01)  # 0.01 SOL = 1e7 lamports
    assert fill["qty_tokens"] == 2.0  # 2_000_000 base units at 6 decimals
    assert abs(fill["price_sol"] - 0.005) < 1e-12
    assert fill["fee_lamports"] == 15_000  # base 5_000 + priority 10_000
    assert fill["signature"]
    assert book.balance_of(TOK) == 2_000_000
    print("PASS test_buy_fill_math")


def test_sell_fill_math():
    book = BalanceBook(10**9)
    book.credit(TOK, 2_000_000)  # 2 tokens at 6 decimals
    bridge, ex = make_bridge(
        quote={"outAmount": "9000000", "priceImpactPct": "0.004"},
        book=book, hourly_vol=0.0)
    fill = bridge.sell(TOK, 2.0)
    # Measured out is NET of the 15_000-lamport network fee — exactly
    # the live executor's on-chain balance measurement (out_after minus
    # out_before on the SOL balance, fee included in the delta).
    assert abs(fill["proceeds_sol"] - (0.009 - 15_000 / LAMPORTS_PER_SOL)) < 1e-15
    assert fill["fee_lamports"] == 15_000
    assert abs(fill["price_sol"] - fill["proceeds_sol"] / 2.0) < 1e-15
    assert ex._jupiter._seen[0] == TOK and ex._jupiter._seen[1] == SOL
    assert ex._jupiter._seen[2] == 2_000_000
    print("PASS test_sell_fill_math")


def test_skips_pass_through_unchanged():
    bridge, _ = make_bridge(quote={"outAmount": "100",
                                   "priceImpactPct": "0.099"})
    try:
        bridge.buy(TOK, 0.01)
        raise AssertionError("expected price_impact_over_cap")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "price_impact_over_cap"
    bridge, _ = make_bridge(quote=None)
    bridge.executor._jupiter._http_status = 400
    try:
        bridge.buy(TOK, 0.01)
        raise AssertionError("expected no_route")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "no_route"
    print("PASS test_skips_pass_through_unchanged")


def test_unknown_timeout_passes_through_with_signature():
    book = BalanceBook(10**9)
    bridge, ex = make_bridge(book=book, hourly_vol=0.0)
    ex.inject_confirm_outage()
    try:
        bridge.buy(TOK, 0.01)
        raise AssertionError("expected confirm_timeout")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "confirm_timeout"
        assert exc.detail.get("signature")
    assert book.balance_of(TOK) == 0 and book.balance_of(SOL) == 10**9
    print("PASS test_unknown_timeout_passes_through_with_signature")


def test_decimals_unknown_refuses_blind_sell():
    book = BalanceBook(10**9)
    ex = SimulationExecutor(
        jupiter=FakeJupiter({"outAmount": "9000000",
                             "priceImpactPct": "0.004"}),
        book=book, hourly_vol=0.0)
    bridge = DemoExecutionBridge(
        ex, lambda mint: (_ for _ in ()).throw(KeyError(mint)))
    try:
        bridge.sell(TOK, 2.0)
        raise AssertionError("expected decimals_unknown")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "decimals_unknown"
    print("PASS test_decimals_unknown_refuses_blind_sell")


def test_amount_too_small_refused():
    bridge, _ = make_bridge()
    try:
        bridge.buy(TOK, 1e-12)
        raise AssertionError("expected amount_too_small")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "amount_too_small"
    print("PASS test_amount_too_small_refused")


if __name__ == "__main__":
    test_buy_fill_math()
    test_sell_fill_math()
    test_skips_pass_through_unchanged()
    test_unknown_timeout_passes_through_with_signature()
    test_decimals_unknown_refuses_blind_sell()
    test_amount_too_small_refused()
    print("ALL DEMO-BRIDGE TESTS PASSED")
