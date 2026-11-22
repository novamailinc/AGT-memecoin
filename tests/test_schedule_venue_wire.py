#!/usr/bin/env python3
"""Scan-schedule + venue wiring tests (advanced build, pieces 8+9).

Covers the wiring (2026-11-21):
- Venue: _stamp_detection stamps ef["venue"] (which pool/venue
  the entry traded on, whether verified) into entry_features,
  so the close carries it. Stream migration_event + pumpswap
  verifies; a mismatched/unverifiable basis stays unverified
  (fail-closed); an empty pool never verifies.
- Schedule: _scan_once consults _scan_schedule_check on the
  main-loop path only. COLD + no fresh + no open RESTs (the
  scan is skipped, exits unaffected); a fresh graduation in
  the feed queue ends REST; COLD with flow REDUCEs to a 30s
  cadence; the fire-first entry loop (fresh_only=True) is never
  gated. Mode transitions journal to scan_windows.jsonl.

No network: every feed is stubbed. Temp ledgers only.
"""
import os
import queue
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import venue as VN

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()

# ---------------- venue: pure classification via _venue_for -------
check("venue: module imported in grad_select", GS._venue is not None)
v = GS._venue_for(
    {"canonical_basis": "migration_event",
     "bonding_curve": "Curve111"},
    {"pool_address": "PoolX", "dex": "pumpswap"})
check("venue: stream pumpswap verifies",
      v == {"venue": "pumpswap", "verified": True,
            "reason": "migration_match"})
v = GS._venue_for(
    {"canonical_basis": "recorder_migration_match",
     "bonding_curve": "Curve111"},
    {"pool_address": "PoolX", "dex": "raydium"})
check("venue: recorder match + raydium verifies",
      v == {"venue": "raydium", "verified": True,
            "reason": "migration_match"})
v = GS._venue_for(
    {"canonical_basis": "recorder_migration_mismatch"},
    {"pool_address": "PoolX", "dex": "pumpswap"})
check("venue: mismatch basis stays unverified (fail-closed)",
      v["verified"] is False and v["venue"] == "unknown")
v = GS._venue_for({"canonical_basis": "migration_event"},
                  {"pool_address": "", "dex": "pumpswap"})
check("venue: empty pool never verifies",
      v["verified"] is False
      and v["reason"] == "unverified_pool")
v = GS._venue_for(
    {"canonical_basis": "migration_event",
     "bonding_curve": "PoolX"},
    {"pool_address": "PoolX", "dex": "weird-dex"})
check("venue: pool == curve address is pumpfun_curve",
      v == {"venue": "pumpfun_curve", "verified": True,
            "reason": "migration_match"})

# ---------------- venue: stamp lands in entry_features ------------
_tmp = tempfile.mkdtemp(prefix="sched-venue-")
eng = GS.make_engine()
eng.LEDGER_PATH = os.path.join(_tmp, "grad_select_ledger.json")
eng.ensure_ledger()
MINT = "VenMint1111111111111111111111111111111111pump"
with eng._ledger_lock():
    d = eng._read()
    d["positions"][MINT] = {
        "mint": MINT, "symbol": "VEN", "chain": "solana",
        "entry_price": 0.001, "entry_ts": NOW,
        "pool_address": "PoolX", "dex": "pumpswap",
        "legs": {"a": {"frac": 0.5, "target": 2.0},
                 "b": {"frac": 0.25, "target": 3.0},
                 "c": {"frac": 0.25, "trail": 0.30}},
        "notional_usd": 25.0, "entry_features": {}}
    eng._write(d)
GS._stamp_detection(
    eng, MINT, "pumpportal-migration",
    {"canonical_basis": "migration_event",
     "bonding_curve": "Curve111",
     "deployer_launches_24h": 1},
    ff={"source": "live_quote",
        "info": {"price_usd": 0.001, "mcap_usd": 60000.0,
                 "pool": "PoolX", "sol_usd": 0.0}})
ef = eng._read()["positions"][MINT].get("entry_features") or {}
check("venue: stamp lands in entry_features",
      (ef.get("venue") or {}).get("venue") == "pumpswap"
      and (ef.get("venue") or {}).get("verified") is True)

# ---------------- schedule: helper decisions ----------------------
check("schedule: module imported in grad_select",
      GS._scan_schedule is not None)


def _reset_sched():
    for k in GS._SCAN_SCHEDULE_STATE:
        GS._SCAN_SCHEDULE_STATE[k].clear()


def _set_closes(n_win, n_loss):
    with eng._ledger_lock():
        d = eng._read()
        d["closes"] = [
            {"pnl_usd": 10.0, "close_ts": NOW - 1000 + i}
            for i in range(n_win)] + [
            {"pnl_usd": -25.0, "close_ts": NOW - 500 + i}
            for i in range(n_loss)]
        d["positions"] = {}
        eng._write(d)


class _Feed:
    def __init__(self, n=0):
        self._q = queue.Queue()
        for i in range(n):
            self._q.put({"mint": "M%d" % i})


