#!/usr/bin/env python3
"""Tests for the shared paper account (2026-11-22, Stages 1-1.2).

The account is the single source of truth for paper capital across
lanes. These tests prove the properties the lanes rely on:

  * conservation: debits + cash + exposure always reconcile; two
    racing processes can never double-spend the same dollars;
  * fail-closed: corruption, bad input and insufficient cash refuse
    debits; realized money is never dropped silently (journal);
  * durability: state survives restarts (fresh module state), a
    corrupt live file falls back to the shadow, and mid-write
    debris is swept;
  * WAL integrity: journaled mutations replay idempotently keyed
    by immutable pos_id (a reopen never absorbs a stale credit),
    exactly one torn TRAILING journal record is tolerated and
    truncated, anything worse degrades the account (debits refused,
    credits journaled-only), and concurrent recovery converges;
  * accounting: per-lane and account-level P/L, daily counter
    rollover, book-vs-marked separation, mark freshness/provenance
    validation (stale or unprovenanced marks never count as
    liquidation equity).

Run: python3 tests/test_paper_account.py
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


def mark(value, age_s=0.0):
    return {"value": value, "ts": time.time() - age_s,
            "source": "test-harness"}


HOME = tempfile.mkdtemp(prefix="paper-account-test-")
pa = fresh(HOME)

# ---- create + snapshot --------------------------------------------
snap = pa.snapshot()
check("first run creates the account at $10.00 default",
      snap is not None and snap["cash"] == 10.00
      and snap["start_balance"] == 10.00
      and snap["cost_exposure"] == 0.0 and snap["book_value"] == 10.00
      and snap["degraded"] is None)

# ---- debit / credit conservation -----------------------------------
ok, why, pid_a = pa.try_debit("grad", "MINT_A", 1.00)
check("debit $1 ok with a pos_id", ok and pid_a == "grad/MINT_A#1")
ok, why, _ = pa.try_debit("grad", "MINT_A", 1.00)
check("duplicate open mint refused", not ok and "already" in why)
ok, why, _ = pa.try_debit("ceo", "MINT_B", 9.50)
check("debit beyond cash refused (fail-closed)",
      not ok and "< stake" in why)
ok, why, pid_b = pa.try_debit("ceo", "MINT_B", 9.00)
check("debit $9 ok", ok and pid_b == "ceo/MINT_B#2")
snap = pa.snapshot()
check("exposure/cash/equity reconcile after debits",
      snap["cash"] == 0.0 and snap["cost_exposure"] == 10.00
      and snap["book_value"] == 10.00)

ok, why = pa.credit("grad", "MINT_A", 2.50, pos_id=pid_a)
check("credit $2.50 proceeds ok (pos_id pinned)", ok)
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
      abs(snap["cash"] + snap["cost_exposure"]
          - (10.00 + snap["realized_pnl"])) < 1e-9)

# ---- reopen gets a fresh instance ID; stale pos_id never settles ---
ok, _, pid_a2 = pa.try_debit("grad", "MINT_A", 1.00)
check("reopen after close ok with a NEW pos_id",
      ok and pid_a2 != pid_a and pid_a2 == "grad/MINT_A#4")
ok, why = pa.credit("grad", "MINT_A", 5.00, pos_id=pid_a)
check("stale pos_id credit refused (wrong instance), not applied",
      not ok and "stale pos_id" in why)
snap = pa.snapshot()
check("refused stale credit moved no money, position still open",
      snap["cash"] == 1.50 and snap["lanes"]["grad"]["open_count"] == 1)
ok, why = pa.credit("grad", "MINT_A", 1.00, pos_id=pid_a2)
check("current pos_id credit applies", ok)

# ---- restart persistence -------------------------------------------
pa2 = fresh(HOME)  # same home, fresh module state
snap = pa2.snapshot()
check("state survives a restart (cash, open ceo position)",
      snap["cash"] == 2.50 and snap["lanes"]["ceo"]["open_count"] == 1
      and snap["realized_pnl"] == 1.50)

# ---- bad input ------------------------------------------------------
ok, why, _ = pa2.try_debit("grad", "MINT_C", 0)
check("zero stake refused", not ok)
ok, why, _ = pa2.try_debit("grad", "MINT_C", -5)
check("negative stake refused", not ok)
ok, why, _ = pa2.try_debit("", "MINT_C", 1)
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
ok, why, _ = pa3.try_debit("grad", "MINT_D", 1.0)
check("both unreadable -> debit refused (fail-closed)", not ok)
ok, why = pa3.credit("ceo", "MINT_B", 9.00)
check("both unreadable -> credit refused", not ok)
journal = open(pa3.JOURNAL_PATH).read()
check("dropped credit journaled for recovery",
      "credit_dropped" in journal and "9.0" in journal)

# ---- recovery: fix the shadow, life goes on -------------------------
pa4 = fresh(HOME)
good = pa4._new_ledger()
good["seq"] = 6  # match the journal tip so no phantom replay
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
        "    ok, _, _pid = pa.try_debit(sys.argv[1], 'M%%d' %% i, 1.00)\n"
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
      snap["cash"] == 0.0 and snap["cost_exposure"] == 10.00
      and snap["book_value"] == 10.00)
os.unlink(racer)

# ---- lane cap env knob ----------------------------------------------
HOME3 = tempfile.mkdtemp(prefix="paper-account-cap-")
pa6 = fresh(HOME3, start=100.0, cap=5.0)
check("env start balance honored",
      pa6.snapshot()["cash"] == 100.0)
ok, _, _p = pa6.try_debit("grad", "M1", 4.0)
ok2, why2, _ = pa6.try_debit("grad", "M2", 2.0)
check("lane cap refuses overflow", ok and not ok2 and "cap" in why2)
ok3, _, _p = pa6.try_debit("ceo", "M3", 5.0)
check("lane cap is per-lane (ceo unaffected, $5 == cap ok)", ok3)
ok4, _, _p = pa6.try_debit("ceo", "M4", 0.5)
check("ceo at cap now refused too", not ok4)

# ---- crash window: journaled debit, unsaved state ----------------
# Simulates a crash between the journal write and the state save:
# the journal is the write-ahead log, so recovery must replay the
# mutation — the cash must NOT come back.
HOME4 = tempfile.mkdtemp(prefix="paper-account-crash-")
pa7 = fresh(HOME4, start=10.0)
check("crash fixture: account created cleanly",
      pa7.snapshot() is not None and pa7.snapshot()["cash"] == 10.0)
_real_save = pa7._save_locked
pa7._save_locked = lambda led: (_ for _ in ()).throw(
    RuntimeError("simulated crash between journal and save"))
try:
    pa7.try_debit("grad", "CRASH1", 2.0)
    check("crash debit raises mid-write", False)
except RuntimeError:
    pass
pa7._save_locked = _real_save
snap = pa7.snapshot()  # reload path reconciles against the journal
check("crash window: journaled debit replayed (cash stays spent)",
      snap is not None and snap["cash"] == 8.0
      and snap["lanes"]["grad"]["open_count"] == 1)

# ---- stale shadow: no duplicate credit on restore ------------------
# Craft the exact failure shape: live file holds the PRE-CLOSE state
# (position open, cash pre-credit), shadow corrupt. The journaled
# close must replay EXACTLY ONCE: no resurrected position, no
# second credit.
pa8 = fresh(HOME4)
with pa8._lock():
    led, healthy, _deg = pa8._load_locked()
    assert healthy
    pre_close = json.loads(json.dumps(led))  # state before close
ok, _ = pa8.credit("grad", "CRASH1", 3.0)
check("fixture: close settles (cash 11.00)",
      ok and pa8.snapshot()["cash"] == 11.0)
with open(pa8.LEDGER_PATH, "w") as f:
    json.dump(pre_close, f)          # live = stale pre-close state
with open(pa8.LEDGER_PATH + ".bak", "w") as f:
    f.write("{corrupt")              # shadow unusable
snap = pa8.snapshot()
check("stale state: close replays once (no duplicate credit)",
      snap is not None and snap["cash"] == 11.0
      and snap["lanes"]["grad"]["n_closes"] == 1
      and snap["lanes"]["grad"]["open_count"] == 0)
# And replaying again changes nothing (idempotent).
snap = fresh(HOME4).snapshot()
check("reconciliation is idempotent across reloads",
      snap["cash"] == 11.0 and snap["lanes"]["grad"]["n_closes"] == 1)

# ---- reopen + stale shadow: a stale credit never settles instance 2
# The review case: mint closed, REOPENED (new pos_id), then a stale
# shadow from before the close is restored. Replaying the journal's
# close for instance 1 must not touch instance 2.
HOME5 = tempfile.mkdtemp(prefix="paper-account-reopen-")
paA = fresh(HOME5, start=10.0)
ok, _, pid1 = paA.try_debit("grad", "REOPEN", 2.0)
ok, _ = paA.credit("grad", "REOPEN", 2.0, pos_id=pid1)
ok, _, pid2 = paA.try_debit("grad", "REOPEN", 2.0)
check("fixture: reopened instance has a distinct pos_id",
      ok and pid1 != pid2)
# Stale shadow: state as of just after the first debit (instance 1
# open, seq 1), journal already holds close#2 and reopen#3.
with paA._lock():
    led, healthy, _ = paA._load_locked()
with open(paA.LEDGER_PATH) as f:
    current = json.load(f)
stale = json.loads(json.dumps(current))
stale["seq"] = 1
stale["cash"] = 8.0
stale["lanes"]["grad"]["open"] = {
    "REOPEN": {"stake": 2.0, "ts": time.time(), "pos_id": pid1}}
stale["lanes"]["grad"]["n_entries"] = 1
stale["lanes"]["grad"]["n_closes"] = 0
with open(paA.LEDGER_PATH, "w") as f:
    json.dump(stale, f)
with open(paA.LEDGER_PATH + ".bak", "w") as f:
    f.write("{corrupt")
snap = paA.snapshot()
check("reopen replay: close#2 settles instance 1, reopen#3 opens "
      "instance 2, exactly one position, no doubled money",
      snap is not None and snap["cash"] == 8.0
      and snap["lanes"]["grad"]["open_count"] == 1
      and snap["lanes"]["grad"]["n_closes"] == 1
      and snap["degraded"] is None)
snap = fresh(HOME5).snapshot()
check("reopen replay idempotent across reloads",
      snap["cash"] == 8.0 and snap["lanes"]["grad"]["n_closes"] == 1)

# ---- pos_id divergence in the journal -> degraded -------------------
HOME6 = tempfile.mkdtemp(prefix="paper-account-diverge-")
paB = fresh(HOME6, start=10.0)
ok, _, pidX = paB.try_debit("grad", "DIV", 2.0)
# Handcraft an impossible WAL entry: a credit for a pos_id that is
# not the open instance, at a future seq.
with paB._lock():
    led, healthy, _ = paB._load_locked()
    tip = led["seq"]
paB._journal({"event": "credit", "lane": "grad", "mint": "DIV",
              "proceeds": 2.0, "seq": tip + 1,
              "pos_id": "grad/DIV#999"})
snap = paB.snapshot()
check("journal/ledger pos_id divergence -> degraded",
      snap is not None and snap["degraded"] is not None
      and "divergence" in snap["degraded"])
ok, why, _ = paB.try_debit("ceo", "OTHER", 1.0)
check("degraded: debit refused", not ok and "degraded" in why)
ok, why = paB.credit("grad", "DIV", 2.0, pos_id=pidX)
check("degraded: credit journaled-only, not applied", not ok)
journal = open(paB.JOURNAL_PATH).read()
check("degraded credit is in the WAL for manual reconciliation",
      "credit_degraded" in journal)
snap = paB.snapshot()
check("degraded credit moved no money",
      snap["cash"] == 8.0 and snap["lanes"]["grad"]["open_count"] == 1)

# ---- torn journal: one bad TRAILING line tolerated + truncated ------
HOME7 = tempfile.mkdtemp(prefix="paper-account-torn-")
paC = fresh(HOME7, start=10.0)
paC.try_debit("grad", "T1", 1.0)
with open(paC.JOURNAL_PATH, "a") as f:
    f.write('{"event": "debit", "lane": "grad", "min')  # crash mid-append
snap = paC.snapshot()
check("torn trailing journal record tolerated (crash signature)",
      snap is not None and snap["cash"] == 9.0
      and snap["degraded"] is None)
tail_ok = True
with open(paC.JOURNAL_PATH) as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                json.loads(line)
            except ValueError:
                tail_ok = False
check("torn tail truncated under the lock (every line parses)",
      tail_ok)
ok, _, _p = paC.try_debit("grad", "T2", 1.0)
check("account fully usable after torn-tail recovery", ok)

# ---- torn journal: bad record MID-FILE -> degraded -------------------
HOME8 = tempfile.mkdtemp(prefix="paper-account-tornmid-")
paD = fresh(HOME8, start=10.0)
paD.try_debit("grad", "T1", 1.0)
with open(paD.JOURNAL_PATH) as f:
    lines = f.readlines()
lines.insert(1, '{"event": "debit", BROKEN\n')
with open(paD.JOURNAL_PATH, "w") as f:
    f.writelines(lines)
snap = paD.snapshot()
check("mid-file torn record -> degraded (no auto-repair)",
      snap is not None and snap["degraded"] is not None
      and "torn" in snap["degraded"])
ok, why, _ = paD.try_debit("grad", "T2", 1.0)
check("torn mid-file: debit refused", not ok and "degraded" in why)

# ---- seq hole -> degraded --------------------------------------------
HOME9 = tempfile.mkdtemp(prefix="paper-account-hole-")
paE = fresh(HOME9, start=10.0)
paE.try_debit("grad", "T1", 1.0)
paE._journal({"event": "credit", "lane": "grad", "mint": "T1",
              "proceeds": 1.0, "seq": 5, "pos_id": "grad/T1#1"})
snap = paE.snapshot()
check("journal seq hole (1 -> 5) -> degraded",
      snap is not None and snap["degraded"] is not None
      and "seq violation" in snap["degraded"])

# ---- concurrent recovery converges -----------------------------------
# Two processes hit a crashed account (journal ahead of state) at
# the same time; both must converge on the same settled state with
# no duplicate credit.
HOME10 = tempfile.mkdtemp(prefix="paper-account-concur-")
paF = fresh(HOME10, start=10.0)
paF.try_debit("grad", "CC1", 2.0)
# Crash window again: credit journaled, state save fails.
paF._save_locked = lambda led: (_ for _ in ()).throw(
    RuntimeError("simulated crash"))
try:
    paF.credit("grad", "CC1", 3.0)
except RuntimeError:
    pass
del paF  # state on disk: pre-close ledger, journal holds the close
reader = os.path.join(os.path.dirname(__file__), "..", "tests",
                      "_paper_account_reader.py")
with open(reader, "w") as f:
    f.write(
        "import os, sys, json\n"
        "sys.path.insert(0, %r)\n"
        "import daemon.launch_scanner.paper_account as pa\n"
        "s = pa.snapshot()\n"
        "print(json.dumps({'cash': s['cash'],"
        " 'closes': s['lanes']['grad']['n_closes']}))\n"
        % os.path.abspath(os.path.join(os.path.dirname(__file__),
                                       "..")))
env = dict(os.environ)
env["FOREX_AGENT_HOME"] = HOME10
procs = [subprocess.Popen([sys.executable, reader],
                          stdout=subprocess.PIPE, env=env, text=True)
         for _ in range(2)]
outs = [json.loads(p.communicate()[0].strip()) for p in procs]
os.unlink(reader)
check("concurrent recovery: both readers converge on cash 11.00, "
      "one close", all(o == {"cash": 11.0, "closes": 1}
                       for o in outs))
paG = fresh(HOME10)
snap = paG.snapshot()
check("concurrent recovery: final state settled exactly once",
      snap["cash"] == 11.0 and snap["lanes"]["grad"]["n_closes"] == 1
      and snap["degraded"] is None)

# ---- marks: book value vs marked equity, freshness/provenance ------
HOME11 = tempfile.mkdtemp(prefix="paper-account-marks-")
pa9 = fresh(HOME11, start=10.0)
pa9.try_debit("grad", "DOWN1", 5.0)
snap = pa9.snapshot()
check("no marks: marked fields None, status unavailable",
      snap["marked_equity"] is None and snap["marked_exposure"] is None
      and snap["unrealized_pnl"] is None
      and snap["marks_status"] == "unavailable")
check("book value ignores the unrealized loss (10.00)",
      snap["book_value"] == 10.00 and snap["cost_exposure"] == 5.0)
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(1.0)}})
check("marked equity shows the -80% unrealized loss (6.00)",
      snap["marked_equity"] == 6.0 and snap["marked_exposure"] == 1.0
      and snap["unrealized_pnl"] == -4.0
      and snap["marks_status"] == "ok")
snap = pa9.snapshot(marks={"grad": {}})
check("missing mark: marked fields None (gate must fail closed, "
      "never fall back to cost)",
      snap["marked_equity"] is None
      and "no mark" in snap["marks_status"])
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(1.0, age_s=3600)}})
check("stale mark (1h old, ttl 120s) rejected: marked fields None",
      snap["marked_equity"] is None
      and "stale" in snap["marks_status"])
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(1.0, age_s=-3600)}})
check("future-dated mark beyond ttl rejected too",
      snap["marked_equity"] is None
      and "stale" in snap["marks_status"])
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(float("nan"))}})
check("NaN mark rejected",
      snap["marked_equity"] is None
      and "invalid value" in snap["marks_status"])
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(float("inf"))}})
check("infinite mark rejected",
      snap["marked_equity"] is None
      and "invalid value" in snap["marks_status"])
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(-0.5)}})
check("negative mark rejected",
      snap["marked_equity"] is None
      and "invalid value" in snap["marks_status"])
snap = pa9.snapshot(marks={"grad": {"DOWN1": 1.0}})
check("bare float mark rejected (provenance required)",
      snap["marked_equity"] is None
      and "malformed" in snap["marks_status"])
nosrc = mark(1.0)
del nosrc["source"]
snap = pa9.snapshot(marks={"grad": {"DOWN1": nosrc}})
check("mark without provenance rejected",
      snap["marked_equity"] is None
      and "no provenance" in snap["marks_status"])
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(1.0)}})
check("valid fresh mark accepted again after the rejects",
      snap["marks_status"] == "ok" and snap["marked_equity"] == 6.0)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER ACCOUNT TESTS PASSED")
