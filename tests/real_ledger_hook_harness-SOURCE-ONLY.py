#!/usr/bin/env python3
"""SOURCE ONLY. NEVER RUN without main's explicit clearance for this exact blob. UNRUN, never executed, only ast.parse.
It spawns CHILD processes, some of which exit ABRUPTLY (os._exit(1)) and one of which starts two in-process daemon threads. The current run authorization does NOT permit any of that.

Purpose: runtime evidence for the R1 abort-hook / commit-refusal / entry-lock design on the REAL grad.py engine module (the one in this tree, loaded the same way as
crash_harness_grad_accounting-SOURCE-ONLY.py) with a temp HOME, a temp ledger and the shared $10 DEMO account only. No network (tests._guard_fixture), no executor, no signing,
no existing ledger. Evidence class: INSTRUMENTED ABRUPT PROCESS EXIT (os._exit) and in-process threads. NOT an external SIGKILL, NOT power loss, NOT the live trader process.

Scenarios (each child opens ONE real position through consider_candidate, then forces a poll abort by making _close_leg raise and _persist_poll_failure return False):
  K0 control, no kill: the abort is counted, the hook writes a durable poll_abort_live fault, commit_refusal for the lane refuses, the position stays open, nothing is debited or sold.
  K1 abrupt exit INSIDE the hook BEFORE the fault write (between the count and the fault). A FRESH process finds NO durable fault. This is the DOCUMENTED GAP: the check records what
     the fresh process does. A fresh process that does not refuse entries is reported as "GAP-K1 observed (as documented)", never as protection. If it refuses, reported as "GAP-K1 not observed".
  K2 abrupt exit just AFTER the hook returned: a FRESH process sees the durable fault, fault_active True, commit_refusal and scan_gate refuse.
  K3 two threads, real ledger flock: thread A runs poll_exits (abort forced, paused INSIDE the ledger lock by a barrier), thread B runs consider_candidate for a SECOND mint and blocks at
     the ledger lock; A releases; B must refuse at the commit gate: second mint NOT opened, no second debit, open positions == 1. K3m mutant control: commit_refusal patched to return None
     must let B open (proves the test can see the difference). Both threads are daemon; a join timeout aborts the child with os._exit(3).
  K4 entry-scan liveness: a child runs T.entry_scan with a scan_fn that exits abruptly; a FRESH process finds record running + free flock = interrupted_entry_scan fault durable, entries refused.
     K4b: same process, lock held on a second descriptor: _entry_check says live, no fault.
CLAIM LIMITS: the real engine, but the fixture feed, fill and safety stubs are the ones from the accounting harness. K3 proves ordering for ONE abort and ONE concurrent entry in one process; it is not
a general race proof. K1 is a GAP check, not a pass of protection. Not covered: two real processes both using the ledger, a kill during the ledger write itself, push/ExitPoller callbacks, exit latency.
"""
import json, os, signal, subprocess, sys, tempfile, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: FAILS.append(name)

