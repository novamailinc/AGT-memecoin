#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED. NEVER RUN WITHOUT main's explicit clearance for this exact blob. Real-engine controlled test of the OPENER commit barrier (grad.consider_candidate: fault lock
held from the pre-debit guard RE-CHECK through try_debit and the ledger write). Real grad.py engine, real paper account and demo filler on a temp HOME (network blocked by the fixture
before any repo import), no trader script, no thread except the explicit fault-writer thread, no kill. Reuses BOOT/PRE2 and the bounded child runner of the hook harness (loaded as
a module). One child per scenario, rc 0 each, JSON on stdout, reached-site flag, positive counters.
LOCK ORDER enumerated (see report): lane ledger lock (grad._ledger_lock) -> [barrier] cycle FAULT lock (grad_cycle._fault_lock, leaf, reentrant per thread) -> account lock / journal I/O
(paper_account try_debit, demo_fill _locked) inside the barrier. The abort hook runs ledger -> fault. Writers of cycle/entry-scan faults take fault only. The test checks the thread's fault-lock depth
is empty and the lock is acquirable after EVERY scenario (no leaked lock).
  Y0 flag OFF control: opens, no fault-lock file, guard called 3 times (pre-lock check at grad.py ~1203, in-lock check ~1338, barrier re-check ~1426).   Y1 flag ON control: opens, one debit, no intent left, guard called twice.
  Y2 RACE: a writer thread holds the fault lock and writes a fault AFTER the early guard passed: the re-check under the barrier refuses with the stub reason; cash unchanged, no position, no debit,
     demo intent released (not leaked), the buy fill stays journaled (replayable, uncommitted).   Y3 SENSITIVITY: barrier replaced by a no-op in the same scenario: the entry OPENS (debit happens).
  Y4 fault-lock ACQUIRE FAILURE: writer holds the lock past the 0.5 s wait: "entry commit barrier unavailable", nothing debited, intent released, lock acquirable afterwards, and a RETRY then opens with exactly one debit
     (no lost or doubled debit/intent).   Y5 guard raises at the re-check: refused fail-closed, same cleanup.   Y6 commit_barrier construction raises: refused, same cleanup.
  Y7 try_debit refuses inside the barrier: skip with its reason, lock released, intent released.
  Y8 same-op REPLAY: fixed candidate, refused at the barrier, fault cleared, same candidate retried: opens from the journaled fill (no second buy, same op id, same price), one debit.
  Y9 STALE replay: same as Y8 but the journaled quote is aged past max_data_age_s: the retry is refused as "quote age" (no re-quote, no debit).
