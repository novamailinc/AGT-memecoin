#!/usr/bin/env python3
"""Wiring tests: grad-engine lane VARIANTS <-> shared paper account.

grad_raw, grad_select, tranche, tranche_protect, raw_tight and moon
are not copies of grad.py — each loads grad.py as an isolated module
instance (importlib, file location) and overrides its lane policy.
Two things must hold for the shared account to work across them:

1. The engine instance (loaded OUTSIDE the package) can still
   resolve the shared paper_account module — a relative import would
   fail there and the fail-open guard would silently disable the
   gate for every variant lane.
2. op_ids are keyed by the LANE override, not a literal, so two
   lanes entering the SAME pool never collapse onto one op_id (the
   retry dedupe would hand the second lane the first lane's outcome
   without debiting).

Drives the REAL entry/close path of both the grad module and a
grad_raw.make_engine() instance against one shared account. Harness
mirrors tests/test_paper_account_grad.py. Run:
python3 tests/test_paper_account_engines.py
"""
import importlib
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests

TMPD = tempfile.mkdtemp(prefix="paper-eng-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="paper-eng-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"  # TEST FIXTURE (mandatory-guard edit): not owner values
os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "1000"
os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "90"
os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "100"
os.environ["PAPER_ACCOUNT_MAX_SIMULTANEOUS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_POSITIONS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
# Room for both stakes: grad $1 + grad_raw $25. The production
# default stays $10; sizing across lanes is an open user decision,
# not something this test prescribes.
os.environ["PAPER_ACCOUNT_START"] = "30"

import daemon.launch_scanner.grad as G
import daemon.launch_scanner.grad_raw as GR
import daemon.launch_scanner.paper_account as PA
_gfb.install_price_feed(at_cost=True)  # mandatory-guard migration (disclosed): mechanics-only marks via real pipeline

G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class FakeKS:
    def __init__(self):
        self.halted = set()

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        pass

    def check_lane(self, lane):
        return {"lane": lane}


TAPE_OK = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
           "chg_m5": 3.0, "price_usd": 0.00007,
           "tape_source": "dexscreener"}


def _patch_module(mod, price=0.00007):
    ps = [patch.object(mod, "_veto"),
          patch.object(mod, "_safety"),
          patch.object(mod, "_ks", FakeKS()),
          patch.object(mod, "quote_price", return_value=price),
          patch.object(mod, "_fresh_tape", return_value=dict(TAPE_OK)),
          patch.object(mod, "QUOTE_RETRY_GAP_S", 0.0)]
    m_veto, m_safety = ps[0].start(), ps[1].start()
    for p in ps[2:]:
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = {
        "passed": True, "unknown": False, "score": 7,
        "flags": [], "fails": [], "source": "rugcheck"}
    return ps


def _unpatch(ps):
    for p in ps:
        p.stop()


BORN = time.time() - 60.0


def _cand(mint):
    return {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
            "chain": "solana", "dex": "pumpswap",
            "created_ts": BORN,
            "pool_address": "POOL1", "reserve_usd": 6_000.0,
            "fdv_usd": 69_000.0, "price_usd": 0.00007,
            "source": "gecko-new-pool"}


def _close_open(mod, mint, mult=2.0):
    with mod._ledger_lock():
        d = mod._read()
        pos = d["positions"][mint]
        exit_price = pos["entry_price"] * mult
        mod._close_leg(pos, "a", exit_price, "test")
        mod._close_leg(pos, "b", exit_price, "test")
        mod._close_leg(pos, "c", exit_price, "test")
        mod._finalize(d, mint, pos, "test")
        mod._write(d)


# ---- the engine instance resolves the shared account --------------
eng = GR.make_engine()
check("engine instance LANE override is grad_raw", eng.LANE == "grad_raw")
check("engine instance resolves the shared paper account module",
      eng._paper_account() is PA)

# ---- two lanes, same mint, same pool-born ts: both debit ----------
ps_g = _patch_module(G)
ps_e = _patch_module(eng)
G.ensure_ledger()
eng.ensure_ledger()

opened_g, why_g, _f = G.consider_candidate(_cand("SAME11pump"))
snap = PA.snapshot()
check("grad enters and debits ($30 - $1)",
      opened_g and abs(snap["cash"] - 29.0) < 1e-9
      and snap["lanes"]["grad"]["open_count"] == 1)

opened_e, why_e, _f = eng.consider_candidate(_cand("SAME11pump"))
snap = PA.snapshot()
check("grad_raw enters the SAME mint and debits its own $25",
      opened_e and abs(snap["cash"] - 4.0) < 1e-9, )
check("both lanes hold their own open position on the mint",
      snap["lanes"]["grad"]["open_count"] == 1
      and snap["lanes"]["grad_raw"]["open_count"] == 1)
pos_g = snap["lanes"]["grad"]["positions"][0]
pos_e = snap["lanes"]["grad_raw"]["positions"][0]
check("distinct position instances per lane",
      pos_g["mint"] == pos_e["mint"] == "SAME11pump")

# Retry dedupe is per-lane: replaying grad's exact entry op_id must
# return grad's original outcome without touching grad_raw's debit.
before = PA.snapshot()["cash"]
qty_g = 1.0 / G._load()["positions"]["SAME11pump"]["entry_price"]
qty_e = 25.0 / eng._load()["positions"]["SAME11pump"]["entry_price"]
ok_r, why_r, pid_r = PA.try_debit(
    "grad", "SAME11pump", 1.0, qty_g,
    "grad:entry:SAME11pump:%d" % int(BORN * 1000))
check("replayed grad entry op_id: original outcome, no re-apply",
      ok_r and abs(PA.snapshot()["cash"] - before) < 1e-9)
ok_r2, why_r2, pid_r2 = PA.try_debit(
    "grad_raw", "SAME11pump", 25.0, qty_e,
    "grad_raw:entry:SAME11pump:%d" % int(BORN * 1000))
check("grad_raw's op_id is a SEPARATE recorded op (its own outcome)",
      ok_r2 and abs(PA.snapshot()["cash"] - before) < 1e-9)
# A replay with DIFFERENT amounts under a recorded op_id is a
# conflict (refused), not a silent retry.
ok_c, why_c, _ = PA.try_debit(
    "grad", "SAME11pump", 999.0, 999.0,
    "grad:entry:SAME11pump:%d" % int(BORN * 1000))
check("op_id replay with different amounts: conflict, refused",
      not ok_c and abs(PA.snapshot()["cash"] - before) < 1e-9)

# ---- both close under their own lane -------------------------------
_close_open(G, "SAME11pump", mult=2.0)
_close_open(eng, "SAME11pump", mult=2.0)
snap = PA.snapshot()
check("both lanes settled: nothing open, one close each",
      snap["lanes"]["grad"]["open_count"] == 0
      and snap["lanes"]["grad_raw"]["open_count"] == 0
      and snap["lanes"]["grad"]["n_closes"] == 1
      and snap["lanes"]["grad_raw"]["n_closes"] == 1)
check("both closes profitable and cash conserved "
      "(30 - 26 + proceeds)",
      snap["lanes"]["grad"]["realized_pnl"] > 0
      and snap["lanes"]["grad_raw"]["realized_pnl"] > 0
      and abs(snap["cash"]
              - (30.0 - 26.0
                 + snap["lanes"]["grad"]["realized_pnl"] + 1.0
                 + snap["lanes"]["grad_raw"]["realized_pnl"] + 25.0))
      < 1e-6)
_unpatch(ps_g)
_unpatch(ps_e)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL ENGINE-VARIANT WIRING TESTS PASSED")
