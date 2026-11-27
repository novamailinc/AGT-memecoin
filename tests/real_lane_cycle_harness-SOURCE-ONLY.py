#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED. NEVER RUN WITHOUT main's explicit clearance for this exact blob. Isolated runtime test of the EXITS-FIRST main_cycle on the REAL grad.py engine
(fake feed/fill/safety stubs, temp HOME, no trader script, no thread, no kill). It reuses the BOOT/PRE2 child programs and the bounded child runner of
tests/real_ledger_hook_harness-SOURCE-ONLY.py by loading that file as a module (import runs no scenario). 3 children, rc 0 each, JSON on stdout.
  X1 flag ON: one open position at 3x, then T.main_cycle(real engine, price_fn, scan_fn, legacy). The scan callback records how many fills the journal already holds when it starts.
     Expect: the legs-a/b exit sales were journaled BEFORE the scan began (fills at scan start > fills before the cycle), legacy never ran, record status done.
  X2 flag OFF control (same setup): main_cycle runs the original legacy order: scan first. Expect the scan saw NO new fills. The X1/X2 divergence is the sensitivity evidence.
  X3 flag ON, exit step aborts (forced failure, restore refused): the abort hook records a durable fault, and the scan gate refuses entries: the scan callback must NOT run;
     exits already ran (abort counted once).
CLAIM LIMITS: one real engine in one process; not the trader scripts (their wiring is only covered by the static ast test); no threads, so the entry thread and push/ExitPoller
callbacks are not exercised; no kill; fake stubs. A passing result does not show exits-first in the supervised processes."""
import importlib.util, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("hook_base", os.path.join(HERE, "real_ledger_hook_harness-SOURCE-ONLY.py"))
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)
FAILS = H.FAILS; check = H.check
COMMON = r"""
EMPTY = {"stream": 0, "opened": 0, "select_rejected": 0, "select_reasons": {}}
r1 = open_first(); T.register(GR)
order = []
f0 = len(raw_fills())
def scan_fn():
    order.append(["scan", len(raw_fills())]); return dict(EMPTY)
def legacy():
    order.append(["legacy_scan_first", len(raw_fills())])
    GR.poll_time_exits(); GR.poll_exits(gpf(3.0))
    return dict(EMPTY), 0
"""
CHILD_X1 = H.PRE2 + COMMON + r"""
if sys.argv[2] == "off": os.environ["AGT_GRAD_CYCLE_ENABLED"] = "0"
FakeJup.mult = 3.0
try: res, n, rec = T.main_cycle(GR, gpf(3.0), scan_fn, legacy)
finally: FakeJup.mult = 1.0
print(json.dumps({"order": order, "f0": f0, "f_end": len(raw_fills()), "n": n, "status": (rec or {}).get("status"), "rec_steps": sorted(((rec or {}).get("steps") or {}).keys()), "skipped": res.get("skipped")}, default=str))
"""
CHILD_X3 = H.PRE2 + COMMON + r"""
force_abort()
FakeJup.mult = 3.0
try: res, n, rec = T.main_cycle(GR, gpf(3.0), scan_fn, legacy)
finally: FakeJup.mult = 1.0
fr = GC._read_fault(GR)
print(json.dumps({"order": order, "aborts": GR._POLL_ABORTS["count"], "fault_reason": (fr or {}).get("reason"), "scan_skipped": (rec or {}).get("scan_skipped"), "status": (rec or {}).get("status"), "res_opened": res.get("opened"), "refusal": T.commit_refusal(GR.LANE)}, default=str))
"""
def main():
    h = H.newhome(); cp, j = H.run(CHILD_X1, h, "on")
    check("X1 child ran (rc=%s, expected 0)" % cp.returncode, cp.returncode == 0 and j is not None)
    if j:
        scans = [o for o in j["order"] if o[0] == "scan"]; leg = [o for o in j["order"] if o[0] != "scan"]
        check("X1 flag on: scan ran once AFTER the exit sales were journaled (fills at scan start %s > before-cycle %s), legacy never ran (%s)" % (scans, j["f0"], leg),
              len(scans) == 1 and scans[0][1] > j["f0"] and leg == [] and str(j["status"]).startswith("done"))
    h = H.newhome(); cp, j2 = H.run(CHILD_X1, h, "off")
    check("X2 child ran (rc=%s, expected 0)" % cp.returncode, cp.returncode == 0 and j2 is not None)
    if j2:
        check("X2 flag OFF control: original order, scan first (fills at scan start %s == before-cycle %s): the X1 ordering is caused by the flag path" % (j2["order"], j2["f0"]),
              len(j2["order"]) == 1 and j2["order"][0][0] == "legacy_scan_first" and j2["order"][0][1] == j2["f0"])
    h = H.newhome(); cp, j3 = H.run(CHILD_X3, h)
    check("X3 child ran (rc=%s, expected 0)" % cp.returncode, cp.returncode == 0 and j3 is not None)
    if j3:
        check("X3 exit step aborted: abort counted once, durable fault %s, scan callback NOT run (order %s), commit_refusal refuses (%s)" % (j3["fault_reason"], j3["order"], j3["refusal"]),
              j3["aborts"] == 1 and j3["fault_reason"] == "poll_abort_live" and j3["order"] == [] and bool(j3["refusal"]) and j3["res_opened"] == 0)
    print("elapsed %.1fs of %.0fs budget; FAILS: %d; stop=%s" % (H.time.time() - H._T0, H.BUDGET_S, len(FAILS), H._STOP["why"])); return 1 if (FAILS or H._STOP["why"]) else 0
if __name__ == "__main__":
    sys.exit(main())
