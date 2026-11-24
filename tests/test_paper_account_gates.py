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
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests (mandatory-guard migration, disclosed)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fresh(home, start="10.0", gates=True, max_daily=5.00,
          max_dd=50.0, max_exp=60.0):
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
    os.environ["PAPER_ACCOUNT_MAX_SIMULTANEOUS"] = "1000"  # TEST-FIXTURE value, not owner
    os.environ["PAPER_ACCOUNT_MAX_POSITIONS"] = "1000"  # TEST-FIXTURE value, not owner
    os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"  # TEST-FIXTURE value, not owner
    os.environ["PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
    os.environ["PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
    import daemon.launch_scanner.paper_account as pa
    pa = importlib.reload(pa)
    # Isolation (disclosed test edit): the central marks default reaches
    # live Jupiter + a mainnet RPC curve probe. Replace it at the test
    # boundary with the fail-closed result (no marks) it already returns
    # when those sources are unreachable. Calls that pass marks= are
    # unaffected.
    pa._central_marks = lambda: {}
    return pa


def mark(value, qty, age_s=0.0, pos_id="p"):
    # qty+pos_id bind the mark to the current open instance.
    return {"value": value, "ts": time.time() - age_s,
            "source": "test-harness", "qty": qty, "pos_id": pos_id}


def marks_at_cost(pa):
    """Mark every open position at its remaining basis (flat)."""
    snap = pa.snapshot()
    out = {}
    for lane, info in snap["lanes"].items():
        for p in info["positions"]:
            out.setdefault(lane, {})[p["mint"]] = mark(
                p["basis"], p["qty"], pos_id=p["pos_id"])
    return out


# ---- default OFF -----------------------------------------------------
H = tempfile.mkdtemp(prefix="gates-off-")
pa = fresh(H, gates=False)
ok, why, _ = pa.try_debit("grad", "A", 5.0, 5.0, "g-off-1")
# CONTRACT CHANGE (mandatory guard, owner directive): gates off no longer
# admits entries; it refuses fail-closed.
check("gates OFF: entry REFUSED fail-closed (mandatory gates)",
      ok is False and "mandatory gates off" in str(why))

# ---- empty account: equity is exactly cash, no marks needed ---------
H_EMPTY = tempfile.mkdtemp(prefix="gates-empty-")
H = H_EMPTY
pa = fresh(H, max_daily=None, max_dd=None, max_exp=None)
ok, why, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-e0")
check("gates ON, NO limits configured anywhere: entry refused "
      "fail-closed (no placeholder fallback)",
      not ok and "no owner-approved limit" in why)
pa = fresh(H)
ok, why, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-e1")
check("gates ON with limits, empty account: first entry allowed "
      "without marks", ok)

# ---- owner_config limits (no env): the owner-approved path -----------
_saved_home = os.environ.get("FOREX_AGENT_HOME")
H = tempfile.mkdtemp(prefix="gates-ownercfg-")
os.environ["FOREX_AGENT_HOME"] = H
import json as _json, os as _os
_os.makedirs(H, exist_ok=True)
with open(_os.path.join(H, "owner_config.json"), "w") as _fh:
    _json.dump({"max_daily_loss_usd": 5.0, "max_drawdown_pct": 50.0,
                "max_exposure_pct": 60.0}, _fh)
pa = fresh(H, max_daily=None, max_dd=None, max_exp=None)
# env threshold vars popped; owner_config file supplies
ok, why, pid_oc = pa.try_debit("grad", "A", 5.0, 5.0, "g-oc1")
check("owner_config limits (no env): entry allowed", ok)
# tighten the owner limit: same loss profile must now refuse
with open(_os.path.join(H, "owner_config.json"), "w") as _fh:
    _json.dump({"max_daily_loss_usd": 0.01, "max_drawdown_pct": 50.0,
                "max_exposure_pct": 60.0}, _fh)
m = {"grad": {"A": mark(4.0, 5.0, pos_id=pid_oc)}}  # equity 9, loss 1
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-oc2", marks=m)
check("owner_config tightened daily-loss limit: refused",
      not ok and "daily loss" in why)
os.environ["FOREX_AGENT_HOME"] = _saved_home

# ---- precedence: env may tighten, never loosen, owner limits ---------
_saved_home = os.environ.get("FOREX_AGENT_HOME")
H = tempfile.mkdtemp(prefix="gates-prec-")
os.environ["FOREX_AGENT_HOME"] = H
with open(os.path.join(H, "owner_config.json"), "w") as _fh:
    _json.dump({"max_daily_loss_usd": 0.50, "max_drawdown_pct": 50.0,
                "max_exposure_pct": 60.0}, _fh)
# Env LOOSER than owner (5.0 > 0.5): owner wins, the $1 loss refuses.
pa = fresh(H, max_daily=5.00, max_dd=None, max_exp=None)
_, _, pid_p = pa.try_debit("grad", "A", 2.0, 2.0, "g-p1")
m = {"grad": {"A": mark(1.0, 2.0, pos_id=pid_p)}}  # daily loss 1.00
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-p2", marks=m)
check("env looser than owner: owner limit wins (refused)",
      not ok and "daily loss" in why)
# Env TIGHTER than owner (0.25 < 0.5): env wins, refused again.
pa = fresh(H, max_daily=0.25, max_dd=None, max_exp=None)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-p3", marks=m)
check("env tighter than owner: env limit wins (refused)",
      not ok and "daily loss" in why)
# Owner limit raised above the loss: env-tighter still applies.
with open(os.path.join(H, "owner_config.json"), "w") as _fh:
    _json.dump({"max_daily_loss_usd": 5.0, "max_drawdown_pct": 50.0,
                "max_exposure_pct": 60.0}, _fh)
pa = fresh(H, max_daily=0.25, max_dd=None, max_exp=None)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-p4", marks=m)
check("env tighter than a raised owner limit: still refused",
      not ok and "daily loss" in why)
pa = fresh(H, max_daily=5.00, max_dd=None, max_exp=None)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-p5", marks=m)
check("env and owner both above the loss: allowed", ok)
os.environ["FOREX_AGENT_HOME"] = _saved_home

# ---- invalid owner_config value: dropped, gate fails closed ----------
_saved_home = os.environ.get("FOREX_AGENT_HOME")
H = tempfile.mkdtemp(prefix="gates-badcfg-")
os.environ["FOREX_AGENT_HOME"] = H
with open(os.path.join(H, "owner_config.json"), "w") as _fh:
    _json.dump({"max_daily_loss_usd": "not-a-number",
                "max_drawdown_pct": 50.0, "max_exposure_pct": 60.0}, _fh)
pa = fresh(H, max_daily=None, max_dd=None, max_exp=None)
ok, why, _ = pa.try_debit("grad", "A", 1.0, 1.0, "g-b1")
check("invalid owner limit value: entry refused fail-closed",
      not ok and "no owner-approved limit" in why)
os.environ["FOREX_AGENT_HOME"] = _saved_home
# Restore the env threshold vars the owner-config blocks popped; the
# blocks below run against env-configured limits again.
os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "100"
os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "100"
os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "100"
os.environ["PAPER_ACCOUNT_MAX_SIMULTANEOUS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_POSITIONS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner

# ---- open position: marks become mandatory ---------------------------
# Re-bind the module to the gates-empty home: the owner-config blocks
# above reloaded it against other homes.
pa = fresh(H_EMPTY)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-e2")
check("open position + no marks: entry refused (fail-closed)",
      not ok and "marks not trustworthy" in why)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-e3",
                          marks={"grad": {"A": mark(5.0, 5.0,
                                                    age_s=3600,
                                                    pos_id=pid)}})
check("stale mark: entry refused", not ok and "stale" in why)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-e4",
                          marks={"grad": {"A": mark(5.0, 5.0, pos_id=pid)}})
check("fresh provenanced mark: entry allowed", ok)

# ---- daily loss gate --------------------------------------------------
# Position $2.00 marked at $1.00: equity 9.00, daily loss 1.00,
# drawdown 10%, exposure after a $1 entry $3.00 <= 60% of 9.00 -
# so the DAILY LOSS limit alone decides.
H = tempfile.mkdtemp(prefix="gates-daily-")
pa = fresh(H, max_daily=0.50)
_, _, pid_d = pa.try_debit("grad", "A", 2.0, 2.0, "g-d1")
m = {"grad": {"A": mark(1.0, 2.0, pos_id=pid_d)}}
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-d2", marks=m)
check("daily loss over the configured limit: refused",
      not ok and "daily loss" in why)
pa = fresh(H, max_daily=5.00)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-d3", marks=m)
check("same loss under a raised limit: allowed", ok)

# ---- drawdown gate -----------------------------------------------------
H = tempfile.mkdtemp(prefix="gates-dd-")
pa = fresh(H, max_dd=5.0)
_, _, pid_x = pa.try_debit("grad", "A", 2.0, 2.0, "g-x1")
m = {"grad": {"A": mark(1.0, 2.0, pos_id=pid_x)}}   # equity 9.00 vs peak 10 -> 10%
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-x2", marks=m)
check("drawdown over the configured limit: refused",
      not ok and "drawdown" in why)
pa = fresh(H, max_dd=50.0)
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-x3", marks=m)
check("drawdown under a raised limit: allowed (10% < 50%)", ok)

# ---- exposure gate: evaluation + reservation are atomic ---------------
H = tempfile.mkdtemp(prefix="gates-exp-")
pa = fresh(H)  # exposure cap 60% of equity 10 = 6.00
ok, why, pid1 = pa.try_debit("grad", "A", 4.0, 4.0, "g-p1")
check("first entry inside capacity ok", ok)
m = {"grad": {"A": mark(4.0, 4.0, pos_id=pid1)}}
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
m = {"grad": {"A": mark(0.5, 5.0, pos_id=pid)}}  # daily loss 4.50 >> 0.50
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-z2", marks=m)
check(" blown daily-loss blocks entries", not ok)
ok, why = pa.credit("grad", "A", 0.50, "g-z3", pos_id=pid, price_kind="current_price_simulated_fill")
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
m_stale = {"grad": {"A": mark(5.0, 5.0, pos_id=pid)}}
# ...then a tranche settles half the units before the gated entry
# reaches the lock (the concurrent-close race, simulated in order).
ok, why = pa.credit("grad", "A", 2.60, "g-q2", pos_id=pid, qty=2.5, price_kind="current_price_simulated_fill")
assert ok, why
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-q3",
                          marks=m_stale)
