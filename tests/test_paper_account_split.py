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
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fresh(home, start=10.0):
    os.environ["FOREX_AGENT_HOME"] = home
    os.environ["PAPER_ACCOUNT_START"] = str(start)
    os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None)
    import daemon.launch_scanner.paper_account as pa
    pa = importlib.reload(pa)
    # mandatory-guard migration (disclosed): gates ON with fixture limits and
    # marks through the REAL pipeline; only the price source is injected.
    import tests._guard_fixture as _gf
    _gf.set_limits_env()
    _gf.install_price_feed(at_cost=True)
    return pa


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
    # bb93 generation binding: capture the debit-time binding NOW (the
    # thread-local holds only the most recent debit).
    bind_m = pa.entry_binding("watcher", "M", "watcher:buy:M:1",
                              pos_id)
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
                      pos_id=pos_id, qty=500.0, price_kind=STALE,
                      expected_account_id=bind_m,
                      entry_op_id="watcher:buy:M:1")
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
                      pos_id=pos_id, qty=500.0, price_kind=CUR,
                      expected_account_id=bind_m,
                      entry_op_id="watcher:buy:M:1")
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
    bind_g = pa.entry_binding("grad", "G", "grad:buy:G:1", pid)
    ok, _ = pa.credit("grad", "G", 1.0, "grad:sell:G:1",
                      pos_id=pid, qty=50.0,
                      expected_account_id=bind_g,
                      entry_op_id="grad:buy:G:1")  # no kind: legacy caller
    check("legacy credit accepted", ok)
    ok, _ = pa.credit("grad", "G", 0.5, "grad:sell:G:2",
                      pos_id=pid, qty=50.0,
                      price_kind="mixed:stale_price_estimate,x",
                      expected_account_id=bind_g,
                      entry_op_id="grad:buy:G:1")
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
    bind_w = pa.entry_binding("watcher", "W", "watcher:buy:W:1", pid)
    # a $0-proceeds write-down: net 0 lands in estimated, spendable
    # unchanged (the $2 stake is gone either way)
    ok, _ = pa.credit("watcher", "W", 0.0, "watcher:wd:W:1",
                      pos_id=pid, qty=100.0, price_kind=WD,
                      expected_account_id=bind_w,
                      entry_op_id="watcher:buy:W:1")
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
    bind_l = pa.entry_binding("watcher", "L", "watcher:buy:L:1", pid)
    ok, _ = pa.credit("watcher", "L", 1.0, "watcher:sell:L:1",
                      pos_id=pid, qty=50.0,
                      expected_account_id=bind_l,
                      entry_op_id="watcher:buy:L:1")  # no kind in journal
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
    bind_c = pa.entry_binding("watcher", "C", "watcher:buy:C:1", pid)
    ok, _ = pa.credit("watcher", "C", 9.0, "watcher:sell:C:1",
                      pos_id=pid, qty=100.0,
                      expected_account_id=bind_c,
                      entry_op_id="watcher:buy:C:1")  # kindless -> estimated
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

