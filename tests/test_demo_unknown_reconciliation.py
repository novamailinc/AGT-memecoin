"""UNKNOWN (confirm_timeout) reconciliation tests — run:
python3 tests/test_demo_unknown_reconciliation.py

An ambiguous first attempt must never be blindly re-executed. Covers:
unlanded / landed-settled / landed-failed truths, blind-retry refusal,
crash-restart durability of both the sealed record and the simulated
chain memory, repeated unknowns, ticket/journal counter integrity."""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import BrokerError, RISK_LIMIT_EXCEEDED, INVALID_ORDER
from broker.memecoin.adapter import MemecoinAdapter
from broker.memecoin.transport import MemecoinMarketData
from broker.memecoin.execution.sim_executor import SimulationExecutor
from broker import OrderRequest

MINT = "BonkMint111111111111111111111111111111111111"
DEC = 6
QUOTE = {"outAmount": "49000000000", "priceImpactPct": "0.40"}


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
    def __init__(self):
        self.calls = 0

    def get_quote(self, input_mint, output_mint, amount_lamports,
                  slippage_bps=100):
        self.calls += 1
        return dict(QUOTE)


def _req(direction, volume, key):
    return OrderRequest(symbol="BONK", direction=direction, volume=volume,
                        stop_loss=0.5, take_profit=2.0,
                        signal_id="sig-demo", idempotency_key=key)


def _adapter(tmp):
    market = FakeMarket()
    market.register("SOL", "So11111111111111111111111111111111111111112")
    market.register("BONK", MINT)
    ex = SimulationExecutor(
        jupiter=FakeJupiter(), seed=7, hourly_vol=0.0,
        chain_state_path=os.path.join(tmp, "memecoin", "sim_chain.json"))
    ad = MemecoinAdapter(data_dir=os.path.join(tmp, "memecoin"),
                         mode="demo", market_data=market,
                         symbols={"BONK": MINT}, demo_executor=ex,
                         demo_decimals_fn=lambda mint: DEC)
    ad.connect({})
    return ad, ex


def _timeout(ad, ex, key, landed):
    ex.inject_confirm_outage(landed=landed)
    try:
        ad.submit_order(_req("BUY", 0.05, key=key))
        raise AssertionError("expected confirm_timeout")
    except BrokerError as exc:
        assert exc.detail.get("reason") == "confirm_timeout"
        return exc.detail["signature"]


def test_unlanded_timeout_refuses_blind_retry_then_clears():
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        before = ad._balance_sol
        sig = _timeout(ad, ex, "k-u", "unlanded")
        assert ad._balance_sol == before and not ad._positions
        try:
            ad.submit_order(_req("BUY", 0.05, key="k-u"))
            raise AssertionError("blind retry must be refused")
        except BrokerError as exc:
            assert exc.detail.get("reason") == "unknown_unresolved"
            assert exc.detail.get("signature") == sig
        # no ticket allocated, nothing journaled
        assert ad._next_ticket == 1
        out = ad.reconcile_demo_execution("k-u")
        assert out == {"status": "unlanded", "settled": False,
                       "fee_lamports": 0, "chain_status": "unlanded",
                       "signature": sig}
        res = ad.submit_order(_req("BUY", 0.05, key="k-u"))
        assert res.raw["tokens"] == 49_000.0
    print("PASS test_unlanded_timeout_refuses_blind_retry_then_clears")


def test_landed_settled_reconcile_adopts_fill():
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        before = ad._balance_sol
        sig = _timeout(ad, ex, "k-s", "settled")
        assert ad._balance_sol == before and not ad._positions
        out = ad.reconcile_demo_execution("k-s")
        assert out["status"] == "settled" and out["ticket"] == 1
        # fill adopted once: stake + fee debited, position journaled
        assert abs(ad._balance_sol - (before - 0.05 - 0.000015)) < 1e-12
        assert ad._positions["1"]["tokens"] == 49_000.0
        # a second reconcile finds nothing; no double-apply
        try:
            ad.reconcile_demo_execution("k-s")
            raise AssertionError("expected no_unknown_execution")
        except BrokerError as exc:
            assert exc.detail.get("reason") == "no_unknown_execution"
        assert abs(ad._balance_sol - (before - 0.05 - 0.000015)) < 1e-12
    print("PASS test_landed_settled_reconcile_adopts_fill")