CLAIM LIMITS: one process; the demo-mode filler path only; stubbed guard (the real guard's limit checks are not what is under test); fault writer is a thread holding the real flock (a second PROCESS is not used); no kill; exits and the abort hook are not exercised here."""
import importlib.util, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("hook_base", os.path.join(HERE, "real_ledger_hook_harness-SOURCE-ONLY.py"))
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)
FAILS = H.FAILS; check = H.check
CHILD = H.PRE2 + r"""
import threading, contextlib
from daemon.launch_scanner import entry_guard as EG
scen = sys.argv[2]
if scen == "Y0": os.environ["AGT_GRAD_CYCLE_ENABLED"] = "0"
calls = []
def fault_file(): return GC._fault_path(GR)
def intents():
    try: return sorted((json.load(open(FILL_PATH)).get("intents") or {}).keys())
    except FileNotFoundError: return []
def snap():
    ps = PA.snapshot()
    return {"cash": ps["cash"], "n_pos": len(raw_ledger().get("positions", {})), "intents": intents(), "fills": len(raw_fills()), "credits": len(raw_credits()[0])}
def stub(lane):
    calls.append(len(calls) + 1)
    if scen == "Y5" and len(calls) == 3: raise RuntimeError("FIXTURE-GUARD-RAISES")
    return "FIXTURE-FAULT-PRESENT" if os.path.exists(fault_file()) else None
EG.mandatory_entry_refusal = stub                      # the SAME qualified module object grad imports at call time
if scen == "Y3": EG.commit_barrier = lambda engine, wait_s=None: contextlib.nullcontext()
if scen == "Y6":
    def _bad(engine, wait_s=None): raise ImportError("FIXTURE-BARRIER-BROKEN")
    EG.commit_barrier = _bad
if scen == "Y7": PA.try_debit = lambda *a, **k: (False, "FIXTURE-DEBIT-REFUSED", None)
started = threading.Event(); hold = {"Y2": 0.3, "Y3": 0.6, "Y4": 1.5, "Y8": 0.3, "Y9": 0.3}.get(scen)
CAND = mkcand(1)                                   # FIXED candidate: same created_ts, so the SAME op id on every attempt (Y8/Y9)
def writer():
    with GC._fault_lock(GR):
        started.set(); time.sleep(hold)
        if scen in ("Y2", "Y3", "Y8", "Y9"): open(fault_file(), "w").write("{}")
t = None
if hold:
    t = threading.Thread(target=writer, daemon=True); t.start(); assert started.wait(5)
s0 = snap(); r = GR.consider_candidate(CAND if scen in ("Y8", "Y9") else mkcand(1), time.time()); s1 = snap()
if t: t.join(10)
retry = None
if scen == "Y4":
    EG.mandatory_entry_refusal = lambda lane: None
    rr = GR.consider_candidate(mkcand(1), time.time()); retry = {"r": [bool(rr[0]), str(rr[1])], "s": snap()}
if scen in ("Y8", "Y9"):
    os.remove(fault_file())                                    # the fault is cleared by the fixture; the stub now passes
    f1 = json.load(open(FILL_PATH))["fills"]; first_ops = sorted(f1); first_px = {k: v.get("price_usd") for k, v in f1.items()}
    if scen == "Y9":
        def _age(j):
            for v in j["fills"].values(): v["provenance"]["quote_requested_at"] = float(v["provenance"]["quote_requested_at"]) - 120.0
        F._locked(_age)
    rr = GR.consider_candidate(CAND, time.time()); f2 = json.load(open(FILL_PATH))["fills"]
    pos = list(raw_ledger().get("positions", {}).values())
    retry = {"r": [bool(rr[0]), str(rr[1])], "s": snap(), "same_ops": sorted(f2) == first_ops, "n_fills_before": len(first_ops), "n_fills_after": len(f2),
             "entry_px": (pos[0].get("entry_price") if pos else None), "first_px": first_px}
try:
    with GC._fault_lock(GR, 0.5): acquirable = True
except Exception as e: acquirable = False
print(json.dumps({"reached": True, "scen": scen, "r": [bool(r[0]), str(r[1])], "s0": s0, "s1": s1, "calls": len(calls), "depth_empty": not getattr(GC._FLT, "depth", {}), "acquirable": acquirable,
                  "lockfile": os.path.exists(GC._fault_lock_path(GR)), "retry": retry}))
"""
def run(scen):
    h = H.newhome(); cp, j = H.run(CHILD, h, scen)
    check("%s child ran (rc=%s, expected 0) and reached the site" % (scen, cp.returncode), cp.returncode == 0 and j is not None and j.get("reached") is True)
    return j
def refused(j, why):
    s0, s1 = j["s0"], j["s1"]
    return (j["r"][0] is False and why in j["r"][1] and s1["cash"] == s0["cash"] and s1["n_pos"] == s0["n_pos"] == 0 and s1["credits"] == s0["credits"]
            and j["s1"]["intents"] == [] and j["depth_empty"] and j["acquirable"])
