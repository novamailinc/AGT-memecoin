#!/usr/bin/env python3
"""Tests for the shared paper account (2026-11-22, Stages 1-1.3).

The account is the single source of truth for paper capital across
lanes. These tests prove the properties the lanes rely on:

  * conservation: cash + open allocated basis moves ONLY by net
    realized P/L (pinned against hand-computed numeric examples,
    including profitable and losing partial sales with fees); two
    racing processes can never double-spend the same dollars;
  * fail-closed: corruption, bad input and insufficient cash refuse
    debits; realized money is never dropped silently (journal);
  * caller retries: a stable op_id makes an uncertain response
    safely retryable — the original outcome returns, never a
    re-apply, even after close + reopen of the same mint;
  * durability: state survives restarts (fresh module state), a
    corrupt live file falls back to the shadow, and mid-write
    debris is swept;
  * WAL integrity: journaled mutations replay idempotently keyed
    by op_id / pos_id (a reopen never absorbs a stale credit),
    exactly one torn TRAILING journal record is tolerated and
    truncated, anything worse degrades the account (debits refused,
    credits journaled-only), and concurrent recovery converges;
  * accounting: per-lane and account-level P/L, daily counter
    rollover, book-vs-marked separation, mark freshness/provenance
    validation (stale or unprovenanced marks never count as
    liquidation equity), and day-start/peak equity baselines that
    are captured only from trustworthy marks and never reset to a
    later favorable value.

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
      and snap["degraded"] is None
      and snap["day_start_equity"] is None
      and snap["peak_equity"] is None)

# ---- debit / credit conservation -----------------------------------
ok, why, pid_a = pa.try_debit("grad", "MINT_A", 1.00, 1.00, "d-a1")
check("debit $1 ok with a pos_id", ok and pid_a == "grad/MINT_A#1")
ok, why, _ = pa.try_debit("grad", "MINT_A", 1.00, 1.00, "d-a2")
check("duplicate open mint refused", not ok and "already" in why)
ok, why, _ = pa.try_debit("ceo", "MINT_B", 9.50, 9.50, "d-b1")
check("debit beyond cash refused (fail-closed)",
      not ok and "< stake" in why)
ok, why, pid_b = pa.try_debit("ceo", "MINT_B", 9.00, 9.00, "d-b2")
check("debit $9 ok", ok and pid_b == "ceo/MINT_B#2")
snap = pa.snapshot()
check("exposure/cash/equity reconcile after debits",
      snap["cash"] == 0.0 and snap["cost_exposure"] == 10.00
      and snap["book_value"] == 10.00)

ok, why = pa.credit("grad", "MINT_A", 2.50, "c-a1", pos_id=pid_a)
check("credit $2.50 proceeds ok (pos_id pinned)", ok)
ok, why = pa.credit("grad", "MINT_A", 1.00, "c-aX")
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
ok, _, pid_a2 = pa.try_debit("grad", "MINT_A", 1.00, 1.00, "d-a3")
check("reopen after close ok with a NEW pos_id",
      ok and pid_a2 != pid_a and pid_a2 == "grad/MINT_A#4")
ok, why = pa.credit("grad", "MINT_A", 5.00, "c-stale", pos_id=pid_a)
check("stale pos_id credit refused (wrong instance), not applied",
      not ok and "stale pos_id" in why)
snap = pa.snapshot()
check("refused stale credit moved no money, position still open",
      snap["cash"] == 1.50 and snap["lanes"]["grad"]["open_count"] == 1)
ok, why = pa.credit("grad", "MINT_A", 1.00, "c-a4", pos_id=pid_a2)
check("current pos_id credit applies", ok)

# ---- op_id: uncertain-response retries ------------------------------
ok, why, pid_r = pa.try_debit("grad", "RETRY", 0.50, 0.50, "op-r1")
check("fixture: retry-test debit ok", ok)
snap_before = pa.snapshot()
ok2, why2, pid_r2 = pa.try_debit("grad", "RETRY", 0.50, 0.50, "op-r1")
check("same op_id retry returns the original outcome",
      ok2 and pid_r2 == pid_r and "retry" in why2)
snap = pa.snapshot()
check("op_id retry applied nothing twice (cash unchanged)",
      snap["cash"] == snap_before["cash"]
      and snap["lanes"]["grad"]["n_entries"] == 3)
ok, why, _ = pa.try_debit("grad", "RETRY2", 0.25, 0.25, "op-r1")
check("same op_id, different params -> conflict refused",
      not ok and "op_id" in why)
# Retry AFTER close + reopen: the old op must not touch instance 2.
ok, _ = pa.credit("grad", "RETRY", 0.60, "op-c1", pos_id=pid_r)
ok, _, pid_r3 = pa.try_debit("grad", "RETRY", 0.50, 0.50, "op-r2")
check("fixture: reopened instance distinct", ok and pid_r3 != pid_r)
ok2, why2, pid_back = pa.try_debit("grad", "RETRY", 0.50, 0.50,
                                   "op-r1")
check("uncertain-response retry after reopen returns instance 1 "
      "outcome, applies nothing", ok2 and pid_back == pid_r)
snap = pa.snapshot()
check("instance 2 untouched by the stale retry",
      snap["lanes"]["grad"]["open_count"] == 1
      and snap["cash"] == round(snap_before["cash"] + 0.60 - 0.50, 6))
ok, _ = pa.credit("grad", "RETRY", 0.55, "op-c2", pos_id=pid_r3)
ok, _ = pa.credit("grad", "RETRY", 0.55, "op-c2", pos_id=pid_r3)
snap = pa.snapshot()
check("credit retry with same op_id does not double-credit",
      ok and snap["cash"] == 2.65)

# ---- restart persistence -------------------------------------------
pa2 = fresh(HOME)  # same home, fresh module state
snap = pa2.snapshot()
check("state survives a restart (cash, open ceo position)",
      snap["cash"] == 2.65 and snap["lanes"]["ceo"]["open_count"] == 1
      and snap["realized_pnl"] == 1.65)

# ---- bad input ------------------------------------------------------
ok, why, _ = pa2.try_debit("grad", "MINT_C", 0, 1.0, "d-z1")
check("zero stake refused", not ok)
ok, why, _ = pa2.try_debit("grad", "MINT_C", -5, 1.0, "d-z2")
check("negative stake refused", not ok)
ok, why, _ = pa2.try_debit("grad", "MINT_C", 1, 0, "d-z3")
check("zero qty refused", not ok)
ok, why, _ = pa2.try_debit("", "MINT_C", 1, 1.0, "d-z4")
check("empty lane refused", not ok)
ok, why, _ = pa2.try_debit("grad", "MINT_C", 1, 1.0, "")
check("missing op_id refused", not ok and "op_id" in why)
ok, why = pa2.credit("grad", "MINT_C", -1, "c-z1")
check("negative proceeds refused", not ok)
ok, why = pa2.credit("grad", "MINT_C", 1.00, "c-z2", fee=1.50)
check("fee exceeding gross proceeds refused", not ok)
ok, why = pa2.credit("grad", "MINT_C", 1.00, "")
check("credit without op_id refused", not ok)

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
      and snap["realized_pnl"] == 1.65)

# ---- corrupt live file -> shadow restore ----------------------------
pa3 = fresh(HOME)
with open(led_path, "w") as f:
    f.write("{corrupt")
snap = pa3.snapshot()
check("corrupt live file falls back to shadow",
      snap is not None and snap["cash"] == 2.65)

# ---- both corrupt -> fail closed, credit journaled ------------------
with open(led_path, "w") as f:
    f.write("{corrupt")
with open(led_path + ".bak", "w") as f:
    f.write("{corrupt")
snap = pa3.snapshot()
check("both unreadable -> snapshot None", snap is None)
ok, why, _ = pa3.try_debit("grad", "MINT_D", 1.0, 1.0, "d-d1")
check("both unreadable -> debit refused (fail-closed)", not ok)
ok, why = pa3.credit("ceo", "MINT_B", 9.00, "c-b2")
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
        "    ok, _, _pid = pa.try_debit(sys.argv[1], 'M%%d' %% i,"
        " 1.00, 1.00, 'op-%%s-%%d' %% (sys.argv[1], i))\n"
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
ok, _, _p = pa6.try_debit("grad", "M1", 4.0, 4.0, "cap-1")
ok2, why2, _ = pa6.try_debit("grad", "M2", 2.0, 2.0, "cap-2")
check("lane cap refuses overflow", ok and not ok2 and "cap" in why2)
ok3, _, _p = pa6.try_debit("ceo", "M3", 5.0, 5.0, "cap-3")
check("lane cap is per-lane (ceo unaffected, $5 == cap ok)", ok3)
ok4, _, _p = pa6.try_debit("ceo", "M4", 0.5, 0.5, "cap-4")
check("ceo at cap now refused too", not ok4)

# ---- partial closes: hand-computed numeric examples -----------------
# Example A (profitable, with fees): buy 100 units for $10.00.
# Sell 40 @ gross $8.00, fee $0.08 -> net 7.92, allocated basis
# 4.00, realized +3.92. cash 7.92 + basis 6.00 == 0 + 10 + 3.92.
# Then sell the remaining 60 @ gross $12.00, fee $0.12 -> net
# 11.88, allocated 6.00, realized +5.88. cash 19.80 == 10 + 9.80.
HOMEA = tempfile.mkdtemp(prefix="paper-account-partial-")
paP = fresh(HOMEA, start=10.0)
ok, _, pidP = paP.try_debit("grad", "EXA", 10.0, 100.0, "p-d1")
check("fixture: 100 units for $10", ok)
ok, why = paP.credit("grad", "EXA", 8.00, "p-c1", pos_id=pidP,
                     qty=40.0, fee=0.08)
check("profitable partial sale applies", ok)
snap = paP.snapshot()
check("partial math: cash 7.92, remaining basis 6.00, "
      "realized +3.92 (hand-computed)",
      snap["cash"] == 7.92 and snap["cost_exposure"] == 6.00
      and snap["realized_pnl"] == 3.92)
check("conservation after partial: cash + basis == start + realized",
      abs(snap["cash"] + snap["cost_exposure"]
          - (10.0 + snap["realized_pnl"])) < 1e-9)
check("position still open after partial (n_closes 0)",
      snap["lanes"]["grad"]["open_count"] == 1
      and snap["lanes"]["grad"]["n_closes"] == 0)
ok, why = paP.credit("grad", "EXA", 12.00, "p-c2", pos_id=pidP,
                     fee=0.12)
check("final close of the remaining 60 units", ok)
snap = paP.snapshot()
check("final math: cash 19.80, realized +9.80 total "
      "(hand-computed)",
      snap["cash"] == 19.80 and snap["realized_pnl"] == 9.80
      and snap["cost_exposure"] == 0.0
      and snap["lanes"]["grad"]["n_closes"] == 1)
check("conservation across the whole life cycle",
      abs(snap["cash"] - (10.0 + snap["realized_pnl"])) < 1e-9)

# Example B (losing, with fees): buy 100 for $10; sell 50 @ gross
# $3.00, fee $0.03 -> net 2.97, alloc 5.00, realized -2.03; then
# close the rest @ gross $3.00 fee $0.03 -> realized -2.03 more.
HOMEB = tempfile.mkdtemp(prefix="paper-account-partial-b-")
paQ = fresh(HOMEB, start=10.0)
paQ.try_debit("grad", "EXB", 10.0, 100.0, "q-d1")
ok, _ = paQ.credit("grad", "EXB", 3.00, "q-c1", qty=50.0, fee=0.03)
snap = paQ.snapshot()
check("losing partial: cash 2.97, basis 5.00, realized -2.03 "
      "(hand-computed)",
      ok and snap["cash"] == 2.97 and snap["cost_exposure"] == 5.00
      and snap["realized_pnl"] == -2.03)
ok, _ = paQ.credit("grad", "EXB", 3.00, "q-c2", fee=0.03)
snap = paQ.snapshot()
check("losing full close: cash 5.94, realized -4.06 total",
      ok and snap["cash"] == 5.94 and snap["realized_pnl"] == -4.06)
check("conservation for the losing cycle",
      abs(snap["cash"] - (10.0 + snap["realized_pnl"])) < 1e-9)

# Example C (a tranche recovering MORE than the whole stake while
# units remain): buy 100 for $10, price 20x, sell 10 units @ gross
# $20.00 -> net 20.00, allocated basis 1.00, realized +19.00;
# remaining basis 9.00 on 90 units — never negative.
HOMEC = tempfile.mkdtemp(prefix="paper-account-partial-c-")
paR = fresh(HOMEC, start=10.0)
paR.try_debit("grad", "EXC", 10.0, 100.0, "r-d1")
ok, _ = paR.credit("grad", "EXC", 20.00, "r-c1", qty=10.0)
snap = paR.snapshot()
check("tranche recovering more than the stake: realized +19.00, "
      "remaining basis 9.00 (never negative)",
      ok and snap["cash"] == 20.00 and snap["cost_exposure"] == 9.00
      and snap["realized_pnl"] == 19.00)
check("conservation for the windfall tranche",
      abs(snap["cash"] + snap["cost_exposure"]
          - (10.0 + snap["realized_pnl"])) < 1e-9)

# Partial-close guard rails.
ok, why = paR.credit("grad", "EXC", 1.00, "r-c3", qty=999.0)
check("qty above remaining units refused", not ok)
ok, why = paR.credit("grad", "EXC", 1.00, "r-c4", qty=-2.0)
check("negative qty refused", not ok)
ok, why = paR.credit("grad", "EXC", 1.00, "r-c5", qty=45.0)
check("valid mid partial applies (45 of 90)", ok)

# ---- legacy Stage-1 position: no qty -> full close only -------------
HOMELEG = tempfile.mkdtemp(prefix="paper-account-legacy-")
paL = fresh(HOMELEG, start=10.0)
with paL._lock():
    led, healthy, _ = paL._load_locked()
    led["seq"] = 1
    led["cash"] = 8.0
    paL._lane(led, "grad")["open"]["LEG"] = {
        "stake": 2.0, "ts": time.time(), "pos_id": "grad/LEG#1"}
    led["lanes"]["grad"]["n_entries"] = 1
    paL._save_locked(led)
ok, why = paL.credit("grad", "LEG", 1.00, "l-c1", qty=1.0)
check("partial close on a legacy position refused",
      not ok and "full close only" in why)
ok, why = paL.credit("grad", "LEG", 3.00, "l-c2",
                     pos_id="grad/LEG#1")
check("full close on a legacy position settles vs stake", ok)
snap = paL.snapshot()
check("legacy settle math: cash 11.00, realized +1.00",
      snap["cash"] == 11.00 and snap["realized_pnl"] == 1.00)

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
    pa7.try_debit("grad", "CRASH1", 2.0, 2.0, "crash-d1")
    check("crash debit raises mid-write", False)
except RuntimeError:
    pass
pa7._save_locked = _real_save
snap = pa7.snapshot()  # reload path reconciles against the journal
check("crash window: journaled debit replayed (cash stays spent)",
      snap is not None and snap["cash"] == 8.0
      and snap["lanes"]["grad"]["open_count"] == 1)
# A caller retry of the crashed op must NOT double-debit: the op
# is already recorded by the replay.
ok, why, _ = pa7.try_debit("grad", "CRASH1", 2.0, 2.0, "crash-d1")
check("retry of crash-window op returns applied outcome",
      ok and "retry" in why)
snap = pa7.snapshot()
check("crash-window retry did not double-spend",
      snap["cash"] == 8.0 and snap["lanes"]["grad"]["n_entries"] == 1)

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
ok, _ = pa8.credit("grad", "CRASH1", 3.0, "crash-c1")
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
ok, _, pid1 = paA.try_debit("grad", "REOPEN", 2.0, 2.0, "ro-d1")
ok, _ = paA.credit("grad", "REOPEN", 2.0, "ro-c1", pos_id=pid1)
ok, _, pid2 = paA.try_debit("grad", "REOPEN", 2.0, 2.0, "ro-d2")
check("fixture: reopened instance has a distinct pos_id",
      ok and pid1 != pid2)
# Stale shadow: state as of just after the first debit (instance 1
# open, seq 1), journal already holds close#2 and reopen#3.
with open(paA.LEDGER_PATH) as f:
    current = json.load(f)
stale = json.loads(json.dumps(current))
stale["seq"] = 1
stale["cash"] = 8.0
stale["ops"] = {}
stale["lanes"]["grad"]["open"] = {
    "REOPEN": {"pos_id": pid1, "qty": 2.0, "basis": 2.0,
               "entry_qty": 2.0, "entry_cost": 2.0,
               "realized_pnl": 0.0, "proceeds": 0.0, "fees": 0.0,
               "ts": time.time()}}
stale["lanes"]["grad"]["n_entries"] = 1
stale["lanes"]["grad"]["n_closes"] = 0
stale["lanes"]["grad"]["realized_pnl"] = 0.0
stale["realized_pnl"] = 0.0
stale["day_realized_pnl"] = 0.0
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
ok, _, pidX = paB.try_debit("grad", "DIV", 2.0, 2.0, "dv-d1")
# Handcraft an impossible WAL entry: a credit for a pos_id that is
# not the open instance, at a future seq.
paB._journal({"event": "credit", "lane": "grad", "mint": "DIV",
              "proceeds": 2.0, "net": 2.0, "fee": 0.0,
              "seq": 2, "pos_id": "grad/DIV#999", "final": True})
snap = paB.snapshot()
check("journal/ledger pos_id divergence -> degraded",
      snap is not None and snap["degraded"] is not None
      and "divergence" in snap["degraded"])
ok, why, _ = paB.try_debit("ceo", "OTHER", 1.0, 1.0, "dv-d2")
check("degraded: debit refused", not ok and "degraded" in why)
ok, why = paB.credit("grad", "DIV", 2.0, "dv-c9", pos_id=pidX)
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
paC.try_debit("grad", "T1", 1.0, 1.0, "t-d1")
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
ok, _, _p = paC.try_debit("grad", "T2", 1.0, 1.0, "t-d2")
check("account fully usable after torn-tail recovery", ok)

# ---- torn journal: bad record MID-FILE -> degraded -------------------
HOME8 = tempfile.mkdtemp(prefix="paper-account-tornmid-")
paD = fresh(HOME8, start=10.0)
paD.try_debit("grad", "T1", 1.0, 1.0, "tm-d1")
with open(paD.JOURNAL_PATH) as f:
    lines = f.readlines()
lines.insert(1, '{"event": "debit", BROKEN\n')
with open(paD.JOURNAL_PATH, "w") as f:
    f.writelines(lines)
snap = paD.snapshot()
check("mid-file torn record -> degraded (no auto-repair)",
      snap is not None and snap["degraded"] is not None
      and "torn" in snap["degraded"])
ok, why, _ = paD.try_debit("grad", "T2", 1.0, 1.0, "tm-d2")
check("torn mid-file: debit refused", not ok and "degraded" in why)

# ---- seq hole -> degraded --------------------------------------------
HOME9 = tempfile.mkdtemp(prefix="paper-account-hole-")
paE = fresh(HOME9, start=10.0)
paE.try_debit("grad", "T1", 1.0, 1.0, "h-d1")
paE._journal({"event": "credit", "lane": "grad", "mint": "T1",
              "proceeds": 1.0, "net": 1.0, "seq": 5,
              "pos_id": "grad/T1#1", "final": True})
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
paF.try_debit("grad", "CC1", 2.0, 2.0, "cc-d1")
# Crash window again: credit journaled, state save fails.
paF._save_locked = lambda led: (_ for _ in ()).throw(
    RuntimeError("simulated crash"))
try:
    paF.credit("grad", "CC1", 3.0, "cc-c1")
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
pa9.try_debit("grad", "DOWN1", 5.0, 5.0, "mk-d1")
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

# ---- equity baselines: capture, carry, never-favorable-reset --------
# The position is down 80%; the first trustworthy mark captures the
# day baseline AT THE LOSS (6.00) and the peak.
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(1.0)}})
check("day-start baseline captured from first trustworthy mark",
      snap["day_start_equity"] is not None
      and abs(snap["day_start_equity"]["value"] - 6.0) < 1e-9
      and snap["daily_loss"] == 0.0)
check("peak equity captured",
      snap["peak_equity"] is not None
      and abs(snap["peak_equity"]["value"] - 6.0) < 1e-9)
# Later same day the mark RECOVERS: baseline must NOT reset to the
# favorable value; the peak moves up, daily loss stays vs baseline.
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(4.0)}})
check("baseline never resets to a later favorable value",
      abs(snap["day_start_equity"]["value"] - 6.0) < 1e-9)
check("peak rises with the recovery; daily loss shows the dip "
      "recovered (0 vs 9.00 equity vs 6.00 baseline? no: gain)",
      abs(snap["peak_equity"]["value"] - 9.0) < 1e-9
      and snap["daily_loss"] == 0.0)
# A dip below baseline counts as daily loss.
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(2.0)}})
check("marked equity 7.00 vs baseline 6.00? no dip -> 0; dip shows "
      "when below", snap["daily_loss"] == 0.0)
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(0.5)}})
check("daily loss vs day-start baseline (6.00 - 5.50 = 0.50)",
      abs(snap["daily_loss"] - 0.50) < 1e-9
      and abs(snap["drawdown_pct"] - (9.0 - 5.5) / 9.0 * 100) < 1e-6)
# Rollover with NO trustworthy mark: baseline carries, marked
# fields and gate inputs go None (fail-closed), nothing resets.
with open(pa9.LEDGER_PATH) as f:
    led = json.load(f)
led["day"] = "2000-01-01"
led["day_baseline"]["day"] = "2000-01-01"  # captured "yesterday"
with open(pa9.LEDGER_PATH, "w") as f:
    json.dump(led, f)
snap = fresh(HOME11).snapshot()
check("rollover without marks: baseline carries, gate inputs None",
      snap["day_start_equity"] is not None
      and snap["day_start_equity"]["day"] != snap["day"]
      and snap["daily_loss"] is None
      and snap["drawdown_pct"] is None
      and snap["marked_equity"] is None)
# First trustworthy mark of the new day captures the new baseline.
snap = pa9.snapshot(marks={"grad": {"DOWN1": mark(2.5)}})
check("new day, first trustworthy mark captures a new baseline",
      snap["day_start_equity"]["day"] == snap["day"]
      and abs(snap["day_start_equity"]["value"] - 7.5) < 1e-9
      and snap["daily_loss"] == 0.0)
check("peak still the all-time high (9.00)",
      abs(snap["peak_equity"]["value"] - 9.0) < 1e-9)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER ACCOUNT TESTS PASSED")
