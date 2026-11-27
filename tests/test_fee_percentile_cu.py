#!/usr/bin/env python3
"""Fee-tier percentile fallback + compute-unit sizing tests
(plain script — run with the repo venv python, never pytest):

    ~/workspace/Forex-agent/.venv/bin/python tests/test_fee_percentile_cu.py

Phase 2 item 5. Covers, with zero network:
  * fee_tiers() percentiles on known fees lists (exact values:
    min = 1st, mid = 50th, max = 95th percentile);
  * fees-list cache: hit within ~10s, expiry past it, force
    refresh, source failures NOT cached, ttl=0 disables caching;
  * no getPriorityFeeEstimate call anywhere in the policy path
    (source inspection + a strict mock RPC that only allows
    getRecentPrioritizationFees);
  * safe_compute_units math (estimated + 100k vs x1.2 max,
    200k default on no estimate, 1.4M cap);
  * sized_compute_units_from_sim on real/flat/failed/missing sims;
  * caps still enforced (absolute + 5%-of-stake) with fee_tiers
    present on skip output, and existing tier behavior unchanged
    (same choose_fee values as tests/test_phase2_execution.py);
  * executor wiring: dry-run execute_swap records compute_units
    from the simulation it already runs (default when the sim
    carries no unitsConsumed), and sized_compute_units never
    raises on garbage input.

Phase 1 is UNPROVEN (one post-fix close, Helius quota spent, zero
new entries). Nothing here claims otherwise.
"""

import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution import tx as tx_mod
from broker.memecoin.execution.fee_policy import (
    DEFAULT_COMPUTE_UNITS, PriorityFeePolicy, safe_compute_units,
    sized_compute_units_from_sim, units_consumed_from_sim)
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, SOL_MINT, associated_token_address)