def main():
    j = run("Y0")
    if j: check("Y0 flag off control: opened (%s), one position, cash down once (%s -> %s), no fault-lock file (%s), guard called %d times, lock depth empty" % (j["r"], j["s0"]["cash"], j["s1"]["cash"], j["lockfile"], j["calls"]),
                j["r"][0] and j["s1"]["n_pos"] == 1 and j["s1"]["cash"] < j["s0"]["cash"] and not j["lockfile"] and j["calls"] == 3 and j["depth_empty"] and j["s1"]["intents"] == [])
    j = run("Y1")
    if j: check("Y1 flag on control: opened, one position, cash down once (%s -> %s), no intent left, guard called %d times (pre-lock, in-lock, barrier re-check), lock free" % (j["s0"]["cash"], j["s1"]["cash"], j["calls"]),
                j["r"][0] and j["s1"]["n_pos"] == 1 and j["s1"]["cash"] < j["s0"]["cash"] and j["s1"]["intents"] == [] and j["calls"] == 3 and j["depth_empty"] and j["acquirable"])
    j = run("Y2")
    if j: check("Y2 RACE: re-check under the barrier refused with the stub reason (%s); cash %s == %s, no position, intent released (%s), buy fill journaled (%d fills), guard called %d times, lock free" % (j["r"], j["s1"]["cash"], j["s0"]["cash"], j["s1"]["intents"], j["s1"]["fills"], j["calls"]),
                refused(j, "FIXTURE-FAULT-PRESENT") and j["calls"] == 3 and j["s1"]["fills"] >= 1)
    j = run("Y3")
    if j: check("Y3 SENSITIVITY: barrier removed -> the same race OPENS the entry (%s) and debits once: the barrier is what refuses in Y2" % (j["r"],), j["r"][0] and j["s1"]["n_pos"] == 1 and j["s1"]["cash"] < j["s0"]["cash"])
    j = run("Y4")
    if j:
        rt = j["retry"] or {}
        check("Y4 fault-lock acquire failure: refused as barrier unavailable (%s), nothing debited, intent released, lock acquirable; retry opened with exactly one debit (cash %s -> %s)" % (j["r"], j["s0"]["cash"], (rt.get("s") or {}).get("cash")),
              refused(j, "entry commit barrier unavailable") and (rt.get("r") or [False])[0] is True and rt["s"]["n_pos"] == 1 and rt["s"]["cash"] < j["s0"]["cash"] and rt["s"]["intents"] == [])
    j = run("Y8")
    if j:
        rt = j["retry"] or {}
        check("Y8 same-op replay: refused once at the barrier (%s), then the SAME candidate retried after the fault cleared opened (%s) with NO second buy (fills %s -> %s, same op ids %s), entry price equals the journaled fill price (%s vs %s), exactly one debit (cash %s -> %s)" % (j["r"], rt.get("r"), rt.get("n_fills_before"), rt.get("n_fills_after"), rt.get("same_ops"), rt.get("entry_px"), list((rt.get("first_px") or {}).values()), j["s0"]["cash"], (rt.get("s") or {}).get("cash")),
              refused(j, "FIXTURE-FAULT-PRESENT") and (rt.get("r") or [False])[0] is True and rt["same_ops"] and rt["n_fills_after"] == rt["n_fills_before"] and rt["entry_px"] in list(rt["first_px"].values()) and rt["s"]["n_pos"] == 1 and rt["s"]["cash"] < j["s0"]["cash"] and rt["s"]["intents"] == [])
    j = run("Y9")
    if j:
        rt = j["retry"] or {}
        check("Y9 STALE replay refused: refused at the barrier (%s), then the same candidate retried with the journaled quote aged 120 s (> max_data_age_s 60) is refused as stale (%s), no position, cash unchanged (%s -> %s), no second buy (fills %s -> %s), intent released, no leaked lock" % (j["r"], rt.get("r"), j["s0"]["cash"], (rt.get("s") or {}).get("cash"), rt.get("n_fills_before"), rt.get("n_fills_after")),
              refused(j, "FIXTURE-FAULT-PRESENT") and (rt.get("r") or [True])[0] is False and "quote age" in (rt.get("r") or ["", ""])[1] and rt["s"]["n_pos"] == 0 and rt["s"]["cash"] == j["s0"]["cash"] and rt["n_fills_after"] == rt["n_fills_before"] and rt["s"]["intents"] == [])
    j = run("Y5")
    if j: check("Y5 guard raises at the re-check (3rd call): refused fail-closed (%s), same cleanup" % (j["r"],), refused(j, "unavailable at commit") and j["calls"] == 3)
    j = run("Y6")
    if j: check("Y6 commit_barrier construction raises: refused (%s), same cleanup" % (j["r"],), refused(j, "entry commit barrier unavailable"))
    j = run("Y7")
    if j: check("Y7 try_debit refuses inside the barrier: skip with its reason (%s), lock released, intent released" % (j["r"],), refused(j, "FIXTURE-DEBIT-REFUSED"))
    print("elapsed %.1fs of %.0fs budget; FAILS: %d; stop=%s" % (H.time.time() - H._T0, H.BUDGET_S, len(FAILS), H._STOP["why"])); return 1 if (FAILS or H._STOP["why"]) else 0
if __name__ == "__main__":
    sys.exit(main())