def test_landed_failed_reconcile_books_fee_loss():
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        before = ad._balance_sol
        _timeout(ad, ex, "k-f", "failed")
        out = ad.reconcile_demo_execution("k-f")
        assert out["status"] == "failed" and out["fee_lamports"] == 15_000
        assert abs(ad._balance_sol - (before - 0.000015)) < 1e-15
        assert not ad._positions and ad._next_ticket == 1
    print("PASS test_landed_failed_reconcile_books_fee_loss")


def test_crash_restart_recovers_sealed_record_and_chain_truth():
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        sig = _timeout(ad, ex, "k-r", "settled")
        # simulate crash: discard both objects, rebuild on the same dir
        ad2, ex2 = _adapter(tmp)
        assert "k-r" in ad2._unresolved
        assert ad2._unresolved["k-r"]["signature"] == sig
        try:
            ad2.submit_order(_req("BUY", 0.05, key="k-r"))
            raise AssertionError("blind retry after restart must be refused")
        except BrokerError as exc:
            assert exc.detail.get("reason") == "unknown_unresolved"
        out = ad2.reconcile_demo_execution("k-r")
        assert out["status"] == "settled" and out["ticket"] == 1
        assert ad2._positions["1"]["tokens"] == 49_000.0
        # chain memory survived independently of the adapter
        assert ex2.reconcile(sig)["status"] == "settled"
    print("PASS test_crash_restart_recovers_sealed_record_and_chain_truth")


def test_repeated_unknowns_and_journal_integrity():
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        _timeout(ad, ex, "k-1", "unlanded")
        _timeout(ad, ex, "k-2", "failed")
        assert set(ad._unresolved) == {"k-1", "k-2"}
        assert ad._next_ticket == 1
        fills = ad._fills_path()
        assert not fills.exists() or not fills.read_text().strip()
        ad.reconcile_demo_execution("k-1")
        ad.reconcile_demo_execution("k-2")
        assert not ad._unresolved and ad._next_ticket == 1
        res = ad.submit_order(_req("BUY", 0.05, key="k-3"))
        assert res.ticket == 1  # counters untouched by unknowns
    print("PASS test_repeated_unknowns_and_journal_integrity")


def test_unknown_signature_refuses_to_guess():
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        try:
            ex.reconcile("deadbeef" * 8)
            raise AssertionError("expected unknown_signature")
        except BrokerError as exc:
            assert exc.detail.get("reason") == "unknown_signature"
    print("PASS test_unknown_signature_refuses_to_guess")


def test_reserved_exposure_blocks_overcommit():
    """Reviewer probe 1: 0.1 SOL, landed-UNKNOWN BUY .05, then a
    different-key .05 BUY must be REFUSED while the first is
    unresolved — the stake + max fee is reserved, not merely logged."""
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        ad._balance_sol = 0.1
        ad._save_state()
        _timeout(ad, ex, "first", "settled")
        assert list(ad._unresolved) == ["first"]
        try:
            ad.submit_order(_req("BUY", 0.05, key="second"))
            raise AssertionError("second buy must be refused: reserved")
        except BrokerError as exc:
            assert exc.code == RISK_LIMIT_EXCEEDED, exc
        assert not ad._positions  # no overcommit, no negative balance
        out = ad.reconcile_demo_execution("first")
        assert out["status"] == "settled"
        # Reservation released into the actual fill: balance exact, and
        # another .05 is honestly refused (stake+fee do not fit) — but
        # a .04 buy proves the reservation was released, not leaked.
        assert abs(ad._balance_sol - (0.1 - 0.05 - 0.000015)) < 1e-12
        try:
            ad.submit_order(_req("BUY", 0.05, key="second"))
            raise AssertionError("still honestly insufficient")
        except BrokerError as exc:
            assert exc.code == RISK_LIMIT_EXCEEDED
        res = ad.submit_order(_req("BUY", 0.04, key="second"))
        assert res.ticket == 2 and len(ad._positions) == 2
        assert ad._balance_sol > 0
    print("PASS test_reserved_exposure_blocks_overcommit")


def test_direct_restart_reconcile_without_submit():
    """Reviewer probe 2a: a fresh adapter that never submitted must
    reconcile directly — the sealed record carries its own request
    fingerprint."""
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        _timeout(ad, ex, "first", "settled")
        ad, ex = _adapter(tmp)  # restart; no submit on this instance
        out = ad.reconcile_demo_execution("first")
        assert out["status"] == "settled" and out["ticket"] == 1
        # idempotent replay of the original request recovers the adopted
        # fill — no fingerprint conflict
        res = ad.submit_order(_req("BUY", 0.05, key="first"))
        assert res.ticket == 1 and len(ad._positions) == 1
    print("PASS test_direct_restart_reconcile_without_submit")


