#!/usr/bin/env python3
"""Adversarial regression: serial-deployer low-count completeness.

Origin: independent review of the serial-coverage deltas (2357 ->
delta2-0003), 2026-11-24. Reviewer's probe, preserved at the
reviewer's request for the producer's regression suite.

THE ADMISSION CASE (case 1): a creator with ZERO indexed births whose
launches the feed never ingested is INDISTINGUISHABLE from a creator
who launched nothing, as long as unrelated event flow (other mints'
births/migrations) keeps the continuity predicate satisfied. Under the
delta2-0003 predicate this PASSES the serial check with count 0 - an
incomplete low count admitted as measured.

Why no recorder-local predicate can settle this: continuity of
observed activity (even birth-only, per-kind) witnesses that SOME
events were ingested, never that ALL of this creator's births were.
A silent upstream drop of one birth (PumpPortal-side, network, parse)
leaves a continuous-looking stream. Elapsed time is not completeness
(round 1); observed-activity continuity is not completeness either
(round 2).

REQUIRED CONTRACT (reviewer + main, 2026-11-24): a low 24h launch
count may pass ONLY with an authoritative complete launch history for
that creator (e.g. on-chain enumeration of the creator's signatures in
the window, classified for create transactions), not from the
recorder's own activity. Until such a source is wired, the
conservative option holds: low counts reject unknown_serial_coverage.

This file encodes that contract: case 1 and 2 FAIL (print FAIL) against
the delta2-0003 predicate and pass once the contract is implemented.
Cases 3-7 are boundary/edge regressions that PASS against delta2-0003
and must KEEP passing under any successor predicate (the outage-window
detection delta2 added is real and must be preserved).

Run: PYTHONPATH=<repo root> python3 tests/agt_serial_masking_regression.py
Exit 0 iff every check passes.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from daemon.launch_scanner import grad_select as sel  # noqa: E402

NOW = time.time()
_failures = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        _failures.append(name)


def reset_index():
    sel._index_refresh = lambda now: None
    for k in ("by_mint", "by_mig", "by_trader"):
        sel._INDEX[k] = {}
    sel._INDEX["event_ts"] = []
    sel._INDEX["coverage_start"] = None
    sel._INDEX["available"] = True


def dense_flow(n=289, step=300):
    """Unrelated global event flow: 5-minute cadence across 24h."""
    return [NOW - 86400 + i * step for i in range(n)]


# --- Contract cases: FAIL against delta2-0003, must pass under the
# --- required authoritative-history / conservative contract. --------

# Case 1 (THE admission case): dense unrelated flow, creator's births
# entirely missing from the index. A serial deployer whose 5 launches
# were never ingested is admitted here with count 0.
reset_index()
sel._INDEX["coverage_start"] = NOW - 90000
sel._INDEX["event_ts"] = dense_flow()
sel._INDEX["by_trader"] = {"CREATOR_X": []}
rj = sel._serial_check("CREATOR_X", NOW, {})
check("contract: dense unrelated flow + creator's births missing "
      "must NOT pass as a measured low count",
      rj is not None and rj.get("reason") == "unknown_serial_coverage")

# Case 2: same shape, creator has one indexed birth (undercount 1 of 5).
reset_index()
sel._INDEX["coverage_start"] = NOW - 90000
sel._INDEX["event_ts"] = dense_flow()
sel._INDEX["by_trader"] = {"CREATOR_X": [NOW - 100]}
rj = sel._serial_check("CREATOR_X", NOW, {})
check("contract: dense unrelated flow + partial births (1 of 5) "
      "must NOT pass as a measured low count",
      rj is not None and rj.get("reason") == "unknown_serial_coverage")

# --- Preservation cases: PASS against delta2-0003; any successor
# --- predicate must keep these behaviors. -----------------------------

# Case 3: outage windows still reject (the real gain of delta2).
reset_index()
sel._INDEX["coverage_start"] = NOW - 90000
sel._INDEX["event_ts"] = [NOW - 87000, NOW - 900, NOW - 600, NOW - 300]
sel._INDEX["by_trader"] = {"CREATOR_X": [NOW - 100]}
rj = sel._serial_check("CREATOR_X", NOW, {})
check("preserve: old coverage_start + 6h mid-window gap rejects "
      "unknown_serial_coverage",
      rj is not None and rj.get("reason") == "unknown_serial_coverage")

# Case 4: definite evidence preserved - >=3 observed launches reject
# serial_deployer even under gappy coverage, with the count stamped.
reset_index()
sel._INDEX["coverage_start"] = NOW - 90000
sel._INDEX["event_ts"] = [NOW - 87000, NOW - 900, NOW - 300]
sel._INDEX["by_trader"] = {"CREATOR_X": [NOW, NOW - 100, NOW - 200]}
rj = sel._serial_check("CREATOR_X", NOW, {})
check("preserve: >=3 under partial coverage rejects serial_deployer "
      "and stamps the count",
      rj is not None and rj.get("reason") == "serial_deployer"
      and rj.get("values", {}).get("deployer_launches_24h") == 3)

# Case 5: fail-closed path never stamps an undercount as measured,
# and journals the gap detail.
reset_index()
sel._INDEX["coverage_start"] = NOW - 90000
sel._INDEX["event_ts"] = [NOW - 87000, NOW - 900]
sel._INDEX["by_trader"] = {"CREATOR_X": [NOW - 100]}
rj = sel._serial_check("CREATOR_X", NOW, {})
vals = (rj or {}).get("values", {})
check("preserve: undercount never stamped; gap detail journaled",
      rj is not None
      and "deployer_launches_24h" not in vals
      and bool(vals.get("recorder_coverage_gap")))

# Case 6: feed-dead trailing edge rejects (last event >15 min ago).
reset_index()
sel._INDEX["coverage_start"] = NOW - 90000
sel._INDEX["event_ts"] = [t for t in dense_flow() if t <= NOW - 1200]
sel._INDEX["by_trader"] = {"CREATOR_X": [NOW - 1300]}
rj = sel._serial_check("CREATOR_X", NOW, {})
check("preserve: trailing-edge feed death rejects",
      rj is not None and rj.get("reason") == "unknown_serial_coverage")

# Case 7: no index data at all still rejects unknown_serial (unchanged).
reset_index()
sel._INDEX["available"] = False
rj = sel._serial_check("CREATOR_X", NOW, {})
check("preserve: empty index rejects unknown_serial",
      rj is not None and rj.get("reason") == "unknown_serial")

print()
if _failures:
    print("FAILURES: %d" % len(_failures))
    for f in _failures:
        print("  - " + f)
    sys.exit(1)
print("ALL SERIAL-MASKING REGRESSION CHECKS PASSED")
