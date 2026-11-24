#!/usr/bin/env python3
"""grad_select recorder-coverage fix tests (2026-10-27).

Proves the curve-oldest-tx creator fallback actually runs in the
trader process (it never did: two independent, 100%-fatal bugs):
  * BUG 1: grad_select imported deployers flat, but deployers.py
    uses a relative import (from . import helius_ratelimit), so in
    the trader _deployers was None and _creator_from_oldest_tx
    returned None before any RPC call.
  * BUG 2: _creator_from_oldest_tx requested
    maxSupportedTransactionVersion 0, but modern pump.fun create
    txs are version 1, so public Solana refuses them (-32015).

The fix touches ONLY creator data resolution (import fallback +
v1 cap). The filter itself is untouched; these tests prove that:
  * _serial_check thresholds are unchanged (2 launches pass,
    3 reject, no index data rejects unknown_serial);
  * a missing birth + failed fallback still rejects
    unknown_serial (fail-closed preserved);
  * a missing birth + working fallback resolves the creator
    through _evaluate (no longer unknown_serial).

No network: all RPC is stubbed. Plain script — run directly,
not via pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
# Trader-identical path order: launch_scanner dir inserted first,
# then repo root inserted at 0 (so repo root ends up first, exactly
# as scripts/grad-select-trader does it).
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

import grad_select as sel
from daemon.launch_scanner import deployers as real_dep

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
# Real getTransaction responses always carry meta; the Batch 3
# meta.err gate fails closed without it, so the fixture carries
# a success meta (err None).
CANNED_TX = {"meta": {"err": None},
             "transaction": {"signatures":["SIG1"],"message": {"accountKeys": [
                 {"pubkey": "DEPLOYER1", "signer": True,
                  "writable": True},
                 {"pubkey": "OTHER1", "signer": False,
                  "writable": True}]}}}
REVERTED_TX = {"meta": {"err": {"InstructionError": [0, "Custom"]}},
               "transaction": {"message": {"accountKeys": [
                   {"pubkey": "DEPLOYER1", "signer": True,
                    "writable": True},
                   {"pubkey": "OTHER1", "signer": False,
                    "writable": True}]}}}


class FakeReserves:
    """Stub _reserves: records calls, serves canned curve + tx."""
    def __init__(self, tx=None):
        self.tx = tx
        self.calls = []

    def derive_curve_pda(self, mint):
        return "CURVEADDR"

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "getSignaturesForAddress":
            # Newest first; 2 sigs < page size => exhausted, exact.
            return [{"signature": "SIG2", "blockTime": NOW - 60},
                    {"signature": "SIG1", "blockTime": NOW - 120}]
        if method == "getTransaction":
            return self.tx
        return None


def gt_calls(fake, method):
    return [p for m, p in fake.calls if m == method]


# ---- 1. the import fix: _deployers resolves in-trader ---------------
check("import fix: _deployers is not None under trader imports",
      sel._deployers is not None)
check("import fix: _deployers has _first_signer",
      sel._deployers is not None
      and callable(getattr(sel._deployers, "_first_signer", None)))

# ---- 2. fallback resolves + uses the v1 cap --------------------------
saved_res, saved_dep = sel._reserves, sel._deployers
fake = FakeReserves(tx=CANNED_TX)
sel._reserves, sel._deployers = fake, real_dep
try:
    got = sel._creator_from_oldest_tx("SIG1")
    check("fallback resolves first signer from v1 tx",
          got == "DEPLOYER1")
    tx_params = gt_calls(fake, "getTransaction")
    check("fallback requests maxSupportedTransactionVersion 1",
          len(tx_params) == 1 and tx_params[0][1].get(
              "maxSupportedTransactionVersion") == 1)
    check("fallback uses jsonParsed encoding",
          len(tx_params) == 1
          and tx_params[0][1].get("encoding") == "jsonParsed")
finally:
    sel._reserves, sel._deployers = saved_res, saved_dep

# ---- 3. fail-closed preserved ---------------------------------------
fake_none = FakeReserves(tx=None)
sel._reserves, sel._deployers = fake_none, real_dep
try:
    check("fallback fail-closed: tx fetch None -> None",
          sel._creator_from_oldest_tx("SIG1") is None)
finally:
    sel._reserves, sel._deployers = saved_res, saved_dep
fake_reverted = FakeReserves(tx=REVERTED_TX)
sel._reserves, sel._deployers = fake_reverted, real_dep
try:
    check("fallback fail-closed: reverted tx -> None (Batch 3 gate)",
          sel._creator_from_oldest_tx("SIG1") is None)
finally:
    sel._reserves, sel._deployers = saved_res, saved_dep
sel._deployers = None
try:
    check("fallback fail-closed: _deployers None -> None",
          sel._creator_from_oldest_tx("SIG1") is None)
finally:
    sel._deployers = saved_dep
check("fallback fail-closed: empty sig -> None",
      sel._creator_from_oldest_tx("") is None)

# ---- 4. filter thresholds unchanged ----------------------------------
saved_index = {k: (dict(v) if isinstance(v, dict) else v)
               for k, v in sel._INDEX.items()}
try:
    sel._INDEX["available"] = True
    sel._INDEX["by_trader"] = {"CREATOR_A": [NOW, NOW - 100]}
    check("filter unchanged: 2 launches in 24h passes serial",
          sel._serial_check("CREATOR_A", NOW, {}) is None)
    sel._INDEX["by_trader"] = {"CREATOR_A": [NOW, NOW - 100, NOW - 200]}
    rj = sel._serial_check("CREATOR_A", NOW, {})
    check("filter unchanged: 3 launches rejects serial_deployer",
          rj is not None and rj.get("reason") == "serial_deployer")
    sel._INDEX["available"] = False
    rj = sel._serial_check("CREATOR_A", NOW, {})
    check("filter unchanged: no index data rejects unknown_serial",
          rj is not None and rj.get("reason") == "unknown_serial")
finally:
    sel._INDEX.clear()
    sel._INDEX.update(saved_index)

# ---- 5. _evaluate: missing birth + dead fallback -> unknown_serial --
#      missing birth + working fallback -> creator resolved (serial
#      passes; later stages may reject on other grounds, but the
#      reason must NOT be unknown_serial).
tmp_home = tempfile.mkdtemp(prefix="gs-cov-test-")
os.environ["FOREX_AGENT_HOME"] = tmp_home
# The copycat check runs before the chain phase (Batch 2
# free-first order), and the candidate's "TEST" ticker has 29
# real launches in the recorder tally — so without a stub the
# evaluation never reaches the serial fallback this section is
# testing. Stub the copycat DATA source (saturation off, canned
# DexScreener count), the way the price layer is stubbed
# elsewhere: the fallback behavior under test is unchanged.
_orig_sat5 = sel._saturation
_orig_c24_5 = sel._ds_ticker.count_24h if sel._ds_ticker else None
sel._saturation = None
if sel._ds_ticker is not None:
    sel._ds_ticker.count_24h = lambda symbol, now=None, self_mint=None: {
        "tally": 1, "others": 0, "top_mint": None,
        "self_seen": True, "pairs_seen": 1}
try:
    sel._INDEX["available"] = True
    sel._INDEX["by_mint"] = {}
    sel._INDEX["by_trader"] = {}
    sel._INDEX["by_mig"] = {}
    sel._INDEX["files"] = {}
    cand = {"mint": "TESTMINT0000000000000000000000000000000000",
            "symbol": "TEST", "source": "pumpportal-migration",
            "created_ts": NOW}
    fake_dead = FakeReserves(tx=None)
    sel._reserves, sel._deployers = fake_dead, real_dep
    v = sel.evaluate_candidate(None, cand, NOW)
    check("evaluate fail-closed: dead fallback -> unknown_serial",
          v.get("reason") == "unknown_serial")
    fake_live = FakeReserves(tx=CANNED_TX)
    sel._reserves, sel._deployers = fake_live, real_dep
    v = sel.evaluate_candidate(None, cand, NOW)
    check("evaluate fallback resolves: reason is not unknown_serial",
          v.get("reason") != "unknown_serial")
    check("evaluate fallback resolves: creator_source curve_oldest_tx",
          (v.get("values") or {}).get("creator_source")
          == "curve_oldest_tx")
    check("evaluate fallback resolves: creator is DEPLOYER1",
          (v.get("values") or {}).get("creator") == "DEPLOYER1")
finally:
    sel._saturation = _orig_sat5
    if sel._ds_ticker is not None and _orig_c24_5 is not None:
        sel._ds_ticker.count_24h = _orig_c24_5
    sel._reserves, sel._deployers = saved_res, saved_dep
    sel._INDEX.clear()
    sel._INDEX.update(saved_index)

print()
if fails:
    print("FAILURES: %d (%s)" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL GRAD-SELECT RECORDER-COVERAGE TESTS PASSED")