# COLD: 50 losing closes (expectancy -25, win rate 0).
_reset_sched()
_set_closes(0, 50)
dec = GS._scan_schedule_check(eng, None, NOW)
check("schedule: COLD + no fresh + no open RESTs",
      dec.get("scan") is False and dec.get("mode") == "REST")
# A fresh graduation in the feed queue ends REST.
dec = GS._scan_schedule_check(eng, _Feed(1), NOW)
check("schedule: fresh graduation in queue ends REST",
      dec.get("scan") is True and dec.get("mode") == "REDUCED")
# REDUCED cadence: a second check 5s later is not due.
_reset_sched()
_set_closes(0, 50)
GS._scan_schedule_check(eng, _Feed(1), NOW)
dec2 = GS._scan_schedule_check(eng, _Feed(1), NOW + 5)
check("schedule: REDUCED cadence blocks a 5s-later scan",
      dec2.get("scan") is False
      and dec2.get("mode") == "REDUCED")
dec3 = GS._scan_schedule_check(eng, _Feed(1), NOW + 31)
check("schedule: REDUCED scan due again after 30s",
      dec3.get("scan") is True)
# NORMAL (thin history -> insufficient_data): FULL, scans.
_reset_sched()
_set_closes(2, 1)
dec = GS._scan_schedule_check(eng, None, NOW)
check("schedule: NORMAL regime scans FULL",
      dec.get("scan") is True and dec.get("mode") == "FULL")
# An open position also ends REST even with no fresh flow.
_reset_sched()
_set_closes(0, 50)
with eng._ledger_lock():
    d = eng._read()
    d["positions"]["OPEN111pump"] = {"mint": "OPEN111pump"}
    eng._write(d)
dec = GS._scan_schedule_check(eng, None, NOW)
check("schedule: open position ends REST",
      dec.get("scan") is True and dec.get("mode") == "REDUCED")

# ---------------- schedule: _scan_once gating ---------------------
# COLD + no fresh + no open: the main-loop scan is skipped.
_reset_sched()
_set_closes(0, 50)
out = {"stream": 0, "pools": 0, "watching": 0, "opened": 0,
       "skipped": 0, "dead": 0, "select_rejected": 0,
       "select_reasons": {}}
res = GS._scan_once(eng, feed=None, gt_rows=None, out=out,
                    now=NOW, fresh_only=False)
check("scan_once: COLD main-loop scan skipped, reason journaled",
      res.get("scan_skipped") is not None
      and res.get("scan_mode") == "REST")
# fresh_only=True is never gated: the same cold lane proceeds
# (empty feed, so nothing opens — but no scan_skipped either).
out2 = {"stream": 0, "pools": 0, "watching": 0, "opened": 0,
        "skipped": 0, "dead": 0, "select_rejected": 0,
        "select_reasons": {}}
res2 = GS._scan_once(eng, feed=None, gt_rows=None, out=out2,
                     now=NOW, fresh_only=True)
check("scan_once: fire-first loop never gated",
      "scan_skipped" not in res2)
# Backfill bypass (regression: 2026-11-21 the FULL 5s cadence
# skipped a backfill scan and lost a fresh entry). A COLD lane
# that is NOT bleeding (win rate 20%, last 5 closes all wins)
# would REST on the schedule gate — but a scan carrying gt_rows
# must reach the filter stage anyway. The evaluator is patched
# so no RPC is attempted.
_reset_sched()
with eng._ledger_lock():
    d = eng._read()
    d["closes"] = (
        [{"pnl_usd": -25.0, "close_ts": NOW - 1000 + i}
         for i in range(40)] +
        [{"pnl_usd": 10.0, "close_ts": NOW - 500 + i}
         for i in range(10)])
    d["positions"] = {}
    eng._write(d)
_orig_eval = GS.evaluate_candidate
GS.evaluate_candidate = lambda *a, **k: {"verdict": "reject",
                                          "reason": "x"}
try:
    out3 = {"stream": 0, "pools": 0, "watching": 0, "opened": 0,
            "skipped": 0, "dead": 0, "select_rejected": 0,
            "select_reasons": {}}
    res3 = GS._scan_once(
        eng, feed=None,
        gt_rows=[{"mint": "BackfillBypass111pump", "symbol": "BB",
                  "created_ts": NOW - 10, "dex": "raydium",
                  "pool_address": "PoolBB",
                  "reserve_usd": 80000.0, "fdv_usd": 70000.0}],
        out=out3, now=NOW, fresh_only=False)
finally:
    GS.evaluate_candidate = _orig_eval
check("scan_once: gt_rows scan never schedule-gated (backfill "
      "bypass reaches the filter stage)",
      "scan_skipped" not in res3
      and res3.get("select_rejected") == 1)
# Mode transitions journalled to scan_windows.jsonl.
import scan_schedule as SS
wins = SS.read_scan_windows(
    home=os.path.dirname(os.path.abspath(eng.LEDGER_PATH)), n=50)
check("schedule: mode transitions journaled to scan_windows",
      len(wins) >= 1 and wins[-1].get("mode") in
      ("FULL", "REDUCED", "REST"))

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("ALL SCHEDULE-VENUE-WIRE TESTS PASSED")
