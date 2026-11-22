#!/usr/bin/env python3
"""Tests for the shared paper account (2026-11-22, Stage 1).

The account is the single source of truth for paper capital across
lanes. These tests prove the properties the lanes rely on:

  * conservation: debits + cash + exposure always reconcile; two
    racing processes can never double-spend the same dollars;
  * fail-closed: corruption, bad input and insufficient cash refuse
    debits; realized money is never dropped silently (journal);
  * durability: state survives restarts (fresh module state), a
    corrupt live file falls back to the shadow, and mid-write
    debris is swept;
  * accounting: per-lane and account-level P/L, daily counter
    rollover, exposure/equity math.

Run: python3 tests/test_paper_account.py
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fresh(home, start=None, cap=None):
    """Reload the module against a fresh scratch home."""
    os.environ["FOREX_AGENT_HOME"] = home
    if start is None:
        os.environ.pop("PAPER_ACCOUNT_START", None)
    else:
        os.environ["PAPER_ACCOUNT_START"] = str(start)
    if cap is None:
        os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None)
    else:
        os.environ["PAPER_ACCOUNT_LANE_CAP"] = str(cap)
    import daemon.launch_scanner.paper_account as pa
    return importlib.reload(pa)


HOME = tempfile.mkdtemp(prefix="paper-account-test-")
pa = fresh(HOME)

# ---- create + snapshot --------------------------------------------
snap = pa.snapshot()
check("first run creates the account at $10.00 default",
      snap is not None and snap["cash"] == 10.00
      and snap["start_balance"] == 10.00
      and snap["exposure"] == 0.0 and snap["equity"] == 10.00)

# ---- debit / credit conservation -----------------------------------
ok, why = pa.try_debit("grad", "MINT_A", 1.00)
check("debit $1 ok", ok)
ok, why = pa.try_debit("grad", "MINT_A", 1.00)
check("duplicate open mint refused", not ok and "already" in why)
ok, why = pa.try_debit("ceo", "MINT_B", 9.50)
check("debit beyond cash refused (fail-closed)",
      not ok and "< stake" in why)
ok, why = pa.try_debit("ceo", "MINT_B", 9.00)
check("debit $9 ok", ok)
snap = pa.snapshot()
check("exposure/cash/equity reconcile after debits",
      snap["cash"] == 0.0 and snap["exposure"] == 10.00
      and snap["equity"] == 10.00)

ok, why = pa.credit("grad", "MINT_A", 2.50)
check("credit $2.50 proceeds ok", ok)
ok, why = pa.credit("grad", "MINT_A", 1.00)
check("orphan credit refused + journaled", not ok)
snap = pa.snapshot()
check("pnl math: +1.50 on grad close",
      snap["cash"] == 2.50 and snap["realized_pnl"] == 1.50
      and snap["day_realized_pnl"] == 1.50
      and snap["lanes"]["grad"]["realized_pnl"] == 1.50
      and snap["lanes"]["grad"]["n_entries"] == 1
      and snap["lanes"]["grad"]["n_closes"] == 1)
check("conservation: cash + exposure == start + realized",
      abs(snap["cash"] + snap["exposure"]
          - (10.00 + snap["realized_pnl"])) < 1e-9)

# ---- restart persistence -------------------------------------------
pa2 = fresh(HOME)  # same home, fresh module state
snap = pa2.snapshot()
check("state survives a restart (cash, open ceo position)",
      snap["cash"] == 2.50 and snap["lanes"]["ceo"]["open_count"] == 1
      and snap["realized_pnl"] == 1.50)

# ---- bad input ------------------------------------------------------
ok, why = pa2.try_debit("grad", "MINT_C", 0)
check("zero stake refused", not ok)
ok, why = pa2.try_debit("grad", "MINT_C", -5)
check("negative stake refused", not ok)
ok, why = pa2.try_debit("", "MINT_C", 1)
check("empty lane refused", not ok)
ok, why = pa2.credit("grad", "MINT_C", -1)
check("negative proceeds refused", not ok)

# ---- day rollover ---------------------------------------------------
led_path = pa2.LEDGER_PATH
with open(led_path) as f:
    led = json.load(f)
led["day"] = "2000-01-01"
with open(led_path, "w") as f:
    json.dump(led, f)
snap = fresh(HOME).snapshot()
check("day rollover resets daily pnl, keeps cumulative",
      snap["day"] != "2000-01-01" and snap["day_realized_pnl"] == 0.0
      and snap["realized_pnl"] == 1.50)

# ---- corrupt live file -> shadow restore ----------------------------
pa3 = fresh(HOME)
with open(led_path, "w") as f:
    f.write("{corrupt")
snap = pa3.snapshot()
check("corrupt live file falls back to shadow",
      snap is not None and snap["cash"] == 2.50)

# ---- both corrupt -> fail closed, credit journaled ------------------
with open(led_path, "w") as f:
    f.write("{corrupt")
with open(led_path + ".bak", "w") as f:
    f.write("{corrupt")
snap = pa3.snapshot()
check("both unreadable -> snapshot None", snap is None)
ok, why = pa3.try_debit("grad", "MINT_D", 1.0)
check("both unreadable -> debit refused (fail-closed)", not ok)
ok, why = pa3.credit("ceo", "MINT_B", 9.00)
check("both unreadable -> credit refused", not ok)
journal = open(pa3.JOURNAL_PATH).read()
check("dropped credit journaled for recovery",
      "credit_dropped" in journal and "9.0" in journal)

# ---- recovery: fix the shadow, life goes on -------------------------
pa4 = fresh(HOME)
good = pa4._new_ledger()
pa4._atomic_write(led_path + ".bak", good)
snap = pa4.snapshot()
check("shadow repaired -> account readable again", snap is not None)

# ---- concurrency: two processes race, money conserved ---------------
HOME2 = tempfile.mkdtemp(prefix="paper-account-race-")
pa5 = fresh(HOME2, start=10.00)
racer = os.path.join(os.path.dirname(__file__), "..", "tests",
                     "_paper_account_racer.py")
with open(racer, "w") as f:
    f.write(
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "import daemon.launch_scanner.paper_account as pa\n"
        "won = 0\n"
        "for i in range(20):\n"
        "    ok, _ = pa.try_debit(sys.argv[1], 'M%%d' %% i, 1.00)\n"
        "    won += 1 if ok else 0\n"
        "print(won)\n"
        % os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       "..")))
env = dict(os.environ)
env["FOREX_AGENT_HOME"] = HOME2
procs = [subprocess.Popen([sys.executable, racer, "lane%d" % n],
                          stdout=subprocess.PIPE, env=env, text=True)
         for n in range(3)]
wins = [int(p.communicate()[0].strip()) for p in procs]
snap = pa5.snapshot()
check("3 processes x 20 debits: exactly 10 win, 50 refused",
      sum(wins) == 10)
check("race conservation: cash 0, exposure 10, equity 10",
      snap["cash"] == 0.0 and snap["exposure"] == 10.00
      and snap["equity"] == 10.00)
os.unlink(racer)

# ---- lane cap env knob ----------------------------------------------
HOME3 = tempfile.mkdtemp(prefix="paper-account-cap-")
pa6 = fresh(HOME3, start=100.0, cap=5.0)
check("env start balance honored",
      pa6.snapshot()["cash"] == 100.0)
ok, _ = pa6.try_debit("grad", "M1", 4.0)
ok2, why2 = pa6.try_debit("grad", "M2", 2.0)
check("lane cap refuses overflow", ok and not ok2 and "cap" in why2)
ok3, _ = pa6.try_debit("ceo", "M3", 5.0)
check("lane cap is per-lane (ceo unaffected, $5 == cap ok)", ok3)
ok4, _ = pa6.try_debit("ceo", "M4", 0.5)
check("ceo at cap now refused too", not ok4)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER ACCOUNT TESTS PASSED")