def test_interleaved_submit_does_not_contaminate_fingerprint():
    """Reviewer probe 2b: an intervening different-key submit must not
    leak its request fingerprint into the reconciled fill."""
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        _timeout(ad, ex, "first", "settled")
        ad.submit_order(_req("BUY", 0.05, key="other"))
        out = ad.reconcile_demo_execution("first")
        assert out["status"] == "settled"
        fills = [json.loads(line) for line in
                 ad._fills_path().read_text().splitlines() if line.strip()]
        fps = {f["idempotency_key"]: f["request_fingerprint"]
               for f in fills if f.get("idempotency_key")}
        assert fps["first"] != fps["other"], "fingerprints must differ"
        # replay of the original request recovers, a conflicting one bites
        res = ad.submit_order(_req("BUY", 0.05, key="first"))
        assert res.ticket == out["ticket"]
        try:
            ad.submit_order(_req("BUY", 0.07, key="first"))
            raise AssertionError("conflicting replay must be refused")
        except BrokerError as exc:
            assert exc.code == INVALID_ORDER
    print("PASS test_interleaved_submit_does_not_contaminate_fingerprint")


def test_commit_exception_keeps_seal_and_recovers():
    """Reviewer probe 3: a commit failure after the swap settled keeps
    the sealed record on disk AND in memory; reconcile adopts once."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        with patch.object(ad, "_commit_native",
                          side_effect=OSError("injected pre-intent failure")):
            try:
                ad.submit_order(_req("BUY", 0.05, key="commit-fail"))
                raise AssertionError("expected OSError")
            except OSError:
                pass
        disk = json.loads(ad._state_path().read_text())["unresolved_execs"]
        assert list(disk) == ["commit-fail"]
        assert list(ad._unresolved) == ["commit-fail"]  # memory reloaded
        ad.submit_order(_req("BUY", 0.05, key="other"))
        disk = json.loads(ad._state_path().read_text())["unresolved_execs"]
        assert list(disk) == ["commit-fail"]
        out = ad.reconcile_demo_execution("commit-fail")
        assert out["status"] == "settled"
        assert len(ad._positions) == 2  # adopted once + other
    print("PASS test_commit_exception_keeps_seal_and_recovers")


def test_persisted_intent_recovers_after_crash():
    """Reviewer probe 5: crash after the native intent is written but
    before recovery must restart cleanly — the seal save is a durable
    transition, so the intent before-image matches disk."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        original = ad._recover_native_intent

        def crash_after_intent():
            if ad._intent_path().exists():
                raise OSError("crash after intent persisted")
            return original()
        with patch.object(ad, "_recover_native_intent",
                          side_effect=crash_after_intent):
            try:
                ad.submit_order(_req("BUY", 0.05, key="persisted-intent"))
                raise AssertionError("expected OSError")
            except OSError:
                pass
        assert ad._intent_path().exists()
        ad, ex = _adapter(tmp)  # restart: recovery completes once
        assert ad._balance_sol == 10.0 - 0.05 - 0.000015
        assert list(ad._positions) == ["1"]
        assert not ad._unresolved
        fills = ad._fills_path().read_text().strip().splitlines()
        assert len(fills) == 1  # journaled exactly once
    print("PASS test_persisted_intent_recovers_after_crash")


