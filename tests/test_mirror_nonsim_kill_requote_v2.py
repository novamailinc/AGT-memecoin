#!/usr/bin/env python3
"""v2 (bb49; bb46 pin preserved, never run, reviewer-blocked): fixes (1) the nonexistent mirror.ensure_ledger call (mirror _read() returns an empty ledger by itself) and (2) the import-time ledger LOCK path (mirror._LOCK_PATH was fixed at import under ~/.forex-agent; the test now points LEDGER_PATH and _LOCK_PATH at the scratch root before any entry), and adds a SEPARATE explicit entry check (entry opened, non-sim, position instance pinned, refusal reason printed) BEFORE the kill is attributed, so a mirror gate refusal is never mistaken for the recovery defect. Gates exercised by the REAL code with fixtures: runner gate (two confirming tape reads, gap 0), mandatory entry guard (limits fixture via _guard_fixture.install), cost gate, quote age, veto, fee venue (tape dex pump), slippage.\nMIRROR lane (degen_mirror, solana) variant: REAL PROCESS-KILL test, NON-DEMO (modelled-fill) exit. Same design as test_other_lanes_nonsim_kill_requote (watcher/momentum). Scenarios: S1 retry 2.3x (case under test) and S3 control retry 2.0x. Expected on the unfixed mirror: S1 red by assertion (mirror _close_leg_impl recomputes honest_exit per call), S3 green. NOT run.\nOriginal header: REAL PROCESS-KILL test, NON-DEMO (modelled-fill) exit for the WATCHER and MOMENTUM lanes: SIGKILL after the account credit, then a FRESH process retries at a
DIFFERENT price. Same structure as tests/test_ceo_nonsim_kill_requote.py (which covers the ceo lane and its fix).
ENGINEERING TEST of the REAL lane module, REAL shared paper account and REAL close-intent journal in a scratch root, canned price feed (not real-provider
acceptance). AGT_EXECUTION_MODE is unset in the test processes (modelled-fill path, the repo default), so the exit uses the live honest_exit path (_pa_settle).
Parent mode (no args) spawns, per lane and scenario, ONE child that opens a position and polls at 2.0x; the child SIGKILLs ITSELF (os.kill(os.getpid(), SIGKILL)) at
close_intents.mark_account_applied (labelled injection point, real process death). A FRESH recovery process on the same scratch root then polls at the retry multiple.
Scenarios per lane: S1 after_credit, retry 2.3x (the case under test); S3 CONTROL: after_credit, retry 2.0x (same price: proves harness and kill are real).
Contract: leg a done; the account credit booked ONCE (cash unchanged by the recovery); the lane leg realized equals the RECORDED net (recorded amount - fee), not a
new quote; lifecycle exactly [intent, account_applied, lane_applied, applied]; nothing pending; no hold.
EXPECTED on source without the recorded-op settle for these lanes: S1 RED by assertion (lane realized at the new quote), S3 GREEN. Not yet run. Uses the REAL killswitch module redirected to a scratch state dir (no stub).
Run: python3 -B tests/test_other_lanes_nonsim_kill_requote.py"""
if __name__ != "__main__":
    import pytest
    pytest.skip("script-style test", allow_module_level=True)
import sys, os
MODE = sys.argv[1] if len(sys.argv) > 1 else "parent"
import tempfile as _tf
ROOTDIR = sys.argv[2] if MODE != "parent" else _tf.mkdtemp(prefix="otherlane-parent-unused-")
LANE_ARG = sys.argv[3] if MODE != "parent" else None
import importlib, json as _json, signal as _signal, subprocess, time
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import tests._guard_fixture as G
G.block_network()   # BEFORE any repo/lane import
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner"))
os.environ.pop("AGT_EXECUTION_MODE", None)   # modelled-fill (non-demo) mode, test processes only

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
from daemon.launch_scanner import signal_bus as BUS
import daemon.launch_scanner.close_intents as CI
LANES = ("mirror",)
if LANE_ARG:
    from daemon.launch_scanner import mirror as _Mi
    L = {"mirror": _Mi}[LANE_ARG]
    LANE_NAME = "degen_mirror"   # pos["lane"] for solana (mirror.LANE_BY_CHAIN)
class _Safe:
    def check_token(self, chain, mint, timeout=10): return {"passed": True, "score": 80, "flags": [], "fails": [], "unknown": False}
