"""UNEXECUTED DRAFT. Unit tests for the write-ahead poll-failure marker (grad_cycle.mark_inflight / unmark_inflight / fault_active / scan_gate / clear_fault
evidence) and the grad_trader_cycle hooks, with a FAKE engine and temp files. No child process, no thread, no network. Not covered: real grad.py, kill/restart
(see tests/real_ledger_hook_harness-SOURCE-ONLY.py W1-W5)."""
import json, os, sys, tempfile, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from daemon import grad_cycle as GC
from daemon import grad_trader_cycle as T
FAILS = 0
def check(name, ok):
    global FAILS
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: FAILS += 1
TMP = tempfile.mkdtemp(prefix="gtc_inflt_"); _N = [0]
CLEAN = {"poll_aborts": {"count": 0}, "n_pending": 0, "n_settlement_pending": 0, "n_migration_blocked": 0}
def fake():
    _N[0] += 1
    g = types.SimpleNamespace(LEDGER_PATH=os.path.join(TMP, "ledi%d.json" % _N[0]), LANE="lane_i%d" % _N[0])
    g._POLL_ABORTS = {"count": 0}; g._demo_mode = lambda: True; g.recon_health = lambda: CLEAN
    pa = types.SimpleNamespace(snapshot=lambda: {"degraded": None, "lanes": {}}); g._paper_account = lambda: pa
    return g
os.environ[GC.ENV_FLAG] = "1"
g = fake(); check("I1 no marker: fault_active False", GC.fault_active(g) == (False, None))
t1 = GC.mark_inflight(g, "m1", "boom")
check("I2 mark writes a durable marker; fault_active True with reason poll_failure_inflight", t1 and os.path.exists(GC._inflight_path(g)) and GC.fault_active(g) == (True, "poll_failure_inflight"))
check("I3 scan_gate refuses entries on the marker", GC.scan_gate(g)[0] is False and "poll_failure_inflight" in GC.scan_gate(g)[1])
check("I4 unmark with the WRONG token keeps the marker", GC.unmark_inflight(g, "nope") is False and os.path.exists(GC._inflight_path(g)))
check("I5 unmark with None keeps the marker", GC.unmark_inflight(g, None) is False and os.path.exists(GC._inflight_path(g)))
check("I6 unmark with the right token removes it", GC.unmark_inflight(g, t1) is True and not os.path.exists(GC._inflight_path(g)) and GC.fault_active(g) == (False, None))
t1 = GC.mark_inflight(g, "m1", "a"); t2 = GC.mark_inflight(g, "m2", "b"); rec = json.load(open(GC._inflight_path(g)))
check("I7 a second mark over an unresolved one keeps the earlier token in prior_tokens, n=2", rec["token"] == t2 and rec["prior_tokens"] == [t1] and rec["n"] == 2)
check("I8 unmark of the newest token does NOT remove a marker with unresolved prior tokens", GC.unmark_inflight(g, t2) is False and os.path.exists(GC._inflight_path(g)))
g = fake(); open(GC._inflight_path(g), "w").write("{not json")
check("I9 unreadable marker counts as active (inflight_marker_unreadable)", GC.fault_active(g) == (True, "inflight_marker_unreadable") and GC.unmark_inflight(g, "x") is False)
g = fake(); os.mkdir(GC._inflight_path(g))
check("I10 mark where the marker path is unwritable returns None, never raises", GC.mark_inflight(g, "m", "e") is None)
g = fake(); orig = GC._FAULT_LOCK_TIMEOUT_S
GC._FAULT_LOCK_TIMEOUT_S = 0.05
import fcntl
lk = open(GC._fault_lock_path(g), "a"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
try: tk = GC.mark_inflight(g, "m", "e")
finally:
    fcntl.flock(lk, fcntl.LOCK_UN); lk.close(); GC._FAULT_LOCK_TIMEOUT_S = orig
check("I11 fault lock held elsewhere: mark returns None after the retry window (no marker, exit path not blocked indefinitely)", tk is None and not os.path.exists(GC._inflight_path(g)))
g = fake(); T.register(g)
check("I12 register installs ABORT, BEGIN and END hooks (flag on)", all(callable(getattr(g, n, None)) for n in ("_ABORT_HOOK", "_FAILURE_BEGIN_HOOK", "_FAILURE_END_HOOK")))
tok = g._FAILURE_BEGIN_HOOK("m", "e")
check("I13 BEGIN hook returns a token and writes the marker; commit_refusal refuses", tok and os.path.exists(GC._inflight_path(g)) and bool(T.commit_refusal(g.LANE)))
g._FAILURE_END_HOOK("m", tok)
check("I14 END hook after a successful restore removes the marker; commit_refusal clean", not os.path.exists(GC._inflight_path(g)) and T.commit_refusal(g.LANE) is None)
g._POLL_ABORTS["count"] = 1
tok = g._FAILURE_BEGIN_HOOK("m", "e"); g._ABORT_HOOK("m", "e")
check("I15 abort hook with a durable fault removes the marker (fault supersedes) and the fault is recorded", not os.path.exists(GC._inflight_path(g)) and GC._read_fault(g) and GC._read_fault(g).get("reason") == "poll_abort_live")
g = fake(); T.register(g); real = GC._record_fault; GC._record_fault = lambda *a, **k: False
try:
    tok = g._FAILURE_BEGIN_HOOK("m", "e"); g._ABORT_HOOK("m", "e")
finally: GC._record_fault = real
check("I16 abort hook that FAILS to record keeps the marker and sets hook_failed", os.path.exists(GC._inflight_path(g)) and T._HOOK_FAILED.get(g.LANE) is True)
g = fake(); T.register(g)
os.environ.pop(GC.ENV_FLAG)
g2 = fake(); T.register(g2)
check("I17 flag off: register installs no BEGIN/END hooks and nothing is written", getattr(g2, "_FAILURE_BEGIN_HOOK", None) is None and T.commit_refusal(g2.LANE) is None)
os.environ[GC.ENV_FLAG] = "1"
g = fake(); GC._MEM.pop(g.LEDGER_PATH, None); tok = GC.mark_inflight(g, "m", "e")
ok, ev = GC.clear_fault(g)
check("I18 clear_fault sees the marker as a fault (not 'no_fault') and reports it in evidence; commit stays disabled", ok is False and ev.get("inflight_marker") == "present" and "no_fault" not in (ev.get("blocked") or []) and "commit_disabled_no_common_writer_lock" in (ev.get("blocked") or []))
g = fake(); open(GC._inflight_path(g), "w").write("{x")
ok, ev = GC.clear_fault(g)
check("I19 clear_fault blocks on an unreadable marker", ok is False and "inflight_marker_unreadable" in (ev.get("blocked") or []))
print("FAILS: %d" % FAILS); sys.exit(1 if FAILS else 0)
