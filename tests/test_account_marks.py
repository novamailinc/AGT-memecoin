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
import time

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

# ---------------- curve fallback (pre-graduation coverage) -------
# A mint Jupiter omits is priced from the bonding curve; the mark
# carries reserves:curve provenance and the same {value,ts,source}
# contract.
H = tempfile.mkdtemp(prefix="marks-curve-")
pa, am = fresh(H)
pa.try_debit("watcher", "CURVEMINT", 1.0, 500.0, "am-c1")
curve_calls = []


def curve_stub(mint):
    curve_calls.append(mint)
    return 0.004, "bonding curve abc123"


m = am.collect_marks(price_fn=lambda mints: {},
                     curve_price_fn=curve_stub)
mk = m.get("watcher", {}).get("CURVEMINT")
check("curve fallback: jup-missing mint marked from the curve",
      mk is not None and abs(mk["value"] - 2.0) < 1e-9)
check("curve fallback: provenance is reserves:curve",
      mk is not None and mk["source"] == "reserves:curve"
      and isinstance(mk["ts"], (int, float)))
check("curve fallback: view accepts the mark (equity 9 + 2 = 11)",
      pa.snapshot(marks=m)["marks_status"] == "ok")

# Jupiter wins: a priced mint never costs a curve call.
curve_calls[:] = []
m = am.collect_marks(price_fn=lambda mints: {"CURVEMINT": 0.01},
                     curve_price_fn=curve_stub)
check("jup-priced mint: no curve call, jup provenance kept",
      curve_calls == []
      and m["watcher"]["CURVEMINT"]["source"] == "jup-v3")

# Graduated-but-jup-missing: the curve reports complete -> absent,
# fail-closed (the marker has no pool address to read vaults from).
m = am.collect_marks(price_fn=lambda mints: {},
                     curve_price_fn=lambda mint: (0.0,
                                                  "curve complete"))
check("completed curve: absent, never coerced", m == {})
check("completed curve: view fails closed",
      pa.snapshot(marks=m)["marked_equity"] is None)

# A raising curve source: absent, marker never raises.
def curve_boom(mint):
    raise RuntimeError("rpc down")


m = am.collect_marks(price_fn=lambda mints: {},
                     curve_price_fn=curve_boom)
check("raising curve source: empty map, no exception", m == {})

# Garbage curve prices are dropped.
for bad, label in [(0.0, "zero"), (-2.0, "negative"),
                   (float("nan"), "NaN"), ("0.004", "string")]:
    m = am.collect_marks(price_fn=lambda mints: {},
                         curve_price_fn=lambda mint, b=bad: (b, "x"))
    check("bad curve price dropped (%s)" % label, m == {})

# ---------------- freshness is the OBSERVATION time ----------------
# A cached price must not masquerade as just-observed: sources that
# report their observation time get it propagated into the mark's
# ts, and the account's TTL then rejects what it must.
H = tempfile.mkdtemp(prefix="marks-obs-")
pa, am = fresh(H)
pa.try_debit("grad", "OBS1", 1.0, 1000.0, "am-o1")
old = time.time() - 300.0   # older than the default 120s TTL
m = am.collect_marks(price_fn=lambda mints: {"OBS1": (0.003, old)})
mk = m["grad"]["OBS1"]
check("jup observation ts propagated, not the wrap time",
      abs(mk["ts"] - old) < 1e-9)
check("stale-observed jup mark: view rejects as stale",
      "stale" in pa.snapshot(marks=m)["marks_status"])
m = am.collect_marks(price_fn=lambda mints: {},
                     curve_price_fn=lambda mint: (0.004, old, "c"))
check("curve observation ts propagated (SOL/cache-age aware)",
      abs(m["grad"]["OBS1"]["ts"] - old) < 1e-9)
check("stale-observed curve mark: view rejects as stale",
      "stale" in pa.snapshot(marks=m)["marks_status"])

# ---------------- marks bind to the position's qty -----------------
mk = am.collect_marks(price_fn=lambda mints: {"OBS1": 0.003})
check("collected marks carry the remaining-unit binding",
      mk["grad"]["OBS1"]["qty"] == 1000.0)
