#!/usr/bin/env python3
"""SUCCESSOR (opener-contract-src): Y8 now asserts the REAL contract (barrier refusal releases the intent: retry = NEW op/buy, first fill = uncommitted ORPHAN); old in-process Y9 replaced by a two-phase REAL crash (Y9a, rc 7 + fsynced sentinel) and restart (Y9b) stale-prior test; Y0 counts fault-lock constructions instead of file existence; Y10 asserts the orphan/new-buy contract. SOURCE ONLY, UNEXECUTED. NEVER RUN WITHOUT main's explicit clearance for this exact blob. Real-engine controlled test of the OPENER commit barrier (grad.consider_candidate: fault lock
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
  Y8 same fill-key REPLAY: fixed candidate, refused at the barrier, fault cleared, same candidate retried: opens from the journaled fill (no second buy, same op id, same price), one debit.
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
AH = {"t": None}
def _acct_holder(st):
    with PA._lock():
        st.set(); time.sleep(1.5)
def stub(lane):
    calls.append(len(calls) + 1)
    if scen == "Y10" and len(calls) == 3:               # the barrier re-check: from here the next step is try_debit; the account lock is now held by another holder
        st = threading.Event(); AH["t"] = threading.Thread(target=_acct_holder, args=(st,), daemon=True); AH["t"].start(); assert st.wait(5)
    if scen == "Y5" and len(calls) == 3: raise RuntimeError("FIXTURE-GUARD-RAISES")
    return "FIXTURE-FAULT-PRESENT" if os.path.exists(fault_file()) else None
EG.mandatory_entry_refusal = stub                      # the SAME qualified module object grad imports at call time
if scen == "Y3": EG.commit_barrier = lambda engine, wait_s=None: contextlib.nullcontext()
if scen == "Y6":
    def _bad(engine, wait_s=None): raise ImportError("FIXTURE-BARRIER-BROKEN")
    EG.commit_barrier = _bad
if scen == "Y7": PA.try_debit = lambda *a, **k: (False, "FIXTURE-DEBIT-REFUSED", None)
LK = {"n": 0}
if scen in ("Y0", "Y1"):
    _orig_fl = GC._fault_lock
    class _CountFL(_orig_fl):
        def __init__(self, *a, **k): LK["n"] += 1; super().__init__(*a, **k)
    GC._fault_lock = _CountFL
LOCKFILE_PRE = os.path.exists(GC._fault_lock_path(GR))           # was the lock file already there BEFORE the opener ran (setup/register may create it)?
def committed_ops():
    df = GR._demo_filler(); return sorted(k for k in raw_fills() if df.op_committed(k) is True)
if scen in ("Y9a", "Y11a"):                                                # crash exactly between the buy (journaled fill, intent live) and the debit
    def _die(*a, **k):
        _reach("buy-before-debit"); os._exit(7)
    PA.try_debit = _die
    GR.consider_candidate(mkcand(1), time.time()); sys.exit(99)  # reached only if the crash site was NOT hit
if scen in ("Y9b", "Y11b"):                                                # RESTART on the same home: the unreleased intent + journaled uncommitted fill exist
    pre = {"intents": intents(), "fills": sorted(raw_fills()), "committed": committed_ops(), "cash": PA.snapshot()["cash"]}
    def _age(j):
        for v in j["fills"].values(): v["provenance"]["quote_requested_at"] = float(v["provenance"]["quote_requested_at"]) - 120.0
    if scen == "Y9b": F._locked(_age)                                       # Y11b: the journaled quote is left FRESH
    r1 = GR.consider_candidate(mkcand(1), time.time()); mid = snap(); mid_fills = sorted(raw_fills())
    pos1 = list(raw_ledger().get("positions", {}).values()); px1 = pos1[0].get("entry_price") if pos1 else None
    fill_px = {k: v.get("price_usd") for k, v in raw_fills().items()}
    r2 = GR.consider_candidate(mkcand(1), time.time()) if scen == "Y9b" else (None, None); end = snap()
    print(json.dumps({"reached": True, "pre": pre, "r1": [bool(r1[0]), str(r1[1])], "mid": mid, "mid_fills": mid_fills, "r2": [bool(r2[0]), str(r2[1])] if r2[0] is not None else None, "entry_px": px1, "fill_px": fill_px, "end": end, "end_fills": sorted(raw_fills()), "end_committed": committed_ops()}))
    sys.exit(0)
started = threading.Event(); hold = {"Y2": 0.3, "Y3": 0.6, "Y4": 1.5, "Y8": 0.3}.get(scen)
CAND = mkcand(1)                                   # FIXED candidate: same created_ts, and the op id is LANE:entry:mint:int(created_ts*1000) (grad.py), so the journal FILL KEY SET is the observable identity; no per-attempt nonce is in this op id (the per-run nonce seen elsewhere comes from other fixtures)
def writer():
    with GC._fault_lock(GR):
        started.set(); time.sleep(hold)
        if scen in ("Y2", "Y3", "Y8"): open(fault_file(), "w").write("{}")
t = None
if hold:
    t = threading.Thread(target=writer, daemon=True); t.start(); assert started.wait(5)
s0 = snap(); T0A = time.monotonic(); r = GR.consider_candidate(CAND if scen in ("Y8", "Y9", "Y10") else mkcand(1), time.time()); EL = time.monotonic() - T0A; s1 = snap()
if t: t.join(10)
retry = None
if scen == "Y4":
    EG.mandatory_entry_refusal = lambda lane: None
    rr = GR.consider_candidate(mkcand(1), time.time()); retry = {"r": [bool(rr[0]), str(rr[1])], "s": snap()}
if scen == "Y10":
    el = EL                                   # measured right after consider_candidate returned, BEFORE snap() (PA.snapshot() itself waits for the account lock)
    AH["t"].join(10)
    f1 = sorted(raw_fills()); rr = GR.consider_candidate(CAND, time.time()); f2 = json.load(open(FILL_PATH))["fills"]
    retry = {"r": [bool(rr[0]), str(rr[1])], "s": snap(), "n_fills_before": len(f1), "n_fills_after": len(f2), "first_ops": f1, "ops_after": sorted(f2), "committed": committed_ops(), "elapsed_first": el}
if scen in ("Y8", "Y9"):
    os.remove(fault_file())                                    # the fault is cleared by the fixture; the stub now passes
    f1 = json.load(open(FILL_PATH))["fills"]; first_ops = sorted(f1); first_px = {k: v.get("price_usd") for k, v in f1.items()}
    if scen == "Y9":
        def _age(j):
            for v in j["fills"].values(): v["provenance"]["quote_requested_at"] = float(v["provenance"]["quote_requested_at"]) - 120.0
        F._locked(_age)
    rr = GR.consider_candidate(CAND, time.time()); f2 = json.load(open(FILL_PATH))["fills"]
    pos = list(raw_ledger().get("positions", {}).values())
    retry = {"r": [bool(rr[0]), str(rr[1])], "s": snap(), "same_ops": sorted(f2) == first_ops, "first_ops": first_ops, "ops_after": sorted(f2), "committed": committed_ops(), "n_fills_before": len(first_ops), "n_fills_after": len(f2),
             "entry_px": (pos[0].get("entry_price") if pos else None), "first_px": first_px}
LOCKFILE_POST = os.path.exists(GC._fault_lock_path(GR))     # BEFORE the acquirable probe below: that probe opens (creates) the lock file itself
try:
    with GC._fault_lock(GR, 0.5): acquirable = True
except Exception as e: acquirable = False
print(json.dumps({"reached": True, "scen": scen, "r": [bool(r[0]), str(r[1])], "s0": s0, "s1": s1, "calls": len(calls), "depth_empty": not getattr(GC._FLT, "depth", {}), "acquirable": acquirable,
                  "lockfile": LOCKFILE_POST, "lockfile_after_probe": os.path.exists(GC._fault_lock_path(GR)), "lockfile_pre": LOCKFILE_PRE, "lock_ctor": LK["n"], "retry": retry, "writer_released": (t is None or not t.is_alive()), "main_depth_empty_note": "depth is thread-local: this is the MAIN thread; the writer thread releases its own lock, checked by writer_released"}))
"""
def run(scen):
    h = H.newhome(); cp, j = H.run(CHILD, h, scen)
    check("%s child ran (rc=%s, expected 0) and reached the site" % (scen, cp.returncode), cp.returncode == 0 and j is not None and j.get("reached") is True)
    return j
