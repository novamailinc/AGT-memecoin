#!/usr/bin/env python3
"""Kill switch atomic-write tests (2026-11-11 fix).

Proves the 2026-11-10 corruption is fixed: several daemon processes
all wrote state.json through ONE shared `.tmp` path, so two writers
could interleave into one torn file (one extra trailing '}') and the
fail-closed gate then blocked ALL new entries for ~9 minutes.

Covered here:
  1. Shadow copy mirrors the primary after every save.
  2. Concurrent writers on distinct lanes lose no trades (lock
     serializes read-modify-write).
  3. Concurrent writers on ONE lane lose no trades (window 100).
  4. A corrupt primary still fails closed even when a good shadow
     exists (the shadow is restore material only, never auto-used).
"""
import sys, os, tempfile, json, multiprocessing

tmpd = tempfile.mkdtemp()
os.environ["FOREX_AGENT_HOME"] = tmpd
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
import killswitch as ks

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# 1. Shadow mirrors primary
ks.record_trade("main", 1.0)
primary = json.load(open(ks.STATE_FILE))
shadow = json.load(open(ks.SHADOW_FILE))
check("shadow exists and parses", shadow == primary)
check("shadow is a separate file", os.path.realpath(ks.SHADOW_FILE) != os.path.realpath(ks.STATE_FILE))
check("lock file exists", os.path.exists(ks.LOCK_FILE))
check("no shared .tmp path left behind",
      not os.path.exists(ks.STATE_FILE + ".tmp"))

def _worker(args):
    lane, n, pnl = args
    for _ in range(n):
        ks.record_trade(lane, pnl)
    return lane

# 2. Concurrent writers, distinct lanes: 8 procs x 10 trades
lanes = ["main", "degen", "challenge", "nadir", "steady",
         "stacker", "spark", "eagle_eye"]
_before = {lane: len(ks._load()["lanes"].get(lane, {}).get("trades", []))
            for lane in lanes}
with multiprocessing.Pool(8) as pool:
    pool.map(_worker, [(lane, 10, 0.5) for lane in lanes])
st = ks._load()
check("distinct lanes: no lost trades",
      all(len(st["lanes"][lane]["trades"]) == _before[lane] + 10
          for lane in lanes))

# 3. Concurrent writers, ONE lane: 4 procs x 20 trades (window 100)
with multiprocessing.Pool(4) as pool:
    pool.map(_worker, [("grad_select", 20, 0.25)] * 4)
st = ks._load()
check("one lane: no lost trades under contention",
      len(st["lanes"]["grad_select"]["trades"]) == 80)

# 4. Corrupt primary + good shadow present -> still fail-closed
with open(ks.STATE_FILE, "w") as f:
    f.write("{not json,,,")
check("corrupt primary + shadow -> is_halted True",
      ks.is_halted("main") is True)
check("corrupt primary + shadow -> check_lane state_error",
      ks.check_lane("main")["action"] == "state_error")
check("corrupt file left untouched, never overwritten",
      open(ks.STATE_FILE).read() == "{not json,,,")
check("shadow still intact after corrupt-primary reads",
      json.load(open(ks.SHADOW_FILE))["lanes"]["main"]["trades"] != [])

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
