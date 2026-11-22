#!/usr/bin/env python3
"""P4 event recorder tests (2026-10-23). Plain script — run with the
venv python, not pytest.

Covers daemon/launch_scanner/recorder.py: record/read roundtrip,
FOREX_AGENT_HOME read at call time (temp-home isolation), the
enabled flag, size rotation + directory-cap pruning with tiny
monkeypatched caps, the replay ladder simulation on synthetic
quotes, and diff classification. No network.
"""
import json
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import recorder as R
from daemon.launch_scanner import grad as G

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


_ORIG_HOME = os.environ.get("FOREX_AGENT_HOME")
_ORIG_ENABLED_ENV = os.environ.get("RECORDER_ENABLED")
os.environ.pop("RECORDER_ENABLED", None)
R.ENABLED = True


def _set_home(path):
    os.environ["FOREX_AGENT_HOME"] = path


def _dir_bytes(path):
    total = 0
    for fn in os.listdir(path):
        total += os.path.getsize(os.path.join(path, fn))
    return total


T0 = 1700000000.0  # fixed epoch; its UTC day names the file
DAY = time.strftime("%Y%m%d", time.gmtime(T0))

# ---------------- record/read roundtrip + call-time home ----------------
home1 = tempfile.mkdtemp(prefix="recorder-home1-")
_set_home(home1)
check("roundtrip: record birth returns True",
      R.record("birth", {"mint": "MINT1pump", "symbol": "B1"}, ts=T0))
check("roundtrip: record quote returns True",
      R.record("quote", {"mint": "MINT1pump", "price": 1.5,
                         "liquidity": 1000.0,
                         "tape_source": "dexscreener"}, ts=T0 + 5))
check("roundtrip: record entry/exit/migration kinds",
      R.record("migration", {"mint": "MINT1pump",
                             "created_ts": T0 + 1}, ts=T0 + 1)
      and R.record("entry", {"mint": "MINT1pump", "lane": "grad",
                             "entry_price": 1.0}, ts=T0 + 2)
      and R.record("exit", {"mint": "MINT1pump", "lane": "grad",
                            "pnl_usd": 0.5, "realized_mult": 1.5,
                            "closed_reason": "ladder_complete",
                            "peak_mult": 2.0}, ts=T0 + 60))
rec_dir = os.path.join(home1, "recorder")
day_file = os.path.join(rec_dir, "events-%s.jsonl" % DAY)
check("roundtrip: day file created under FOREX_AGENT_HOME/recorder",
      os.path.isfile(day_file))
evs = R.read_events(T0 - 10, T0 + 120)
check("roundtrip: read_events returns all 5 in order",
      len(evs) == 5
      and [e["kind"] for e in evs] == ["birth", "quote", "migration",
                                       "entry", "exit"])
check("roundtrip: payload fields preserved + ts/kind stamped",
      evs[1]["mint"] == "MINT1pump" and evs[1]["price"] == 1.5
      and evs[1]["tape_source"] == "dexscreener"
      and evs[0]["ts"] == T0 and evs[4]["realized_mult"] == 1.5)
check("roundtrip: kind filter works",
      len(R.read_events(T0 - 10, T0 + 120, kinds=("quote",))) == 1)
check("roundtrip: window filter excludes out-of-window events",
      len(R.read_events(T0 + 1000, T0 + 2000)) == 0)
# Call-time home: switching env after import redirects new records.
home1b = tempfile.mkdtemp(prefix="recorder-home1b-")
_set_home(home1b)
R.record("quote", {"mint": "OTHER", "price": 2.0}, ts=T0)
check("call-time home: new home sees only its own record",
      len(R.read_events(T0 - 10, T0 + 10)) == 1
      and R.read_events(T0 - 10, T0 + 10)[0]["mint"] == "OTHER")
_set_home(home1)

