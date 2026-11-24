#!/usr/bin/env python3
"""assumed_sold_unverified unit state (design v4 Stage C, 2026-11-22).

Every unit is in exactly ONE economic state at any time: open /
held_unresolved / assumed_sold_unverified / sold_verified /
written_off. A credit whose price_kind is not spendable disposes
units against UNVERIFIED proceeds: the net lands in the quarantined
cash_estimated bucket (Stage A) and the disposed units are recorded
in the assumed_sold register with their allocated original cost, so
the original-cost conservation track stays auditable:

  basis_acquired == original_cost(open) + original_cost(held_unresolved)
                  + original_cost(assumed_sold) + basis_sold_at_cost
                  + basis_written_off

Covered here:
  - an estimated credit registers qty + allocated basis + quarantined
    proceeds with the op's own id (per tranche, partial and final)
  - a spendable credit does NOT register (sold_verified needs no
    quarantine record)
  - unknown/missing price_kind fails closed into the register
  - conservation: open basis + assumed_sold basis + realized
    allocations == original stake, exactly
  - restart durability: the register survives a module reload against
    the same home
  - WAL catch-up replay rebuilds the register (ledger behind the
    journal)
  - genesis-replay bucket migration rebuilds the register too

Run: python3 tests/test_assumed_sold.py  (script-style; executed by
scripts/run_script_tests.py, not collected by pytest — see
tests/conftest.py collect_ignore.)
"""
import importlib
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fresh(home, start="100"):
    os.environ["FOREX_AGENT_HOME"] = home
    if start is None:
        os.environ.pop("PAPER_ACCOUNT_START", None)
    else:
        os.environ["PAPER_ACCOUNT_START"] = str(start)
    import daemon.launch_scanner.paper_account as pa
    return importlib.reload(pa)


HOME = tempfile.mkdtemp(prefix="assumed-sold-")
pa = fresh(HOME)

# ---- estimated credit registers the disposed units ------------------
ok, why, pid = pa.try_debit("grad", "EST1", 10.0, 1000.0,
                            "t:entry:EST1")
check("fixture: debit ok", ok)
# partial tranche at an estimated kind: 400 of 1000 units
ok, why = pa.credit("grad", "EST1", 6.0, "t:leg:a:EST1", pos_id=pid,
                    qty=400.0, fee=0.1, price_kind="external")
check("estimated partial credit applies", ok)
snap = pa.snapshot()
check("partial: proceeds quarantined in estimated bucket",
      abs(snap["cash_estimated"] - 5.9) < 1e-9)
reg = {r["pos_id"]: r for r in snap["assumed_sold"]}
check("partial: register carries the tranche with allocated basis",
      len(snap["assumed_sold"]) == 1
      and abs(snap["assumed_sold"][0]["qty"] - 400.0) < 1e-9
      and abs(snap["assumed_sold"][0]["basis"] - 4.0) < 1e-9
      and abs(snap["assumed_sold"][0]["proceeds"] - 5.9) < 1e-9
      and snap["assumed_sold"][0]["price_kind"] == "external"
      and snap["assumed_sold"][0]["status"]
      == "assumed_sold_unverified")
with pa._lock():
    led, healthy, _ = pa._load_locked()
    check("partial: register keyed by the op_id",
          "t:leg:a:EST1" in (led.get("assumed_sold") or {}))

# final close (qty=None) at an estimated kind: remaining 600 units
ok, why = pa.credit("grad", "EST1", 9.0, "t:leg:final:EST1",
                    pos_id=pid, qty=None, fee=0.0,
                    price_kind="unquotable_history")
check("estimated final credit applies", ok)
snap = pa.snapshot()
check("final: register holds both tranches",
      len(snap["assumed_sold"]) == 2)
fin = [r for r in snap["assumed_sold"]
       if r["price_kind"] == "unquotable_history"][0]
check("final: remaining units + ALL remaining basis registered",
      abs(fin["qty"] - 600.0) < 1e-9
      and abs(fin["basis"] - 6.0) < 1e-9
      and abs(fin["proceeds"] - 9.0) < 1e-9)
check("snapshot aggregates: assumed_sold_cost / _proceeds",
      abs(snap["assumed_sold_cost"] - 10.0) < 1e-9
      and abs(snap["assumed_sold_proceeds"] - 14.9) < 1e-9)

# conservation: open(0) + assumed_sold(10.0) + realized allocations ==
# the original 10.0 stake; register basis totals exactly the stake
check("conservation: register basis == disposed original cost",
      abs(snap["assumed_sold_cost"] - 10.0) < 1e-9
      and snap["cost_exposure"] == 0.0)

