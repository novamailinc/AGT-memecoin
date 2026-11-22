#!/usr/bin/env python3
"""Automatic switches + scan schedule + venue selection
(advanced build, 2026-11-21).

Covers switches.py (auto_quiet_decision engage/release/hold,
switch-log append/read/bound), scan_schedule.py (should_scan
FULL/REDUCED/REST, REST-exit, cadence gating, fail-open,
window journal), and venue.py (classify_venue per venue +
fail-closed unverified_pool + empty pool).

No network, no live ledgers, no daemons: all journal writes go
to a temp home (FOREX_AGENT_HOME is pointed at it, and every
call passes home explicitly). Plain script — run directly with
the venv python, not pytest.
"""
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

TMP = tempfile.mkdtemp(prefix="switches-scan-venue-")
os.environ["FOREX_AGENT_HOME"] = TMP

from daemon.launch_scanner import switches as SW  # noqa: E402
from daemon.launch_scanner import scan_schedule as SC  # noqa: E402
from daemon.launch_scanner import venue as VN  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = 1700000000.0
POOL = "Pool11111111111111111111111111111111111111"
MIG = "Pool11111111111111111111111111111111111111"
OTHER = "Other1111111111111111111111111111111111111"
CURVE = "Curve1111111111111111111111111111111111111"

# ---------------- auto_quiet_decision ------------------------------
# Quiet inactive: no "until" in the snapshot -> flag alone decides.
r = SW.auto_quiet_decision(5, {"active": False})
check("quiet: engage at exactly 5 429s, quiet inactive",
      r["action"] == "engage")
check("quiet: engage duration is 3600s (1h, not 12h)",
      r["duration_s"] == 3600)
r = SW.auto_quiet_decision(9, {"active": False, "until": 0.0})
check("quiet: engage above threshold (9 429s)",
      r["action"] == "engage" and r["duration_s"] == 3600)
r = SW.auto_quiet_decision(4, {"active": False})
check("quiet: hold below threshold (4 429s)",
      r["action"] == "hold" and r["duration_s"] == 0)
r = SW.auto_quiet_decision(0, {"active": False})
check("quiet: hold at zero 429s", r["action"] == "hold")

# No engage when already active (until in the future).
r = SW.auto_quiet_decision(9, {"active": True,
                               "until": time.time() + 3600})
check("quiet: no engage when already active",
      r["action"] == "hold")
# Expired quiet (until in the past) counts as inactive -> engage.
r = SW.auto_quiet_decision(7, {"active": True,
                               "until": time.time() - 60})
check("quiet: expired quiet engages again",
      r["action"] == "engage")

# Release: probe succeeded and 429s < 5, quiet active.
r = SW.auto_quiet_decision(2, {"active": True,
                               "until": time.time() + 3600,
                               "probe_result": {"ts": NOW, "ok": True}})
check("quiet: release on probe success + 429s < 5",
      r["action"] == "release" and r["duration_s"] == 0)
# Probe success but 429s still >= 5 -> hold, not release.
r = SW.auto_quiet_decision(6, {"active": True,
                               "until": time.time() + 3600,
                               "probe_result": {"ts": NOW, "ok": True}})
check("quiet: probe ok but 429s >= 5 holds",
      r["action"] == "hold")
# Probe success with quiet already inactive -> nothing to release.
r = SW.auto_quiet_decision(1, {"active": False,
                               "probe_result": {"ts": NOW, "ok": True}})
check("quiet: probe ok + quiet inactive holds",
      r["action"] == "hold")
# Probe failed -> hold (the quiet module re-enters 12h itself).
r = SW.auto_quiet_decision(1, {"active": True,
                               "until": time.time() + 3600,
                               "probe_result": {"ts": NOW, "ok": False}})
check("quiet: probe failed holds",
      r["action"] == "hold")
# Garbage input never raises and fails soft.
r = SW.auto_quiet_decision("junk", None)
check("quiet: garbage input holds, never raises",
      r["action"] == "hold")
r = SW.auto_quiet_decision(-3, {"active": False})
check("quiet: negative count reads as 0 (hold)",
      r["action"] == "hold")
check("quiet: default bound constant is 500",
      SW.SWITCH_LOG_MAX == 500)

# ---------------- switch log ---------------------------------------
check("switch log: empty read is []",
      SW.read_switch_log(TMP) == [])