# ---------------- enabled flag ----------------
os.environ["RECORDER_ENABLED"] = "0"
check("enabled: RECORDER_ENABLED=0 drops silently",
      R.record("quote", {"mint": "X", "price": 1.0}, ts=T0) is False)
os.environ.pop("RECORDER_ENABLED", None)
_saved_enabled = R.ENABLED
R.ENABLED = False
check("enabled: module ENABLED=False drops silently",
      R.record("quote", {"mint": "X", "price": 1.0}, ts=T0) is False)
R.ENABLED = _saved_enabled
check("enabled: re-enabled records again",
      R.record("quote", {"mint": "X", "price": 1.0}, ts=T0) is True)

# ---------------- fail-soft ----------------
check("fail-soft: record with non-dict payload does not raise",
      isinstance(R.record("quote", payload="junk", ts=T0), bool))
check("fail-soft: record to unwritable home returns False",
      _set_home("/proc/definitely-not-writable-xyz") or True)
check("fail-soft: record returns False on bad home",
      R.record("quote", {"mint": "X", "price": 1.0}, ts=T0) is False)
_set_home(home1)

# ---------------- size rotation with tiny caps ----------------
home2 = tempfile.mkdtemp(prefix="recorder-home2-")
_set_home(home2)
_old_file_cap, _old_dir_cap = R.MAX_FILE_BYTES, R.MAX_DIR_BYTES
try:
    R.MAX_FILE_BYTES = 300
    R.MAX_DIR_BYTES = 10 * 1024 * 1024
    n_written = 20
    for i in range(n_written):
        ok = R.record("quote", {"mint": "ROTPADpump", "seq": i,
                                "price": 1.0,
                                "pad": "x" * 40}, ts=T0 + i)
        assert ok
    files2 = sorted(os.listdir(os.path.join(home2, "recorder")))
    check("rotation: tiny file cap rolls to .<n> files",
          len(files2) > 1 and any(".1." in f or f.endswith(".1.jsonl")
                                  or ".1.jsonl" in f for f in files2))
    check("rotation: every file within the tiny cap",
          all(os.path.getsize(os.path.join(home2, "recorder", f))
              <= 300 for f in files2))
    evs2 = R.read_events(T0 - 10, T0 + 100)
    check("rotation: all records still readable across rolled files",
          len(evs2) == n_written
          and [e["seq"] for e in evs2] == list(range(n_written)))
finally:
    R.MAX_FILE_BYTES, R.MAX_DIR_BYTES = _old_file_cap, _old_dir_cap

# ---------------- directory cap pruning ----------------
home3 = tempfile.mkdtemp(prefix="recorder-home3-")
_set_home(home3)
try:
    R.MAX_FILE_BYTES = 200
    R.MAX_DIR_BYTES = 500
    for i in range(40):
        R.record("quote", {"mint": "CAPPADpump", "seq": i,
                           "price": 1.0, "pad": "y" * 40},
                 ts=T0 + i)
    rec_dir3 = os.path.join(home3, "recorder")
    total3 = _dir_bytes(rec_dir3)
    check("cap: directory pruned to <= MAX_DIR_BYTES",
          total3 <= 500)
    evs3 = R.read_events(T0 - 10, T0 + 100)
    seqs3 = sorted(e["seq"] for e in evs3)
    check("cap: pruning dropped oldest, kept the newest records",
          0 < len(seqs3) < 40 and seqs3[-1] == 39
          and seqs3 == list(range(40 - len(seqs3), 40)))
finally:
    R.MAX_FILE_BYTES, R.MAX_DIR_BYTES = _old_file_cap, _old_dir_cap

# ---------------- ladder constants parity with grad.py ----------------
check("ladder: recorder constants match grad.py",
      R.LEG_A_FRAC == G.LEG_A_FRAC and R.LEG_A_TARGET == G.LEG_A_TARGET
      and R.LEG_B_FRAC == G.LEG_B_FRAC
      and R.LEG_B_TARGET == G.LEG_B_TARGET
      and R.LEG_C_FRAC == G.LEG_C_FRAC and R.LEG_C_TRAIL == G.LEG_C_TRAIL
      and R.HARD_STOP_MULT == G.HARD_STOP_MULT)
