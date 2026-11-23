#!/usr/bin/env python3
"""Hermetic tests for the central account marker.

No network: the price function is injected. The contract under test
is that collect_marks() builds paper_account-valid marks
({value: remaining qty x price, ts, source}) and that anything it
cannot price is ABSENT (so the account view fails closed), never
coerced to zero or book value.
"""
import importlib
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

FAILURES = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + " " + name)
    if not cond:
        FAILURES.append(name)


def fresh(home):
    os.environ["FOREX_AGENT_HOME"] = home
    os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
    import daemon.launch_scanner.paper_account as pa
    import daemon.launch_scanner.account_marks as am
    importlib.reload(pa)
    importlib.reload(am)
    return pa, am


H = tempfile.mkdtemp(prefix="marks-empty-")
pa, am = fresh(H)
m = am.collect_marks(price_fn=lambda mints: {"X": 1.0})
check("empty account: no marks", m == {})

# One grad position: $2.00 stake buying 1000 units (try_debit's 4th
# argument is QTY, matching the lane wiring: stake / entry price).
H = tempfile.mkdtemp(prefix="marks-one-")
pa, am = fresh(H)
ok, why, pid = pa.try_debit("grad", "MINTA", 2.0, 1000.0, "am-1")
assert ok, why
m = am.collect_marks(price_fn=lambda mints: {"MINTA": 0.003})
mk = m.get("grad", {}).get("MINTA")
check("mark present for a priced position",
      mk is not None and abs(mk["value"] - 3.0) < 1e-9)
check("mark carries ts and jup provenance",
      mk is not None and isinstance(mk["ts"], (int, float))
      and mk["source"] == "jup-v3")
snap = pa.snapshot(marks=m)
check("collected marks make the account view ok (equity 8+3=11)",
      snap["marks_status"] == "ok"
      and abs(snap["marked_equity"] - 11.0) < 1e-9)

# Unpriced position: absent, view fails closed.
m = am.collect_marks(price_fn=lambda mints: {})
check("unpriced position: absent from the map", m == {})
snap = pa.snapshot(marks=m)
check("absent mark: account view fails closed",
      snap["marks_status"] != "ok" and snap["marked_equity"] is None)

# Garbage prices are dropped, never coerced.
for bad, label in [(0.0, "zero"), (-1.5, "negative"),
                   (float("nan"), "NaN"), ("0.003", "string")]:
    m = am.collect_marks(price_fn=lambda mints, b=bad: {"MINTA": b})
    check("bad price dropped (%s)" % label, m == {})

# Price function that raises: marker degrades to empty, never raises.
def boom(mints):
    raise RuntimeError("network down")

m = am.collect_marks(price_fn=boom)
check("raising price source: empty map, no exception", m == {})

# Two lanes, partial pricing coverage.
H = tempfile.mkdtemp(prefix="marks-two-")
pa, am = fresh(H)
pa.try_debit("grad", "A", 2.0, 1000.0, "am-t1")
pa.try_debit("ceo", "B", 1.0, 1000.0, "am-t2")
m = am.collect_marks(price_fn=lambda mints: {"A": 0.004})
check("only the priced lane appears",
      "A" in m.get("grad", {}) and "ceo" not in m)
snap = pa.snapshot(marks=m)
check("partial coverage: whole account fails closed",
      snap["marked_equity"] is None
      and "ceo/B" in snap["marks_status"])
m = am.collect_marks(price_fn=lambda mints: {"A": 0.004, "B": 0.001})
snap = pa.snapshot(marks=m)
check("full coverage: equity 7 + 4 + 1 = 12",
      snap["marks_status"] == "ok"
      and abs(snap["marked_equity"] - 12.0) < 1e-9)

if FAILURES:
    print("\nFAILURES: %s" % FAILURES)
    sys.exit(1)
print("\nALL ACCOUNT MARKS TESTS PASSED")
