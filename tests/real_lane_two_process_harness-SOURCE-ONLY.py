#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). Needs main's explicit clearance for this exact blob before any run.
TP3 (P1, P2): two REAL processes on one temp home sharing the REAL grad.py lane ledger, the REAL paper_account flock/WAL and the REAL grad_cycle fault lock, with genuine lane positions
(so lane "grad" is provable by grad.legacy_unproven_pos_ids; no marks seam is added). Children are the helper's PRE2 programs (tests/real_ledger_hook_harness-SOURCE-ONLY.py, imported as a module, import runs
no scenario). The PARENT imports no project code. Each child is its own session, killed and reaped in a finally block, one global 90s deadline. No kill-for-effect: SIGKILL is only the reaper's.
P3 (the crash window between the account debit and the lane ledger write) is NOT in this file: it is a separate source (needs the kill-seam instrumentation and its own review).
INSTRUMENTATION (child-local, no production change): P1 child F takes grad_cycle._fault_lock(GR, 5.0), the lock every fault writer holds, and parks inside it. It STANDS IN for a fault writer parked while
holding the fault lock; it is NOT a real _record_fault. P2 child X wraps paper_account._lock; the ONE acquisition whose immediate caller frame is `credit` (past the wrapper and contextlib __enter__) writes
X_HELD (post-acquire, pre-mutation of the first credit) and parks. Blocked-wait is EVIDENCE by inference (no result token + child alive + the parent's own non-blocking flock probe fails), not a kernel observation.
SCENARIOS and EXPECTATIONS, each from source at b2adde0f:
  P1a F parks inside the fault lock; O (consider_candidate for a NEW mint, flag on, T.register done) runs. grad.py (commit barrier block): commit_barrier -> grad_cycle._fault_lock(.., ENTRY_FAULT_WAIT_S=0.5) raises
      FaultLockTimeout("fault lock busy") after non-blocking retries; the opener releases the demo intent and returns _skip(mint, "entry commit barrier unavailable (%r); entries blocked fail-closed", False).
      Asserted: result (False, why, False) with why containing "entry commit barrier unavailable" and "fault lock busy"; elapsed >= 0.4s and < 8s; NO account debit; cash 10.0; lane positions 0; the demo intent key
      "grad|<mint>" absent afterward (consume_intent/release_intent pops it). The refused entry's journaled FILL is not asserted (buy() persists a fill before the gate; recorded as INFO).
  P1b control, same home after F is released: a FRESH O opens a different mint: result (True, "opened", True), exactly one debit, lane positions 1, cash == 10 - that debit's stake. This is the sensitivity contrast: same code,
      lock free.
  P2  setup child S opens ONE position through consider_candidate (flag on). Then X runs poll_exits at 3x (legs a and b due, see S1) and parks in the account lock at its FIRST credit while holding the lane ledger lock
      (grad.py _poll_exits holds _ledger_lock across the close calls); O starts consider_candidate for a second mint and must block at the lane ledger lock (inferred). After release X finishes. INVARIANT-ONLY assertions
      (O's own accept/refuse is NOT derivable from source without running, so it is recorded as INFO and only checked for consistency): X rc 0; journal has exactly 2 credit events for the first position's account
      pos_id, distinct op ids, none final; journal status None; WAL seq strictly increasing and unique; cash == 10 - sum(debit stakes) + sum(credit nets) (tolerance 1e-5); O reported opened IFF a second grad debit exists;
      if that debit exists its seq is greater than both credit seqs (O could not enter until X's whole exit loop released the lane lock, per grad.py lock structure).
NOT COVERED: kills, the debit-to-lane-write window (P3), power loss/directory fsync, a real _record_fault writer, exits racing a fault write, the other openers, supervised scripts, Telegram. TEST-FIXTURE values only.
CLAIM LIMITS: one machine and namespace; fixture feed/fill/safety stubs from the hook helper; P2 does not prove O's gating outcome."""
import fcntl, importlib.util, json, os, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location("hook_base", os.path.join(HERE, "real_ledger_hook_harness-SOURCE-ONLY.py"))
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)
FAILS = H.FAILS; check = H.check
DEADLINE = time.time() + 90.0; KIDS = []
class Stop(Exception): pass

CTLP = r'''
import threading, inspect, contextlib
CTL = os.path.join(home, "ctl"); name = sys.argv[2]
def token(tag, **kw):
    fh = open(os.path.join(CTL, name + "." + tag), "w"); json.dump(dict(kw, t=time.time(), pid=os.getpid()), fh); fh.flush(); os.fsync(fh.fileno()); fh.close()
def wait_for(path, bound):
    end = time.monotonic() + bound
    while not os.path.exists(path):
        if time.monotonic() > end: token("TIMEOUT", waiting=os.path.basename(path)); os._exit(3)
        time.sleep(0.01)
def intents():
    try: return sorted((json.load(open(FILL_PATH)).get("intents") or {}).keys())
    except Exception: return None
def jstate():
    ents, status, _ = PA._journal_read()
    sn = PA.snapshot(); ln = (sn.get("lanes") or {}).get("grad") or {}
    return {"status": status, "events": [{k: e.get(k) for k in ("event", "op_id", "seq", "stake", "net", "final", "pos_id", "lane")} for e in ents if e.get("event") in ("debit", "credit")],
            "cash": sn.get("cash"), "open_count": ln.get("open_count"), "lane_positions": sorted(GR.open_positions())}
'''
F_CHILD = H.PRE2 + CTLP + r'''
with GC._fault_lock(GR, 5.0):
    token("HELD", lock=GC._fault_lock_path(GR))
    wait_for(os.path.join(CTL, "RELEASE_" + name), 30.0)
token("DONE")
print(json.dumps({"name": name, "released": True}))
'''
O_CHILD = H.PRE2 + CTLP + r'''
n = int(sys.argv[3]); T.register(GR)
token("START"); t0 = time.monotonic()
r = GR.consider_candidate(mkcand(n), time.time()); dt = time.monotonic() - t0
res = {"name": name, "ok": bool(r[0]), "why": str(r[1]), "final": r[2], "dt": dt, "mint": mkcand(n)["mint"], "intents": intents(), "fills": len(raw_fills()), "j": jstate()}
token("DONE", ok=res["ok"]); print(json.dumps(res, default=str))
'''
S_CHILD = H.PRE2 + CTLP + r'''
r = open_first(); T.register(GR)
print(json.dumps({"name": name, "opened": bool(r and r[0]), "j": jstate()}, default=str))
'''
X_CHILD = H.PRE2 + CTLP + r'''
T.register(GR)
_orig = PA._lock; _fired = {"n": 0}
def site():
    for f in inspect.stack():
        if f.function in ("site", "_lk", "__enter__"): continue
        return f.function
@contextlib.contextmanager
def _lk():
    with _orig():
        if site() == "credit" and _fired["n"] == 0:
            _fired["n"] = 1
            token("X_HELD", lock_file=PA.LOCK_FILE, caller="credit")
            wait_for(os.path.join(CTL, "RELEASE_" + name), 30.0)
        yield
PA._lock = _lk
FakeJup.mult = 3.0
try: GR.poll_exits(price_fn=gpf(3.0))
finally: FakeJup.mult = 1.0
print(json.dumps({"name": name, "fired": _fired["n"], "j": jstate()}, default=str))
'''
def left(): return DEADLINE - time.time()
def spawn(code, home, name, *args):
    if left() < 5: raise Stop("global deadline")
    outp = os.path.join(home, "out." + name); errp = os.path.join(home, "err." + name)
    p = subprocess.Popen([sys.executable, "-c", code, home, name] + [str(a) for a in args], stdout=open(outp, "w"), stderr=open(errp, "w"), env=dict(os.environ, AGT_REPO_ROOT=REPO), start_new_session=True)
    KIDS.append(p); p.out = outp; return p
def reap_all():
    for p in KIDS:
        if p.poll() is None:
            try: os.killpg(p.pid, signal.SIGKILL)
            except Exception: pass
        try: p.wait(timeout=5)
        except Exception: pass
def last_json(path):
    try:
        for ln in reversed(open(path).read().strip().splitlines()):
            try: return json.loads(ln)
            except Exception: continue
    except Exception: pass
    return None
def finish(p, bound=20.0):
    try: p.wait(timeout=min(bound, max(1.0, left())))
    except Exception: raise Stop("child did not exit in time")
    return last_json(p.out)
def tok(home, name, tag, bound=15.0):
    path = os.path.join(home, "ctl", name + "." + tag); end = time.monotonic() + min(bound, max(1.0, left()))
    while time.monotonic() < end:
        if os.path.exists(path):
            try: return json.load(open(path))
            except Exception: pass
        time.sleep(0.01)
    return None
def need(home, name, tag, bound=20.0):
    t = tok(home, name, tag, bound)
    if t is None: raise Stop("missing token %s.%s" % (name, tag))
    return t
def release(home, name):
    fh = open(os.path.join(home, "ctl", "RELEASE_" + name), "w"); json.dump({"t": time.time()}, fh); fh.flush(); os.fsync(fh.fileno()); fh.close()
def probe_held(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN); return False
        except BlockingIOError: return True
    finally: os.close(fd)
def newhome():
    h = tempfile.mkdtemp(prefix="tp3h_"); os.makedirs(os.path.join(h, "ctl")); return h
def scen(name, fn):
    try: fn()
    except Stop as e: check("%s did not complete: %s" % (name, e), False)
    finally: reap_all()
def stakes(j): return [float(e["stake"]) for e in j["events"] if e["event"] == "debit" and e.get("lane") == "grad"]
def p1():
    h = newhome(); pf = spawn(F_CHILD, h, "f1")
    held = need(h, "f1", "HELD"); lock = held.get("lock")
    check("P1a F HELD the fault lock (token), parent's non-blocking flock probe on %s FAILS" % os.path.basename(str(lock)), bool(lock) and probe_held(lock) is True)
    po = spawn(O_CHILD, h, "o2", 2); ro = finish(po)
    why = (ro or {}).get("why", ""); j = (ro or {}).get("j") or {}
    check("P1a O refused by the commit barrier timeout: (%s, %.2fs) why=%s" % ((ro or {}).get("ok"), (ro or {}).get("dt", -1), why[:160]),
          ro is not None and po.returncode == 0 and ro["ok"] is False and ro["final"] is False and "entry commit barrier unavailable" in why and "fault lock busy" in why and 0.4 <= ro["dt"] < 8.0)
    check("P1a no account debit, cash 10.0, lane positions 0, intent key absent (intents %s, fills in journal %s INFO)" % ((ro or {}).get("intents"), (ro or {}).get("fills")),
          stakes(j) == [] and abs(float(j.get("cash", -1)) - 10.0) < 1e-9 and j.get("lane_positions") == [] and "grad|%s" % ro["mint"] not in (ro.get("intents") or []))
    release(h, "f1"); rf = finish(pf)
    check("P1a F released and exited rc 0", pf.returncode == 0 and (rf or {}).get("released") is True)
    p3 = spawn(O_CHILD, h, "o3", 3); r3 = finish(p3); j3 = (r3 or {}).get("j") or {}
    check("P1b control, lock free: fresh O opened (%s %s), exactly one debit %s, lane positions %d, cash == 10 - stake" % ((r3 or {}).get("ok"), (r3 or {}).get("why"), stakes(j3), len(j3.get("lane_positions") or [])),
          r3 is not None and p3.returncode == 0 and r3["ok"] is True and r3["why"] == "opened" and len(stakes(j3)) == 1 and len(j3["lane_positions"]) == 1 and abs(float(j3["cash"]) - (10.0 - stakes(j3)[0])) < 1e-5)
def p2():
    h = newhome(); ps = spawn(S_CHILD, h, "s0"); rs = finish(ps)
    check("P2 setup: one position opened through consider_candidate (%s)" % ((rs or {}).get("opened"),), ps.returncode == 0 and (rs or {}).get("opened") is True and len(stakes(rs["j"])) == 1)
    px = spawn(X_CHILD, h, "x1"); held = need(h, "x1", "X_HELD"); lf = held.get("lock_file")
    check("P2 X parked post-acquire at its first credit (caller frame %s), parent's account flock probe FAILS" % held.get("caller"), probe_held(lf) is True and held.get("caller") == "credit")
    po = spawn(O_CHILD, h, "o2", 2); need(h, "o2", "START")
    blocked = True; end = time.monotonic() + 1.0
    while time.monotonic() < end:
        if tok(h, "o2", "DONE", 0.0) is not None or po.poll() is not None: blocked = False
        time.sleep(0.05)
    check("P2 O started but has NO result after 1.0s while X holds the account lock inside its exit loop (blocked-wait evidence by inference)", blocked)
    release(h, "x1"); rx = finish(px); ro = finish(po)
    jx = (rx or {}).get("j") or {}; jo = (ro or {}).get("j") or {}
    ev = jo.get("events") or []; creds = [e for e in ev if e["event"] == "credit"]; debs = [e for e in ev if e["event"] == "debit"]
    seqs = [e["seq"] for e in ev]
    check("P2 X rc 0 and fired its seam once; O rc 0 (O ok=%s why=%s INFO)" % ((ro or {}).get("ok"), str((ro or {}).get("why"))[:120]), px.returncode == 0 and (rx or {}).get("fired") == 1 and po.returncode == 0 and ro is not None)
    check("P2 journal: exactly 2 credit events, distinct op ids, none final; status None; seqs unique and increasing (%s)" % seqs,
          len(creds) == 2 and len({e["op_id"] for e in creds}) == 2 and not any(e.get("final") for e in creds) and jo.get("status") is None and seqs == sorted(set(seqs)))
    nets = sum(float(e["net"]) for e in creds); st = sum(float(e["stake"]) for e in debs)
    check("P2 cash identity from the journal: %s vs %.6f" % (jo.get("cash"), 10.0 - st + nets), abs(float(jo["cash"]) - (10.0 - st + nets)) < 1e-5)
    first_seq = min(d["seq"] for d in debs) if debs else None
    second = [e for e in debs if e["seq"] != first_seq]
    o_ok = bool((ro or {}).get("ok"))
    after_credits = all(second[0]["seq"] > c["seq"] for c in creds) if second else True
    check("P2 O opened IFF exactly one extra grad debit exists (ok=%s, extra debits=%d); if it exists its seq follows both credits (%s)" % (o_ok, len(second), after_credits),
          len(second) <= 1 and o_ok == (len(second) == 1) and after_credits)
def main():
    try:
        for n, f in (("P1", p1), ("P2", p2)): scen(n, f)
    finally:
        reap_all()
    alive = [p.pid for p in KIDS if p.poll() is None]
    check("all children reaped (still alive: %s), total %d children spawned" % (alive, len(KIDS)), not alive)
    print("elapsed %.1fs of 90s budget; FAILS: %d" % (90.0 - left(), len(FAILS))); return 1 if FAILS else 0
if __name__ == "__main__":
    sys.exit(main())