ok = SW.log_switch(TMP, {"switch": "helius_quiet", "action": "engage",
                         "reason": "5 429s in 10m", "ts": NOW})
check("switch log: append returns True", ok is True)
rows = SW.read_switch_log(TMP)
check("switch log: one record read back",
      len(rows) == 1 and rows[0]["switch"] == "helius_quiet"
      and rows[0]["action"] == "engage"
      and rows[0]["reason"] == "5 429s in 10m"
      and rows[0]["ts"] == NOW)
SW.log_switch(TMP, {"switch": "helius_quiet", "action": "release",
                    "reason": "probe ok", "ts": NOW + 3600})
rows = SW.read_switch_log(TMP)
check("switch log: newest first",
      len(rows) == 2 and rows[0]["action"] == "release"
      and rows[1]["action"] == "engage")
check("switch log: n=1 returns only the newest",
      len(SW.read_switch_log(TMP, n=1)) == 1
      and SW.read_switch_log(TMP, n=1)[0]["action"] == "release")
check("switch log: missing switch refuses",
      SW.log_switch(TMP, {"action": "engage"}) is False)
check("switch log: path is <HOME>/switch_log.jsonl",
      SW.switch_log_path(TMP).endswith("switch_log.jsonl"))
# Bound: patched MAX of 10 keeps the newest 10 of 12.
_old_max = SW.SWITCH_LOG_MAX
SW.SWITCH_LOG_MAX = 10
try:
    for i in range(12):
        SW.log_switch(TMP, {"switch": "bound", "action": "engage",
                            "reason": "event %d" % i,
                            "ts": NOW + 7200 + i})
    rows = SW.read_switch_log(TMP, n=50)
    bound_rows = [x for x in rows if x["switch"] == "bound"]
    check("switch log: bound keeps newest 10 of 12",
          len(bound_rows) == 10)
    check("switch log: newest bound event first",
          bound_rows[0]["reason"] == "event 11")
    check("switch log: oldest bound event dropped",
          bound_rows[-1]["reason"] == "event 2")
finally:
    SW.SWITCH_LOG_MAX = _old_max
check("switch log: MAX restored to 500",
      SW.SWITCH_LOG_MAX == 500)

# ---------------- should_scan --------------------------------------
def _st(**kw):
    st = {"regime": "NORMAL", "rate429_active": False,
          "fresh_graduations_last_10m": 3, "positions_open": 0,
          "last_scan_ts": None}
    st.update(kw)
    return st


r = SC.should_scan(NOW, _st())
check("scan: FULL at 5s cadence, scan True",
      r["mode"] == "FULL" and r["scan"] is True
      and r["cadence_s"] == 5)
r = SC.should_scan(NOW, _st(regime="HOT", fresh_graduations_last_10m=0))
check("scan: HOT with no fresh graduations is still FULL",
      r["mode"] == "FULL" and r["scan"] is True)
r = SC.should_scan(NOW, _st(rate429_active=True))
check("scan: REDUCED at 30s when 429 active",
      r["mode"] == "REDUCED" and r["scan"] is True
      and r["cadence_s"] == 30)
r = SC.should_scan(NOW, _st(regime="COLD",
                            fresh_graduations_last_10m=2))
check("scan: REDUCED when regime COLD but fresh flow exists",
      r["mode"] == "REDUCED" and r["cadence_s"] == 30)
r = SC.should_scan(NOW, _st(regime="cold",
                            fresh_graduations_last_10m=0,
                            positions_open=0))
check("scan: REST when COLD + no fresh + no positions",
      r["mode"] == "REST" and r["scan"] is False)
check("scan: REST cadence is 30s (re-check interval)",
      SC.should_scan(NOW, _st(regime="COLD",
                              fresh_graduations_last_10m=0,
                              positions_open=0))["cadence_s"] == 30)

# REST exits the moment flow resumes or a position opens.
r = SC.should_scan(NOW, _st(regime="COLD",
                            fresh_graduations_last_10m=1,
                            positions_open=0))
check("scan: REST ends when a fresh graduation arrives",
      r["mode"] == "REDUCED" and r["scan"] is True)
r = SC.should_scan(NOW, _st(regime="COLD",
                            fresh_graduations_last_10m=0,
                            positions_open=1))
check("scan: REST ends when a position opens",
      r["mode"] == "REDUCED" and r["scan"] is True)
r = SC.should_scan(NOW, _st(regime="COLD",
                            fresh_graduations_last_10m=0,
                            positions_open=0, rate429_active=True))