def test_pre_send_crash_reconciles_unlanded():
    """Reviewer probe 6: crash between seal and execute leaves a durable
    'allocated' chain record — provably unlanded, resolvable, never a
    guess."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        with patch.object(ex, "execute_swap",
                          side_effect=OSError("crash before swap")):
            try:
                ad.submit_order(_req("BUY", 0.05, key="pre-send"))
                raise AssertionError("expected OSError")
            except OSError:
                pass
        ad, ex = _adapter(tmp)  # restart: chain memory has the record
        out = ad.reconcile_demo_execution("pre-send")
        assert out["status"] == "unlanded"
        assert out["chain_status"] == "allocated"
        res = ad.submit_order(_req("BUY", 0.05, key="pre-send"))
        assert res.raw["tokens"] == 49_000.0
    print("PASS test_pre_send_crash_reconciles_unlanded")


def test_landed_failure_fee_is_durable():
    """Reviewer probe 7: an ordinary landed failure (not UNKNOWN) must
    book its fee against authoritative state — the scratch reset must
    never erase it."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        before = ad._balance_sol
        ex._vol = 1.0
        with patch.object(ex._rng, "gauss", return_value=-0.1):
            try:
                ad.submit_order(_req("BUY", 0.05, key="clean-failed"))
                raise AssertionError("expected tx_failed")
            except BrokerError as exc:
                assert exc.detail["reason"] == "tx_failed"
        assert not ad._unresolved
        assert abs(ad._balance_sol - (before - 0.000015)) < 1e-15
        ad.submit_order(_req("BUY", 0.05, key="next-after-failed"))
        assert abs(ad._balance_sol
                   - (before - 0.000015 - 0.05 - 0.000015)) < 1e-12
        # durable across restart too
        ad, ex = _adapter(tmp)
        assert abs(ad._balance_sol
                   - (before - 0.000015 - 0.05 - 0.000015)) < 1e-12
    print("PASS test_landed_failure_fee_is_durable")


def test_crash_between_settle_and_record_stays_unlanded():
    """Crash after the scratch book settled but BEFORE the chain record
    write: the durable record stays 'allocated' (provably never sent —
    the book is ephemeral scratch, never a ledger), so the attempt
    resolves unlanded and the retry settles exactly once."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        original = ex._chain_record

        def crash_on_settle(sig, record):
            if record.get("status") == "settled":
                raise OSError("crash between settle and record")
            return original(sig, record)
        with patch.object(ex, "_chain_record", side_effect=crash_on_settle):
            try:
                ad.submit_order(_req("BUY", 0.05, key="mid-settle"))
                raise AssertionError("expected OSError")
            except OSError:
                pass
        ad, ex = _adapter(tmp)  # restart: scratch book is gone
        out = ad.reconcile_demo_execution("mid-settle")
        assert out["status"] == "unlanded"
        assert out["chain_status"] == "allocated"
        res = ad.submit_order(_req("BUY", 0.05, key="mid-settle"))
        assert res.ticket == 1 and len(ad._positions) == 1
        assert abs(ad._balance_sol - (10.0 - 0.05 - 0.000015)) < 1e-12
    print("PASS test_crash_between_settle_and_record_stays_unlanded")


def test_fee_booking_crash_is_atomic():
    """Crash during the landed-failure fee booking: the atomic state
    write leaves the record sealed, and reconcile books the fee exactly
    once — never zero, never twice."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        ex._vol = 1.0
        original_save = ad._save_state
        calls = []

        def flaky_save():
            calls.append(1)
            if len(calls) == 2:  # 1st = seal, 2nd = fee booking
                raise OSError("crash during fee booking")
            return original_save()
        with patch.object(ad, "_save_state", side_effect=flaky_save), \
                patch.object(ex._rng, "gauss", return_value=-0.1):
            try:
                ad.submit_order(_req("BUY", 0.05, key="fee-crash"))
                raise AssertionError("expected OSError")
            except OSError:
                pass
        # disk still shows the sealed record; memory was reloaded
        assert list(json.loads(ad._state_path().read_text())
                    ["unresolved_execs"]) == ["fee-crash"]
        assert list(ad._unresolved) == ["fee-crash"]
        out = ad.reconcile_demo_execution("fee-crash")
        assert out["status"] == "failed" and out["fee_lamports"] == 15_000
        assert abs(ad._balance_sol - (10.0 - 0.000015)) < 1e-15
        try:
            ad.reconcile_demo_execution("fee-crash")
            raise AssertionError("expected no_unknown_execution")
        except BrokerError as exc:
            assert exc.detail.get("reason") == "no_unknown_execution"
        assert abs(ad._balance_sol - (10.0 - 0.000015)) < 1e-15
    print("PASS test_fee_booking_crash_is_atomic")