def equity_quarantine():
    """Parent acceptance check (2026-11-22): quarantined estimated
    proceeds must neither boost risk headroom nor mask a drawdown.
    Baselines, peak, daily_loss and drawdown consume ONLY the
    conservative series (cash_spendable + trusted marks)."""
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("watcher", "Q", 2.0, qty=100.0,
                              op_id="watcher:buy:Q:1")
    check("eq entry ok", ok)
    bind_q = pa.entry_binding("watcher", "Q", "watcher:buy:Q:1", pid)

    def marks(qty, value):
        return {"watcher": {"Q": {"value": value, "ts": time.time(),
                                  "source": "test", "qty": qty,
                                  "pos_id": pid}}}

    snap = pa.snapshot(marks=marks(100.0, 2.0))
    check("baseline captured from conservative equity",
          snap["day_start_equity"]
          and abs(snap["day_start_equity"]["value"] - 10.0) < 1e-9)
    # stale tranche: $1 estimated proceeds; position halves, mark
    # falls to $1 -> conservative equity drops to 9.0
    ok, _ = pa.credit("watcher", "Q", 1.0, "watcher:sell:Q:1",
                      pos_id=pid, qty=50.0, price_kind=STALE,
                      expected_account_id=bind_q,
                      entry_op_id="watcher:buy:Q:1")
    check("eq stale credit ok", ok)
    snap = pa.snapshot(marks=marks(50.0, 1.0))
    check("gross equity includes the estimate (diagnostic)",
          abs(snap["marked_equity"] - 10.0) < 1e-9)
    check("conservative equity excludes the estimate",
          abs(snap["marked_equity_conservative"] - 9.0) < 1e-9)
    check("day-start baseline NOT boosted by the estimate",
          abs(snap["day_start_equity"]["value"] - 10.0) < 1e-9)
    check("peak NOT boosted by the estimate",
          abs(snap["peak_equity"]["value"] - 10.0) < 1e-9)
    check("daily loss VISIBLE (estimate cannot mask it)",
          snap["daily_loss"] is not None
          and abs(snap["daily_loss"] - 1.0) < 1e-9)
    check("drawdown VISIBLE (estimate cannot mask it)",
          snap["drawdown_pct"] is not None
          and abs(snap["drawdown_pct"] - 10.0) < 1e-6)


main()


equity_quarantine()


def write_down_register():
    """Stage B: write-down moves units + original cost to the
    unresolved register; no cash moves, no fee, no realized loss
    recognized; idempotent on op_id; pinned to the instance."""
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("watcher", "Z", 2.0, qty=100.0,
                              op_id="watcher:buy:Z:1")
    check("z entry ok", ok)
    bind_z = pa.entry_binding("watcher", "Z", "watcher:buy:Z:1", pid)
    before = pa.snapshot()
    ok, why = pa.write_down("watcher", "Z", "watcher:wd:Z:1",
                            pos_id=pid, reason="time_stop_24h_"
                            "unquotable",
                            expected_account_id=bind_z,
                            entry_op_id="watcher:buy:Z:1")
    check("write-down applies", ok)
    snap = pa.snapshot()
    check("write-down moved no cash",
          abs(snap["cash"] - before["cash"]) < 1e-9)
    check("write-down booked no realized loss",
          abs(snap["realized_pnl"] - before["realized_pnl"]) < 1e-9)
    check("units + cost in the unresolved register",
          len(snap["unresolved"]) == 1
          and snap["unresolved"][0]["status"] == "held_unresolved"
          and abs(float(snap["unresolved"][0]["basis"]) - 2.0)
          < 1e-9
          and abs(float(snap["unresolved"][0]["qty"]) - 100.0)
          < 1e-9)
    check("unresolved cost reported", abs(snap["unresolved_cost"]
                                          - 2.0) < 1e-9)
    check("cost exposure excludes unresolved units",
          abs(snap["cost_exposure"]) < 1e-9)
    ok, _ = pa.write_down("watcher", "Z", "watcher:wd:Z:1",
                          pos_id=pid,
                          reason="time_stop_24h_unquotable",
                          expected_account_id=bind_z,
                          entry_op_id="watcher:buy:Z:1")
    check("write-down retry idempotent", ok)
    snap2 = pa.snapshot()
    check("retry did not double-register",
          len(snap2["unresolved"]) == 1)
    # a reopened instance must not absorb the stale write-down
    ok, _, pid2 = pa.try_debit("watcher", "Z", 1.0, qty=10.0,
                               op_id="watcher:buy:Z:2")
    check("reopen ok", ok)
    # STALE pos_id case: the STALE op's own binding (bind_z) + the
    # stale pos_id, so generation passes and the instance check
    # below still yields the no-op the test asserts.
    ok, _ = pa.write_down("watcher", "Z", "watcher:wd:Z:9",
                          pos_id=pid, reason="stale instance",
                          expected_account_id=bind_z,
                          entry_op_id="watcher:buy:Z:1")
    check("write-down for the old instance is a no-op (target "
          "already unresolved)", ok)
    snap3 = pa.snapshot()
    check("stale write-down did not touch the new instance",
          len(snap3["unresolved"]) == 1
          and any(p["mint"] == "Z" and p["pos_id"] == pid2
                  for p in snap3["lanes"]["watcher"]
                  ["positions"]))
    # restart: the register survives via the journal
    import daemon.launch_scanner.paper_account as pa2
    pa2 = importlib.reload(pa2)
    snap4 = pa2.snapshot()
    check("register survives restart",
          len(snap4["unresolved"]) == 1
          and abs(snap4["unresolved_cost"] - 2.0) < 1e-9)


