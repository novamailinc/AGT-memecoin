#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED. NEVER RUN WITHOUT main's explicit clearance for this exact blob. Real paper_account on a temp HOME (network blocked by the fixture before any repo import): real flock
holders in THREADS (separate open file descriptions, so they exclude each other exactly like processes) test paper_account.bounded_lock_wait / AccountBusy. Hook-harness BOOT child, one child, rc 0, JSON.
  A1 DEFAULT UNCHANGED: outside any scope, a plain PA.snapshot() while another holder holds the account lock BLOCKS (elapsed >= the hold) and then completes (exits, credits, reconcile keep the blocking acquire).
  A2 BOUNDED: inside bounded_lock_wait(0.3) a held lock raises AccountBusy after about 0.3 s (not before 0.25, well before the 1.5 s hold), the scope is reset (PA._WAIT.s is None) and the lock is acquirable after the holder releases.
  A3 NOTHING WRITTEN: PA.try_debit inside the scope under the holder raises AccountBusy with cash, WAL entry count and positions unchanged; after the holder releases, the SAME op id debits exactly once (cash down once), and a second call with the same op id does not debit again (idempotent).
  A4 SCOPE ISOLATION: a second thread without a scope is not affected by another thread's scope (thread-local): it blocks, it does not raise.
CLAIM LIMITS: the bound is on the ACQUIRE only; I/O inside the critical section is not bounded and not tested; holders are threads not processes; one account."""
import importlib.util, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("hook_base", os.path.join(HERE, "real_ledger_hook_harness-SOURCE-ONLY.py"))
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)
FAILS = H.FAILS; check = H.check
CHILD = H.BOOT + r"""
import threading
res = {"reached": True}
def holder(sec, started):
    with PA._lock():
        started.set(); time.sleep(sec)
def start(sec):
    st = threading.Event(); t = threading.Thread(target=holder, args=(sec, st), daemon=True); t.start(); assert st.wait(5); return t
def wal_n():
    ents, status, _ = PA._journal_read(); return len(ents)
# A1
t = start(0.6); t0 = time.monotonic(); PA.snapshot(); res["a1_elapsed"] = time.monotonic() - t0; t.join(5)
# A2
t = start(1.5); t0 = time.monotonic(); err = None
try:
    with PA.bounded_lock_wait(0.3):
        with PA._lock(): pass
except PA.AccountBusy as e: err = str(e)
res["a2_elapsed"] = time.monotonic() - t0; res["a2_err"] = err; res["a2_scope_reset"] = getattr(PA._WAIT, "s", None) is None
t.join(5)
with PA.bounded_lock_wait(0.5):
    with PA._lock(): res["a2_acquirable_after"] = True
# A3
c0, w0 = PA.snapshot()["cash"], wal_n(); err3 = None; t = start(1.5)
try:
    with PA.bounded_lock_wait(0.3): PA.try_debit("grad", "AcctBoundMint1pump", 1.0, 1000.0, "op:acct-bound:1")
except PA.AccountBusy as e: err3 = str(e)
c1, w1 = PA.snapshot.__call__()["cash"] if False else None, None
t.join(5)
c1, w1 = PA.snapshot()["cash"], wal_n()
ok1 = PA.try_debit("grad", "AcctBoundMint1pump", 1.0, 1000.0, "op:acct-bound:1"); c2 = PA.snapshot()["cash"]
ok2 = PA.try_debit("grad", "AcctBoundMint1pump", 1.0, 1000.0, "op:acct-bound:1"); c3 = PA.snapshot()["cash"]
res.update(a3_err=err3, a3_cash=[c0, c1, c2, c3], a3_wal_before_after=[w0, w1], a3_ok=[list(ok1)[:2], list(ok2)[:2]])
# A4
t = start(1.0); out = {}
def other():
    t0 = time.monotonic()
    try: PA.snapshot(); out["r"] = "completed"
    except Exception as e: out["r"] = repr(e)
    out["el"] = time.monotonic() - t0
with PA.bounded_lock_wait(0.2):
    th = threading.Thread(target=other, daemon=True); th.start(); th.join(8)
t.join(5); res["a4"] = out
print(json.dumps(res, default=str))
"""
def main():
    h = H.newhome(); cp, j = H.run(CHILD, h)
    check("A0 child ran (rc=%s, expected 0) and reached the site" % cp.returncode, cp.returncode == 0 and j is not None and j.get("reached") is True)
    if j:
        check("A1 default unchanged: plain acquire blocked for %.2fs (>= 0.45) and completed" % j["a1_elapsed"], j["a1_elapsed"] >= 0.45)
        check("A2 bounded: AccountBusy after %.2fs (0.25..0.9), scope reset %s, lock acquirable after release %s, err %r" % (j["a2_elapsed"], j["a2_scope_reset"], j.get("a2_acquirable_after"), j["a2_err"]),
              j["a2_err"] and 0.25 <= j["a2_elapsed"] < 0.9 and j["a2_scope_reset"] and j.get("a2_acquirable_after") is True)
        c = j["a3_cash"]; w = j["a3_wal_before_after"]
        check("A3 nothing written under busy (cash %s, WAL %s, err %r); same op id then debits exactly once (cash %s) and a repeat does not debit again (%s)" % (c[:2], w, j["a3_err"], c[1:3], c[2:4]),
              j["a3_err"] and c[0] == c[1] and w[0] == w[1] and j["a3_ok"][0][0] is True and c[2] < c[1] and c[3] == c[2])
        check("A4 thread-local: another thread without a scope blocked (%.2fs >= 0.7) and completed (%s), did not raise" % (j["a4"].get("el", 0), j["a4"].get("r")), j["a4"].get("r") == "completed" and j["a4"].get("el", 0) >= 0.7)
    print("elapsed %.1fs of %.0fs budget; FAILS: %d; stop=%s" % (H.time.time() - H._T0, H.BUDGET_S, len(FAILS), H._STOP["why"])); return 1 if (FAILS or H._STOP["why"]) else 0
if __name__ == "__main__":
    sys.exit(main())
