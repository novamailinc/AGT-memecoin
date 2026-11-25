#!/usr/bin/env python3
"""G1 REACHABILITY REPRODUCTION (source-only, not yet run): grad tranche divergence freeze, then a stop-path finalize.

Question: with a FROZEN tranche intent (op grad:leg:a, from the divergence path) on a position, does a later hard-stop poll still open a
"grad:close" intent beside it (no frozen-aware gate in grad)? This test OBSERVES the journal and states the invariant "no second
intent opens while a tranche intent is frozen". It is expected RED before any repair if the overlap is reachable; a GREEN result would
rule the overlap out. Real grad entry (consider_candidate), real _close_leg, real poll_exits; the divergence is produced by removing the
position from the account lane (same disclosed fixture as test_close_intents_grad GHOSTB1). Fault injection is labelled. No mocks of grad
logic; veto/safety/tape/quote are the same patched inputs as test_close_intents_grad. Modelled-fill mode (harness pops AGT_EXECUTION_MODE).
Run: python3 tests/test_grad_frozen_tranche_stop_g1.py
"""
import importlib
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests (mandatory-guard migration, disclosed)

TMPD = tempfile.mkdtemp(prefix="ci-grad-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="ci-grad-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"

import daemon.launch_scanner.grad as G
import daemon.launch_scanner.paper_account as PA
_gfb.set_limits_env(); _gfb.install_price_feed(at_cost=True)  # mandatory-guard migration (disclosed): gates on + TEST-FIXTURE limits, mechanics-only marks
import daemon.launch_scanner.close_intents as CI

G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class FakeKS:
    def __init__(self):
        self.halted = set()
        self.recorded = []

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        self.halted.add(lane)
        return {"lane": lane, "action": "HALTED"}


FAKE_KS = FakeKS()
SAFETY_OK = {"passed": True, "unknown": False, "score": 7,
             "flags": [], "fails": [], "source": "rugcheck"}
TAPE_OK = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
           "chg_m5": 3.0, "price_usd": 0.00007,
           "tape_source": "dexscreener"}


def _patch_all(price=0.00007):
    p1 = patch.object(G, "_veto")
    p2 = patch.object(G, "_safety")
    p3 = patch.object(G, "_ks", FAKE_KS)
    p4 = patch.object(G, "quote_price", return_value=price)
    p5 = patch.object(G, "_fresh_tape", return_value=dict(TAPE_OK))
    p6 = patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)
    m_veto, m_safety = p1.start(), p2.start()
    for p in (p3, p4, p5, p6):
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = dict(SAFETY_OK)
    return (p1, p2, p3, p4, p5, p6), m_veto, m_safety


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.00007):
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
        import shutil as _shl; _shl.rmtree(PA.ACCOUNT_DIR, ignore_errors=True)  # DISCLOSED TEST EDIT: reset the account with the lane ledger (see test_regime)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps, m_veto, m_safety = _patch_all(price=price)
    G.ensure_ledger()
    return ps, m_veto, m_safety


def _cand(mint="AAA111pump", age_s=60.0, dex="pumpswap", **kw):
    c = {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
         "chain": "solana", "dex": dex,
         "created_ts": time.time() - age_s,
         "pool_address": "POOL1", "reserve_usd": 6_000.0,
         "fdv_usd": 69_000.0, "price_usd": 0.00007,
         "source": "gecko-new-pool"}
    c.update(kw)
    return c


def _close_open(mint, mult=2.0):
    with G._ledger_lock():
        d = G._read()
        pos = d["positions"][mint]
        exit_price = pos["entry_price"] * mult
        G._close_leg(pos, "a", exit_price, "test")
        G._close_leg(pos, "b", exit_price, "test")
        G._close_leg(pos, "c", exit_price, "test")
        G._finalize(d, mint, pos, "test")
        G._write(d)


def _events():
    path = CI.journal_path("grad")
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def _snap(mint):
    return G._read()["positions"].get(mint)
def _ints():
    return [e for e in _events() if e.get("ev") == "intent"]
def _frozen_ids():
    return [e["intent_id"] for e in _events() if e.get("ev") == "frozen"]

M = "G1FRZ11pump"
ps, _mv, _ms = _fresh()
G._ENTRY_BLOCKED = False
opened, _w, _f = G.consider_candidate(_cand(mint=M))
check("G1 fixture: real grad entry opened", opened)
with PA._lock():
    led, _h, _dg = PA._load_locked()
    led["lanes"]["grad"]["open"].pop(M, None)   # DISCLOSED FIXTURE: the debited position is absent from the account (divergence)
    PA._save_locked(led)
with G._ledger_lock():
    d = G._read(); pos = d["positions"][M]
    G._close_leg(pos, "a", pos["entry_price"] * 2.5, "g1_leg_a")
    G._write(d)
p1 = _snap(M)
_i1 = _ints(); _f1 = _frozen_ids()
check("G1 setup: leg a divergence froze ONE tranche intent (intents %r, frozen %r, why %r)" % ([e["intent_id"] for e in _i1], _f1, (p1.get("pending_exit") or {}).get("why")),
      len(_i1) == 1 and len(_f1) == 1 and (p1.get("pending_exit") or {}).get("why") == "tranche_credit_divergence" and p1["legs"]["a"]["done"] is False)
_ent = float(p1["entry_price"])
_closes0 = len(G._load()["closes"])
# a hard-stop poll: price at 0.3x entry (HARD_STOP_MULT 0.50). poll_exits' external price_fn wins over every other source.
for _n in range(3):
    G.poll_exits(price_fn=lambda chain, mint, si=None: _ent * 0.3)
_iN = _ints(); _fN = _frozen_ids(); _pN = _snap(M)
_ops = [[o.get("op_id") for o in e.get("settle_ops") or []] for e in _iN]
print("  OBSERVED after 3 stop-priced polls: intents %r, frozen %r, position open %r, pending_exit %r, settlement_pending %r, closes %d"
      % (_ops, _fN, _pN is not None, (_pN or {}).get("pending_exit"), (_pN or {}).get("settlement_pending"), len(G._load()["closes"]) - _closes0))
_close_ops = [o for ops in _ops for o in ops if ":close:" in o]
check("G1 INVARIANT (expected RED before repair if reachable): no grad:close intent opens while the tranche intent is frozen (close ops %r)" % (_close_ops,), not _close_ops)
check("G1 INVARIANT: the position is not booked closed while a tranche intent is frozen and unresolved (position still open %r, new closes %d)" % (_pN is not None, len(G._load()["closes"]) - _closes0),
      _pN is not None and len(G._load()["closes"]) == _closes0)
check("G1 INVARIANT: the frozen tranche intent is neither applied nor superseded (events %r)" % ([e.get("ev") for e in _events() if e.get("intent_id") == _i1[0]["intent_id"]],),
      [e.get("ev") for e in _events() if e.get("intent_id") == _i1[0]["intent_id"]] == ["intent", "frozen"])
check("G1 entries stay blocked (flag %r)" % (G._ENTRY_BLOCKED,), G._ENTRY_BLOCKED is True)
_unpatch(ps)

if FAILS:
    print("FAILS: %d" % len(FAILS))
    sys.exit(1)
print("FAILS: 0")
