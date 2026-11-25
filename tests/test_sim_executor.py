"""Contract-parity tests for SimulationExecutor — run:
python3 tests/test_sim_executor.py

The demo executor must take the same decisions as MainnetExecutor on
the same quote: same skips, same economics, same UNKNOWN semantics.
All quotes here are canned fixtures (no network)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution import sim_executor
from broker.memecoin.execution.sim_executor import (
    BalanceBook, SimulationExecutor, estimate_volatility_from_series)

SOL = sim_executor.SOL_MINT
TOK = "TokenMint1111111111111111111111111111111111"


class FakeJupiter:
    """Stands in for JupiterClient with a canned quote."""

    def __init__(self, quote=None, http_status=None):
        self._quote = quote
        self._http_status = http_status

    def get_quote(self, input_mint, output_mint, amount_lamports,
                  slippage_bps=100):
        if self._http_status is not None:
            raise BrokerError("unavailable", "quote failed",
                              detail={"reason": "jupiter_http_error",
                                      "http_status": self._http_status})
        return self._quote


def good_quote(impact="0.005", out="2000000"):
    return {"outAmount": out, "priceImpactPct": impact}


def make(quote=None, http_status=None, seed=7, **kw):
    return SimulationExecutor(
        jupiter=FakeJupiter(quote if quote is not None else good_quote(),
                            http_status),
        book=kw.pop("book", BalanceBook(10**9)), seed=seed, **kw)


def test_skip_parity_no_route():
    for quote, status in ((None, 400), ({"priceImpactPct": "0.001"}, None),
                          ({"outAmount": "abc", "priceImpactPct": "0.001"},
                           None)):
        try:
            make(quote, status).execute_swap(SOL, TOK, 10**7)
            raise AssertionError("expected no_route skip")
        except BrokerError as exc:
            assert exc.detail.get("reason") == "no_route", exc.detail
    print("PASS test_skip_parity_no_route")


def test_skip_parity_price_impact():
    for quote, reason in (({"outAmount": "100"}, "price_impact_unknown"),
                          ({"outAmount": "100", "priceImpactPct": "zz"},
                           "price_impact_unknown"),
                          (good_quote(impact="0.035"),
                           "price_impact_over_cap")):
        try:
            make(quote).execute_swap(SOL, TOK, 10**7)
            raise AssertionError(f"expected {reason}")
        except BrokerError as exc:
            assert exc.detail.get("reason") == reason, exc.detail
    print("PASS test_skip_parity_price_impact")


def test_skip_parity_fee_over_cap():
    # stake 0.001 SOL = 1e6 lamports; 5% cap = 50_000 lamports
    try:
        make().execute_swap(SOL, TOK, 10**6, priority_fee_lamports=60_000)
        raise AssertionError("expected fee_over_cap")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "fee_over_cap", exc.detail
    print("PASS test_skip_parity_fee_over_cap")


def test_outage_flag_not_leaked_by_landed_failure():
    class HugeMove(SimulationExecutor):
        def __init__(self, **kw):
            super().__init__(hourly_vol=1e9, latency_seconds=3600, **kw)
    book = BalanceBook(10**9)
    ex = HugeMove(jupiter=FakeJupiter(good_quote()), book=book, seed=5)
    ex.inject_confirm_outage(landed="settled")
    try:
        ex.execute_swap(SOL, TOK, 10**7)
        raise AssertionError("expected tx_failed")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "tx_failed"
    # The armed outage died with the attempt that consumed it: the next
    # swap is an ordinary landed failure, NOT a confirm_timeout.
    try:
        ex.execute_swap(SOL, TOK, 10**7)
        raise AssertionError("expected tx_failed")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "tx_failed", exc.detail
    print("PASS test_outage_flag_not_leaked_by_landed_failure")


def test_insolvency_refused_not_clamped():
    book = BalanceBook(10**9)
    ex = make(book=book, seed=3, hourly_vol=0.0)
    ex.set_book_balance(SOL, 12_000)  # below base+priority fee
    try:
        ex.execute_swap(SOL, TOK, 10**7)
        raise AssertionError("expected insufficient_funds_for_fee")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "insufficient_funds_for_fee"
    assert book.balance_of(SOL) == 12_000  # nothing was clamped or taken
    ex.set_book_balance(SOL, 20_000)  # fee ok, swap + fee not
    try:
        ex.execute_swap(SOL, TOK, 10**7)
        raise AssertionError("expected insufficient_funds")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "insufficient_funds"
    assert book.balance_of(SOL) == 20_000
    print("PASS test_insolvency_refused_not_clamped")


def test_fill_measured_from_book_deltas():
    book = BalanceBook(10**9)
    ex = make(book=book, seed=3, hourly_vol=0.0)
    res = ex.execute_swap(SOL, TOK, 10**7)
    assert res["simulated"] is True and res["signature"]
    assert res["out_amount"] == 2_000_000  # vol=0 -> quote amount
    assert book.balance_of(TOK) == 2_000_000
    assert book.balance_of(SOL) == 10**9 - 10**7 - 15_000
    assert res["sol_after"] == book.balance_of(SOL)
    assert res["priority_fee_lamports"] == 15_000  # base 5_000 + priority 10_000
    assert res["base_fee_lamports"] == 5_000
    assert res["quote_out_amount"] == 2_000_000
    print("PASS test_fill_measured_from_book_deltas")


def test_latency_move_within_tolerance_adjusts_fill():
    book = BalanceBook(10**9)
    ex = make(book=book, seed=11, hourly_vol=0.12, latency_seconds=2.0)
    res = ex.execute_swap(SOL, TOK, 10**7)
    assert res["latency_move_pct"] is not None
    assert abs(res["latency_move_pct"]) <= 100.0 * 100 / 10_000  # within 100bps... or the swap failed
    assert res["out_amount"] == int(2_000_000 *
                                    (1 + res["latency_move_pct"] / 100))
    print("PASS test_latency_move_within_tolerance_adjusts_fill")


def test_adverse_move_beyond_tolerance_is_landed_failure():
    class HugeMove(SimulationExecutor):
        def __init__(self, **kw):
            super().__init__(hourly_vol=1e9, latency_seconds=3600, **kw)
    book = BalanceBook(10**9)
    sol_before = book.balance_of(SOL)
    try:
        HugeMove(jupiter=FakeJupiter(good_quote()), book=book,
                 seed=5).execute_swap(SOL, TOK, 10**7)
        raise AssertionError("expected tx_failed")
    except BrokerError as exc:
        # Same taxonomy as the live executor's landed-failure reason.
        assert exc.detail.get("reason") == "tx_failed", exc.detail
        assert exc.detail.get("signature")
        assert exc.detail.get("fee_lamports") == 15_000
    # fee lost, no tokens, input not spent
    assert book.balance_of(TOK) == 0
    assert book.balance_of(SOL) == sol_before - 15_000
    print("PASS test_adverse_move_beyond_tolerance_is_landed_failure")


def test_confirm_outage_is_unknown_with_signature():
    book = BalanceBook(10**9)
    ex = make(book=book, hourly_vol=0.0)
    ex.inject_confirm_outage()
    try:
        ex.execute_swap(SOL, TOK, 10**7)
        raise AssertionError("expected confirm_timeout")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "confirm_timeout", exc.detail
        assert exc.detail.get("signature")
    # UNKNOWN: no state settled
    assert book.balance_of(TOK) == 0 and book.balance_of(SOL) == 10**9
    print("PASS test_confirm_outage_is_unknown_with_signature")


def test_deterministic_seed_replays_identically():
    r1 = make(seed=42).execute_swap(SOL, TOK, 10**7)
    r2 = make(seed=42).execute_swap(SOL, TOK, 10**7)
    assert r1 == r2, "same seed must replay the same simulated fill"
    print("PASS test_deterministic_seed_replays_identically")


def test_zero_output_guard_preserved():
    # A truthy-but-zero outAmount passes quote validation exactly as on
    # the live executor (the string "0" is not falsy) and must trip the
    # same zero_output measurement guard after settlement, with the
    # signature attached for reconciliation.
    book = BalanceBook(10**9)
    try:
        make(good_quote(out="0"), book=book).execute_swap(SOL, TOK, 10**7)
        raise AssertionError("expected zero_output")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "zero_output", exc.detail
        assert exc.detail.get("signature")
    print("PASS test_zero_output_guard_preserved")


def test_bundle_mode_requires_jito_and_policy():
    try:
        make().execute_swap(SOL, TOK, 10**7, use_bundle=True)
        raise AssertionError("expected jito_not_configured")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "jito_not_configured", exc.detail
    print("PASS test_bundle_mode_requires_jito_and_policy")


def test_volatility_estimator():
    import math
    prices = [100 * math.exp(0.001 * (i % 5)) for i in range(50)]
    vol = estimate_volatility_from_series(prices, 60.0)
    assert vol > 0
    try:
        estimate_volatility_from_series([1.0, 2.0], 60.0)
        raise AssertionError("expected refusal on too-short series")
    except BrokerError:
        pass
    try:
        estimate_volatility_from_series([1.0, 0.0, 1.1, 1.2], 60.0)
        raise AssertionError("nonpositive tick must fail, not be dropped")
    except BrokerError:
        pass
    print("PASS test_volatility_estimator")


if __name__ == "__main__":
    test_skip_parity_no_route()
    test_skip_parity_price_impact()
    test_skip_parity_fee_over_cap()
    test_fill_measured_from_book_deltas()
    test_latency_move_within_tolerance_adjusts_fill()
    test_adverse_move_beyond_tolerance_is_landed_failure()
    test_outage_flag_not_leaked_by_landed_failure()
    test_insolvency_refused_not_clamped()
    test_confirm_outage_is_unknown_with_signature()
    test_deterministic_seed_replays_identically()
    test_zero_output_guard_preserved()
    test_bundle_mode_requires_jito_and_policy()
    test_volatility_estimator()
    print("ALL SIM-EXECUTOR TESTS PASSED")