snap = pa.snapshot(marks={"grad": {"OBS1": {
    "value": 3.0, "ts": time.time(), "source": "test",
    "qty": 9999.0}}})
check("wrong-qty mark rejected as qty drift",
      "qty drift" in snap["marks_status"]
      and snap["marked_equity"] is None)
snap = pa.snapshot(marks={"grad": {"OBS1": {
    "value": 3.0, "ts": time.time(), "source": "test"}}})
check("mark without a qty binding rejected",
      "no qty binding" in snap["marks_status"])

# ---------------- per-source equity split ---------------------------
H = tempfile.mkdtemp(prefix="marks-src-")
pa, am = fresh(H)
pa.try_debit("grad", "LISTED", 1.0, 1000.0, "am-s1")
pa.try_debit("watcher", "CURVED", 1.0, 500.0, "am-s2")
m = am.collect_marks(price_fn=lambda mints: {"LISTED": 0.003},
                     curve_price_fn=lambda mint: (0.004, "c"))
snap = pa.snapshot(marks=m)
check("mixed-source account fully marked",
      snap["marks_status"] == "ok")
check("marked_by_source splits jup vs curve value (3.0 / 2.0)",
      snap["marked_by_source"] == {"jup-v3": 3.0,
                                   "reserves:curve": 2.0})

# ---------------- auto-marks inside try_debit ----------------------
# With gates enabled and no caller-supplied marks, try_debit
# collects centrally pre-lock: valid marks admit, an empty collect
# blocks fail-closed. Gates stay default-OFF; this proves the
# wiring for when the owner enables them.
os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"
# gates are fail-closed without configured limits: set explicit ones
os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "5.0"
os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "50.0"
os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "60.0"
H = tempfile.mkdtemp(prefix="marks-auto-")
pa, am = fresh(H)
ok, why, pid = pa.try_debit("grad", "MINTA", 2.0, 1000.0, "g-1")
check("gated first entry on an empty account passes", ok)

am.collect_marks = lambda price_fn=None, curve_price_fn=None, \
    now=None: {"grad": {"MINTA": {"value": 3.0, "ts": time.time(),
                                  "source": "test", "qty": 1000.0, "pos_id": pid}}}
ok, why, pid2 = pa.try_debit("ceo", "MINTB", 1.0, 100.0, "g-2")
check("gated second entry: central marks admit", ok)

am.collect_marks = lambda price_fn=None, curve_price_fn=None, \
    now=None: {}
ok, why, _ = pa.try_debit("watcher", "MINTC", 1.0, 100.0, "g-3")
check("gated entry with an empty collect: blocked fail-closed",
      not ok and "marks not trustworthy" in why)

# Caller-supplied marks still win over the central collect.
am.collect_marks = lambda price_fn=None, curve_price_fn=None, \
    now=None: {"grad": {"MINTA": {"value": 3.0, "ts": time.time(),
                                  "source": "test", "qty": 1000.0,
                                  "pos_id": pid}},
               "ceo": {"MINTB": {"value": 1.0, "ts": time.time(),
                                 "source": "test", "qty": 100.0,
                                 "pos_id": pid2}}}
ok, why, _ = pa.try_debit(
    "watcher", "MINTC", 1.0, 100.0, "g-4",
    marks={"grad": {"MINTA": {"value": 3.0, "ts": time.time(),
                              "source": "test", "qty": 1000.0,
                              "pos_id": pid}},
           "ceo": {"MINTB": {"value": 1.0, "ts": time.time(),
                             "source": "test", "qty": 100.0,
                             "pos_id": pid2}}})
check("caller-supplied marks still accepted", ok)
os.environ.pop("PAPER_ACCOUNT_GATES_ENABLED")
os.environ.pop("PAPER_ACCOUNT_MAX_DAILY_LOSS_USD", None)
os.environ.pop("PAPER_ACCOUNT_MAX_DRAWDOWN_PCT", None)
os.environ.pop("PAPER_ACCOUNT_MAX_EXPOSURE_PCT", None)

if FAILURES:
    print("\nFAILURES: %s" % FAILURES)
    sys.exit(1)
print("\nALL ACCOUNT MARKS TESTS PASSED")