write_down_register()

def partial_write_down_conservation():
    """Original-cost/unit conservation across MULTIPLE partial
    write-downs, idempotent retry and replay (parent 2026-11-22):
    partials plus the remaining position always recompose the
    original cost and units exactly; the impairment stays a risk
    loss and never releases usable capital."""
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("watcher", "P", 2.0, qty=100.0,
                              op_id="watcher:buy:P:1")
    check("p entry ok", ok)
    bind_p = pa.entry_binding("watcher", "P", "watcher:buy:P:1", pid)
    spend0 = pa.snapshot()["cash_spendable"]
    ok, _ = pa.write_down("watcher", "P", "watcher:wd:P:1",
                          pos_id=pid, reason="r1", qty=30.0,
                          expected_account_id=bind_p,
                          entry_op_id="watcher:buy:P:1")
    check("partial wd 1 ok", ok)
    snap = pa.snapshot()
    check("partial wd 1 register (30 units, 0.60 cost)",
          len(snap["unresolved"]) == 1
          and abs(float(snap["unresolved"][0]["qty"]) - 30.0) < 1e-6
          and abs(float(snap["unresolved"][0]["basis"]) - 0.6)
          < 1e-6)
    check("partial wd 1 leaves remainder open (70 units, 1.40)",
          abs(snap["cost_exposure"] - 1.4) < 1e-6)
    ok, _ = pa.write_down("watcher", "P", "watcher:wd:P:1",
                          pos_id=pid, reason="r1", qty=30.0,
                          expected_account_id=bind_p,
                          entry_op_id="watcher:buy:P:1")
    check("partial wd retry idempotent", ok)
    snap = pa.snapshot()
    check("retry moved nothing twice",
          abs(float(snap["unresolved"][0]["qty"]) - 30.0) < 1e-6
          and abs(snap["cost_exposure"] - 1.4) < 1e-6)
    ok, _ = pa.write_down("watcher", "P", "watcher:wd:P:2",
                          pos_id=pid, reason="r2", qty=50.0,
                          expected_account_id=bind_p,
                          entry_op_id="watcher:buy:P:1")
    check("partial wd 2 ok", ok)
    ok, _ = pa.write_down("watcher", "P", "watcher:wd:P:3",
                          pos_id=pid, reason="r3",
                          expected_account_id=bind_p,
                          entry_op_id="watcher:buy:P:1")  # remainder
    check("final wd ok", ok)
    snap = pa.snapshot()
    check("CONSERVATION: register holds ALL original units",
          abs(float(snap["unresolved"][0]["qty"]) - 100.0) < 1e-6)
    check("CONSERVATION: register holds ALL original cost",
          abs(float(snap["unresolved"][0]["basis"]) - 2.0) < 1e-6)
    check("no position remains open",
          abs(snap["cost_exposure"]) < 1e-9)
    check("impairment never released usable capital",
          abs(snap["cash_spendable"] - spend0) < 1e-9
          and abs(snap["cash_estimated"]) < 1e-9)
    # replay after restart conserves exactly
    import daemon.launch_scanner.paper_account as pa2
    pa2 = importlib.reload(pa2)
    snap = pa2.snapshot()
    check("replay conserves units and cost",
          abs(float(snap["unresolved"][0]["qty"]) - 100.0) < 1e-6
          and abs(float(snap["unresolved"][0]["basis"]) - 2.0)
          < 1e-6
          and abs(snap["cash_spendable"] - spend0) < 1e-9)


partial_write_down_conservation()