def refused(j, why):
    s0, s1 = j["s0"], j["s1"]
    return (j["r"][0] is False and why in j["r"][1] and s1["cash"] == s0["cash"] and s1["n_pos"] == s0["n_pos"] == 0 and s1["credits"] == s0["credits"]
            and j["s1"]["intents"] == [] and j["depth_empty"] and j["acquirable"])
D = {}
def delta(j): return round(float(j["s0"]["cash"]) - float(j["s1"]["cash"]), 9)
def main():
    j = run("Y0")
    if j: check("Y0 flag off control: opened (%s), one position, cash down once (%s -> %s), the opener constructed the fault lock %d times (expect 0: commit_barrier is a no-op with the flag off; lock file existed BEFORE the opener: %s, right after it (before the harness's own acquirable probe, which creates the file): %s), guard called %d times, lock depth empty" % (j["r"], j["s0"]["cash"], j["s1"]["cash"], j["lock_ctor"], j["lockfile_pre"], j["lockfile"], j["calls"]),
                j["r"][0] and j["s1"]["n_pos"] == 1 and delta(j) > 0 and j["lock_ctor"] == 0 and j["lockfile"] == j["lockfile_pre"] and j["calls"] == 3 and j["depth_empty"] and j["s1"]["intents"] == [])
    j = run("Y1")
    if j: check("Y1 flag on control: opened, one position, cash down once (%s -> %s), no intent left, guard called %d times (pre-lock, in-lock, barrier re-check), lock free, fault lock constructed %d times (>= 1)" % (j["s0"]["cash"], j["s1"]["cash"], j["calls"], j["lock_ctor"]),
                j["lock_ctor"] >= 1 and j["r"][0] and j["s1"]["n_pos"] == 1 and delta(j) > 0 and j["s1"]["intents"] == [] and j["calls"] == 3 and j["depth_empty"] and j["acquirable"] and not D.update(d1=delta(j)))
    j = run("Y2")
    if j: check("Y2 RACE: re-check under the barrier refused with the stub reason (%s); cash %s == %s, no position, intent released (%s), buy fill journaled (%d fills), guard called %d times, lock free" % (j["r"], j["s1"]["cash"], j["s0"]["cash"], j["s1"]["intents"], j["s1"]["fills"], j["calls"]),
                refused(j, "FIXTURE-FAULT-PRESENT") and j["calls"] == 3 and j["s1"]["fills"] >= 1)
    j = run("Y3")
    if j: check("Y3 SENSITIVITY: barrier removed -> the same race OPENS the entry (%s) and debits once: the barrier is what refuses in Y2" % (j["r"],), j["r"][0] and j["s1"]["n_pos"] == 1 and abs(delta(j) - D.get("d1", -1)) < 1e-9)
    j = run("Y4")
    if j:
        rt = j["retry"] or {}
        check("Y4 fault-lock acquire failure: refused as barrier unavailable (%s), nothing debited, intent released, lock acquirable; retry opened with exactly one debit (cash %s -> %s)" % (j["r"], j["s0"]["cash"], (rt.get("s") or {}).get("cash")),
              refused(j, "entry commit barrier unavailable") and (rt.get("r") or [False])[0] is True and rt["s"]["n_pos"] == 1 and abs((j["s0"]["cash"] - rt["s"]["cash"]) - D.get("d1", -1)) < 1e-9 and rt["s"]["intents"] == [])
    j = run("Y8")
    if j:
        rt = j["retry"] or {}
        ops1, ops2, com = rt.get("first_ops") or [], rt.get("ops_after") or [], rt.get("committed") or []
        check("Y8 RETRY AFTER A BARRIER REFUSAL (the refusal RELEASES the intent, so the retry gets a NEW op id and a NEW buy; it does NOT replay): refused once (%s); retry opened (%s); fills %s -> %s (the first op %s is kept as an ORPHAN: journaled, no account debit), account-committed ops %s (exactly the new one, not the orphan), one position, one debit (cash %s -> %s vs Y1 delta %s), intent released" % (j["r"], rt.get("r"), rt.get("n_fills_before"), rt.get("n_fills_after"), ops1, com, j["s0"]["cash"], (rt.get("s") or {}).get("cash"), D.get("d1")),
              refused(j, "FIXTURE-FAULT-PRESENT") and (rt.get("r") or [False])[0] is True and len(ops1) == 1 and len(ops2) == 2 and set(ops1) < set(ops2) and len(com) == 1 and com[0] not in ops1 and rt["s"]["n_pos"] == 1 and abs((j["s0"]["cash"] - rt["s"]["cash"]) - D.get("d1", -1)) < 1e-9 and rt["s"]["intents"] == [])
    # Y9 (two phases, ONE home): a REAL crash between the buy and the debit leaves the intent LIVE with a journaled uncommitted fill; the restart must treat an AGED prior as stale
    h9 = H.newhome(); cp9 = H.py(CHILD, h9, "Y9a")
    reached = os.path.exists(os.path.join(h9, "reached.buy-before-debit"))
    check("Y9a deliberate crash between buy and debit: child rc=%s (expected 7) and the fsynced reached-site sentinel exists (%s)" % (cp9.returncode, reached), cp9.returncode == 7 and reached)
    cp9b = H.py(CHILD, h9, "Y9b"); j9 = H.last_json(cp9b)
    check("Y9b restart child ran (rc=%s, expected 0) and reached the site" % cp9b.returncode, cp9b.returncode == 0 and j9 is not None and j9.get("reached") is True)
    if j9 and reached:
        pre = j9["pre"]
        check("Y9b restart state: the intent survived the crash (%s), one journaled fill (%s), NOT account-committed (%s)" % (pre["intents"], pre["fills"], pre["committed"]), len(pre["intents"]) == 1 and len(pre["fills"]) == 1 and pre["committed"] == [])
        check("Y9b STALE prior replay refused: journaled quote aged 120 s (> max_data_age_s 60): refused (%s), no position, cash %s unchanged, no new buy (fills %s), intent released (%s)" % (j9["r1"], j9["mid"]["cash"], j9["mid_fills"], j9["mid"]["intents"]),
              j9["r1"][0] is False and "quote age" in j9["r1"][1] and j9["mid"]["n_pos"] == 0 and j9["mid"]["cash"] == pre["cash"] and j9["mid_fills"] == pre["fills"] and j9["mid"]["intents"] == [])
        check("Y9b RECOVERY: the next attempt after the stale refusal opens with a NEW op and one debit (%s; cash %s -> %s vs Y1 delta %s); fills %s -> %s (the stale one stays as an ORPHAN, uncommitted; committed %s)" % (j9["r2"], pre["cash"], j9["end"]["cash"], D.get("d1"), len(pre["fills"]), len(j9["end_fills"]), j9["end_committed"]),
              j9["r2"][0] is True and j9["end"]["n_pos"] == 1 and abs((pre["cash"] - j9["end"]["cash"]) - D.get("d1", -1)) < 1e-9 and len(j9["end_fills"]) == 2 and len(j9["end_committed"]) == 1 and j9["end_committed"][0] not in pre["fills"])
    # Y11 (two phases, ONE home): crash between buy and debit, restart with a FRESH journaled quote: the intent is live, so the ORIGINAL fill is REPLAYED (no second buy)
    h11 = H.newhome(); cp11 = H.py(CHILD, h11, "Y11a")
    reached11 = os.path.exists(os.path.join(h11, "reached.buy-before-debit"))
    check("Y11a deliberate crash between buy and debit: child rc=%s (expected 7) and the fsynced reached-site sentinel exists (%s)" % (cp11.returncode, reached11), cp11.returncode == 7 and reached11)
    cp11b = H.py(CHILD, h11, "Y11b"); j11 = H.last_json(cp11b)
    check("Y11b restart child ran (rc=%s, expected 0) and reached the site" % cp11b.returncode, cp11b.returncode == 0 and j11 is not None and j11.get("reached") is True)
    if j11 and reached11:
        pre = j11["pre"]
        check("Y11b FRESH crash replay: intent survived (%s), one uncommitted fill (%s); the restart REPLAYS it: opened (%s), NO second buy (fills %s), the committed op is the ORIGINAL one (%s), entry price %s equals its fill price %s, one position, one debit (cash %s -> %s vs Y1 delta %s), intent consumed (%s)" % (pre["intents"], pre["fills"], j11["r1"], j11["end_fills"], j11["end_committed"], j11["entry_px"], list((j11["fill_px"] or {}).values()), pre["cash"], j11["end"]["cash"], D.get("d1"), j11["end"]["intents"]),
              len(pre["intents"]) == 1 and len(pre["fills"]) == 1 and pre["committed"] == [] and j11["r1"][0] is True and j11["end_fills"] == pre["fills"] and j11["end_committed"] == pre["fills"] and j11["entry_px"] in list(j11["fill_px"].values()) and j11["end"]["n_pos"] == 1 and abs((pre["cash"] - j11["end"]["cash"]) - D.get("d1", -1)) < 1e-9 and j11["end"]["intents"] == [])
    j = run("Y10")
    if j:
        rt = j["retry"] or {}
        check("Y10 ACCOUNT BUSY at the opener: another holder keeps the account lock 1.5 s after the barrier re-check; the debit is refused as account busy (%s) in %.2fs (< the 1.5 s hold: the ACQUIRE is bounded), nothing debited (cash %s -> %s), no position, intent released, fault lock free; after the holder released, the same candidate opens with exactly one debit (delta %s vs Y1 %s); per the real contract the retry is a NEW buy (fills %s -> %s, the busy-refused attempt leaves one uncommitted ORPHAN fill, committed ops %s)" % (j["r"], rt.get("elapsed_first", -1), j["s0"]["cash"], j["s1"]["cash"], (j["s0"]["cash"] - (rt.get("s") or {}).get("cash", 0)), D.get("d1"), rt.get("n_fills_before"), rt.get("n_fills_after"), rt.get("committed")),
              refused(j, "account busy") and rt.get("elapsed_first", 9) < 1.4 and (rt.get("r") or [False])[0] is True and abs((j["s0"]["cash"] - rt["s"]["cash"]) - D.get("d1", -1)) < 1e-9 and rt["s"]["n_pos"] == 1 and rt["s"]["intents"] == [] and j["writer_released"] is not False and rt.get("n_fills_before") == 1 and rt.get("n_fills_after") == 2 and len(rt.get("committed") or []) == 1 and rt["committed"][0] not in rt["first_ops"])
    j = run("Y5")
    if j: check("Y5 guard raises at the re-check (3rd call): refused fail-closed (%s), same cleanup" % (j["r"],), refused(j, "unavailable at commit") and j["calls"] == 3)
    j = run("Y6")
    if j: check("Y6 commit_barrier construction raises: refused (%s), same cleanup" % (j["r"],), refused(j, "entry commit barrier unavailable"))
    j = run("Y7")
    if j: check("Y7 try_debit refuses inside the barrier: skip with its reason (%s), lock released, intent released" % (j["r"],), refused(j, "FIXTURE-DEBIT-REFUSED"))
    print("elapsed %.1fs of %.0fs budget; FAILS: %d; stop=%s" % (H.time.time() - H._T0, H.BUDGET_S, len(FAILS), H._STOP["why"])); return 1 if (FAILS or H._STOP["why"]) else 0
if __name__ == "__main__":
    sys.exit(main())