def _b58encode(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = alphabet[r] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + (out or "")


class TestPercentileTiers(unittest.TestCase):
    def test_exact_values_ten_samples(self):
        fees = [1000 * i for i in range(1, 11)]  # 1000..10000
        p = PriorityFeePolicy(fee_source=lambda: list(fees))
        tiers = p.fee_tiers()
        self.assertEqual(tiers["min"], 1000)   # 1st percentile
        self.assertEqual(tiers["mid"], 5000)   # 50th percentile
        self.assertEqual(tiers["max"], 10000)  # 95th percentile
        self.assertEqual(tiers["samples"], 10)

    def test_exact_values_three_samples(self):
        p = PriorityFeePolicy(fee_source=lambda: [9000, 5000, 8000])
        tiers = p.fee_tiers()
        self.assertEqual(tiers, {"min": 5000, "mid": 8000,
                                 "max": 9000, "samples": 3})

    def test_single_sample_all_tiers_equal(self):
        p = PriorityFeePolicy(fee_source=lambda: [12000])
        self.assertEqual(p.fee_tiers(),
                         {"min": 12000, "mid": 12000, "max": 12000,
                          "samples": 1})

    def test_empty_source_falls_back_to_base_fee(self):
        p = PriorityFeePolicy(fee_source=lambda: [])
        self.assertEqual(p.fee_tiers(),
                         {"min": 10_000, "mid": 10_000, "max": 10_000,
                          "samples": 0})

    def test_rpc_rows_parsed_and_sorted(self):
        rows = [{"slot": 3, "prioritizationFee": 9000},
                {"slot": 1, "prioritizationFee": 1000},
                {"slot": 2, "prioritizationFee": 5000}]
        p = PriorityFeePolicy(fee_source=lambda: rows)
        tiers = p.fee_tiers()
        self.assertEqual((tiers["min"], tiers["mid"], tiers["max"]),
                         (1000, 5000, 9000))

    def test_choose_fee_carries_tiers(self):
        p = PriorityFeePolicy(fee_source=lambda: [50000] * 5)
        d = p.choose_fee(10_000_000)
        self.assertEqual(d["fee_tiers"],
                         {"min": 50000, "mid": 50000, "max": 50000,
                          "samples": 5})

    def test_existing_tier_behavior_unchanged(self):
        # Same vectors + same fees as test_phase2_execution.py.
        for fees, want_tier, want_fee in (
                ([5000, 8000, 9000], "calm", 10_000),
                ([50000] * 5, "normal", 50_000),
                ([200000] * 10, "hot", 200_000)):
            p = PriorityFeePolicy(fee_source=lambda f=fees: list(f))
            d = p.choose_fee(10_000_000)
            self.assertEqual(d["verdict"], "ok")
            self.assertEqual(d["tier"], want_tier)
            self.assertEqual(d["fee_lamports"], want_fee)


class TestFeesCache(unittest.TestCase):
    def test_hit_within_ttl(self):
        calls = []

        def _source():
            calls.append(1)
            return [5000, 8000, 9000]

        now = [1000.0]
        p = PriorityFeePolicy(fee_source=_source,
                              clock=lambda: now[0])
        self.assertEqual(p.choose_fee(10_000_000)["fee_lamports"],
                         10_000)
        self.assertEqual(p.choose_fee(10_000_000)["fee_lamports"],
                         10_000)
        self.assertEqual(p.sample_fees(), [5000, 8000, 9000])
        self.assertEqual(len(calls), 1)  # one fetch, rest cached

    def test_expiry_past_ttl_refetches(self):
        calls = []

        def _source():
            calls.append(1)
            return [5000, 8000, 9000]

        now = [1000.0]
        p = PriorityFeePolicy(fee_source=_source,
                              clock=lambda: now[0])
        p.sample_fees()
        now[0] = 1009.9  # inside the 10s window
        p.sample_fees()
        self.assertEqual(len(calls), 1)
        now[0] = 1010.0  # at/ past the 10s window
        p.sample_fees()
        self.assertEqual(len(calls), 2)

    def test_force_refresh_bypasses_cache(self):
        calls = []

        def _source():
            calls.append(1)
            return [5000]

        p = PriorityFeePolicy(fee_source=_source)
        p.sample_fees()
        p.sample_fees(force_refresh=True)
        self.assertEqual(len(calls), 2)
        p.invalidate_fee_cache()
        p.sample_fees()
        self.assertEqual(len(calls), 3)

    def test_source_failure_not_cached(self):
        attempts = []

        def _source():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("rpc down")
            return [7000, 9000]

        p = PriorityFeePolicy(fee_source=_source)
        self.assertEqual(p.sample_fees(), [])  # failure -> []
        self.assertEqual(p.sample_fees(), [7000, 9000])  # retry works
        self.assertEqual(len(attempts), 2)

    def test_cached_empty_list_refetched_after_ttl(self):
        returns = [[], [5000]]
        now = [1000.0]
        p = PriorityFeePolicy(fee_source=lambda: returns.pop(0),
                              clock=lambda: now[0])
        self.assertEqual(p.sample_fees(), [])
        self.assertEqual(p.sample_fees(), [])  # cached empty
        now[0] = 1011.0
        self.assertEqual(p.sample_fees(), [5000])

    def test_ttl_zero_disables_caching(self):
        calls = []

        def _source():
            calls.append(1)
            return [5000]

        p = PriorityFeePolicy(fee_source=_source,
                              fee_cache_ttl_seconds=0)
        p.sample_fees()
        p.sample_fees()
        self.assertEqual(len(calls), 2)


class TestNoEstimateCall(unittest.TestCase):
    def test_source_has_no_estimate_call(self):
        # The policy must never PASS getPriorityFeeEstimate to an
        # RPC call: the method name must not appear as a string
        # literal (an rpc.call argument) anywhere in the policy or
        # executor source. Bare prose mentions in docstrings (which
        # document the absence) do not count as calls.
        here = os.path.dirname(os.path.abspath(__file__))
        repo = os.path.dirname(here)
        for rel in ("broker/memecoin/execution/fee_policy.py",
                    "broker/memecoin/execution/mainnet_executor.py"):
            with open(os.path.join(repo, rel)) as f:
                src = f.read()
            self.assertNotIn('"getPriorityFeeEstimate"', src, rel)
            self.assertNotIn("'getPriorityFeeEstimate'", src, rel)
            self.assertNotIn("getPriorityFeeEstimate(", src, rel)

    def test_mock_rpc_only_sees_prioritization_fees(self):
        # NOTE: the mock records methods and checks them AFTER the
        # calls — asserting inside call() would be swallowed by the
        # policy source's fail-open handler (any exception -> []),
        # which would mask the failure as empty fees.
        class _StrictRPC:
            def __init__(self):
                self.calls = []

            def call(self, method, params):
                self.calls.append((method, params))
                return [{"slot": 1, "prioritizationFee": 12000}]

        rpc = _StrictRPC()
        p = PriorityFeePolicy(rpc=rpc)
        d = p.choose_fee(10_000_000)
        self.assertEqual(d["fee_lamports"], 12000)
        self.assertEqual(d["tier"], "normal")
        tiers = p.fee_tiers()
        self.assertEqual(tiers["mid"], 12000)
        self.assertTrue(rpc.calls)
        self.assertTrue(all(m == "getRecentPrioritizationFees"
                            for m, _ in rpc.calls))


class TestSafeComputeUnits(unittest.TestCase):
    def test_no_estimate_gives_default(self):
        self.assertEqual(safe_compute_units(None), 200_000)
        self.assertEqual(safe_compute_units(), 200_000)
        self.assertEqual(safe_compute_units(0), 200_000)
        self.assertEqual(safe_compute_units(-5), 200_000)
        self.assertEqual(safe_compute_units("bad"), 200_000)
        self.assertEqual(safe_compute_units(True), 200_000)

    def test_pad_add_wins_at_small_estimate(self):
        # max(100k + 100k, 100k * 1.2) = 200k
        self.assertEqual(safe_compute_units(100_000), 200_000)
        # max(50k + 100k, 50k * 1.2) = 150k
        self.assertEqual(safe_compute_units(50_000), 150_000)

    def test_pad_factor_wins_at_large_estimate(self):
        # max(1M + 100k, 1M * 1.2) = 1.2M
        self.assertEqual(safe_compute_units(1_000_000), 1_200_000)

    def test_both_pads_agree(self):
        # max(500k + 100k, 500k * 1.2) = 600k either way
        self.assertEqual(safe_compute_units(500_000), 600_000)
        # max(200k + 100k, 200k * 1.2) = 300k
        self.assertEqual(safe_compute_units(200_000), 300_000)

    def test_capped_at_per_tx_limit(self):
        # max(1.3M + 100k, 1.3M * 1.2) = 1.56M -> capped at 1.4M
        self.assertEqual(safe_compute_units(1_300_000), 1_400_000)
        self.assertEqual(safe_compute_units(5_000_000), 1_400_000)

    def test_custom_default(self):
        self.assertEqual(safe_compute_units(None, default=150_000),
                         150_000)
        # An estimate ignores the custom default.
        self.assertEqual(safe_compute_units(100_000, default=150_000),
                         200_000)
        self.assertEqual(safe_compute_units(None, default=0), 200_000)


class TestSimSizing(unittest.TestCase):
    def test_real_rpc_shape(self):
        sized = sized_compute_units_from_sim(
            {"value": {"unitsConsumed": 100_000}})
        self.assertEqual(sized, {"compute_units": 200_000,
                                 "source": "simulated",
                                 "units_consumed": 100_000})
        sized = sized_compute_units_from_sim(
            {"value": {"unitsConsumed": 1_000_000}})
        self.assertEqual(sized["compute_units"], 1_200_000)
        self.assertEqual(sized["source"], "simulated")

    def test_flat_shape(self):
        sized = sized_compute_units_from_sim({"unitsConsumed": 500_000})
        self.assertEqual(sized["compute_units"], 600_000)
        self.assertEqual(sized["source"], "simulated")

    def test_string_units_parsed(self):
        sized = sized_compute_units_from_sim(
            {"value": {"unitsConsumed": "750000"}})
        self.assertEqual(sized["compute_units"], 900_000)
        self.assertEqual(sized["source"], "simulated")

    def test_no_sim_falls_back_to_default(self):
        for sim in (None, "garbage", {}, {"err": None},
                    {"value": {}}, {"value": {"unitsConsumed": 0}},
                    {"value": {"unitsConsumed": "bad"}},
                    {"value": {"unitsConsumed": -10}}):
            sized = sized_compute_units_from_sim(sim)
            self.assertEqual(sized, {"compute_units": 200_000,
                                     "source": "default",
                                     "units_consumed": None}, sim)

    def test_failed_sim_falls_back_to_default(self):
        for sim in ({"err": "oops"},
                    {"value": {"err": "oops",
                               "unitsConsumed": 5000}},
                    {"err": "oops",
                     "value": {"unitsConsumed": 5000}}):
            sized = sized_compute_units_from_sim(sim)
            self.assertEqual(sized["source"], "default", sim)
            self.assertEqual(sized["compute_units"], 200_000)

    def test_units_consumed_extraction(self):
        self.assertEqual(
            units_consumed_from_sim(
                {"value": {"unitsConsumed": 42}}), 42)
        self.assertIsNone(units_consumed_from_sim(None))
        self.assertIsNone(units_consumed_from_sim({"err": "x"}))


class TestCapsStillEnforced(unittest.TestCase):
    def test_absolute_cap_skip_carries_tiers(self):
        p = PriorityFeePolicy(fee_source=lambda: [2_000_000] * 5)
        d = p.choose_fee(100_000_000)
        self.assertEqual(d["verdict"], "skip")
        self.assertEqual(d["reason"], "fee_over_cap")
        self.assertEqual(d["fee_tiers"]["mid"], 2_000_000)

    def test_frac_cap_skip(self):
        p = PriorityFeePolicy(fee_source=lambda: [100_000] * 5)
        d = p.choose_fee(1_000_000)  # 5% of stake is 50k < 100k fee
        self.assertEqual(d["verdict"], "skip")
        self.assertEqual(d["reason"], "fee_over_cap")

    def test_caps_unchanged_values(self):
        # The caps themselves were not moved by this change.
        from broker.memecoin.execution.fee_policy import (
            DEFAULT_ABSOLUTE_FEE_CAP_LAMPORTS, MAX_FEE_FRAC_OF_STAKE)
        self.assertEqual(DEFAULT_ABSOLUTE_FEE_CAP_LAMPORTS, 1_000_000)
        self.assertEqual(MAX_FEE_FRAC_OF_STAKE, 0.05)
        self.assertEqual(DEFAULT_COMPUTE_UNITS, 200_000)


# -- executor wiring (minimal harness, same shape as
#    tests/test_phase2_execution.py) -------------------------------

def _canned_swap_b64(kp, input_mint, output_mint):
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    accounts = [AccountMeta(kp.pubkey(), True, True),
                AccountMeta(Pubkey.from_string(input_mint), False, True),
                AccountMeta(Pubkey.from_string(output_mint), False, True)]
    ix = Instruction(
        Pubkey.from_string(tx_mod.JUPITER_AGGREGATOR_PROGRAM_ID),
        b"\x00\x01", accounts)
    msg = MessageV0.try_compile(
        kp.pubkey(), [ix], [], Hash.from_string(_b58encode(bytes(range(32)))))
    vtx = VersionedTransaction(msg, [kp])
    return base64.b64encode(bytes(vtx)).decode("ascii")


class _StaticProvider:
    def __init__(self, secret):
        self._secret = secret

    def get_key(self, chain_name):
        return self._secret

    def describe_source(self, chain_name):
        return "test-static"


class _FakeJupiter:
    def __init__(self, kp, quote=None):
        self._kp = kp
        self._quote = quote or {
            "outAmount": "1000000", "priceImpactPct": "0.005"}

    def get_quote(self, input_mint, output_mint, amount,
                  slippage_bps=100):
        q = dict(self._quote)
        q["inputMint"] = input_mint
        q["outputMint"] = output_mint
        q["inAmount"] = str(amount)
        return q

    def get_swap_transaction(self, quote, user_pubkey,
                             prioritization_fee_lamports=None):
        return {"swapTransaction": _canned_swap_b64(
            self._kp, quote["inputMint"], quote["outputMint"])}


class _FakeRPC:
    def __init__(self, sol_before=10_000_000_000,
                 token_ata=None, sim=None):
        self.sol_before = sol_before
        self.token_ata = token_ata
        self.sim = {"err": None} if sim is None else sim
        self.sent = []

    def get_health(self):
        return "ok"

    def get_slot(self):
        return 123456

    def get_balance(self, pubkey):
        return self.sol_before

    def get_token_account_balance(self, pubkey):
        if pubkey == self.token_ata:
            return {"amount": "0", "decimals": 6, "uiAmount": 0.0}
        raise BrokerError("rpc_error", "account not found",
                          detail={"reason": "rpc_error"})

    def simulate_transaction(self, b64_tx):
        return dict(self.sim)

    def send_transaction(self, b64_tx):
        self.sent.append(b64_tx)
        vtx = tx_mod.deserialize_versioned_tx(b64_tx)
        return tx_mod.versioned_signature_str(vtx)

    def get_signature_statuses(self, sigs):
        return [{"confirmationStatus": "confirmed", "err": None}
                for _ in sigs]


class TestExecutorWiring(unittest.TestCase):
    def setUp(self):
        from solders.keypair import Keypair
        self.kp = Keypair()
        secret = json.dumps(list(bytes(self.kp.to_bytes())))
        self.provider = _StaticProvider(secret)
        self.pubkey = str(self.kp.pubkey())
        self.tok = _b58encode(bytes(range(1, 33)))
        self.ata = associated_token_address(self.pubkey, self.tok)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _executor(self, rpc, **kw):
        ex = MainnetExecutor(key_provider=self.provider, rpc=rpc,
                             jupiter=_FakeJupiter(self.kp),
                             live_home=self.tmp.name, **kw)
        marker = os.path.join(self.tmp.name, "live", "deployment.json")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            json.dump({"machine": "test-vm", "armed": True}, f)
        ex.connect()
        return ex

    def test_dry_run_records_default_cu_without_sim_units(self):
        ex = self._executor(_FakeRPC(token_ata=self.ata))
        res = ex.execute_swap(SOL_MINT, self.tok, 1_000_000_000)
        self.assertTrue(res["simulated"])
        self.assertEqual(res["compute_units"], 200_000)
        self.assertEqual(res["compute_units_source"], "default")
        self.assertIsNone(res["units_consumed"])

    def test_dry_run_records_sized_cu_from_sim_units(self):
        sim = {"err": None, "value": {"unitsConsumed": 1_000_000}}
        ex = self._executor(_FakeRPC(token_ata=self.ata, sim=sim))
        res = ex.execute_swap(SOL_MINT, self.tok, 1_000_000_000)
        self.assertTrue(res["simulated"])
        self.assertEqual(res["compute_units"], 1_200_000)
        self.assertEqual(res["compute_units_source"], "simulated")
        self.assertEqual(res["units_consumed"], 1_000_000)

    def test_sized_compute_units_never_raises(self):
        for bad in (None, "junk", 123, {}, {"value": "junk"},
                    {"value": {"unitsConsumed": "bad"}},
                    {"err": "boom"}):
            sized = MainnetExecutor.sized_compute_units(bad)
            self.assertEqual(sized["compute_units"], 200_000, bad)
            self.assertEqual(sized["source"], "default", bad)
            self.assertIsNone(sized["units_consumed"], bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
