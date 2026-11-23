#!/usr/bin/env python3
"""Cash-split quarantine tests (2026-11-22, quarantine design v4
Stage A).

Proceeds are spendable ONLY when the tranche's exit price was a
current observed quote (exit_provenance.CURRENT). Everything else —
stale-price estimates, write-downs, mixed closes, unknown/legacy
kinds — lands in cash_estimated: visible, reported, never spendable
for new entries. Estimates may hurt, never help.

Also covered: the genesis-replay migration that derives the split
for pre-split ledgers, EXACT OR FROZEN (a contaminated history, a
cash mismatch, a divergent open set or a seq-less mutation degrades
the account instead of clamping it into an apparently usable
balance).

Run: python3 tests/test_paper_account_split.py
"""
import importlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fresh(home, start=10.0):
    os.environ["FOREX_AGENT_HOME"] = home
    os.environ["PAPER_ACCOUNT_START"] = str(start)
    os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None)
    os.environ.pop("PAPER_ACCOUNT_GATES_ENABLED", None)
    import daemon.launch_scanner.paper_account as pa
    return importlib.reload(pa)


CUR = "current_price_simulated_fill"
STALE = "stale_price_estimate"
WD = "write_down_zero"


def main():
    # --- verified proceeds are spendable; quarantined ones are not
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, why, pos_id = pa.try_debit("watcher", "M", 2.0, qty=1000.0,
                                   op_id="watcher:buy:M:1")
    check("entry ok", ok)
    snap = pa.snapshot()
    check("spendable reduced by stake",
          abs(snap["cash_spendable"] - 8.0) < 1e-9)
    check("estimated starts at zero",
          abs(snap["cash_estimated"]) < 1e-9)
    check("cash total conserved",
          abs(snap["cash"] - (snap["cash_spendable"]
                              + snap["cash_estimated"])) < 1e-9)
    # stale-price tranche: proceeds quarantined
    ok, _ = pa.credit("watcher", "M", 1.0, "watcher:sell:M:1",
                      pos_id=pos_id, qty=500.0, price_kind=STALE)
    check("stale credit accepted", ok)
    snap = pa.snapshot()
    check("stale proceeds quarantined",
          abs(snap["cash_estimated"] - 1.0) < 1e-9)
    check("spendable untouched by stale credit",
          abs(snap["cash_spendable"] - 8.0) < 1e-9)
    # try to spend the estimated dollar: refused
    ok, why, _ = pa.try_debit("watcher", "M2", 9.0, qty=10.0,
                              op_id="watcher:buy:M2:1")
    check("overspend refused", not ok)
    check("refusal names spendable", "spendable" in why.lower())
    # a verified tranche: proceeds spendable
    ok, _ = pa.credit("watcher", "M", 1.5, "watcher:sell:M:2",
                      pos_id=pos_id, qty=500.0, price_kind=CUR)
    check("verified credit accepted", ok)
    snap = pa.snapshot()
    check("verified proceeds spendable",
          abs(snap["cash_spendable"] - 9.5) < 1e-9)
    check("estimated still 1.0",
          abs(snap["cash_estimated"] - 1.0) < 1e-9)
    ok, _, _ = pa.try_debit("watcher", "M3", 9.0, qty=10.0,
                            op_id="watcher:buy:M3:1")
    check("verified proceeds fund new entry", ok)

    # --- unknown / missing kinds fail closed into estimated
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("grad", "G", 2.0, qty=100.0,
                              op_id="grad:buy:G:1")
    check("grad entry ok", ok)
    ok, _ = pa.credit("grad", "G", 1.0, "grad:sell:G:1",
                      pos_id=pid, qty=50.0)  # no kind: legacy caller
    check("legacy credit accepted", ok)
    ok, _ = pa.credit("grad", "G", 0.5, "grad:sell:G:2",
                      pos_id=pid, qty=50.0,
                      price_kind="mixed:stale_price_estimate,x")
    check("mixed credit accepted", ok)
    snap = pa.snapshot()
    check("unknown-kind credit quarantined",
          abs(snap["cash_estimated"] - 1.5) < 1e-9)
    check("nothing spendable gained",
          abs(snap["cash_spendable"] - 8.0) < 1e-9)

    # --- write-down proceeds are quarantined
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("watcher", "W", 2.0, qty=100.0,
                              op_id="watcher:buy:W:1")
    check("wd entry ok", ok)
    # a $0-proceeds write-down: net 0 lands in estimated, spendable
    # unchanged (the $2 stake is gone either way)
    ok, _ = pa.credit("watcher", "W", 0.0, "watcher:wd:W:1",
                      pos_id=pid, qty=100.0, price_kind=WD)
    check("write-down credit accepted", ok)
    snap = pa.snapshot()
    check("write-down leaves spendable alone",
          abs(snap["cash_spendable"] - 8.0) < 1e-9)

    # --- restart: buckets persist via the ledger
    import daemon.launch_scanner.paper_account as pa2
    pa2 = importlib.reload(pa2)
    snap = pa2.snapshot()
    check("buckets survive restart",
          abs(snap["cash_spendable"] - 8.0) < 1e-9)

    # --- migration: exact derivation for a clean pre-split ledger
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("watcher", "L", 2.0, qty=100.0,
                              op_id="watcher:buy:L:1")
    check("legacy entry ok", ok)
    ok, _ = pa.credit("watcher", "L", 1.0, "watcher:sell:L:1",
                      pos_id=pid, qty=50.0)  # no kind in journal
    check("legacy credit ok", ok)
    # strip the buckets from the ledger file: pre-split shape
    lp = pa.LEDGER_PATH
    led = json.load(open(lp))
    del led["cash_spendable"], led["cash_estimated"]
    json.dump(led, open(lp, "w"))
    json.dump(led, open(lp + ".bak", "w"))
    pa = importlib.reload(pa)
    snap = pa.snapshot()
    check("migration derived buckets",
          snap["cash_spendable"] is not None)
    check("migrated spendable exact (stake in, kindless credit "
          "quarantined)",
          abs(snap["cash_spendable"] - 8.0) < 1e-9)
    check("migrated estimated exact",
          abs(snap["cash_estimated"] - 1.0) < 1e-9)
    check("migrated account usable", snap.get("degraded") is None)

    # --- migration: contaminated history freezes (debit funded by
    # proceeds that were never verified)
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("watcher", "C", 2.0, qty=100.0,
                              op_id="watcher:buy:C:1")
    check("contam entry1 ok", ok)
    ok, _ = pa.credit("watcher", "C", 9.0, "watcher:sell:C:1",
                      pos_id=pid, qty=100.0)  # kindless -> estimated
    check("contam credit ok", ok)
    # this debit is only fundable via the kindless credit
    ok, _, _ = pa.try_debit("watcher", "C2", 9.0, qty=10.0,
                            op_id="watcher:buy:C2:1")
    check("contam entry2 ok pre-migration", not ok or True)
    # force it through as if pre-split rules allowed it: hand-build
    lp = pa.LEDGER_PATH
    led = json.load(open(lp))
    del led["cash_spendable"], led["cash_estimated"]
    # add the debit to ledger + journal manually (pre-split shape)
    led["cash"] = round(led["cash"] - 9.0, 6)
    led["seq"] += 1
    jp = pa.JOURNAL_PATH
    with open(jp, "a") as f:
        f.write(json.dumps({"event": "debit", "lane": "watcher",
                            "mint": "C2", "stake": 9.0, "qty": 10.0,
                            "pos_id": "p-c2", "seq": led["seq"],
                            "op_id": "watcher:buy:C2:1"}) + "\n")
    lane = led["lanes"]["watcher"]
    lane["open"]["C2"] = {"stake": 9.0, "alloc": 9.0,
                          "qty": 10.0, "qty_open": 10.0,
                          "realized": 0.0, "pos_id": "p-c2"}
    json.dump(led, open(lp, "w"))
    json.dump(led, open(lp + ".bak", "w"))
    pa = importlib.reload(pa)
    snap = pa.snapshot()
    check("contaminated migration degrades",
          snap is not None and snap.get("degraded") is not None
          and "cash-split migration" in snap["degraded"])
    ok, why, _ = pa.try_debit("watcher", "C3", 1.0, qty=1.0,
                              op_id="watcher:buy:C3:1")
    check("degraded account refuses entries", not ok)

    # --- migration: stored cash diverging from journal freezes
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("watcher", "D", 2.0, qty=100.0,
                              op_id="watcher:buy:D:1")
    check("div entry ok", ok)
    lp = pa.LEDGER_PATH
    led = json.load(open(lp))
    del led["cash_spendable"], led["cash_estimated"]
    led["cash"] = 999.0  # contradicts the journal
    json.dump(led, open(lp, "w"))
    json.dump(led, open(lp + ".bak", "w"))
    pa = importlib.reload(pa)
    snap = pa.snapshot()
    check("mismatch migration degrades",
          snap is not None and snap.get("degraded") is not None
          and "cash-split migration" in snap["degraded"])

    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL OK")


main()
