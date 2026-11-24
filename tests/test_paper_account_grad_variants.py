#!/usr/bin/env python3
"""Per-variant wiring coverage: every grad loader variant <-> account.

test_paper_account_engines.py proves the mechanism for grad +
grad_raw (isolated engine instance, LANE-keyed op ids, shared-module
resolution outside the package). This file covers the REMAINING five
loaders individually: grad_select, tranche, tranche_protect,
raw_tight, moon. For each one it asserts the effective identity the
loader stamps (LANE, NOTIONAL_USD, MAX_OPEN), the import provenance
(engine loaded from grad.py by file location, isolated from the
package grad module), and drives the variant's REAL entry path with
PAPER_ACCOUNT_ENABLED=1, asserting the shared account debits the
VARIANT's notional under the VARIANT's lane, pins the position id,
and that the flag-off engine never touches the account.

Construction-inheritance is not accepted as coverage: every variant
runs its own entry through this file. Run:
python3 tests/test_paper_account_grad_variants.py
"""
import importlib
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TMPD = tempfile.mkdtemp(prefix="paper-var-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="paper-var-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
# Big enough for every variant's stake in one account. This is a
# wiring proof, not a sizing prescription: the production default
# stays 10 dollars and per-lane sizing is an open user decision.
os.environ["PAPER_ACCOUNT_START"] = "200"

import daemon.launch_scanner.grad as G
import daemon.launch_scanner.paper_account as PA

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class FakeKS:
    def is_halted(self, lane):
        return False

    def gate(self, lane):
        return True

    def record_trade(self, lane, pnl, ts=None):
        pass

    def check_lane(self, lane):
        return {"lane": lane}


TAPE_OK = {"usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
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


def _reset_account():
    os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
        prefix="paper-var-home-")
    importlib.reload(PA)


# (loader module name, expected LANE, expected NOTIONAL, MAX_OPEN)
VARIANTS = [
    ("grad_select", "grad_select", 25.00, 20),
    ("tranche", "tranche", 25.00, 20),
    ("tranche_protect", "tranche_protect", 25.00, 20),
    ("raw_tight", "raw_tight", 25.00, 150),
    ("moon_lane", "moon", 1.00, 150),
]

GRAD_PY = os.path.realpath(G.__file__)

for modname, lane, notional, max_open in VARIANTS:
    loader = importlib.import_module(
        "daemon.launch_scanner." + modname)
    check("%s: loader exposes the make_engine factory" % modname,
          callable(getattr(loader, "make_engine", None)))
    check("%s: loader module constants match its stamped policy"
          % modname,
          loader.LANE == lane
          and abs(loader.NOTIONAL_USD - notional) < 1e-9
          and loader.MAX_OPEN == max_open)

    eng = loader.make_engine()
    check("%s: engine is an isolated instance, not the package grad"
          % modname, eng is not G)
    check("%s: engine imported from grad.py by file location"
          % modname,
          os.path.realpath(eng.__file__) == GRAD_PY)
    check("%s: effective identity is the variant's "
          "(LANE=%s, notional=%.2f, max_open=%d)"
          % (modname, lane, notional, max_open),
          eng.LANE == lane
          and abs(eng.NOTIONAL_USD - notional) < 1e-9
          and eng.MAX_OPEN == max_open)
    check("%s: engine resolves the shared paper account module"
          % modname, eng._paper_account() is PA)

    # Drive the REAL entry path against a fresh shared account.
    _reset_account()
    eng.LEDGER_PATH = os.path.join(TMPD, "%s_ledger.json" % lane)
    if os.path.exists(eng.LEDGER_PATH):
        os.remove(eng.LEDGER_PATH)
    ps = _patch_module(eng)
    eng.ensure_ledger()
    mint = ("VAR" + lane + "AAAAAAAAAAAAAAAAAAAAAA")[:40] + "pump"
    opened, why, _f = eng.consider_candidate(_cand(mint))
    snap = PA.snapshot()
    check("%s: entry opened and debited the variant's notional "
          "(%.2f)" % (modname, notional),
          opened
          and abs(snap["cash"] - (200.0 - notional)) < 1e-9)
    lpos = eng._load()["positions"].get(mint, {})
    check("%s: position pinned under the variant's lane identity"
          % modname,
          snap["lanes"][lane]["open_count"] == 1
          and str(lpos.get("pa_pos_id", ""))
          .startswith("%s/%s#" % (lane, mint))
          and abs(snap["lanes"][lane]["positions"][0]["basis"]
                  - notional) < 1e-9)
    _unpatch(ps)

# Cross-variant isolation on one account: one shared mint entered by
# every variant must produce five distinct pinned positions.
_reset_account()
mint = "SAMESHARE" + "A" * 33 + "pump"
engines = []
for modname, lane, notional, max_open in VARIANTS:
    loader = importlib.import_module(
        "daemon.launch_scanner." + modname)
    eng = loader.make_engine()
    eng.LEDGER_PATH = os.path.join(TMPD, "%s_ledger.json" % lane)
    if os.path.exists(eng.LEDGER_PATH):
        os.remove(eng.LEDGER_PATH)
    engines.append((eng, lane, notional,
                    _patch_module(eng)))
    eng.ensure_ledger()
total = 0.0
for eng, lane, notional, _ps in engines:
    opened, why, _f = eng.consider_candidate(_cand(mint))
    check("shared mint: %s entered (notional %.2f)" % (lane, notional),
          opened)
    total += notional
snap = PA.snapshot()
check("shared mint: cash debited by every variant's own notional",
      abs(snap["cash"] - (200.0 - total)) < 1e-9)
check("shared mint: five distinct lanes hold their own position",
      sorted(l for l in snap["lanes"]
             if snap["lanes"][l]["open_count"] == 1)
      == sorted(lane for _m, lane, _n, _o in VARIANTS))
check("shared mint: no op-id collapse (five distinct pos_ids)",
      len({str(eng._load()["positions"][mint].get("pa_pos_id"))
           for eng, _l, _n, _p in engines}) == 5)
for _e, _l, _n, ps in engines:
    _unpatch(ps)

# Flag off: a variant engine never touches the account.
os.environ.pop("PAPER_ACCOUNT_ENABLED")
_reset_account()
loader = importlib.import_module("daemon.launch_scanner.moon_lane")
eng = loader.make_engine()
eng.LEDGER_PATH = os.path.join(TMPD, "moon_off_ledger.json")
if os.path.exists(eng.LEDGER_PATH):
    os.remove(eng.LEDGER_PATH)
ps = _patch_module(eng)
eng.ensure_ledger()
opened, why, _f = eng.consider_candidate(
    _cand("OFF" + "A" * 37 + "pump"))
snap = PA.snapshot()
check("flag off: variant entry leaves the shared account pristine",
      opened and snap["cash"] == 200.0
      and snap["lanes"].get("moon", {}).get("open_count", 0) == 0)
_unpatch(ps)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER-ACCOUNT GRAD-VARIANT COVERAGE TESTS PASSED")