BOOT = r'''
import importlib, os, sys, time, json
home = sys.argv[1]
sys.path.insert(0, os.environ["AGT_REPO_ROOT"]); sys.path.insert(0, os.path.join(os.environ["AGT_REPO_ROOT"], "daemon", "launch_scanner"))
import tests._guard_fixture as G; G.block_network()            # BEFORE any repo import
os.environ["FOREX_AGENT_HOME"] = home
for k in list(os.environ):
    if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = "10"
os.environ["PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT"] = "2.0"      # TEST-FIXTURE, same as the fixtures file
os.environ["AGT_EXECUTION_MODE"] = "jupiter_quote_sim"
import daemon.launch_scanner.paper_account as PA
G.install(PA); os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "60"   # TEST-FIXTURE, after install(), same order as fresh()
from daemon.launch_scanner import demo_fill as DF, grad as GR
SOLM = "So11111111111111111111111111111111111111112"
class FakeJup:
    mult = 1.0
    def get_quote(self, inp, out, amount, slippage_bps=100):
        if inp == SOLM: o = int(amount / 1e9 / 0.00001 * 1e6)
        else: o = int(amount / 1e6 * 0.00001 * 1e9 * FakeJup.mult)
        return {"outAmount": str(o), "priceImpactPct": 0.004, "routePlan": [{"swapInfo": {"label": "FakeAMM"}}]}
JUP = FakeJup()
os.makedirs(os.path.join(home, "j"), exist_ok=True); os.makedirs(os.path.join(home, "led"), exist_ok=True)
FILL_PATH = os.path.join(home, "j", "j.json")
F = DF.DemoFiller(JUP, sol_usd_fn=lambda: (100.0, time.time()), decimals_fn=lambda m: 6, journal_path=FILL_PATH, seed=1, hourly_vol=0.0)
class KS:
    def is_halted(self, lane): return False
    def gate(self, lane): return True
    def record_trade(self, lane, pnl, ts=None): pass
    def check_lane(self, lane): return {"lane": lane}
class Veto:
    def veto_check(self, mint, chain, symbol="", safety_report=None, run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [], "details": {}}
class GSafe:
    def check_token(self, chain, mint, timeout=10): return {"passed": True, "unknown": False, "score": 7, "flags": [], "fails": [], "source": "rugcheck"}
GR.LEDGER_PATH = os.path.join(home, "led", "grad.json")
if not os.path.exists(GR.LEDGER_PATH): GR.ensure_ledger()
GR._demo_mode = lambda: True; GR._demo_filler = lambda F=F: F
GR._ks = KS(); GR._veto = Veto(); GR.QUOTE_RETRY_GAP_S = 0.0; GR.MAX_OPEN = 50; GR._safety = GSafe()
def _gt(m):
    n = time.time(); return {"usd_liquidity": 8000.0, "usd_market_cap": 69000.0, "chg_m5": 3.0, "price_usd": 0.00007, "tape_source": "dexscreener", "fetched_at": n, "received_at": n}
GR._fresh_tape = _gt
GR.quote_price = G.stamped_quote(GR, lambda chain, mint, spot_index=None: 0.00007)
def gpf(mult): return lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * mult
def sell_ab():
    FakeJup.mult = 3.0
    try: GR.poll_exits(price_fn=gpf(3.0))
    finally: FakeJup.mult = 1.0
def finish():
    FakeJup.mult = 2.0
    try: GR.poll_exits(price_fn=gpf(2.0))
    finally: FakeJup.mult = 1.0
def raw_ledger():
    return json.load(open(GR.LEDGER_PATH))
def raw_fills():
    try: return json.load(open(FILL_PATH)).get("fills", {})
    except FileNotFoundError: return {}
def raw_credits():
    ents, status, _ = PA._journal_read()
    return [e for e in ents if e.get("event") == "credit"], status
'''

