"""Adapter-level causal tests for the account-wide hold primitive.

End-to-end through the real demo bridge + real shared account:
P7 (two books, one account), P9 (settled failed tx fee), unlanded
release, orphan-hold sweep after a crash between hold and seal, and
the sweep never touching another book's live reservation."""
import os
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from broker import BrokerError, OrderRequest
from broker.memecoin.adapter import MemecoinAdapter
from broker.memecoin.transport import MemecoinMarketData
from broker.memecoin.execution.sim_executor import SimulationExecutor
from tests.test_paper_account_instances import account
from daemon.launch_scanner import paper_account as pa

MINT = "BonkMint111111111111111111111111111111111111"
DEC = 6
QUOTE = {"outAmount": "49000000000", "priceImpactPct": "0.004"}


class FakeMarket(MemecoinMarketData):
    def __init__(self):
        super().__init__()
        self.usd = {"SOL": 100.0, "BONK": 0.0001}

    def get_quote(self, symbol):
        if not self.mint_for(symbol):
            return None
        px = self.usd.get(symbol.upper())
        return {"symbol": symbol.upper(), "mint": self.mint_for(symbol),
                "source": "fixture:observed", "price_usd": px,
                "price_sol": None, "pair": None,
                "ts": datetime.now(timezone.utc)}

    def get_candles(self, symbol, timeframe, count):
        return []


class FakeJupiter:
    def get_quote(self, input_mint, output_mint, amount_lamports,
                  slippage_bps=100):
        return dict(QUOTE)


def _req(direction, volume, key):
    return OrderRequest(symbol="BONK", direction=direction, volume=volume,
                        stop_loss=0.5, take_profit=2.0,
                        signal_id="sig-demo", idempotency_key=key)


def _adapter(tmp, book, account):
    market = FakeMarket()
    market.register("SOL", "So11111111111111111111111111111111111111112")
    market.register("BONK", MINT)
    ex = SimulationExecutor(
        jupiter=FakeJupiter(), seed=7, hourly_vol=0.0,
        chain_state_path=os.path.join(tmp, book, "sim_chain.json"))
    ad = MemecoinAdapter(
        data_dir=os.path.join(tmp, book), mode="demo", market_data=market,
        symbols={"BONK": MINT}, demo_executor=ex,
        demo_decimals_fn=lambda mint: DEC, shared_account=pa,
        fx_observer=lambda: {"usd_per_sol": 100.,
                             "observed_at": time.time(),
                             "source": "fixture:SOL"})
    ad.connect({})
    return ad, ex


def _timeout(ad, ex, key, landed):
    ex.inject_confirm_outage(landed=landed)
    with pytest.raises(BrokerError) as ei:
        ad.submit_order(_req("BUY", 0.05, key))
    assert ei.value.detail.get("reason") == "confirm_timeout"
    return ei.value.detail["signature"]


class TestCrossBookHold:
    def test_unknown_hold_blocks_second_book_then_adoption_frees(
            self, account, tmp_path):
        # P7: book A's execution settles but is not yet adopted; book
        # B must see the reservation account-wide.
        a, exa = _adapter(str(tmp_path), "bookA", account)
        b, _ = _adapter(str(tmp_path), "bookB", account)
        _timeout(a, exa, "k1", "settled")
        snap = pa.snapshot()
        assert snap["cash"] == 10.0
        assert snap["held_cash"] > 0
        assert snap["cash_spendable"] == 10.0 - snap["held_cash"]
        with pytest.raises(BrokerError, match="refused"):
            b.submit_order(_req("BUY", 0.05, "b1"))
        # Reconcile A's landed fill: adoption consumes the hold.
        out = a.reconcile_demo_execution("k1")
        assert out["settled"] is True
        snap = pa.snapshot()
        assert snap["held_cash"] == 0.0
        # stake = (0.05 SOL + 15000-lamport fee) * $100
        assert snap["cash"] == pytest.approx(10.0 - 5.0015)
        assert snap["cash_spendable"] == pytest.approx(snap["cash"])
        assert snap["lanes"]["main"]["open_count"] == 1
        # B can now enter with what actually remains.
        assert b.submit_order(_req("BUY", 0.045, "b2")).ticket == 1
        # B's stake = (0.045 SOL + 15000-lamport fee) * $100
        assert pa.snapshot()["cash"] == pytest.approx(
            10.0 - 5.0015 - 4.5015)