check("caps: production defaults are 25MB / 200MB",
      _old_file_cap == 25 * 1024 * 1024
      and _old_dir_cap == 200 * 1024 * 1024)

# ---------------- replay ladder simulation ----------------
# 3x-then-crash: A fires @2x, B @3x, C trails out 33% below the peak.
quotes = [(1000.0, 1.0), (1005.0, 2.0), (1010.0, 3.0), (1015.0, 2.0)]
sim = R.simulate_ladder(1.0, quotes)
# 0.5*2.0 + 0.25*3.0 + 0.25*2.0 = 2.25
check("sim: 3x-then-crash reproduces ladder_complete at 2.25x",
      sim["closed_reason"] == "ladder_complete"
      and sim["realized_mult"] is not None
      and abs(sim["realized_mult"] - 2.25) < 1e-9
      and all(sim["legs_done"].values()))
# Same series in quote-record dict form.
sim2 = R.simulate_ladder(1.0, [{"ts": t, "price": p} for t, p in quotes])
check("sim: dict quote records accepted",
      sim2["realized_mult"] == sim["realized_mult"]
      and sim2["closed_reason"] == "ladder_complete")
# Hard stop: -60% closes everything at that print.
sim3 = R.simulate_ladder(1.0, [(1000.0, 1.0), (1005.0, 0.4)])
check("sim: hard stop at -50% closes all",
      sim3["closed_reason"] == "hard_stop_-50pct"
      and abs(sim3["realized_mult"] - 0.4) < 1e-9)
# Flat series: remaining legs mark to the last quote.
sim4 = R.simulate_ladder(1.0, [(1000.0, 1.0), (1005.0, 1.1)])
check("sim: uncompleted ladder marks remaining to last quote",
      sim4["closed_reason"] == "end_of_quotes"
      and abs(sim4["realized_mult"] - 1.1) < 1e-9)
# Missing data.
sim5 = R.simulate_ladder(1.0, [])
check("sim: no quotes -> realized None / missing_data",
      sim5["realized_mult"] is None
      and sim5["closed_reason"] == "missing_data")
check("sim: bad entry price -> missing_data",
      R.simulate_ladder(0.0, quotes)["realized_mult"] is None)

# ---------------- diff classification ----------------
check("classify: 0.10 -> MATCH", R.classify_diff(0.10) == "MATCH")
check("classify: 0.15 boundary -> MATCH",
      R.classify_diff(0.15) == "MATCH")
check("classify: 0.30 -> DRIFT", R.classify_diff(0.30) == "DRIFT")
check("classify: 0.50 boundary -> DRIFT",
      R.classify_diff(0.50) == "DRIFT")
check("classify: 0.51 -> MISS", R.classify_diff(0.51) == "MISS")
check("classify: negative diff uses abs",
      R.classify_diff(-0.10) == "MATCH")
check("classify: missing data -> MISS",
      R.classify_diff(None) == "MISS"
      and R.classify_diff(0.05, has_data=False) == "MISS")
# A simulated-vs-actual pair from the sim above classifies sanely.
check("classify: sim 2.25 vs actual 2.30 -> MATCH",
      R.classify_diff(abs(sim["realized_mult"] - 2.30)) == "MATCH")

# ---------------- restore env ----------------
if _ORIG_HOME is None:
    os.environ.pop("FOREX_AGENT_HOME", None)
else:
    os.environ["FOREX_AGENT_HOME"] = _ORIG_HOME
if _ORIG_ENABLED_ENV is not None:
    os.environ["RECORDER_ENABLED"] = _ORIG_ENABLED_ENV

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all recorder tests passed")