class _Veto:
    def veto_check(self, mint, chain, symbol="", safety_report=None, run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [], "details": {}}
def _tape():
    n = time.time()
    return {"fetched_at": n, "received_at": n, "dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0, "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40,
            "vol_m5": 9000.0, "buys_h1": 400, "sells_h1": 150, "price_usd": 0.001, "pair_created_ms": (time.time() - 3600) * 1000, "tape_source": "dexscreener"}
MINT = "OtherLaneMint1"
def _enter(MINT=MINT):
    return L.mirror_card(chain="solana", source="test", mint=MINT, symbol="TKN", score=70.0, tier="pick", mcap=500_000.0, liquidity=50_000.0)
def fresh(start="10"):
    h = os.path.join(ROOTDIR, "home"); os.makedirs(h, exist_ok=True); os.environ["FOREX_AGENT_HOME"] = h
    for k in list(os.environ):
        if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = start
    import daemon.launch_scanner.paper_account as PA
    PA = importlib.reload(PA); G.install(PA)
    led = os.path.join(ROOTDIR, "led"); os.makedirs(led, exist_ok=True)
    L.LEDGER_PATH = os.path.join(led, LANE_ARG + ".json")
    L._LOCK_PATH = os.path.join(led, LANE_ARG + ".lock")   # module constant fixed at import: redirect to scratch
    L._fresh_tape = lambda chain, mint: _tape()
    L.quote_price = G.stamped_const(L, 0.01)
    L.RUNNER_CONFIRM_GAP_S = 0.0
    L._safety = _Safe()
    L._veto = _Veto()
    L._ENTRY_BLOCKED = False; L._RECOVERY_DONE = False
    return PA
def cash(PA): return PA.snapshot()["cash"]
def pf_mult(m):
    def pf(chain, mint, spot_index=None):
        pos = (L.open_positions() or {}).get(mint) or {}
        return float(pos.get("entry_price") or 0) * m
    return pf
def _cents(x): return int(round(float(x) * 100))
def _die():
    print("KILLHIT lane=%s at mark_account_applied" % LANE_ARG, flush=True)   # hit counter: the lane reached the injected point
    os.kill(os.getpid(), _signal.SIGKILL)

def child_run(mult):
    PA = fresh(); r = _enter()
    pos = (L.open_positions() or {}).get(MINT) or {}
    if not (r[0] and pos.get("pa_pos_id")):
        print("ENTRY-REFUSED lane=%s ok=%s reason=%r pos_present=%s pa_pos_id=%s ledger=%s" % (LANE_ARG, bool(r[0]), r[1], bool(pos), bool(pos.get("pa_pos_id")), L.LEDGER_PATH), flush=True)
        sys.exit(5)
    print("ENTRY lane=%s ok=%s cash=%.6f nonsim=%s pa_pos_id=%s why=%r" % (LANE_ARG, bool(r[0]), cash(PA), (pos.get("execution") or {}).get("source") != "jupiter_quote_sim", bool(pos.get("pa_pos_id")), r[1] if not r[0] else ""), flush=True)
    CI.mark_account_applied = lambda lane, iid: _die()   # labelled injection point: real SIGKILL of THIS process right after the durable account credit
    L.poll_exits(price_fn=pf_mult(float(mult)))
    print("CHILD SURVIVED (kill point not reached)", flush=True); sys.exit(7)

def child_recover(mult):
    PA = fresh()
    ents0, st0, _t = CI._read(CI.journal_path(LANE_NAME))
    i0 = [e for e in ents0 if e.get("ev") == "intent"]
    op0 = ((i0[0].get("settle_ops") or [{}])[0]) if i0 else {}
    pre = {"pre_cash": cash(PA), "pre_ev": [e.get("ev") for e in ents0 if e.get("intent_id")], "op_amount": op0.get("amount"), "op_fee": op0.get("fee"), "n_pre": len(i0)}
    print("PRE " + _json.dumps(pre), flush=True)
    for _ in range(3):
        L.poll_exits(price_fn=pf_mult(float(mult)))
    pos = (L.open_positions() or {}).get(MINT) or {}
    ents, st, _ = CI._read(CI.journal_path(LANE_NAME))
    leg = pos.get("legs", {}).get("a", {})
    res = {"cash": cash(PA), "a_done": bool(leg.get("done")), "a_realized": leg.get("realized_usd"), "journal_status": st,
           "ev": [e.get("ev") for e in ents if e.get("intent_id")], "n_intents": len([e for e in ents if e.get("ev") == "intent"]),
           "pending": len(CI.pending(LANE_NAME)), "pend_why": (pos.get("pending_exit") or {}).get("why")}
    print("RESULT " + _json.dumps(res), flush=True)

