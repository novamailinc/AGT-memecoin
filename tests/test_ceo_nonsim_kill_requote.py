#!/usr/bin/env python3
"""REAL PROCESS-KILL test, NON-DEMO (modelled-fill) ceo exit: SIGKILL after the account credit, then a FRESH process retries at a DIFFERENT price.
ENGINEERING TEST of the REAL ceo lane, REAL shared paper account and REAL close-intent journal in a scratch root, canned price feed. The lane is NOT in demo
mode: AGT_EXECUTION_MODE is unset in the test processes (demo_fill.mode_enabled() is False; that is the production default when the variable is not set),
so the exit uses the live honest_exit price path (_pa_settle) and no DemoFiller. Test-scoped only: no config or flag is changed anywhere.
Parent mode (no args) spawns, per scenario, ONE child that opens a ceo position and polls at 2.0x; the child SIGKILLs ITSELF (os.kill(os.getpid(), SIGKILL), never
any other pid) at the named point (a labelled injection at close_intents.mark_account_applied / mark_lane_applied). A FRESH recovery process on the same scratch
root then polls at the scenario's retry multiple. The parent signals no process.
Scenarios: S1 after_credit, retry 2.3x (the case under test); S2 pre_lane_marker, retry 2.3x; S3 after_credit, retry 2.0x (CONTROL: identical price, proves harness
and kill are real and the retry price is the only variable).
Contract: leg a done after recovery; the account credit is booked ONCE; the retry settles the SAME amounts the durable intent recorded (net = recorded amount - recorded
fee), not a new quote: account cash delta since entry == recorded net == the lane leg realized; lifecycle exactly one intent (intent, account_applied, lane_applied,
applied); no pending intent; journal cleanly readable; no hold (pending_exit empty).
Run: python3 -B tests/test_ceo_nonsim_kill_requote.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = sys.argv[2] if MODE != "parent" else _tf.mkdtemp(prefix="nonsimkill-parent-unused-")
import importlib, json as _json, signal as _signal, subprocess, time
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()   # BEFORE any repo/lane import
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
os.environ.pop("AGT_EXECUTION_MODE", None)   # modelled-fill (non-demo) mode, test processes only

from unittest.mock import patch
from daemon.launch_scanner import killswitch as KSM
_ks = os.path.join(ROOTDIR, "ks"); os.makedirs(_ks, exist_ok=True); KSM.STATE_DIR = _ks
for _n, _f in (("STATE_FILE", "state.json"), ("SHADOW_FILE", "shadow.json"), ("LOCK_FILE", "state.lock")):
    setattr(KSM, _n, os.path.join(_ks, _f))
import broker.memecoin.execution.mainnet_executor as _MX   # constants only; CONSTRUCTION is forbidden
for _nm in dir(_MX):
    _o = getattr(_MX, _nm)
    if isinstance(_o, type) and _o.__module__ == _MX.__name__ and "Executor" in _nm:
        def _mk(_c=_o):
            def _init(self, *a, **k): raise RuntimeError("live executor construction forbidden")
            return _init
        _o.__init__ = _mk()
from daemon.launch_scanner import ceo as C, signal_bus as BUS
import daemon.launch_scanner.close_intents as CI

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

def fresh(start="10"):
    h = os.path.join(ROOTDIR, "home"); os.makedirs(h, exist_ok=True); os.environ["FOREX_AGENT_HOME"] = h
    for k in list(os.environ):
        if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = start
    import daemon.launch_scanner.paper_account as PA
    PA = importlib.reload(PA); G.install(PA)
    led = os.path.join(ROOTDIR, "led"); os.makedirs(led, exist_ok=True)
    C.LEDGER_PATH = os.path.join(led, "ceo.json"); C.ensure_ledger(); C.BUS_PATH = os.path.join(led, "bus.jsonl")
    C._ks = KS(); C._safety = Safe(); C._veto = Veto(); C._fresh_tape = lambda chain, mint: tape()
    C.quote_price = G.stamped_quote(C, lambda chain, mint, spot_index=None: 0.001)
    C.RUNNER_CONFIRM_GAP_S = 0.0; C._ENTRY_BLOCKED = False; C._RECOVERY_DONE = False
    return PA

def cash(PA): return PA.snapshot()["cash"]
def csig(mint):
    return BUS.make_signal(chain="solana", source="test-feed", mint=mint, symbol="TKN", score=70.0, tier="pick", mcap=500000.0, liquidity=50000.0)
def pf_mult(m):
    def pf(chain, mint, spot_index=None):
        pos = (C.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * m
    return pf
def _cents(x): return int(round(float(x) * 100))
def _die(): os.kill(os.getpid(), _signal.SIGKILL)
MINT = "CeoMint1"

def child_run(point):
    PA = fresh(); r = C.consider_card(csig(MINT))
    pos = (C.open_positions() or {}).get(MINT) or {}
    print("ENTRY ok=%s cash=%.6f nonsim=%s pa_pos_id=%s" % (bool(r[0]), cash(PA), (pos.get("execution") or {}).get("source") != "jupiter_quote_sim", bool(pos.get("pa_pos_id"))), flush=True)
    if point == "after_credit":
        CI.mark_account_applied = lambda lane, iid: _die()
    elif point == "pre_lane_marker":
        CI.mark_lane_applied = lambda lane, iid: _die()
    C.poll_exits(price_fn=pf_mult(2.0))
    print("CHILD SURVIVED point=%s (kill point not reached)" % point, flush=True); sys.exit(7)

def child_recover(mult):
    PA = fresh()
    ents0, st0, _t = CI._read(CI.journal_path("ceo"))
    intents0 = [e for e in ents0 if e.get("ev") == "intent"]
    op0 = ((intents0[0].get("settle_ops") or [{}])[0]) if intents0 else {}
    pre = {"pre_cash": cash(PA), "pre_status": st0, "pre_ev": [e.get("ev") for e in ents0 if e.get("intent_id")],
           "op_amount": op0.get("amount"), "op_fee": op0.get("fee"), "op_id": op0.get("op_id")}
    print("PRE " + _json.dumps(pre), flush=True)
    for _ in range(3):
        C.poll_exits(price_fn=pf_mult(float(mult)))
    pos = (C.open_positions() or {}).get(MINT) or {}
    ents, st, _ = CI._read(CI.journal_path("ceo"))
    res = {"cash": cash(PA), "a_done": bool(pos.get("legs", {}).get("a", {}).get("done")), "a_realized": pos.get("legs", {}).get("a", {}).get("realized_usd"),
           "a_exit_price": pos.get("legs", {}).get("a", {}).get("exit_price"), "entry_price": pos.get("entry_price"),
           "pending": len(CI.pending("ceo")), "journal_status": st, "ev": [e.get("ev") for e in ents if e.get("intent_id")],
           "n_intents": len([e for e in ents if e.get("ev") == "intent"]), "pend_why": (pos.get("pending_exit") or {}).get("why"),
           "n_pos": len(C.open_positions() or {})}
    print("RESULT " + _json.dumps(res), flush=True)

if MODE == "run": child_run(sys.argv[3]); sys.exit(0)
if MODE == "recover": child_recover(sys.argv[3]); sys.exit(0)

# ---- parent
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def spawn(args):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr
for tag, point, mult in (("S1", "after_credit", "2.3"), ("S2", "pre_lane_marker", "2.3"), ("S3-control", "after_credit", "2.0")):
    root = tempfile.mkdtemp(prefix="nonsimkill-%s-" % tag)
    rc, out, err = spawn(["run", root, point])
    ec = [l for l in out.splitlines() if l.startswith("ENTRY")]
    check("%s: child KILLED by SIGKILL (rc %d, want -%d), kill point reached" % (tag, rc, _signal.SIGKILL), rc == -_signal.SIGKILL and "SURVIVED" not in out)
    check("%s: entry opened on the NON-DEMO path (%s)" % (tag, ec[:1]), bool(ec) and "ok=True" in ec[0] and "nonsim=True" in ec[0] and "pa_pos_id=True" in ec[0])
    entry_cash = float(ec[0].split("cash=")[1].split()[0]) if ec else None
    rc2, out2, err2 = spawn(["recover", root, mult])
    rl = [l for l in out2.splitlines() if l.startswith("RESULT ")]; pl = [l for l in out2.splitlines() if l.startswith("PRE ")]
    check("%s: recovery process exited 0 with PRE and RESULT (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), rc2 == 0 and bool(rl) and bool(pl) and "Traceback" not in err2)
    if not rl or not pl: continue
    pre = _json.loads(pl[0][4:]); r = _json.loads(rl[0][7:])
    print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
    want_ev = ["intent"] if point == "after_credit" else ["intent", "account_applied"]
    check("%s: durable state at kill: journal status %r (want None), lifecycle %r (want %r), credit already in the account (cash %.6f > entry %.6f), recorded op amount %r fee %r"
          % (tag, pre["pre_status"], pre["pre_ev"], want_ev, pre["pre_cash"], entry_cash, pre["op_amount"], pre["op_fee"]),
          pre["pre_status"] is None and pre["pre_ev"] == want_ev and _cents(pre["pre_cash"]) > _cents(entry_cash) and pre["op_amount"] is not None)
    rec_net = round(float(pre["op_amount"]) - float(pre["op_fee"] or 0.0), 6)
    check("%s: retry at %sx: leg a done %r, pending_exit %r (no hold loop)" % (tag, mult, r["a_done"], r["pend_why"]), r["a_done"] is True and not r["pend_why"])
    check("%s: credit booked ONCE at the RECORDED amounts: cash delta since entry %.6f == recorded net %.6f, lane leg realized %r == recorded net (retry exit price %r vs entry %r)"
          % (tag, r["cash"] - entry_cash, rec_net, r["a_realized"], r["a_exit_price"], r["entry_price"]),
          _cents(r["cash"] - entry_cash) == _cents(rec_net) and r["a_realized"] is not None and abs(float(r["a_realized"]) - rec_net) < 1e-6)
    check("%s: lifecycle %r == one intent (%d) with ['intent', 'account_applied', 'lane_applied', 'applied'], pending %d, journal readable %r"
          % (tag, r["ev"], r["n_intents"], r["pending"], r["journal_status"]),
          r["ev"] == ["intent", "account_applied", "lane_applied", "applied"] and r["n_intents"] == 1 and r["pending"] == 0 and r["journal_status"] is None)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
