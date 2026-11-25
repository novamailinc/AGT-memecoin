"""Demo-mode adapter lifecycle tests — run:
python3 tests/test_demo_adapter_lifecycle.py

End-to-end through MemecoinAdapter(mode="demo"): measured fills with
fees, skip propagation, idempotent replay, UNKNOWN timeout semantics.
Canned Jupiter quotes; deterministic seeded executor; no network."""

import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.adapter import MemecoinAdapter
from broker.memecoin.transport import MemecoinMarketData
from broker.memecoin.execution.sim_executor import SimulationExecutor
from broker import OrderRequest


class FakeMarket(MemecoinMarketData):
    def __init__(self, usd):
        super().__init__()
        self.usd = dict(usd)

    def get_quote(self, symbol):
        if not self.mint_for(symbol):
            return None
        px = self.usd.get(symbol.upper())
        if px is None:
            return None
        return {"symbol": symbol.upper(), "mint": self.mint_for(symbol),
                "source": "fixture:observed",
                "price_usd": px, "price_sol": None,
                "pair": None, "ts": datetime.now(timezone.utc)}

    def get_candles(self, symbol, timeframe, count):
        return []


class FakeJupiter:
    def __init__(self, quotes):
        self.quotes = list(quotes)
        self.calls = 0

    def get_quote(self, input_mint, output_mint, amount_lamports,
                  slippage_bps=100):
        self.calls += 1
        return self.quotes[min(self.calls - 1, len(self.quotes) - 1)]


MINT = "BonkMint111111111111111111111111111111111111"
DEC = 6


def _req(direction, volume, key=None):
    return OrderRequest(symbol="BONK", direction=direction, volume=volume,
                        stop_loss=0.5, take_profit=2.0,
                        signal_id="sig-demo", idempotency_key=key)


def _adapter(tmp, jupiter, seed=7):
    market = FakeMarket({"SOL": 100.0, "BONK": 0.0001})
    market.register("SOL",
                    "So11111111111111111111111111111111111111112")
    market.register("BONK", MINT)
    ex = SimulationExecutor(jupiter=jupiter, seed=seed, hourly_vol=0.0)
    ad = MemecoinAdapter(data_dir=os.path.join(tmp, "memecoin"),
                         mode="demo", market_data=market,
                         symbols={"BONK": MINT},
                         demo_executor=ex,
                         demo_decimals_fn=lambda mint: DEC)
    ad.connect({})
    return ad, ex


def test_demo_buy_measured_fill_with_fee():
    with tempfile.TemporaryDirectory() as tmp:
        # 0.05 SOL buy; quote returns 49_000 tokens (base units at 6 dec)
        ad, ex = _adapter(tmp, FakeJupiter(
            [{"outAmount": "49000000000", "priceImpactPct": "0.004"}]))
        res = ad.submit_order(_req("BUY", 0.05))
        assert res.raw["mode"] == "demo"
        assert res.raw["tokens"] == 49_000.0
        assert res.raw["fee_lamports"] == 15_000  # base + priority
        assert res.raw["signature"]
        # authoritative balance = start - stake - measured fee
        assert abs(ad._balance_sol - (10.0 - 0.05 - 0.000015)) < 1e-12
        pos = ad._positions[str(res.ticket)]
        assert pos["tokens"] == 49_000.0
        assert abs(pos["fee_sol"] - 0.000015) < 1e-15
        assert abs(pos["entry_price_sol"] - 0.05 / 49_000.0) < 1e-18
    print("PASS test_demo_buy_measured_fill_with_fee")


def test_demo_skip_leaves_state_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp, FakeJupiter(
            [{"outAmount": "49000000000", "priceImpactPct": "0.099"}]))
        before = ad._balance_sol
        try:
            ad.submit_order(_req("BUY", 0.05))
            raise AssertionError("expected price_impact_over_cap")
        except BrokerError as exc:
            assert exc.detail.get("reason") == "price_impact_over_cap"
        assert ad._balance_sol == before and not ad._positions
    print("PASS test_demo_skip_leaves_state_untouched")


def test_demo_idempotent_replay_executes_once():
    with tempfile.TemporaryDirectory() as tmp:
        jup = FakeJupiter(
            [{"outAmount": "49000000000", "priceImpactPct": "0.004"}])
        ad, ex = _adapter(tmp, jup)
        r1 = ad.submit_order(_req("BUY", 0.05, key="k-1"))
        r2 = ad.submit_order(_req("BUY", 0.05, key="k-1"))
        assert r1.ticket == r2.ticket
        assert jup.calls == 1, f"replay must not re-execute ({jup.calls})"
    print("PASS test_demo_idempotent_replay_executes_once")


def test_demo_unknown_timeout_then_retry():
    with tempfile.TemporaryDirectory() as tmp:
        jup = FakeJupiter(
            [{"outAmount": "49000000000", "priceImpactPct": "0.004"}])
        ad, ex = _adapter(tmp, jup)
        before = ad._balance_sol
        ex.inject_confirm_outage()
        try:
            ad.submit_order(_req("BUY", 0.05, key="k-2"))
            raise AssertionError("expected confirm_timeout")
        except BrokerError as exc:
            assert exc.detail.get("reason") == "confirm_timeout"
            assert exc.detail.get("signature")
        assert ad._balance_sol == before and not ad._positions
        res = ad.submit_order(_req("BUY", 0.05, key="k-3"))
        assert res.raw["tokens"] == 49_000.0
    print("PASS test_demo_unknown_timeout_then_retry")


def test_demo_sell_fifo_net_of_fee():
    with tempfile.TemporaryDirectory() as tmp:
        jup = FakeJupiter([
            {"outAmount": "49000000000", "priceImpactPct": "0.004"},
            {"outAmount": "24500000", "priceImpactPct": "0.003"},
        ])
        ad, ex = _adapter(tmp, jup)
        buy = ad.submit_order(_req("BUY", 0.05))
        # sell 0.0245 SOL worth: est price 1e-6 SOL -> 24_500 tokens
        sell = ad.submit_order(_req("SELL", 0.0245))
        # measured proceeds net of the 15_000-lamport fee
        assert abs(sell.raw["tokens"] - 24_500.0) < 1e-6
        assert abs(sell.volume - (0.0245 - 0.000015)) < 1e-12
        pos = ad._positions[str(buy.ticket)]
        assert abs(pos["tokens"] - (49_000.0 - 24_500.0)) < 1e-6
        expected = 10.0 - 0.05 - 0.000015 + (0.0245 - 0.000015)
        assert abs(ad._balance_sol - expected) < 1e-9, ad._balance_sol
    print("PASS test_demo_sell_fifo_net_of_fee")


if __name__ == "__main__":
    test_demo_buy_measured_fill_with_fee()
    test_demo_skip_leaves_state_untouched()
    test_demo_idempotent_replay_executes_once()
    test_demo_unknown_timeout_then_retry()
    test_demo_sell_fifo_net_of_fee()
    print("ALL DEMO-ADAPTER LIFECYCLE TESTS PASSED")