PRE2 = BOOT + r"""
os.environ["AGT_GRAD_CYCLE_ENABLED"] = "1"
for _k in ("AGT_GRAD_CYCLE_LANES",): os.environ.pop(_k, None)
from daemon import grad_cycle as GC, grad_trader_cycle as T
def _reach(tag):
    # POSITIVE reached-target evidence: written and fsynced by the child immediately before its deliberate exit, so an unrelated rc=1 cannot pass
    fh = open(os.path.join(home, "reached." + tag), "w"); fh.write(tag); fh.flush(); os.fsync(fh.fileno()); fh.close()
def mkcand(n):
    return {"mint": "GradMint%dpump" % n, "symbol": "TK%d" % n, "name": "TK%d / SOL" % n, "chain": "solana", "dex": "pumpswap", "created_ts": time.time() - 60.0,
            "pool_address": "POOL%d" % n, "reserve_usd": 6000.0, "fdv_usd": 69000.0, "price_usd": 0.00007, "source": "gecko-new-pool"}
def open_first():
    r = GR.consider_candidate(mkcand(1), time.time())
    return r
def force_abort():
    def _boom(*a, **k): raise RuntimeError("forced close_leg failure")
    GR._close_leg = _boom
    GR._persist_poll_failure = lambda d, m, p, e: False
def poll_abort():
    FakeJup.mult = 3.0
    try: GR.poll_exits(price_fn=gpf(3.0))
    finally: FakeJup.mult = 1.0
def _sha():
    import hashlib
    return hashlib.sha256(open(GR.LEDGER_PATH, "rb").read()).hexdigest()
def save_baseline():
    # SCENARIO-LOCAL durable pre-kill baseline: state of THIS home right before the abort is forced, fsynced, so a fresh process compares against what this scenario actually had
    b = dict(state(), ledger_sha=_sha()); fh = open(os.path.join(home, "baseline.json"), "w"); json.dump(b, fh); fh.flush(); os.fsync(fh.fileno()); fh.close(); return b
def state():
    L = raw_ledger()
    return {"n_pos": len(L.get("positions", {})), "mints": sorted(L.get("positions", {})), "aborts": GR._POLL_ABORTS["count"], "cash": PA.snapshot()["cash"],
            "fills": len(raw_fills()), "credits": len(raw_credits()[0])}
"""
CHILD_K0 = PRE2 + r"""
r1 = open_first(); T.register(GR); force_abort(); s0 = state(); poll_abort(); s1 = state()
fr = GC._read_fault(GR)
print(json.dumps({"opened": bool(r1[0]), "s0": s0, "s1": s1, "fault_reason": (fr or {}).get("reason"), "fault_reasons": [e.get("reason") for e in (fr or {}).get("reasons", [])],
                  "refusal": T.commit_refusal(GR.LANE), "ps_fills_credits": [s1["fills"] - s0["fills"], s1["credits"] - s0["credits"]]}))
"""
CHILD_K1 = PRE2 + r"""
r1 = open_first(); T.register(GR); force_abort(); save_baseline()
def die_before_write(mint, err): _reach("K1_before_fault_write"); os._exit(1)           # ABRUPT exit between the count and the fault write (fsynced reached-site sentinel first)
GR._ABORT_HOOK = die_before_write
poll_abort(); os._exit(99)                                  # reached only if the kill site was NOT hit (expected rc is 1, from the hook)
"""
CHILD_K2 = PRE2 + r"""
r1 = open_first(); T.register(GR); force_abort(); save_baseline()
_h = GR._ABORT_HOOK
def die_after(mint, err):
    _h(mint, err); _reach("K2_after_hook"); os._exit(1)                           # ABRUPT exit right after the hook returned (sentinel written after the hook, before the exit)
GR._ABORT_HOOK = die_after
poll_abort(); os._exit(99)                                 # reached only if the kill site was NOT hit
"""

CHILD_W1 = PRE2 + r"""
r1 = open_first(); T.register(GR); force_abort(); save_baseline()
def die_in_restore(d, m, p, e): _reach("W1_in_restore"); os._exit(1)            # ABRUPT exit inside the restore I/O: after the write-ahead marker, before the abort is counted
GR._persist_poll_failure = die_in_restore
poll_abort(); os._exit(0)
"""
CHILD_W2 = PRE2 + r"""
r1 = open_first(); T.register(GR); force_abort(); save_baseline()
if sys.argv[2] == "mutant": GR._FAILURE_BEGIN_HOOK = None      # sensitivity control: no write-ahead marker
def die_before_write(mint, err): _reach("W2_before_fault_write_" + sys.argv[2]); os._exit(1)           # the K1 kill point: between the count and the fault write
GR._ABORT_HOOK = die_before_write
poll_abort(); os._exit(0)
"""
CHILD_W3 = PRE2 + r"""
r1 = open_first(); T.register(GR); force_abort(); save_baseline()
GC._record_fault = lambda *a, **k: False               # the abort hook cannot make the fault durable (failed hook)
poll_abort()
print(json.dumps({"refusal_inproc": T.commit_refusal(GR.LANE), "hook_failed": dict(T._HOOK_FAILED), "marker": os.path.exists(GR.LEDGER_PATH + ".poll-inflight.json")}), flush=True)
_reach("W3_after_abort_failed_hook"); os._exit(1)
"""
CHILD_W4 = PRE2 + r"""
r1 = open_first(); T.register(GR)
def _boom(*a, **k): raise RuntimeError("forced close_leg failure")
GR._close_leg = _boom
GR._persist_poll_failure = lambda d, m, p, e: True      # restore reported successful: failure recovered, no abort
poll_abort()
print(json.dumps({"marker": os.path.exists(GR.LEDGER_PATH + ".poll-inflight.json"), "aborts": GR._POLL_ABORTS["count"], "refusal": T.commit_refusal(GR.LANE), "fault": bool(GC._read_fault(GR))}))
"""
CHILD_W5 = PRE2 + r"""
r1 = open_first(); T.register(GR)
open(GR.LEDGER_PATH + ".poll-inflight.json", "w").write(json.dumps({"token": "t", "since": time.time(), "mint": "x", "error": "e", "n": 1}))
s0 = state(); refusal = T.commit_refusal(GR.LANE); cl = None
FakeJup.mult = 3.0
try: cl = GR.poll_exits(price_fn=gpf(3.0))
finally: FakeJup.mult = 1.0
s1 = state()
print(json.dumps({"refusal": refusal, "closes_or_progress": [s1["fills"] - s0["fills"], s1["credits"] - s0["credits"]], "ncl": len(cl or []), "aborts": GR._POLL_ABORTS["count"]}))
"""

