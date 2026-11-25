#!/usr/bin/env python3
"""Fail-closed close-intent durability for the ceo, watcher, momentum and mirror lanes (2026-11-25). ENGINEERING TEST of the real lane functions with
the real close_intents journal in a temp home; the shared account is replaced only by a SPY object that records credit calls (the point is that NO
account call happens), and the journal write failure is an INJECTED, named fault at close_intents._append.

Contract under test (per lane):
  1. journal not writable  -> _close_leg/_close_remaining return 0.0 BEFORE anything is sold or booked: leg not done, pos["realized_usd"]
     unchanged, no _pa_settle call, pending_exit why close_intent_not_durable (visible, retried next poll).
  2. storage back          -> the same call proceeds (the gate passes and the leg is executed through the lane's own path is NOT exercised here:
     only the gate/begin contract is; full lane exits are covered by their own suites).
  3. the race (probe ok, real intent write fails AFTER the sale fill): the exit is rolled back in memory, no account credit, nothing persisted,
     pending_exit close_intent_not_durable; the retry re-reads the same journaled fill and settles once.
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

def mkpos(mint, lane=None):
    p = mkpos0(mint)
    if lane:
        p["lane"] = lane
    return p

def mkpos0(mint):
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
        _g = M._intent_gate(mkpos("FCok" + modname[:3], "mirror" if modname == "mirror" else None), "a")
    check("%s: storage back -> the gate passes and the journal holds no intent (probe only)" % modname,
          _g is True and not [e for e in CI._read(CI.journal_path(lane))[0] if e.get("ev") == "intent"])
    # THE RACE (probe ok, the real intent write fails AFTER the sale fill): the exit is rolled back in memory, NO account op, nothing persisted;
    # the retry re-reads the SAME journaled fill (deterministic exit op id, filler returns the existing fill) and only the intent write is retried.
    # The sale itself is a CANNED fill object returned by a fake filler (engineering fixture: the point is the journal/rollback/retry contract, not execution).
    class _FakeFiller:
        def __init__(self):
            self.fills = {}; self.calls = []
        def sell(self, op_id, mint, qty, cap, *a, **k):
            self.calls.append(op_id)
            if op_id not in self.fills:
                self.fills[op_id] = {"price_usd": 0.001, "gross_usd": 0.1, "fee_usd": 0.001, "execution_source": "canned_fixture",
                                     "signature": "fixture-sig", "provenance": {"fixture": True}}
            return self.fills[op_id]
    ff = _FakeFiller(); spy2 = _SpyAccount(); spy2.max_price_impact_pct = lambda: 50.0
    pos2 = mkpos("FR" + modname[:3], "mirror" if modname == "mirror" else None)
    if modname == "mirror":
        pos2["lane"] = "mirror"
    _alias_a = pos2["legs"]["a"]; _alias_legs = pos2["legs"]
    with patch.object(M, "_paper_account", lambda: spy2), patch.object(M, "_demo_filler", lambda: ff), patch.object(M, "_exit_fee_venue", lambda p: None), \
         patch.object(CI, "_append", _no_intent_only):
        r_fail = M._close_leg(pos2, "a", 0.001, "leg_a_2x")
    pe2 = pos2.get("pending_exit") or {}
    check("%s rollback keeps the ORIGINAL leg dict objects (aliases held by the poll loop stay valid and show not-done)" % modname,
          pos2["legs"]["a"] is _alias_a and _alias_a["done"] is False and pos2["legs"] is _alias_legs)
    check("%s race: exit rolled back in memory (leg a done=%r, realized %r), NO account credit (%d), pending_exit %r, sale called %d time(s)"
          % (modname, pos2["legs"]["a"]["done"], pos2["realized_usd"], len(spy2.credits), pe2.get("why"), len(ff.calls)),
          r_fail == 0.0 and pos2["legs"]["a"]["done"] is False and pos2["realized_usd"] == 0.0 and not spy2.credits
          and pe2.get("why") == "close_intent_not_durable" and len(ff.calls) == 1)
    check("%s race: nothing durable was written for the failed attempt (no intent event for this mint)" % modname,
          not [e for e in CI._read(CI.journal_path(lane))[0] if e.get("mint") == pos2["mint"]])
    with patch.object(M, "_paper_account", lambda: spy2), patch.object(M, "_demo_filler", lambda: ff), patch.object(M, "_exit_fee_venue", lambda p: None):
        r_ok = M._close_leg(pos2, "a", 0.001, "leg_a_2x")
    ev2 = [e for e in CI._read(CI.journal_path(lane))[0] if e.get("mint") == pos2["mint"]]
    check("%s retry after storage returns: SAME fill re-read (sale op ids %r, one distinct), leg done, ONE credit with the fill's gross (%r), intent journaled then account_applied (%r)"
          % (modname, len(ff.calls), [c[3] if len(c) > 3 else None for c in spy2.credits], [e.get("ev") for e in ev2]),
          r_ok > 0 and pos2["legs"]["a"]["done"] is True and len(set(ff.calls)) == 1 and len(ff.calls) == 2 and len(spy2.credits) == 1
          and abs(float(spy2.credits[0][2]) - 0.1) < 1e-12 and [e.get("ev") for e in ev2][:2] == ["intent", "account_applied"])
    # RESUME after a held exit: the position is persisted (JSON round trip = process restart), the price trigger is GONE (a reversal), and the poll-start
    # hook still resumes THE SAME exit (same deterministic sale op id, same journaled fill) and settles once; a pre-existing pending_exit field survives the hold.
    lm = "mirror" if modname == "mirror" else None
    ff5 = _FakeFiller(); spy5 = _SpyAccount(); spy5.max_price_impact_pct = lambda: 50.0
    pos5 = mkpos("FS" + modname[:3], lm); pos5["pending_exit"] = {"leg": "a", "why": "earlier_reason", "custom_field": 42}; pos5["peak_price"] = 7.0
    with patch.object(M, "_paper_account", lambda: spy5), patch.object(M, "_demo_filler", lambda: ff5), patch.object(M, "_exit_fee_venue", lambda p: None), \
         patch.object(CI, "_append", _no_intent_only):
        M._close_leg(pos5, "a", 0.001, "leg_a_2x")
    pe5 = pos5.get("pending_exit") or {}
    check("%s hold keeps the pre-existing pending fields (prior %r, peak_price %r) and records the resume target (%r)"
          % (modname, (pe5.get("prior") or {}).get("custom_field"), pos5.get("peak_price"), (pe5.get("resume") or {}).get("key")),
          (pe5.get("prior") or {}).get("custom_field") == 42 and pos5.get("peak_price") == 7.0 and (pe5.get("resume") or {}).get("fn") == "leg"
          and (pe5.get("resume") or {}).get("key") == "a" and pe5.get("why") == "close_intent_not_durable")
    import json as _json
    pos5r = _json.loads(_json.dumps(pos5))   # persisted and reloaded: a restart
    with patch.object(M, "_paper_account", lambda: spy5), patch.object(M, "_demo_filler", lambda: ff5), patch.object(M, "_exit_fee_venue", lambda p: None):
        _rr = M._resume_pending_exit(pos5r)   # NO price trigger is evaluated anywhere in this call
    check("%s restart + price reversal: resume returns %r, leg a done, ONE credit (%d), the SAME sale op id reused (%r), pending_exit cleared (%r)"
          % (modname, _rr, len(spy5.credits), sorted(set(ff5.calls)), pos5r.get("pending_exit")),
          _rr == "resumed" and pos5r["legs"]["a"]["done"] is True and len(spy5.credits) == 1 and len(set(ff5.calls)) == 1 and "pending_exit" not in pos5r)
    check("%s resume with nothing pending is a no-op (None)" % modname, M._resume_pending_exit(pos5r) is None)

    # an exception that is NOT IntentNotDurable still propagates (the rollback is narrow, never a blanket catch)
    pos3 = mkpos("FX" + modname[:3], "mirror" if modname == "mirror" else None)
    if modname == "mirror":
        pos3["lane"] = "mirror"
    class _Boom(Exception):
        pass
    def _boom_filler():
        raise _Boom("x")
    with patch.object(M, "_paper_account", lambda: spy2), patch.object(M, "_demo_filler", _boom_filler), patch.object(M, "_exit_fee_venue", lambda p: None):
        try:
            M._close_leg(pos3, "a", 0.001, "leg_a_2x"); _prop = False
        except _Boom:
            _prop = True
    check("%s: an unrelated exception still propagates out of the guarded close (rollback is narrow)" % modname, _prop)

    # _finalize must NEVER book a close for a position with a leg still pending (the 0.0 return of a held exit cannot drop the position)
    pos4 = mkpos("FF" + modname[:3], "mirror" if modname == "mirror" else None)
    pos4["symbol"] = "T"; pos4["source"] = "t"; pos4["entry_ts"] = 1.0; pos4["entry_price"] = 0.001; pos4["entry_features"] = {}
    d4 = {"positions": {pos4["mint"]: pos4}, "closes": [], "bankroll": 10.0, "state": "running"}
    wr = []
    with patch.object(M, "_write", lambda dd: wr.append(1)), patch.object(M, "_recover_close_intents", lambda: None):
        rec4 = M._finalize(d4, pos4["mint"], pos4, "ladder_complete")
    check("%s: _finalize with legs not done books nothing (rec %r, still open %r, closes %d, bankroll %r, pending_exit %r)"
          % (modname, rec4, pos4["mint"] in d4["positions"], len(d4["closes"]), d4["bankroll"], (pos4.get("pending_exit") or {}).get("why")),
          rec4 is None and pos4["mint"] in d4["positions"] and not d4["closes"] and d4["bankroll"] == 10.0 and (pos4.get("pending_exit") or {}).get("why"))
    cl4 = []
    M._append_close(cl4, rec4); M._append_close(cl4, {"x": 1})
    check("%s: _append_close skips None and keeps a real record" % modname, cl4 == [{"x": 1}])

print("fails: %d" % FAILS)
sys.exit(1 if FAILS else 0)