def test_shared_entry_reserved_across_keys():
    """Reviewer probe 4: shared $10 — an UNKNOWN entry's stake + max fee
    is reserved at seal, so a competing key is refused up front and the
    landed fill's adoption can never be refused for cash."""
    import time
    from daemon.launch_scanner import paper_account as pa
    with tempfile.TemporaryDirectory() as tmp:
        folder = os.path.join(tmp, "shared")
        pa.ACCOUNT_DIR = folder
        pa.LEDGER_PATH = folder + "/ledger.json"
        pa.JOURNAL_PATH = folder + "/events.jsonl"
        pa.LOCK_FILE = folder + "/.lock"
        os.environ["PAPER_ACCOUNT_START"] = "10"
        os.environ["PAPER_ACCOUNT_LANE_CAP"] = "10"
        try:
            ad, ex = _adapter(tmp)
            ad._shared_account = pa
            ad._fx_observer = lambda: {
                "usd_per_sol": 100., "observed_at": time.time(),
                "source": "fixture:SOL"}
            _timeout(ad, ex, "shared-u", "settled")
            assert pa.snapshot()["cash"] == 10.0  # nothing debited yet
            try:
                ad.submit_order(_req("BUY", 0.05, key="shared-other"))
                raise AssertionError("expected shared reservation refusal")
            except BrokerError as exc:
                assert exc.detail.get("reason") == "shared_entry_reserved"
            assert pa.snapshot()["cash"] == 10.0
            out = ad.reconcile_demo_execution("shared-u")
            assert out["status"] == "settled"
            # stake $5 + actual fee $0.0015 debited exactly once
            assert abs(pa.snapshot()["cash"] - (10.0 - 5.0 - 0.0015)) < 1e-9
        finally:
            del os.environ["PAPER_ACCOUNT_START"]
            del os.environ["PAPER_ACCOUNT_LANE_CAP"]
    print("PASS test_shared_entry_reserved_across_keys")


def _bind_shared(ad, tmp):
    import time
    from daemon.launch_scanner import paper_account as pa
    folder = os.path.join(tmp, "shared")
    pa.ACCOUNT_DIR = folder
    pa.LEDGER_PATH = folder + "/ledger.json"
    pa.JOURNAL_PATH = folder + "/events.jsonl"
    pa.LOCK_FILE = folder + "/.lock"
    os.environ["PAPER_ACCOUNT_START"] = "10"
    os.environ["PAPER_ACCOUNT_LANE_CAP"] = "10"
    ad._shared_account = pa
    ad._fx_observer = lambda: {
        "usd_per_sol": 100., "observed_at": time.time(),
        "source": "fixture:SOL"}
    return pa


def test_stale_fx_adoption_validates_as_of_seal():
    """A fill settled long before its adoption must not be stranded by
    the 60s FX-freshness window: the pinned evidence validates as of
    the seal time."""
    import time
    with tempfile.TemporaryDirectory() as tmp:
        ad, ex = _adapter(tmp)
        pa = _bind_shared(ad, tmp)
        try:
            _timeout(ad, ex, "stale-fx", "settled")
            # Age BOTH the pinned FX evidence and the seal past the 60s
            # window: fresh when sealed, stale by adoption wall-clock.
            sealed = time.time() - 3600
            rec = ad._unresolved["stale-fx"]
            rec["entry_pricing_fx"] = {
                "usd_per_sol": 100.,
                "observed_at": sealed,
                "source": "fixture:SOL"}
            rec["sealed_at"] = datetime.fromtimestamp(
                sealed + 5, tz=timezone.utc).isoformat()
            ad._save_state()
            out = ad.reconcile_demo_execution("stale-fx")
            assert out["status"] == "settled", out
            assert abs(pa.snapshot()["cash"] - (10.0 - 5.0 - 0.0015)) < 1e-9
        finally:
            del os.environ["PAPER_ACCOUNT_START"]
            del os.environ["PAPER_ACCOUNT_LANE_CAP"]
    print("PASS test_stale_fx_adoption_validates_as_of_seal")


if __name__ == "__main__":
    test_unlanded_timeout_refuses_blind_retry_then_clears()
    test_landed_settled_reconcile_adopts_fill()
    test_landed_failed_reconcile_books_fee_loss()
    test_crash_restart_recovers_sealed_record_and_chain_truth()
    test_repeated_unknowns_and_journal_integrity()
    test_unknown_signature_refuses_to_guess()
    test_reserved_exposure_blocks_overcommit()
    test_direct_restart_reconcile_without_submit()
    test_interleaved_submit_does_not_contaminate_fingerprint()
    test_commit_exception_keeps_seal_and_recovers()
    test_persisted_intent_recovers_after_crash()
    test_pre_send_crash_reconciles_unlanded()
    test_landed_failure_fee_is_durable()
    test_crash_between_settle_and_record_stays_unlanded()
    test_fee_booking_crash_is_atomic()
    test_shared_entry_reserved_across_keys()
    test_stale_fx_adoption_validates_as_of_seal()
    print("ALL UNKNOWN-RECONCILIATION TESTS PASSED")
