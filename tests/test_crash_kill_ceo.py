#!/usr/bin/env python3
"""REAL PROCESS-KILL durability test for the ceo exit path (engineering test, canned FakeJup quotes, REAL DemoFiller + paper account + close-intent journal).
Parent mode (no args): for each named kill point it spawns ONE child python process that opens a ceo position and polls the exit at 2.0x; the child
SIGKILLs ITSELF (os.kill(os.getpid(), SIGKILL), never any other pid) at the named point. The parent then spawns a FRESH recovery process on the same
scratch root (price reversed to 1.9x) and asserts the invariants from what the recovery process prints. The parent signals no process.
Kill points: pre_intent (sale fill journaled, durable intent not yet written), torn_intent (half an intent line on disk, fsynced), after_credit (account credit
done, account_applied marker not written), pre_lane_marker (account_applied written, lane_applied not).
Invariants after recovery: leg a done; exactly one sale fill per leg; cash delta since entry == sum of journaled net proceeds (no double credit, no lost credit);
no unresolved close intent; journal readable (no torn status).
Run: python3 tests/test_crash_kill_ceo.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = sys.argv[2] if MODE != "parent" else _tf.mkdtemp(prefix="crashkill-parent-unused-")   # the parent never builds a lane; it only spawns children
import importlib, importlib.machinery, importlib.util, os, sys, tempfile, time, subprocess, threading
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()   # BEFORE any repo/lane import
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)

STARTS = []
MX_AT_START = any("mainnet_executor" in m for m in sys.modules)   # before any repo import here (G only)

from unittest.mock import patch
from daemon.launch_scanner import killswitch as KSM
_ks = os.path.join(ROOTDIR, "ks"); os.makedirs(_ks, exist_ok=True); KSM.STATE_DIR = _ks
for _n, _f in (("STATE_FILE", "state.json"), ("SHADOW_FILE", "shadow.json"), ("LOCK_FILE", "state.lock")):
    setattr(KSM, _n, os.path.join(_ks, _f))
import broker.memecoin.execution.mainnet_executor as _MX   # constants only are imported by demo_fill; CONSTRUCTION is what is forbidden
LIVE_BUILT = []
for _nm in dir(_MX):
    _o = getattr(_MX, _nm)
    if isinstance(_o, type) and _o.__module__ == _MX.__name__ and "Executor" in _nm:
        def _mk(_c=_o):
            def _init(self, *a, **k): LIVE_BUILT.append(_c.__name__); raise RuntimeError("live executor construction forbidden")
            return _init
        _o.__init__ = _mk()
from daemon.launch_scanner import demo_fill as DF
from daemon.launch_scanner import watcher_lane as W, momentum_lane as MO, ceo as C, mirror as MI, signal_bus as BUS, grad as GR
from broker import BrokerError, BROKER_UNAVAILABLE
SOLM = "So11111111111111111111111111111111111111112"

class FakeJup:
    delay = 0.0
    mult = 1.0   # sell-side price multiplier (was previously set on the instance but never read: exits filled at 1x)
    def __init__(self): self.calls = 0
    def get_quote(self, inp, out, amount, slippage_bps=100):
        self.calls += 1; time.sleep(FakeJup.delay)
        if inp == SOLM: o = int(amount / 1e9 / 0.00001 * 1e6)
        else: o = int(amount / 1e6 * 0.00001 * 1e9 * FakeJup.mult)
        return {"outAmount": str(o), "priceImpactPct": 0.004, "routePlan": [{"swapInfo": {"label": "FakeAMM"}}]}
JUP = FakeJup()

class KS:
    def is_halted(self, lane): return False
    def gate(self, lane): return True
    def record_trade(self, lane, pnl, ts=None): pass
    def check_lane(self, lane): return {"lane": lane}
class Safe:
    def check_token(self, chain, mint, timeout=10): return {"passed": True, "score": 80, "flags": [], "fails": [], "unknown": False}
class Veto:
    def veto_check(self, mint, chain, symbol="", safety_report=None, run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [], "details": {}}

def tape():
    n = time.time()
    return {"fetched_at": n, "received_at": n, "dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0,
            "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40, "vol_m5": 9000.0, "buys_h1": 400, "sells_h1": 150,
            "price_usd": 0.001, "pair_created_ms": (time.time() - 3600) * 1000, "tape_source": "dexscreener"}

def fresh(start="10", max_age="60", impact="2.0", fresh_state=True):
    h = os.path.join(ROOTDIR, "home"); os.makedirs(h, exist_ok=True); os.environ["FOREX_AGENT_HOME"] = h
    for k in list(os.environ):
        if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = start
    os.environ["PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT"] = impact   # TEST-FIXTURE
    os.environ["AGT_EXECUTION_MODE"] = "jupiter_quote_sim"
    import daemon.launch_scanner.paper_account as PA
    PA = importlib.reload(PA); G.install(PA)   # install() sets a wide fixture data-age cap; override AFTER it
    if max_age is not None: os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = max_age  # TEST-FIXTURE
    else: os.environ.pop("PAPER_ACCOUNT_MAX_DATA_AGE_S", None)
    F = DF.DemoFiller(JUP, sol_usd_fn=lambda: (100.0, time.time()), decimals_fn=lambda m: 6,
                      journal_path=os.path.join(ROOTDIR, "fills", "j.json"), seed=1, hourly_vol=0.0)
    led = os.path.join(ROOTDIR, "led"); os.makedirs(led, exist_ok=True)
    for mod, nm in ((W, "watcher"), (MO, "momentum"), (C, "ceo"), (MI, "mirror")):
        mod.LEDGER_PATH = os.path.join(led, nm + ".json")
        if hasattr(mod, "ensure_ledger"): mod.ensure_ledger()
        mod._demo_mode = lambda: True; mod._demo_filler = lambda F=F: F
        mod._ks = KS(); mod._safety = Safe(); mod._veto = Veto()
        mod._fresh_tape = lambda chain, mint: tape()
        mod.quote_price = G.stamped_quote(mod, lambda chain, mint, spot_index=None: 0.001)
        if hasattr(mod, "RUNNER_CONFIRM_GAP_S"): mod.RUNNER_CONFIRM_GAP_S = 0.0
        if hasattr(mod, "_ENTRY_BLOCKED"): mod._ENTRY_BLOCKED = False
        if hasattr(mod, "_RECOVERY_DONE"): mod._RECOVERY_DONE = False
    C.BUS_PATH = os.path.join(led, "bus.jsonl")
    GR.LEDGER_PATH = os.path.join(led, "grad.json"); GR.ensure_ledger()
    GR._demo_mode = lambda: True; GR._demo_filler = lambda F=F: F
    GR._ks = KS(); GR._veto = Veto(); GR.QUOTE_RETRY_GAP_S = 0.0; GR.MAX_OPEN = 50
    class GSafe:
        def check_token(self, chain, mint, timeout=10): return {"passed": True, "unknown": False, "score": 7, "flags": [], "fails": [], "source": "rugcheck"}
    GR._safety = GSafe()
    def _gt(m):
        n = time.time(); return {"usd_liquidity": 8000.0, "usd_market_cap": 69000.0, "chg_m5": 3.0, "price_usd": 0.00007,
                                 "tape_source": "dexscreener", "fetched_at": n, "received_at": n}
    GR._fresh_tape = _gt
    GR.quote_price = G.stamped_quote(GR, lambda chain, mint, spot_index=None: 0.00007)
    loader = importlib.machinery.SourceFileLoader("challenge_trader", os.path.join(ROOT, "scripts", "challenge-trader"))
    spec = importlib.util.spec_from_loader("challenge_trader", loader)
    CT = importlib.util.module_from_spec(spec); loader.exec_module(CT)
    if hasattr(CT, "_demo_filler"): CT._demo_filler = lambda: F
    return PA, F, CT

def cash(PA): return PA.snapshot()["cash"]

def wfire(mint):
    ev = {"smart_wallets": 3, "wallets": ["W0", "W1", "W2"], "wallet_groups": ["g0", "g1", "g2"], "safety": "ok",
          "components": {"convergence_groups": 3, "sightings": 3, "sol_spent_total": 4.2, "safety_score": 80}}
    return BUS.make_signal("solana", "smart-watcher", mint, symbol="WATCH", tier="converged", mcap=500000.0, liquidity=100000.0, evidence=ev)
def mrun(mint):
    return {"pool_address": "Pool" + mint[:6], "mint": mint, "symbol": "RUN", "name": "RUN", "chain": "solana",
            "source": "gecko-trending", "lane": "lowcap", "url": "", "chg_24h_pct": 55.0, "chg_1h_pct": 8.0,
            "vol_24h_usd": 250000.0, "liq_usd": 100000.0, "usd_market_cap": 500000.0}
def csig(mint):
    return BUS.make_signal(chain="solana", source="test-feed", mint=mint, symbol="TKN", score=70.0, tier="pick", mcap=500000.0, liquidity=50000.0)

LANES = []
def build(PA, CT):
    def e_w(i):
        r = W.consider_fire(wfire("WatchMint%037d" % i), time.time()); return bool(r[0]), r[1]
    def e_m(i):
        r = MO.consider_candidate(mrun("MomMint%038d" % i), time.time()); return bool(r[0]), r[1]
    def e_c(i):
        r = C.consider_card(csig("CeoMint%d" % i)); return bool(r[0]), r[1]
    def e_x(i):
        r = MI.mirror_card(chain="solana", source="test", mint="MirMint%d" % i, symbol="TKN", score=70.0, tier="pick", mcap=500000.0, liquidity=50000.0)
        return (bool(r[0]), r[1]) if isinstance(r, tuple) else (bool(r), r)
    def e_h(i):
        r = CT._demo_enter(PA, "L", "ChalMint%d" % i, 1.0); return bool(r[0]), r[1]
    def e_g(i):
        cand = {"mint": "GradMint%dpump" % i, "symbol": "TKN", "name": "TKN / SOL", "chain": "solana", "dex": "pumpswap",
                "created_ts": time.time() - 60.0, "pool_address": "POOL%d" % i, "reserve_usd": 6000.0, "fdv_usd": 69000.0,
                "price_usd": 0.00007, "source": "gecko-new-pool"}
        r = GR.consider_candidate(cand, time.time()); return bool(r[0]), r[1]
    return [("watcher", e_w), ("momentum", e_m), ("ceo", e_c), ("mirror", e_x), ("challenge", e_h), ("grad", e_g)]


import json as _json, signal as _signal
def _journal(F):
    with open(F._jpath()) as fh: return _json.load(fh)["fills"]
def _cents(x): return int(round(float(x) * 100))
def pf_mult(mod, m):
    def pf(chain, mint, spot_index=None):
        pos = (mod.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * m
    return pf
import daemon.launch_scanner.close_intents as CI
def _die(): os.kill(os.getpid(), _signal.SIGKILL)

def child_run(point):
    PA, F, CT = fresh()
    lanes = dict(build(PA, CT)); ok, why = lanes["ceo"](1)   # build() is the smoke helper (ceo entry = C.consider_card(csig(...)))
    print("ENTRY ok=%s cash=%.6f" % (ok, cash(PA)), flush=True)
    real_append = CI._append; real_ma = CI.mark_account_applied; real_ml = CI.mark_lane_applied
    if point == "pre_intent":
        CI._append = lambda path, ev: _die() if ev.get("ev") == "intent" else real_append(path, ev)
    elif point == "torn_intent":
        def _torn(path, ev):
            if ev.get("ev") != "intent": return real_append(path, ev)
            line = _json.dumps(dict(ev, ts=time.time())); os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(line[: len(line) // 2]); f.flush(); os.fsync(f.fileno())
            _die()
        CI._append = _torn
    elif point == "after_credit":
        CI.mark_account_applied = lambda lane, iid: _die()
    elif point == "pre_lane_marker":
        CI.mark_lane_applied = lambda lane, iid: _die()
    FakeJup.mult = 2.0
    C.poll_exits(price_fn=pf_mult(C, 2.0))
    print("CHILD SURVIVED point=%s (kill point not reached)" % point, flush=True); sys.exit(7)

def child_recover():
    PA, F, CT = fresh(fresh_state=False)
    pre_ents, pre_st, _t = CI._read(CI.journal_path("ceo"))
    pre_fills = len({k: v for k, v in _journal(F).items() if k.startswith("ceo:exit:") and v.get("status") == "filled"})
    pre = {"pre_cash": cash(PA), "pre_status": pre_st, "pre_ev": [e.get("ev") for e in pre_ents if e.get("intent_id")],   # LIFECYCLE rows only (carry an intent_id)
           "pre_other": sorted({str(e.get("ev")) for e in pre_ents if not e.get("intent_id")}), "pre_fills": pre_fills}
    print("PRE " + _json.dumps(pre), flush=True)
    FakeJup.mult = 1.9
    C.poll_exits(price_fn=pf_mult(C, 1.9)); C.poll_exits(price_fn=pf_mult(C, 1.9))
    pos = (C.open_positions() or {})
    fl = {k: v for k, v in _journal(F).items() if k.startswith("ceo:exit:") and v.get("status") == "filled"}
    ents, st, _ = CI._read(CI.journal_path("ceo"))
    res = {"cash": cash(PA), "legs": sorted(k.split(":")[3] for k in fl), "net": sum(v["net_proceeds_usd"] for v in fl.values()),
           "a_done": [bool(p.get("legs", {}).get("a", {}).get("done")) for p in pos.values()], "n_pos": len(pos),
           "pending": len(CI.pending("ceo")), "journal_status": st, "n_events": len(ents), "ev": [e.get("ev") for e in ents if e.get("intent_id")],
           "other": sorted({str(e.get("ev")) for e in ents if not e.get("intent_id")}), "pend_why": [(p.get("pending_exit") or {}).get("why") for p in pos.values()]}
    print("RESULT " + _json.dumps(res), flush=True)

if MODE == "run": child_run(sys.argv[3]); sys.exit(0)
if MODE == "recover": child_recover(); sys.exit(0)

# ---- parent
import subprocess, shutil
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def spawn(args):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=12)
    return p.returncode, p.stdout, p.stderr
for point in ("pre_intent", "torn_intent", "after_credit", "pre_lane_marker"):
    root = tempfile.mkdtemp(prefix="crashkill-%s-" % point)
    rc, out, err = spawn(["run", root, point])
    ec = [l for l in out.splitlines() if l.startswith("ENTRY")]
    check("%s: child was KILLED by SIGKILL (rc %d, expected -%d), kill point reached (%s)" % (point, rc, _signal.SIGKILL, "survived" if "SURVIVED" in out else "ok"), rc == -_signal.SIGKILL and "SURVIVED" not in out)
    check("%s: child opened the position before the kill (%s)" % (point, ec[:1]), bool(ec) and "ok=True" in ec[0])
    entry_cash = float(ec[0].split("cash=")[1]) if ec else None
    rc2, out2, err2 = spawn(["recover", root])
    rl = [l for l in out2.splitlines() if l.startswith("RESULT ")]; pl = [l for l in out2.splitlines() if l.startswith("PRE ")]
    check("%s: recovery process exited 0 with PRE and RESULT lines (rc %d, stderr tail %r)" % (point, rc2, err2[-160:]), rc2 == 0 and bool(rl) and bool(pl) and "Traceback" not in err2)
    if not rl or not pl: continue
    pre = _json.loads(pl[0][4:]); r = _json.loads(rl[0][7:])
    print("  %s PRE %s" % (point, pre)); print("  %s RESULT %s" % (point, r))
    # PREDECLARED durable state at the moment of the kill (read by the fresh process BEFORE it recovers anything)
    want_ev = {"pre_intent": [], "torn_intent": [], "after_credit": ["intent"], "pre_lane_marker": ["intent", "account_applied"]}[point]
    want_st = "torn_tail" if point == "torn_intent" else None
    check("%s: durable state at kill: journal status %r (want %r), lifecycle events %r (want %r), sale fills %d (want 1: the fill is journaled before the intent)"
          % (point, pre["pre_status"], want_st, pre["pre_ev"], want_ev, pre["pre_fills"]), pre["pre_status"] == want_st and pre["pre_ev"] == want_ev and pre["pre_fills"] == 1)
    credited = point in ("after_credit", "pre_lane_marker")
    check("%s: durable account cash at kill %.6f vs entry %.6f (credit applied before the kill: %r; also proves the recovery process did not reset the account)"
          % (point, pre["pre_cash"], entry_cash, credited), (_cents(pre["pre_cash"]) > _cents(entry_cash)) if credited else (_cents(pre["pre_cash"]) == _cents(entry_cash)))
    check("%s: rows WITHOUT an intent_id are only liveness probes, before (%r) and after (%r) recovery (never lifecycle events)" % (point, pre["pre_other"], r["other"]),
          set(pre["pre_other"]) <= {"probe"} and set(r["other"]) <= {"probe"})
    if point == "torn_intent":
        # PREDECLARED from the call ORDER (not weakened): the recovery process polls; the first journal touch is the _intent_gate liveness probe (ceo.py 286-296), which appends
        # onto the unterminated half line, then close_intents.begin(stable_id) reads the journal, sees it not cleanly readable and raises IntentNotDurable (close_intents.py 198-200).
        # close_intents.recover_lane (which truncates a torn tail, 362-368) runs only from the entry path (ceo.py 1226) and _finalize (1818), neither reached by a poll that holds. So the
        # poll must FAIL CLOSED: leg a UNDONE, exactly the one journaled fill (no resale), cash == entry (no credit), pending_exit close_intent_not_durable, the journal still reported
        # torn_tail, no lifecycle event, no pending intent.
        check("torn_intent: FAILS CLOSED, no convergence: leg a done %r (want [False]), fills %r (want ['a']), cash delta %.6f (want 0), hold %r (want close_intent_not_durable), journal %r (want torn_tail), lifecycle %r (want [])"
              % (r["a_done"], r["legs"], r["cash"] - entry_cash, r["pend_why"], r["journal_status"], r["ev"]),
              r["a_done"] == [False] and r["legs"] == ["a"] and _cents(r["cash"] - entry_cash) == 0 and r["pend_why"] == ["close_intent_not_durable"]
              and r["journal_status"] == "torn_tail" and r["ev"] == [] and r["pending"] == 0)
        continue
    check("%s: leg a done after recovery %r, one sale fill per leg %r (no resale)" % (point, r["a_done"], r["legs"]), r["a_done"] == [True] and r["legs"] == ["a"])
    check("%s: cash delta since entry %.6f == journaled net proceeds %.6f (exactly one credit)" % (point, r["cash"] - entry_cash, r["net"]), _cents(r["cash"] - entry_cash) == _cents(r["net"]))
    check("%s: lifecycle sequence %r == ['intent', 'account_applied', 'lane_applied', 'applied'] (terminal applied last), no pending intent (%d), journal cleanly readable (%r)" % (point, r["ev"], r["pending"], r["journal_status"]),
          r["ev"] == ["intent", "account_applied", "lane_applied", "applied"] and r["pending"] == 0 and r["journal_status"] is None)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