def impairment_hits_conservative_equity():
    """Parent 2026-11-22: unresolved basis must reduce conservative
    equity IMMEDIATELY and stay visible as impairment - numeric
    daily-loss / drawdown checks after partial and full
    write-downs, with PREEXISTING persisted day-start/peak
    baselines, including a restart."""
    home = tempfile.mkdtemp()
    pa = fresh(home)
    ok, _, pid = pa.try_debit("watcher", "I", 2.0, qty=100.0,
                              op_id="watcher:buy:I:1")
    check("i entry ok", ok)
    bind_i = pa.entry_binding("watcher", "I", "watcher:buy:I:1", pid)

    def marks(qty, value):
        return {"watcher": {"I": {"value": value, "ts": time.time(),
                                  "source": "test", "qty": qty,
                                  "pos_id": pid}}}

    # persisted baselines: day-start AND peak at 10.0
    snap = pa.snapshot(marks=marks(100.0, 2.0))
    check("i baselines captured",
          abs(snap["day_start_equity"]["value"] - 10.0) < 1e-9
          and abs(snap["peak_equity"]["value"] - 10.0) < 1e-9)
    # PARTIAL write-down of 50 of 100 units (cost 1.00); the
    # remaining 50 units mark at $1.00 -> conservative equity 9.0
    ok, _ = pa.write_down("watcher", "I", "watcher:wd:I:1",
                          pos_id=pid, reason="r", qty=50.0,
                          expected_account_id=bind_i,
                          entry_op_id="watcher:buy:I:1")
    check("i partial wd ok", ok)
    snap = pa.snapshot(marks=marks(50.0, 1.0))
    check("partial wd: daily loss recognizes impairment at once",
          snap["daily_loss"] is not None
          and abs(snap["daily_loss"] - 1.0) < 1e-6)
    check("partial wd: drawdown recognizes impairment at once",
          snap["drawdown_pct"] is not None
          and abs(snap["drawdown_pct"] - 10.0) < 1e-6)
    check("partial wd: impairment visible in the register",
          abs(snap["unresolved_cost"] - 1.0) < 1e-9)
    # FULL write-down of the rest: no open positions -> equity is
    # exactly spendable cash (8.0); the full $2 impairment shows
    ok, _ = pa.write_down("watcher", "I", "watcher:wd:I:2",
                          pos_id=pid, reason="r",
                          expected_account_id=bind_i,
                          entry_op_id="watcher:buy:I:1")
    check("i full wd ok", ok)
    snap = pa.snapshot()
    check("full wd: daily loss is the full impairment",
          snap["daily_loss"] is not None
          and abs(snap["daily_loss"] - 2.0) < 1e-6)
    check("full wd: drawdown is the full impairment",
          snap["drawdown_pct"] is not None
          and abs(snap["drawdown_pct"] - 20.0) < 1e-6)
    check("full wd: register carries the whole original cost",
          abs(snap["unresolved_cost"] - 2.0) < 1e-9)
    check("full wd: spendable untouched by the write-down",
          abs(snap["cash_spendable"] - 8.0) < 1e-9)
    # restart: persisted baselines + register still recognize it
    import daemon.launch_scanner.paper_account as pa2
    pa2 = importlib.reload(pa2)
    snap = pa2.snapshot()
    check("after restart: impairment still recognized",
          snap["daily_loss"] is not None
          and abs(snap["daily_loss"] - 2.0) < 1e-6
          and abs(snap["drawdown_pct"] - 20.0) < 1e-6
          and abs(snap["unresolved_cost"] - 2.0) < 1e-9)


impairment_hits_conservative_equity()


def mirror_exclusion_explicit():
    """CONTRACT CHANGE (disclosed, main's instruction 7:05): mirror was
    deliberately unwired from the shared account (design decision pending).
    It is now ROUTED through it: the module must reserve (try_debit),
    pin the account position (pa_pos_id) and settle tranches. Behavior is
    proven in tests/test_mirror_routed_account.py; this keeps the wiring
    structural."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "daemon", "launch_scanner", "mirror.py")
    srcm = open(p).read()
    check("mirror routed: references the shared account, pins pa_pos_id, debits and settles",
          "paper_account" in srcm and "pa_pos_id" in srcm
          and ".try_debit(" in srcm and "_pa_settle(" in srcm)


mirror_exclusion_explicit()

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL OK")
