#!/usr/bin/env python3
"""Stage 2 account-level risk gates (2026-11-22).

PROVISIONAL and INACTIVE by default: PAPER_ACCOUNT_GATES_ENABLED=1
opts in, and every threshold is an agent-chosen placeholder for the
$10 demo, not an owner-approved limit. The gates run INSIDE the
debit's lock (evaluation and reservation are one atomic section),
fail closed on any untrusted input, and never read book_value.
Exits never pass through the entry gates.

Run: python3 tests/test_paper_account_gates.py
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fresh(home, start="10.0", gates=True, max_daily=None,
          max_dd=None, max_exp=None):
    os.environ["FOREX_AGENT_HOME"] = home
    os.environ["PAPER_ACCOUNT_START"] = start
    os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None)
    if gates:
        os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"
    else:
        os.environ.pop("PAPER_ACCOUNT_GATES_ENABLED", None)
    for k, v in (("PAPER_ACCOUNT_MAX_DAILY_LOSS_USD", max_daily),
                 ("PAPER_ACCOUNT_MAX_DRAWDOWN_PCT", max_dd),
                 ("PAPER_ACCOUNT_MAX_EXPOSURE_PCT", max_exp)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    import daemon.launch_scanner.paper_account as pa
    return importlib.reload(pa)


def mark(value, qty, age_s=0.0):
    # qty binds the mark to the position's current remaining units.
    return {"value": value, "ts": time.time() - age_s,
            "source": "test-harness", "qty": qty}


def marks_at_cost(pa):
    """Mark every open position at its remaining basis (flat)."""
    snap = pa.snapshot()
    out = {}
    for lane, info in snap["lanes"].items():
        for p in info["positions"]:
            out.setdefault(lane, {})[p["mint"]] = mark(p["basis"],
                                                       p["qty"])
    return out


# ---- default OFF -----------------------------------------------------
H = tempfile.mkdtemp(prefix="gates-off-")
pa = fresh(H, gates=False)
ok, why, _ = pa.try_debit("grad", "A", 5.0, 5.0, "g-off-1")
check("gates OFF by default: entry without marks succeeds", ok)

# ---- empty account: equity is exactly cash, no marks needed ---------
H = tempfile.mkdtemp(prefix="gates-empty-")
pa = fresh(H)
ok, why, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-e1")
check("gates ON, empty account: first entry allowed without marks",
      ok)

# ---- open position: marks become mandatory ---------------------------
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-e2")
check("open position + no marks: entry refused (fail-closed)",
      not ok and "marks not trustworthy" in why)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-e3",
                          marks={"grad": {"A": mark(5.0, 5.0,
                                                    age_s=3600)}})
check("stale mark: entry refused", not ok and "stale" in why)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-e4",
                          marks={"grad": {"A": mark(5.0, 5.0)}})
check("fresh provenanced mark: entry allowed", ok)

# ---- daily loss gate --------------------------------------------------
# Position $2.00 marked at $1.00: equity 9.00, daily loss 1.00,
# drawdown 10%, exposure after a $1 entry $3.00 <= 60% of 9.00 -
# so the DAILY LOSS limit alone decides.
H = tempfile.mkdtemp(prefix="gates-daily-")
pa = fresh(H, max_daily=0.50)
pa.try_debit("grad", "A", 2.0, 2.0, "g-d1")
m = {"grad": {"A": mark(1.0, 2.0)}}
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-d2", marks=m)
check("daily loss over the provisional limit: refused",
      not ok and "daily loss" in why)
pa = fresh(H, max_daily=5.00)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-d3", marks=m)
check("same loss under a raised limit: allowed", ok)

# ---- drawdown gate -----------------------------------------------------
H = tempfile.mkdtemp(prefix="gates-dd-")
pa = fresh(H, max_dd=5.0)
pa.try_debit("grad", "A", 2.0, 2.0, "g-x1")
m = {"grad": {"A": mark(1.0, 2.0)}}   # equity 9.00 vs peak 10 -> 10%
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-x2", marks=m)
check("drawdown over the provisional limit: refused",
      not ok and "drawdown" in why)
pa = fresh(H, max_dd=50.0)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-x3", marks=m)
check("drawdown under a raised limit: allowed (10% < 50%)", ok)

# ---- exposure gate: evaluation + reservation are atomic ---------------
H = tempfile.mkdtemp(prefix="gates-exp-")
pa = fresh(H)  # default exposure cap 60% of equity 10 = 6.00
ok, why, pid1 = pa.try_debit("grad", "A", 4.0, 4.0, "g-p1")
check("first entry inside capacity ok", ok)
m = {"grad": {"A": mark(4.0, 4.0)}}
ok, why, _ = pa.try_debit("ceo", "B", 3.0, 3.0, "g-p2", marks=m)
check("second entry over 60% exposure refused ($4+$3 > $6)",
      not ok and "exposure" in why)
ok, why, _ = pa.try_debit("ceo", "B", 2.0, 2.0, "g-p3", marks=m)
check("entry exactly at capacity allowed ($4+$2 == $6)", ok)

# Two processes, one slot of capacity: exactly one wins.
H = tempfile.mkdtemp(prefix="gates-race-")
pa = fresh(H)
racer = os.path.join(os.path.dirname(__file__), "..", "tests",
                     "_paper_gate_racer.py")
with open(racer, "w") as f:
    f.write(
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "import daemon.launch_scanner.paper_account as pa\n"
        "snap = pa.snapshot()\n"
        "marks = {}\n"
        "for lane, info in snap['lanes'].items():\n"
        "    for p in info['positions']:\n"
        "        import time\n"
        "        marks.setdefault(lane, {})[p['mint']] = {\n"
        "            'value': p['basis'], 'ts': time.time(),\n"
        "            'source': 'racer'}\n"
        "ok, why, _ = pa.try_debit(sys.argv[1], sys.argv[2],"
        " 4.0, 4.0, sys.argv[3], marks=marks)\n"
        "print('WIN' if ok else 'LOSE')\n"
        % os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       "..")))
env = dict(os.environ)
env["FOREX_AGENT_HOME"] = H
env["PAPER_ACCOUNT_GATES_ENABLED"] = "1"
procs = [subprocess.Popen([sys.executable, racer, "lane%d" % n,
                           "RACE%d" % n, "race-%d" % n],
                          stdout=subprocess.PIPE, env=env, text=True)
         for n in range(2)]
outs = [p.communicate()[0].strip() for p in procs]
os.unlink(racer)
check("two lanes race one capacity slot: exactly one wins",
      sorted(outs) == ["LOSE", "WIN"])
snap = pa.snapshot()
check("race left a consistent account (exposure $4 <= $6)",
      snap["cost_exposure"] == 4.0)

# ---- exits bypass the entry gates --------------------------------------
H = tempfile.mkdtemp(prefix="gates-exit-")
pa = fresh(H, max_daily=0.50)
ok, _, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-z1")
m = {"grad": {"A": mark(0.5, 5.0)}}  # daily loss 4.50 >> 0.50
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-z2", marks=m)
check(" blown daily-loss blocks entries", not ok)
ok, why = pa.credit("grad", "A", 0.50, "g-z3", pos_id=pid)
check("exits bypass entry gates: close settles at a loss", ok)
snap = pa.snapshot()
check("settled at the loss (cash 5.50, realized -4.50)",
      snap["cash"] == 5.50 and snap["realized_pnl"] == -4.50)

# ---- qty drift: marks collected before a position change -------------
# The pre-lock collect / in-lock evaluate split must not let a mark
# sized for a position that no longer exists through the gate.
H = tempfile.mkdtemp(prefix="gates-drift-")
pa = fresh(H)
ok, _, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-q1")
# Marks sized for the full 5.0 units...
m_stale = {"grad": {"A": mark(5.0, 5.0)}}
# ...then a tranche settles half the units before the gated entry
# reaches the lock (the concurrent-close race, simulated in order).
ok, why = pa.credit("grad", "A", 2.60, "g-q2", pos_id=pid, qty=2.5)
assert ok, why
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-q3",
                          marks=m_stale)
check("tranche between collect and lock: entry refused (qty drift)",
      not ok and "qty drift" in why)
m_now = {"grad": {"A": mark(2.6, 2.5)}}
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-q4", marks=m_now)
check("re-collected marks sized to the CURRENT units: allowed", ok)

# Reopen drift: close fully, reopen the same mint bigger; the old
# marks still name grad/A and must not bind to the new instance.
H = tempfile.mkdtemp(prefix="gates-reopen-")
pa = fresh(H)
ok, _, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-o1")
m_old = {"grad": {"A": mark(5.0, 5.0)}}
pa.credit("grad", "A", 5.0, "g-o2", pos_id=pid)
ok, _, pid2 = pa.try_debit("grad", "A", 5.0, 50.0, "g-o3",
                           marks={})
assert ok, pid2
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-o4",
                          marks=m_old)
check("reopen with different size: old marks refused (qty drift)",
      not ok and "qty drift" in why)

# ---- rollover with no marks: fail closed -------------------------------
H = tempfile.mkdtemp(prefix="gates-rollover-")
pa = fresh(H)
pa.try_debit("grad", "A", 5.0, 5.0, "g-r1")
pa.snapshot(marks={"grad": {"A": mark(5.0, 5.0)}})  # capture baselines
with open(pa.LEDGER_PATH) as f:
    led = json.load(f)
led["day"] = "2000-01-01"
led["day_baseline"]["day"] = "2000-01-01"   # captured "yesterday"
with open(pa.LEDGER_PATH, "w") as f:
    json.dump(led, f)
pa = fresh(H)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-r2")
check("rollover + no marks: entry refused (fail-closed)", not ok)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-r3",
                          marks={"grad": {"A": mark(5.0, 5.0)}})
check("rollover + trustworthy marks: entry allowed, new baseline",
      ok)
snap = pa.snapshot()
check("new-day baseline captured from the post-rollover mark",
      snap["day_start_equity"]["day"] == snap["day"]
      and abs(snap["day_start_equity"]["value"] - 10.0) < 1e-9)

# ---- baselines survive a restart ---------------------------------------
pa2 = fresh(H)
snap = pa2.snapshot()
check("baselines persist across a restart (fresh module state)",
      snap["day_start_equity"] is not None
      and abs(snap["day_start_equity"]["value"] - 10.0) < 1e-9
      and snap["peak_equity"] is not None)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER ACCOUNT GATE TESTS PASSED")
