#!/usr/bin/env python3
"""bb88 FROZEN-OVERLAP HOLD for ceo, watcher_lane and momentum_lane (the lanes that share the reviewed gate). ENGINEERING TEST, SOURCE ONLY (never run):
the REAL lane close functions and the REAL close_intents journal in a temp home; the shared account is a SPY (records calls, no real account) and the sale
is a CANNED fill from a fake filler. The freeze is applied with close_intents.freeze after a declined account attempt (the same way recover_lane freezes).
It does NOT prove real-account behavior (see the BB86 momentum harness for the real-poll pattern); mirror and grad are NOT covered here."""
import hashlib, importlib, os, sys, tempfile
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="ci-frozenhold-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
import daemon.launch_scanner.close_intents as CI

FAILS = 0
def check(name, cond):
    global FAILS
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAILS += 1

class _Spy:
    def __init__(self, decline=False):
        self.credits = []; self.writedowns = []; self.decline = decline
    def credit(self, *a, **k):
        self.credits.append(a); return (False, "declined (test)") if self.decline else (True, "")
    def write_down(self, *a, **k):
        self.writedowns.append(a); return (False, "declined (test)") if self.decline else (True, "")
    def op_outcome(self, *a, **k): return None
    def pos_ever_existed(self, *a, **k): return True
    def max_price_impact_pct(self): return 50.0
class _Filler:
    def __init__(self): self.fills = {}; self.calls = []
    def sell(self, op_id, mint, qty, cap, *a, **k):
        self.calls.append(op_id)
        if op_id not in self.fills:
            self.fills[op_id] = {"status": "filled", "price_usd": 0.001, "gross_usd": 0.1, "fee_usd": 0.001, "execution_source": "canned_fixture",
                                 "signature": "fixture-sig", "provenance": {"fixture": True}}
            if k.get("meta"): self.fills[op_id]["intent"] = dict(k["meta"])
        return self.fills[op_id]
    def get_fill(self, op_id): return self.fills.get(op_id)
    def fills_by_prefix(self, prefix, suffix=""): return {k: dict(v) for k, v in self.fills.items() if k.startswith(prefix) and k.endswith(suffix)}
def mkpos(mint, gen=1):
    return {"mint": mint, "chain": "solana", "pa_pos_id": "lane/%s#%d" % (mint, gen), "tokens": 300.0, "realized_usd": 0.0,
            "execution": {"source": "jupiter_quote_sim"}, "entry_snapshot": {},
            "legs": {k: {"done": False, "frac": 1.0 / 3.0} for k in ("a", "b", "c")}}
def ctx(M, spy, ff):
    return (patch.object(M, "_paper_account", lambda: spy), patch.object(M, "_demo_filler", lambda: ff), patch.object(M, "_exit_fee_venue", lambda p: None))
def run(M, spy, ff, fn, *a):
    c1, c2, c3 = ctx(M, spy, ff)
    with c1, c2, c3: return fn(*a)
def jsha(lane):
    try: return hashlib.sha256(open(CI.journal_path(lane), "rb").read()).hexdigest()[:16]
    except OSError: return "none"
def ints(lane, mint): return [e for e in CI._read(CI.journal_path(lane))[0] if e.get("ev") == "intent" and e.get("mint") == mint]