class TestSharedFeeSettlement:
    def test_landed_failure_fee_reduces_shared_ledger(
            self, account, tmp_path):
        # P9: the fee of a settled failed tx leaves the shared USD
        # ledger, not just the native SOL mirror.
        a, exa = _adapter(str(tmp_path), "bookA", account)
        _timeout(a, exa, "k1", "failed")
        sol_before = a._balance_sol
        out = a.reconcile_demo_execution("k1")
        assert out["status"] == "failed" and out["fee_lamports"] > 0
        fee_usd = round(out["fee_lamports"] / 1e9 * 100., 6)
        snap = pa.snapshot()
        assert snap["cash"] == pytest.approx(10.0 - fee_usd)
        assert snap["held_cash"] == 0.0
        assert snap["cash_spendable"] == pytest.approx(snap["cash"])
        assert a._balance_sol == pytest.approx(
            sol_before - out["fee_lamports"] / 1e9)
        assert pa.list_holds() == {}


class TestUnlandedRelease:
    def test_unlanded_reconcile_releases_hold(self, account, tmp_path):
        a, exa = _adapter(str(tmp_path), "bookA", account)
        _timeout(a, exa, "k1", "unlanded")
        assert pa.snapshot()["held_cash"] > 0
        out = a.reconcile_demo_execution("k1")
        assert out["status"] == "unlanded"
        snap = pa.snapshot()
        assert snap["held_cash"] == 0.0
        assert snap["cash_spendable"] == 10.0


class TestOrphanSweep:
    def test_seal_write_failure_releases_inline(
            self, account, tmp_path):
        # LOW fix: a same-process seal-write failure must not strand
        # the reservation until restart - the chain effect has not
        # happened yet, so the hold is provably unbacked.
        a, exa = _adapter(str(tmp_path), "bookA", account)
        with patch.object(a, "_save_state",
                          side_effect=OSError("seal write lost")):
            with pytest.raises(OSError):
                a.submit_order(_req("BUY", 0.05, "k1"))
        assert pa.list_holds() == {}
        assert pa.snapshot()["cash_spendable"] == 10.0

    def test_orphan_hold_after_failed_inline_release_swept_on_restart(
            self, account, tmp_path):
        a, exa = _adapter(str(tmp_path), "bookA", account)
        # Both the seal write AND the inline release fail: the restart
        # sweep is the backstop for the reservation.
        with patch.object(a, "_save_state",
                          side_effect=OSError("seal write lost")), \
             patch.object(pa, "release_hold",
                          side_effect=OSError("release lost")):
            with pytest.raises(OSError):
                a.submit_order(_req("BUY", 0.05, "k1"))
        assert len(pa.list_holds()) == 1
        # Restart: the new process owns no record for the hold.
        b, _ = _adapter(str(tmp_path), "bookA", account)
        assert pa.list_holds() == {}
        assert pa.snapshot()["cash_spendable"] == 10.0

    def test_sweep_never_touches_other_books_or_live_records(
            self, account, tmp_path):
        a, _ = _adapter(str(tmp_path), "bookA", account)
        b, _ = _adapter(str(tmp_path), "bookB", account)
        # Another book's hold (its own crash gap or in-flight
        # reservation) is outside this book's sweep prefix.
        pa.hold_stake("main", 1.0,
                      "native-hold:" + b._shared_book_id + ":live", mint="TKM")
        # This book's hold WITH a live sealed record survives.
        a_key_hold = ("native-hold:" + a._shared_book_id + ":k1")
        pa.hold_stake("main", 1.0, a_key_hold, mint="TKM")
        a._unresolved["k1"] = {"signature": "s", "hold_op_id": a_key_hold,
                               "direction": "BUY"}
        a._save_state()
        c, _ = _adapter(str(tmp_path), "bookA", account)
        holds = pa.list_holds()
        assert set(holds) == {"native-hold:" + b._shared_book_id + ":live",
                              a_key_hold}