if MODE == "run": child_run(sys.argv[4]); sys.exit(0)
if MODE == "recover": child_recover(sys.argv[4]); sys.exit(0)

FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n, flush=True)
    if not c: FAILS.append(n)
def spawn(args):
    p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__)] + args, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr
FOUR = ["intent", "account_applied", "lane_applied", "applied"]
for lane in LANES:
    for tag, retry in (("S1-%s-retry-2.3x" % lane, "2.3"), ("S3-%s-control-retry-2.0x" % lane, "2.0")):
        root = _tf.mkdtemp(prefix="otherlane-%s-" % tag)
        rc, out, err = spawn(["run", root, lane, "2.0"])
        _el0 = [l for l in out.splitlines() if l.startswith("ENTRY ")]; _rf = [l for l in out.splitlines() if l.startswith("ENTRY-REFUSED")]
        _entry_ok = bool(_el0) and "ok=True" in _el0[0] and "nonsim=True" in _el0[0] and "pa_pos_id=True" in _el0[0] and not _rf and "Traceback" not in err
        check("%s: SETUP: the mirror entry OPENED on the non-sim path with a pinned position instance (%s%s; rc %d; stderr tail %r)" % (tag, _el0[:1], _rf[:1], rc, err[-200:]), _entry_ok)
        check("%s: child KILLED by SIGKILL (rc %d, want -9), KILLHIT reached" % (tag, rc), rc == -_signal.SIGKILL and "SURVIVED" not in out and "KILLHIT lane=%s" % lane in out)
        if not _entry_ok:
            continue   # setup failure: the recovery scenario is not attributable; the refusal reason is printed above
        rc2, out2, err2 = spawn(["recover", root, lane, retry])
        pl = [l for l in out2.splitlines() if l.startswith("PRE ")]; rl = [l for l in out2.splitlines() if l.startswith("RESULT ")]
        pre = _json.loads(pl[0][4:]) if pl else None; r = _json.loads(rl[0][7:]) if rl else None
        check("%s: recovery process exited 0 with PRE and RESULT (rc %d, stderr tail %r)" % (tag, rc2, err2[-160:]), rc2 == 0 and pre is not None and r is not None and "Traceback" not in err2)
        if pre is None or r is None:
            continue
        print("  %s PRE %s" % (tag, pre)); print("  %s RESULT %s" % (tag, r))
        _el = [l for l in out.splitlines() if l.startswith("ENTRY ")]
        entry_cash = float(_el[0].split("cash=")[1].split()[0]) if _el else 1e18
        rec_net = round(float(pre["op_amount"]) - float(pre["op_fee"] or 0.0), 6)
        check("%s: durable state before recovery: ONE intent, account_applied NOT yet marked (killed inside it), credit already in cash (%r > entry cash %r) (n %d, ev %r, recorded net %r)" % (tag, pre["pre_cash"], entry_cash, pre["n_pre"], pre["pre_ev"], rec_net),
              pre["n_pre"] == 1 and pre["pre_ev"] == ["intent"] and rec_net > 0 and pre["pre_cash"] > entry_cash + 1e-9)
        check("%s: retry: leg a done %r, no hold (pending_exit %r), nothing pending (%r)" % (tag, r["a_done"], r["pend_why"], r["pending"]), r["a_done"] is True and r["pend_why"] is None and r["pending"] == 0)
        check("%s: credit booked ONCE (cash %r vs before recovery %r) and the lane leg realized %r equals the RECORDED net %r (not a new quote)" % (tag, r["cash"], pre["pre_cash"], r["a_realized"], rec_net),
              _cents(r["cash"]) == _cents(pre["pre_cash"]) and r["a_realized"] is not None and abs(float(r["a_realized"]) - rec_net) < 1e-6)
        check("%s: lifecycle %r == %r, one intent (%d), journal readable (%r)" % (tag, r["ev"][:4], FOUR, r["n_intents"], r["journal_status"]),
              r["ev"][:4] == FOUR and r["n_intents"] == 1 and r["journal_status"] is None)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