check("tranche between collect and lock: entry refused (qty drift)",
      not ok and "qty drift" in why)
m_now = {"grad": {"A": mark(2.6, 2.5, pos_id=pid)}}
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-q4", marks=m_now)
check("re-collected marks sized to the CURRENT units: allowed", ok)

# Reopen drift: close fully, reopen the same mint bigger; the old
# marks still name grad/A and must not bind to the new instance.
H = tempfile.mkdtemp(prefix="gates-reopen-")
pa = fresh(H)
ok, _, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-o1")
m_old = {"grad": {"A": mark(5.0, 5.0, pos_id=pid)}}
pa.credit("grad", "A", 5.0, "g-o2", pos_id=pid, price_kind="current_price_simulated_fill")
ok, _, pid2 = pa.try_debit("grad", "A", 5.0, 50.0, "g-o3",
                           marks={})
assert ok, pid2
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-o4",
                          marks=m_old)
check("reopen with different size: old marks refused (qty drift)",
      not ok and "qty drift" in why)

# Same-size reopen: quantity alone must NOT bind. The new instance
# gets a new pos_id, so the old instance's marks die with it.
H = tempfile.mkdtemp(prefix="gates-reopen-same-")
pa = fresh(H)
ok, _, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-s1")
m_old = {"grad": {"A": mark(5.0, 5.0, pos_id=pid)}}
pa.credit("grad", "A", 5.0, "g-s2", pos_id=pid, price_kind="current_price_simulated_fill")
ok, _, pid2 = pa.try_debit("grad", "A", 5.0, 5.0, "g-s3",
                           marks={})