check("scan: REST still holds under 429 pressure (no flow, no pos)",
      r["mode"] == "REST" and r["scan"] is False)

# Cadence gating via last_scan_ts.
r = SC.should_scan(NOW, _st(rate429_active=True,
                            last_scan_ts=NOW - 5))
check("scan: REDUCED not due 5s after last scan",
      r["mode"] == "REDUCED" and r["scan"] is False)
r = SC.should_scan(NOW, _st(rate429_active=True,
                            last_scan_ts=NOW - 30))
check("scan: REDUCED due 30s after last scan",
      r["mode"] == "REDUCED" and r["scan"] is True)
r = SC.should_scan(NOW, _st(last_scan_ts=NOW - 2))
check("scan: FULL not due 2s after last scan",
      r["mode"] == "FULL" and r["scan"] is False)
r = SC.should_scan(NOW, _st(last_scan_ts=NOW - 5))
check("scan: FULL due 5s after last scan",
      r["mode"] == "FULL" and r["scan"] is True)
r = SC.should_scan(NOW, _st(last_scan_ts=NOW + 100))
check("scan: future last_scan_ts means due (clock skew)",
      r["scan"] is True)

# Fail-open on garbage input.
r = SC.should_scan(NOW, None)
check("scan: fail-open on None state",
      r == {"scan": True, "cadence_s": 5, "mode": "FULL",
            "reason": "fail_open"})
r = SC.should_scan(NOW, "garbage")
check("scan: fail-open on non-dict state",
      r["reason"] == "fail_open" and r["scan"] is True)
r = SC.should_scan(NOW, _st(regime="WARM"))
check("scan: fail-open on unknown regime",
      r["reason"] == "fail_open" and r["mode"] == "FULL")
r = SC.should_scan(NOW, _st(fresh_graduations_last_10m="abc"))
check("scan: fail-open on unparseable count",
      r["reason"] == "fail_open")
r = SC.should_scan("junk", _st())
check("scan: fail-open on unparseable now",
      r["reason"] == "fail_open" and r["scan"] is True)
r = SC.should_scan(NOW, _st(last_scan_ts="abc"))
check("scan: fail-open on unparseable last_scan_ts",
      r["reason"] == "fail_open")

# ---------------- scan windows journal -----------------------------
check("scan windows: empty read is []",
      SC.read_scan_windows(TMP) == [])
ok = SC.journal_window(TMP, {"start_ts": NOW, "end_ts": NOW + 1800,
                             "mode": "REDUCED",
                             "reason": "429 active"})
check("scan windows: journal returns True", ok is True)
rows = SC.read_scan_windows(TMP)
check("scan windows: one record read back",
      len(rows) == 1 and rows[0]["mode"] == "REDUCED"
      and rows[0]["start_ts"] == NOW and rows[0]["end_ts"] == NOW + 1800)
ok = SC.journal_window(TMP, {"start_ts": NOW + 1800, "end_ts": None,
                             "mode": "REST",
                             "reason": "cold, no flow"})
check("scan windows: open window (end None) journals",
      ok is True
      and SC.read_scan_windows(TMP)[0]["end_ts"] is None)
check("scan windows: bad mode refuses",
      SC.journal_window(TMP, {"start_ts": NOW, "mode": "IDLE"}) is False)
check("scan windows: missing start_ts refuses",
      SC.journal_window(TMP, {"mode": "FULL"}) is False)
check("scan windows: default bound constant is 500",
      SC.SCAN_WINDOW_MAX == 500)
_old_max = SC.SCAN_WINDOW_MAX
SC.SCAN_WINDOW_MAX = 10
try:
    for i in range(12):
        SC.journal_window(TMP, {"start_ts": NOW + 10000 + i,
                                "end_ts": NOW + 10001 + i,
                                "mode": "FULL", "reason": "w%d" % i})
    rows = [x for x in SC.read_scan_windows(TMP, n=50)
            if str(x.get("reason", "")).startswith("w")]
    check("scan windows: bound keeps newest 10 of 12",
          len(rows) == 10 and rows[0]["reason"] == "w11"
          and rows[-1]["reason"] == "w2")
finally:
    SC.SCAN_WINDOW_MAX = _old_max

# ---------------- classify_venue -----------------------------------
r = VN.classify_venue({"pool": POOL, "dex": "pump.fun",
                       "migration_pool": MIG,
                       "canonical_basis": "migration_event",
                       "curve_address": CURVE})