for modname in ("ceo", "watcher_lane", "momentum_lane"):
    M = importlib.import_module("daemon.launch_scanner." + modname); lane = M.LANE; ff = _Filler(); T = modname[:3]
    # --- freeze a LEG-a obligation: the first attempt is declined, the intent stays open, the test freezes it
    ma = "FA" + T; pa = mkpos(ma)
    run(M, _Spy(decline=True), ff, M._close_leg, pa, "a", 0.001, "leg_a_2x")
    ia = ints(lane, ma); iid = ia[0]["intent_id"] if ia else None
    if iid: CI.freeze(lane, iid, "test: frozen after the account declined")
    # (1) the retry of the SAME leg is held under the real cause, no credit, no probe, no second intent
    pa2 = mkpos(ma); spy = _Spy(); sha0 = jsha(lane)
    r1 = run(M, spy, ff, M._close_leg, pa2, "a", 0.001, "leg_a_2x"); sha1 = jsha(lane)
    for _i in range(3): run(M, spy, ff, M._close_leg, pa2, "a", 0.001, "leg_a_2x")
    pe = pa2.get("pending_exit") or {}
    check("%s (1) frozen leg a: the retry is held (ret %r, leg done %r, why %r, detail %r), NO credit (%d), no second intent (%d), journal bytes unchanged over 4 held calls (%s -> %s -> %s)"
          % (modname, r1, pa2["legs"]["a"]["done"], pe.get("why"), [d.get("intent_id") for d in ((pe.get("detail") or {}).get("frozen_intents") or [])], len(spy.credits), len(ints(lane, ma)), sha0, sha1, jsha(lane)),
          bool(iid) and r1 == 0.0 and pa2["legs"]["a"]["done"] is False and pe.get("why") == "frozen_intent_unresolved" and [d.get("intent_id") for d in pe["detail"]["frozen_intents"]] == [iid]
          and not spy.credits and len(ints(lane, ma)) == 1 and sha0 == sha1 == jsha(lane))
    # (2) a REMAINING close spans the position: held by the frozen leg-a intent, no credit, no new intent
    spy = _Spy(); pr = mkpos(ma)
    rr = run(M, spy, ff, M._close_remaining, pr, 0.001, "hard_stop")
    check("%s (2) frozen leg a holds a REMAINING close (ret %r, no leg done %r, why %r, credits %d, intents %d)" % (modname, rr, [v["done"] for v in pr["legs"].values()], (pr.get("pending_exit") or {}).get("why"), len(spy.credits), len(ints(lane, ma))),
          rr == 0.0 and not any(v["done"] for v in pr["legs"].values()) and (pr.get("pending_exit") or {}).get("why") == "frozen_intent_unresolved" and not spy.credits and len(ints(lane, ma)) == 1)
    # (3) a WRITE-DOWN close spans the position too: no write_down account call, no new intent
    spy = _Spy(); pw = mkpos(ma)
    rw = run(M, spy, ff, M._close_remaining, pw, 0.0, "time_stop_24h_unquotable")
    check("%s (3) frozen leg a holds a WRITE-DOWN close (ret %r, why %r, write-downs %d, credits %d, intents %d)" % (modname, rw, (pw.get("pending_exit") or {}).get("why"), len(spy.writedowns), len(spy.credits), len(ints(lane, ma))),
          rw == 0.0 and (pw.get("pending_exit") or {}).get("why") == "frozen_intent_unresolved" and not spy.writedowns and not spy.credits and len(ints(lane, ma)) == 1)
    # (4) INDEPENDENCE: leg b of the same position is NOT held by the frozen leg a (positive control: it settles once)
    spy = _Spy(); pb = mkpos(ma)
    rb = run(M, spy, ff, M._close_leg, pb, "b", 0.001, "leg_b_3x")
    check("%s (4) independent leg b still settles (ret %r, leg b done %r, credits %d, pending why %r)" % (modname, rb, pb["legs"]["b"]["done"], len(spy.credits), (pb.get("pending_exit") or {}).get("why")),
          rb > 0 and pb["legs"]["b"]["done"] is True and len(spy.credits) == 1 and (pb.get("pending_exit") or {}).get("why") != "frozen_intent_unresolved")
    # (5) a NEW generation of the same mint (different pa_pos_id) is not held by the old generation's frozen intent
    spy = _Spy(); pg = mkpos(ma, gen=2)
    rg = run(M, spy, ff, M._close_leg, pg, "a", 0.001, "leg_a_2x")
    check("%s (5) a new position generation of the same mint is not held (ret %r, credits %d)" % (modname, rg, len(spy.credits)), rg > 0 and len(spy.credits) == 1)
    # --- freeze a WRITE-DOWN obligation (declined), then every close of that position is held
    mw = "FW" + T; pz = mkpos(mw)
    run(M, _Spy(decline=True), ff, M._close_remaining, pz, 0.0, "time_stop_24h_unquotable")
    iw = [e for e in ints(lane, mw) if any(":writedown:" in str(o.get("op_id")) for o in e.get("settle_ops") or [])]
    wid = iw[0]["intent_id"] if iw else None
    if wid: CI.freeze(lane, wid, "test: frozen write-down")
    spy = _Spy(); pl = mkpos(mw); sha0 = jsha(lane)
    rl = run(M, spy, ff, M._close_leg, pl, "c", 0.001, "leg_c_trail_30pct")
    check("%s (6) a frozen WRITE-DOWN holds a leg close (write-down intent %s, ret %r, why %r, credits %d, journal unchanged %s)" % (modname, bool(wid), rl, (pl.get("pending_exit") or {}).get("why"), len(spy.credits), sha0 == jsha(lane)),
          bool(wid) and rl == 0.0 and (pl.get("pending_exit") or {}).get("why") == "frozen_intent_unresolved" and not spy.credits and sha0 == jsha(lane))
    # --- CONTROL: a position with NO frozen intent is not held at all (so the holds above are caused by the freeze)
    spy = _Spy(); pc = mkpos("FN" + T)
    rc = run(M, spy, ff, M._close_leg, pc, "a", 0.001, "leg_a_2x")
    check("%s (7) CONTROL no frozen intent: the same close settles (ret %r, credits %d)" % (modname, rc, len(spy.credits)), rc > 0 and len(spy.credits) == 1)

print("fails: %d" % FAILS)
sys.exit(1 if FAILS else 0)