assert ok and pid2 != pid
ok, why, _ = pa.try_debit("grad", "B", 1.0, 1.0, "g-s4",
                          marks=m_old)
check("same-qty reopen: old-instance marks refused (pos_id drift)",
      not ok and "pos_id drift" in why)
ok, why, _ = pa.try_debit(
    "grad", "B", 1.0, 1.0, "g-s5",
    marks={"grad": {"A": mark(5.0, 5.0, pos_id=pid2)}})
check("marks bound to the CURRENT instance: allowed", ok)

# Future-dated source timestamps are rejected, not trusted.
H = tempfile.mkdtemp(prefix="gates-future-")
pa = fresh(H)
ok, _, pid = pa.try_debit("grad", "A", 5.0, 5.0, "g-f1")
ok, why, _ = pa.try_debit(
    "grad", "B", 1.0, 1.0, "g-f2",
    marks={"grad": {"A": mark(5.0, 5.0, age_s=-3600,
                              pos_id=pid)}})
check("future-dated mark: entry refused", not ok)

# ---- rollover with no marks: fail closed -------------------------------
H = tempfile.mkdtemp(prefix="gates-rollover-")
pa = fresh(H)
_, _, pid_r = pa.try_debit("grad", "A", 5.0, 5.0, "g-r1")
pa.snapshot(marks={"grad": {"A": mark(5.0, 5.0, pos_id=pid_r)}})  # capture baselines
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
                          marks={"grad": {"A": mark(5.0, 5.0, pos_id=pid_r)}})
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