CHILD_W6 = PRE2 + r"""
r1 = open_first(); T.register(GR)
def _boom(*a, **k): raise RuntimeError("forced close_leg failure")
GR._close_leg = _boom
GC.mark_inflight = lambda *a, **k: None                 # the write-ahead marker cannot be written (BEGIN failure)
GR._persist_poll_failure = lambda d, m, p, e: True      # the restore itself succeeds: no abort, no fault
poll_abort()
sr = T.startup_report(GR)
print(json.dumps({"marker": os.path.exists(GR.LEDGER_PATH + ".poll-inflight.json"), "aborts": GR._POLL_ABORTS["count"], "refusal": T.commit_refusal(GR.LANE), "begin_failed": sr.get("begin_failed"), "fault": bool(GC._read_fault(GR))}))
"""
VERIFY = PRE2 + r"""
_raw_sha = _sha()        # raw ledger digest BEFORE any snapshot/heal or register in this fresh process
T.register(GR)           # a FRESH process: the abort counter and baseline start at 0
try: _base = json.load(open(os.path.join(home, "baseline.json")))
except Exception as e: _base = {"error": repr(e)}
fr = GC._read_fault(GR); act, why = GC.fault_active(GR)
try: sg = GC.scan_gate(GR)
except Exception as e: sg = ("error", repr(e))
print(json.dumps({"fault_file": bool(fr), "fault_reason": (fr or {}).get("reason"), "fault_active": bool(act), "why": why, "refusal": T.commit_refusal(GR.LANE),
                  "scan_gate": list(sg), "state": state(), "baseline": _base, "ledger_sha_raw": _raw_sha, "entry_state": T._entry_state(GR)}, default=str))
"""
CHILD_K3 = PRE2 + r"""
import threading
mutant = sys.argv[2] == "mutant"
r1 = open_first(); T.register(GR); force_abort()
reached, go = threading.Event(), threading.Event()
_orig_pf = GR._persist_poll_failure
def paused(d, m, p, e):
    reached.set(); go.wait(10); return False             # A is INSIDE the Phase-3 ledger lock here
GR._persist_poll_failure = paused
if mutant: T.commit_refusal = lambda lane: None
res = {}
def A():
    try: poll_abort(); res["a"] = "done"
    except BaseException as e: res["a"] = "raised:%r" % (e,)
def B():
    try: res["b"] = list(GR.consider_candidate(mkcand(2), time.time()))[:2]
    except BaseException as e: res["b"] = "raised:%r" % (e,)
ta = threading.Thread(target=A, daemon=True); ta.start()
if not reached.wait(10): print("FAIL abort never reached the pause"); sys.stdout.flush(); os._exit(3)
tb = threading.Thread(target=B, daemon=True); tb.start(); time.sleep(0.5); b_blocked = tb.is_alive()
go.set(); ta.join(15); tb.join(15)
if ta.is_alive() or tb.is_alive(): print("FAIL thread still alive after join timeout"); sys.stdout.flush(); os._exit(3)
print(json.dumps({"mutant": mutant, "b_blocked_midway": b_blocked, "res": res, "state": state(), "fault_reason": (GC._read_fault(GR) or {}).get("reason")}, default=str))
"""
CHILD_K4 = PRE2 + r"""
r1 = open_first(); T.register(GR)
def dying_scan(): _reach("K4_mid_entry_scan"); os._exit(1)                            # ABRUPT exit mid entry scan, entry lock held, record running (sentinel first)
T.entry_scan(GR, dying_scan); os._exit(99)             # reached only if the scan callback was NOT invoked
"""
CHILD_K4B = PRE2 + r"""
import fcntl
r1 = open_first(); T.register(GR)
T.entry_scan(GR, lambda: {"opened": 0})                  # writes a done record, releases the lock
jp, lp = T._entry_paths(GR); j = json.load(open(jp)); j["status"] = "running"; json.dump(j, open(jp, "w"))
lk = open(lp, "w"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
ok, why = T._entry_check(GR); act = GC.fault_active(GR)[0]
print(json.dumps({"ok": ok, "why": why, "fault_active": bool(act)}))
"""
BUDGET_S = 100.0
_T0 = time.time(); _STOP = {"why": None}
def _remaining(): return BUDGET_S - (time.time() - _T0)
def py(code, *args, timeout=60):
    rem = _remaining()
    if rem <= 1.0:
        _STOP["why"] = "budget_exhausted"; raise TimeoutError("harness budget exhausted")
    env = dict(os.environ, AGT_REPO_ROOT=REPO)
    p = subprocess.Popen([sys.executable, "-c", code] + [str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    try:
        out, err = p.communicate(timeout=min(timeout, rem))
    except BaseException:
        try: os.killpg(p.pid, signal.SIGKILL)
        except Exception: pass
        try: p.wait(timeout=5)
        except Exception: pass
        _STOP["why"] = "child_timeout_or_error"; raise
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)
def last_json(cp):
    for ln in reversed((cp.stdout or "").strip().splitlines()):
        try: return json.loads(ln)
        except Exception: continue
    return None
def run(code, home, *args):
    cp = py(code, home, *args); return cp, last_json(cp)
def newhome(): return tempfile.mkdtemp(prefix="hookh_")

K0_BASE = {}
def same_as_base(v):
    b, st = v.get("baseline") or {}, v["state"]
    if "n_pos" not in b: return False
    return st["n_pos"] == b["n_pos"] and st["mints"] == b["mints"] and st["fills"] == b["fills"] and st["credits"] == b["credits"] and abs(float(st["cash"]) - float(b["cash"])) < 1e-9
def s_K0():
    h = newhome(); cp, j = run(CHILD_K0, h)
    check("K0 child ran (rc=%s)" % cp.returncode, cp.returncode == 0 and j is not None)
    if not j: return
    K0_BASE.update(fills=j["s0"]["fills"], credits=j["s0"]["credits"], n_pos=j["s0"]["n_pos"])      # post-entry, pre-abort: the entry buy itself journals a fill
    check("K0 position opened first; the abort was counted once; no sale, fill or credit by the aborted poll", j["opened"] and j["s1"]["aborts"] == 1 and j["ps_fills_credits"] == [0, 0] and j["s1"]["n_pos"] == 1)
    check("K0 hook wrote a durable poll_abort_live fault and commit_refusal refuses (%s)" % j["refusal"], "poll_abort_live" in (j["fault_reasons"] or []) and bool(j["refusal"]))
def s_K1():
    h = newhome(); cp, j = run(CHILD_K1, h); check("K1 child exited abruptly (rc=%s, expected 1) AND the fsynced kill-site sentinel is present (%s)" % (cp.returncode, _reached(h, "K1_before_fault_write")), cp.returncode == 1 and _reached(h, "K1_before_fault_write"))
    cv, v = run(VERIFY, h)
    check("K1 verify ran", cv.returncode == 0 and v is not None)
    if not v: return
    check("K1 conservation vs THIS scenario's durable pre-kill baseline (now %s, baseline %s)" % (v["state"], v["baseline"]), same_as_base(v))
    print("INFO K1 raw ledger bytes identical to pre-kill baseline: %s" % (v["ledger_sha_raw"] == (v["baseline"] or {}).get("ledger_sha")))
    gap = (not v["fault_file"]) and v["refusal"] is None
    check("GAP-K1 %s: kill between count and fault write leaves NO durable fault and the fresh process does not refuse entries (documented gap, NOT protection)" % ("observed" if gap else "NOT observed (fault=%s refusal=%s)" % (v["fault_file"], v["refusal"])), True)
def s_K2():
    h = newhome(); cp, j = run(CHILD_K2, h); check("K2 child exited abruptly (rc=%s, expected 1) AND the fsynced kill-site sentinel is present (%s)" % (cp.returncode, _reached(h, "K2_after_hook")), cp.returncode == 1 and _reached(h, "K2_after_hook"))
    cv, v = run(VERIFY, h)
    check("K2 verify ran", cv.returncode == 0 and v is not None)
    if not v: return
    check("K2 conservation vs THIS scenario's durable pre-kill baseline (now %s, baseline %s)" % (v["state"], v["baseline"]), same_as_base(v))
    print("INFO K2 raw ledger bytes identical to pre-kill baseline: %s" % (v["ledger_sha_raw"] == (v["baseline"] or {}).get("ledger_sha")))
    check("K2 fresh process: durable poll_abort_live fault, fault_active, commit_refusal and scan_gate refuse", v["fault_file"] and v["fault_reason"] == "poll_abort_live" and v["fault_active"] and v["refusal"] and v["scan_gate"][0] is False)
def s_K3(kind):
    h = newhome(); cp, j = run(CHILD_K3, h, kind)
    check("K3%s child ran (rc=%s)" % ("m" if kind == "mutant" else "", cp.returncode), cp.returncode == 0 and j is not None)
    if not j: return
    st = j["state"]
    if kind == "real":
        check("K3 B blocked at the ledger lock while A held it, then refused: second mint NOT opened, one open position, one abort counted, B reason=%s" % (j["res"].get("b"),),
              j["b_blocked_midway"] and isinstance(j["res"].get("b"), list) and j["res"]["b"][0] is False and st["n_pos"] == 1 and st["aborts"] == 1 and j["fault_reason"] == "poll_abort_live")
    else:
        check("K3m MUTANT (commit_refusal patched to None): B DOES open the second mint (test sensitivity control; expected to differ from K3)", isinstance(j["res"].get("b"), list) and j["res"]["b"][0] is True and st["n_pos"] == 2)
def s_K4():
    h = newhome(); cp, j = run(CHILD_K4, h); check("K4 child exited abruptly mid entry scan (rc=%s, expected 1) AND the fsynced kill-site sentinel is present (%s)" % (cp.returncode, _reached(h, "K4_mid_entry_scan")), cp.returncode == 1 and _reached(h, "K4_mid_entry_scan"))
    cv, v = run(VERIFY_K4, h)
    check("K4 verify ran", cv.returncode == 0 and v is not None)
    if v: check("K4 fresh process: running record + free lock -> interrupted_entry_scan durable, fault active, entries refused (%s)" % v, v["check"] == [False, "interrupted_entry_scan"] and v["fault_active"] and v["entry_ok"] is False)
    cp2, j2 = run(CHILD_K4B, newhome())
    check("K4b live holder (lock held on a second descriptor): not a fault", cp2.returncode == 0 and j2 is not None and j2["ok"] is True and j2["fault_active"] is False)
VERIFY_K4 = PRE2 + r"""
T.register(GR); ck = T._entry_check(GR); ran, why, _r = T.entry_scan(GR, lambda: {"opened": 0})
print(json.dumps({"check": list(ck), "fault_active": bool(GC.fault_active(GR)[0]), "entry_ok": ran, "why": why}))
"""

def _reached(h, tag): return os.path.exists(os.path.join(h, "reached." + tag))
def s_W():
    h = newhome(); cp, j = run(CHILD_W1, h); check("W1 child exited abruptly INSIDE the restore: rc=%s (expected 1) AND reached-target sentinel present=%s" % (cp.returncode, _reached(h, "W1_in_restore")), cp.returncode == 1 and _reached(h, "W1_in_restore"))
    cv, v = run(VERIFY, h)
    check("W1 fresh process: write-ahead marker alone makes fault_active (%s), entries refused, scan_gate refuses, conservation vs baseline holds" % (v and v["why"],),
          bool(v) and v["why"] == "poll_failure_inflight" and v["fault_active"] and bool(v["refusal"]) and v["scan_gate"][0] is False and same_as_base(v))
    h = newhome(); cp, j = run(CHILD_W2, h, "real"); check("W2 (K1 kill point) child: rc=%s (expected 1) AND reached-target sentinel present=%s" % (cp.returncode, _reached(h, "W2_before_fault_write_real")), cp.returncode == 1 and _reached(h, "W2_before_fault_write_real"))
    cv, v = run(VERIFY, h)
    check("W2 K1 GAP CLOSED: kill between count and fault write now leaves the durable in-flight marker, fresh process refuses entries (fault_file=%s why=%s refusal=%s)" % (v and v["fault_file"], v and v["why"], v and v["refusal"]),
          bool(v) and (not v["fault_file"]) and v["why"] == "poll_failure_inflight" and bool(v["refusal"]) and same_as_base(v))
    h = newhome(); cp, j = run(CHILD_W2, h, "mutant"); cv, v = run(VERIFY, h)
    check("W2m child: rc=%s (expected 1) AND reached-target sentinel present=%s AND verify ran" % (cp.returncode, _reached(h, "W2_before_fault_write_mutant")), cp.returncode == 1 and _reached(h, "W2_before_fault_write_mutant") and cv.returncode == 0 and v is not None)
    check("W2m MUTANT (BEGIN hook removed): the K1 gap reappears (no fault file, no refusal): sensitivity control", bool(v) and (not v["fault_file"]) and v["refusal"] is None and v["fault_active"] is False)
    h = newhome(); cp, j = run(CHILD_W3, h)
    check("W3 child: rc=%s (expected 1), reached-target sentinel present=%s; failed hook: in-process refusal AND marker kept (%s)" % (cp.returncode, _reached(h, "W3_after_abort_failed_hook"), j), cp.returncode == 1 and _reached(h, "W3_after_abort_failed_hook") and bool(j) and bool(j["refusal_inproc"]) and j["marker"] is True)
    cv, v = run(VERIFY, h)
    check("W3 failed hook, FRESH process after restart: still refuses via the marker (%s)" % (v and v["why"],), bool(v) and v["why"] == "poll_failure_inflight" and bool(v["refusal"]))
    h = newhome(); cp, j = run(CHILD_W4, h)
    check("W4 child rc=%s (expected 0); recovered failure: marker removed, no abort, no fault, entries NOT refused (%s)" % (cp.returncode, j), cp.returncode == 0 and bool(j) and j["marker"] is False and j["aborts"] == 0 and j["refusal"] is None and j["fault"] is False)
    h = newhome(); cp, j = run(CHILD_W5, h)
    check("W5 marker present: entries refused BUT the risk-reducing exit poll still runs and sells (fills/credits delta %s, closes %s, aborts %s, refusal %s)" % (j and j["closes_or_progress"], j and j["ncl"], j and j["aborts"], j and j["refusal"]),
          cp.returncode == 0 and bool(j) and bool(j["refusal"]) and j["aborts"] == 0 and (j["closes_or_progress"][0] > 0 or j["ncl"] > 0))
def _w6():
    h = newhome(); cp, j = run(CHILD_W6, h)
    check("W6 BEGIN failure is VISIBLE: child rc=%s (expected 0); no marker, no abort, but commit_refusal refuses (%s) and startup_report shows begin_failed (%s)" % (cp.returncode, j and j["refusal"], j and j["begin_failed"]),
          cp.returncode == 0 and bool(j) and j["marker"] is False and j["aborts"] == 0 and bool(j["refusal"]) and "marker could not be written" in j["refusal"] and j["begin_failed"] is True)
def main():
    for f in (s_K0, s_K1, s_K2, lambda: s_K3("real"), lambda: s_K3("mutant"), s_K4, s_W, _w6):
        if FAILS or _STOP["why"]:
            print("EARLY STOP (fails=%d, stop=%s)" % (len(FAILS), _STOP["why"])); break
        f()
    print("elapsed %.1fs of %.0fs budget; FAILS: %d; stop=%s" % (time.time() - _T0, BUDGET_S, len(FAILS), _STOP["why"])); print("RESULT: %d failures" % len(FAILS)); sys.stdout.flush(); return 1 if (FAILS or _STOP["why"]) else 0
if __name__ == "__main__":
    sys.exit(main())