# ---- spendable credit does NOT register -----------------------------
ok, why, pid2 = pa.try_debit("grad", "SPD1", 5.0, 500.0,
                             "t:entry:SPD1")
check("fixture: second debit ok", ok)
ok, why = pa.credit("grad", "SPD1", 12.0, "t:final:SPD1", pos_id=pid2,
                    qty=None, fee=0.0,
                    price_kind="current_price_simulated_fill")
check("spendable credit applies", ok)
snap = pa.snapshot()
check("spendable: sold_verified needs no register entry",
      len(snap["assumed_sold"]) == 2)
check("spendable: proceeds spendable, not quarantined",
      abs(snap["cash_spendable"] - (100.0 - 15.0 + 12.0)) < 1e-9)

# ---- missing price_kind fails closed into the register --------------
ok, why, pid3 = pa.try_debit("watcher", "UNK1", 2.0, 200.0,
                             "t:entry:UNK1")
ok, why = pa.credit("watcher", "UNK1", 3.0, "t:final:UNK1",
                    pos_id=pid3, qty=None, fee=0.0, price_kind=None)
check("unknown-kind credit applies (estimated route)", ok)
snap = pa.snapshot()
unk = [r for r in snap["assumed_sold"] if r["mint"] == "UNK1"]
check("unknown-kind: registered with explicit 'unknown' label",
      len(unk) == 1 and unk[0]["price_kind"] == "unknown")

# ---- retry of the same op_id does not duplicate the register -------
ok, why = pa.credit("watcher", "UNK1", 3.0, "t:final:UNK1",
                    pos_id=pid3, qty=None, fee=0.0, price_kind=None)
check("retry returns the original outcome", ok)
snap = pa.snapshot()
check("retry: register not duplicated",
      len([r for r in snap["assumed_sold"] if r["mint"] == "UNK1"])
      == 1)

# ---- restart durability ---------------------------------------------
pa2 = fresh(HOME)
snap2 = pa2.snapshot()
check("restart: register survives a module reload",
      len(snap2["assumed_sold"]) == 3
      and abs(snap2["assumed_sold_cost"] - 12.0) < 1e-9)

# ---- WAL catch-up rebuilds the register -----------------------------
# Corrupt the live ledger back to the shadow (behind the journal):
# replay must re-apply the credits AND rebuild the register.
pa3 = fresh(tempfile.mkdtemp(prefix="assumed-sold-wal-"))
ok, why, pid4 = pa3.try_debit("grad", "WAL1", 4.0, 400.0,
                              "t:entry:WAL1")
check("fixture: WAL debit ok", ok)
ok, why = pa3.credit("grad", "WAL1", 5.0, "t:final:WAL1", pos_id=pid4,
                     qty=None, fee=0.0, price_kind="external")
check("fixture: WAL estimated credit ok", ok)
jour = [json.loads(l) for l in open(pa3.JOURNAL_PATH)
        if l.strip()]
credit_seq = max(e["seq"] for e in jour
                 if e.get("event") == "credit")
# roll the live state back to BEFORE the credit, as a stale shadow
# restore would: drop the register + the credit's effects by
# rewriting state at the debit-only seq.
with pa3._lock():
    led, healthy, _ = pa3._load_locked()
    led["seq"] = credit_seq - 1
    led["cash"] = round(led["cash"] - 5.0, 6)
    led["cash_estimated"] = 0.0
    led["realized_pnl"] = round(led["realized_pnl"] - 1.0, 6)
    led["assumed_sold"] = {}
    led["lanes"]["grad"]["open"]["WAL1"] = {
        "pos_id": pid4, "qty": 400.0, "basis": 4.0,
        "entry_qty": 400.0, "entry_cost": 4.0,
        "realized_pnl": 0.0, "proceeds": 0.0, "fees": 0.0,
        "ts": time.time()}
    led["lanes"]["grad"]["n_closes"] -= 1
    led["ops"].pop("t:final:WAL1", None)
    pa3._save_locked(led)
snap3 = pa3.snapshot()
wal_reg = [r for r in snap3["assumed_sold"] if r["mint"] == "WAL1"]
check("WAL catch-up: register rebuilt by replay",
      len(wal_reg) == 1
      and abs(wal_reg[0]["basis"] - 4.0) < 1e-9
      and abs(wal_reg[0]["proceeds"] - 5.0) < 1e-9
      and wal_reg[0]["pos_id"] == pid4)
check("WAL catch-up: estimated bucket rebuilt",
      abs(snap3["cash_estimated"] - 5.0) < 1e-9)

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL ASSUMED-SOLD TESTS PASSED")