check("venue: pump.fun dex + migration match -> pumpfun_curve",
      r == {"venue": "pumpfun_curve", "verified": True,
            "reason": "migration_match"})
r = VN.classify_venue({"pool": POOL, "dex": "Pump.fun",
                       "migration_pool": None,
                       "canonical_basis": "migration_event",
                       "curve_address": CURVE})
check("venue: migration_event basis verifies with no migration_pool",
      r["venue"] == "pumpfun_curve" and r["verified"] is True
      and r["reason"] == "migration_event")
r = VN.classify_venue({"pool": CURVE, "dex": "",
                       "migration_pool": CURVE,
                       "canonical_basis": None,
                       "curve_address": CURVE})
check("venue: pool == curve address -> pumpfun_curve",
      r["venue"] == "pumpfun_curve" and r["verified"] is True)
r = VN.classify_venue({"pool": POOL, "dex": "PumpSwap",
                       "migration_pool": MIG,
                       "canonical_basis": "recorder_migration_match",
                       "curve_address": CURVE})
check("venue: pumpswap dex + migration match -> pumpswap",
      r == {"venue": "pumpswap", "verified": True,
            "reason": "migration_match"})
r = VN.classify_venue({"pool": POOL, "dex": "raydium",
                       "migration_pool": MIG,
                       "canonical_basis": "chain_migration_match",
                       "curve_address": CURVE})
check("venue: raydium dex + migration match -> raydium",
      r == {"venue": "raydium", "verified": True,
            "reason": "migration_match"})
# recorder/chain match basis ALONE (no migration_pool) does NOT
# verify — fail-closed, the match must be passed as the pool.
r = VN.classify_venue({"pool": POOL, "dex": "pumpswap",
                       "migration_pool": None,
                       "canonical_basis": "recorder_migration_match",
                       "curve_address": CURVE})
check("venue: recorder-match basis alone does not verify",
      r == {"venue": "unknown", "verified": False,
            "reason": "unverified_pool"})
r = VN.classify_venue({"pool": POOL, "dex": "pumpswap",
                       "migration_pool": OTHER,
                       "canonical_basis": "recorder_migration_mismatch",
                       "curve_address": CURVE})
check("venue: pool != migration pool -> unverified_pool",
      r == {"venue": "unknown", "verified": False,
            "reason": "unverified_pool"})
r = VN.classify_venue({"pool": POOL, "dex": "pumpswap",
                       "migration_pool": OTHER,
                       "canonical_basis": "migration_event",
                       "curve_address": CURVE})
check("venue: migration_event basis loses to a mismatching pool",
      r["reason"] == "unverified_pool" and r["verified"] is False)
r = VN.classify_venue({"pool": "", "dex": "pumpswap",
                       "migration_pool": MIG,
                       "canonical_basis": "migration_event"})
check("venue: empty pool -> unverified_pool",
      r == {"venue": "unknown", "verified": False,
            "reason": "unverified_pool"})
r = VN.classify_venue({"pool": POOL, "dex": "orca",
                       "migration_pool": MIG,
                       "canonical_basis": None,
                       "curve_address": CURVE})
check("venue: matched pool + unknown dex -> unknown_venue",
      r == {"venue": "unknown", "verified": False,
            "reason": "unknown_venue"})
r = VN.classify_venue({"pool": POOL, "dex": "pumpswap"})
check("venue: no provenance at all -> unverified_pool",
      r == {"venue": "unknown", "verified": False,
            "reason": "unverified_pool"})
r = VN.classify_venue(None)
check("venue: None input fails closed, never raises",
      r == {"venue": "unknown", "verified": False,
            "reason": "unverified_pool"})
r = VN.classify_venue({"pool": POOL, "dex": "  PumpSwap  ",
                       "migration_pool": MIG})
check("venue: dex label case/whitespace tolerant",
      r["venue"] == "pumpswap" and r["verified"] is True)
r = VN.classify_venue({"pool": POOL, "dex": "bonding-curve",
                       "migration_pool": MIG})
check("venue: bonding-curve dex -> pumpfun_curve",
      r["venue"] == "pumpfun_curve" and r["verified"] is True)

print("")
if fails:
    print("FAILURES: %d — %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("ALL SWITCHES/SCAN/VENUE TESTS PASSED")
