#!/usr/bin/env python3
"""Fail-closed close-intent durability for the ceo, watcher, momentum and mirror lanes (2026-11-25). ENGINEERING TEST of the real lane functions with
the real close_intents journal in a temp home; the shared account is replaced only by a SPY object that records credit calls (the point is that NO
account call happens), and the journal write failure is an INJECTED, named fault at close_intents._append.

Contract under test (per lane):
  1. journal not writable  -> _close_leg/_close_remaining return 0.0 BEFORE anything is sold or booked: leg not done, pos["realized_usd"]
     unchanged, no _pa_settle call, pending_exit why close_intent_not_durable (visible, retried next poll).
  2. storage back          -> the same call proceeds (the gate passes and the leg is executed through the lane's own path is NOT exercised here:
     only the gate/begin contract is; full lane exits are covered by their own suites).
  3. the narrow race (probe ok, real intent write fails): _intent_open returns False, ENTRIES BLOCKED, and _pa_settle makes NO account credit.
Run: python3 -B tests/test_close_intents_failclosed_lanes.py
"""
import importlib, os, sys, tempfile, time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="ci-failclosed-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
import daemon.launch_scanner.close_intents as CI

FAILS = 0
def check(name, cond):
    global FAILS
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS += 1

_real_append = CI._append
def _no_write(path, event):
    return False   # INJECTED journal write failure (named fault), every event kind
def _no_intent_only(path, event):
    return False if event.get("ev") == "intent" else _real_append(path, event)

class _SpyAccount:
    def __init__(self):
        self.credits = []; self.writedowns = []
    def credit(self, *a, **k):
        self.credits.append(a); return (True, "")
    def write_down(self, *a, **k):
        self.writedowns.append(a); return (True, "")
    def op_outcome(self, *a, **k): return None
    def pos_ever_existed(self, *a, **k): return True

def mkpos(mint):
    return {"mint": mint, "chain": "solana", "pa_pos_id": "lane/%s#1" % mint, "tokens": 300.0, "realized_usd": 0.0,
            "execution": {"source": "jupiter_quote_sim"}, "entry_snapshot": {},
            "legs": {k: {"done": False, "frac": 1.0 / 3.0} for k in ("a", "b", "c")}}

for modname in ("ceo", "watcher_lane", "momentum_lane", "mirror"):
    try:
        M = importlib.import_module("daemon.launch_scanner." + modname)
    except Exception as e:
        check("%s imports (%r)" % (modname, e), False); continue
    spy = _SpyAccount(); settle_calls = []
    pos = mkpos("FC" + modname[:3])
    if modname == "mirror":
        pos["lane"] = "mirror"
    lane = pos.get("lane") or getattr(M, "LANE", modname)
    with patch.object(M, "_paper_account", lambda: spy), patch.object(M, "_pa_settle", lambda *a, **k: settle_calls.append(a)), \
         patch.object(M, "_pa_write_down", lambda *a, **k: settle_calls.append(a)), patch.object(CI, "_append", _no_write):
        r1 = M._close_leg(pos, "a", 0.001, "hard_stop")
        r2 = M._close_remaining(pos, 0.001, "hard_stop")
    pe = pos.get("pending_exit") or {}
    check("%s: journal not writable -> _close_leg and _close_remaining return 0.0, nothing booked (leg a done=%r, realized %r), no settle/write-down call (%d), pending_exit why %r"
          % (modname, pos["legs"]["a"]["done"], pos["realized_usd"], len(settle_calls), pe.get("why")),
          r1 == 0.0 and r2 == 0.0 and not any(v["done"] for v in pos["legs"].values()) and pos["realized_usd"] == 0.0
          and not settle_calls and pe.get("why") == "close_intent_not_durable")
    check("%s: no account credit or write-down reached the account spy (%d/%d)" % (modname, len(spy.credits), len(spy.writedowns)), not spy.credits and not spy.writedowns)
    # the gate itself passes again once storage returns, and leaves no intent behind
    with patch.object(M, "_paper_account", lambda: spy):
        _g = M._intent_gate(mkpos("FCok" + modname[:3]), "a")
    check("%s: storage back -> the gate passes and the journal holds no intent (probe only)" % modname,
          _g is True and not [e for e in CI._read(CI.journal_path(lane))[0] if e.get("ev") == "intent"])
    # the narrow race: gate passes (probe ok) but the real intent write fails -> no account credit, entries blocked
    spy2 = _SpyAccount(); pos2 = mkpos("FR" + modname[:3]); pos2["pa_pos_id"] = "lane/%s#1" % pos2["mint"]
    M._ENTRY_BLOCKED = False
    with patch.object(M, "_paper_account", lambda: spy2), patch.object(CI, "_append", _no_intent_only):
        if modname == "mirror":
            M._pa_settle(pos2, 100.0, 1.0, 0.01, "leg:a")
        else:
            M._pa_settle(pos2, 100.0, 1.0, 0.01, "leg:a")
    check("%s: race (probe ok, intent write fails): NO account credit (%d) and ENTRIES BLOCKED (%r)" % (modname, len(spy2.credits), M._ENTRY_BLOCKED),
          not spy2.credits and M._ENTRY_BLOCKED is True)
    M._ENTRY_BLOCKED = False

print("fails: %d" % FAILS)
sys.exit(1 if FAILS else 0)
