#!/usr/bin/env python3
"""Kill switch tests: halt logic, re-enable, fail-closed behavior."""
import sys, os, tempfile, json
# Isolate state
tmpd = tempfile.mkdtemp()
os.environ["FOREX_AGENT_HOME"] = tmpd
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
# Re-import after env set (module reads env at import)
import importlib
import killswitch
importlib.reload(killswitch)
ks = killswitch

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# 1. No data → warming_up, not halted
r = ks.check_lane("main")
check("no data → warming_up", r["action"] == "warming_up")
check("not halted", not ks.is_halted("main"))

# 2. Feed 20 losing trades → should HALT (min_trades=20 for main)
for i in range(20):
    ks.record_trade("main", -0.01)
r = ks.check_lane("main")
check("20 losses → HALTED", r["action"] == "HALTED")
check("is_halted true", ks.is_halted("main"))
check("negative expectancy", r["expectancy"] < 0)

# 3. Stays halted on re-check
r = ks.check_lane("main")
check("stays halted", r["action"] == "halted")

# 4. Winning lane → ok, not halted
for i in range(20):
    ks.record_trade("degen", 0.02 if i % 3 == 0 else -0.005)
r = ks.check_lane("degen")
# degen: 7 wins @ 0.02 = 0.14, 13 losses @ -0.005 = -0.065 → net +0.075, exp positive
check("winning lane → ok", r["action"] == "ok")
check("degen not halted", not ks.is_halted("degen"))

# 5. Not enough data → warming_up (challenge needs 10)
for i in range(5):
    ks.record_trade("challenge", -1.0)
r = ks.check_lane("challenge")
check("5 trades → warming_up", r["action"] == "warming_up")
check("not halted yet", not ks.is_halted("challenge"))
# 5 more losses → now 10, should halt
for i in range(5):
    ks.record_trade("challenge", -1.0)
r = ks.check_lane("challenge")
check("10 losses → HALTED", r["action"] == "HALTED")

# 6. Human re-enable clears halt and resets
r = ks.reenable("main", "test re-enable")
check("reenable works", r["action"] == "reenabled" and r["was_halted"])
check("no longer halted", not ks.is_halted("main"))
check("trades reset", len(ks._load()["lanes"]["main"]["trades"]) == 0)

# 7. Events logged
evs = ks.recent_events(5)
check("events logged", any(e["action"] == "HALT" for e in evs))
check("reenable logged", any(e["action"] == "REENABLE" for e in evs))

# 8. Status covers all lanes
st = ks.status()
check("status all lanes",
      set(st.keys()) == set(ks.CFG.keys()))

# 9. Corrupt state fails closed (2026-10-25: a torn state.json was
# silently re-seeded blank, evaporating every halt). The corrupt
# file must be left untouched, is_halted must read True on EVERY
# call (not just the first), check_lane must refuse to decide,
# and no blank state may ever be persisted over it.
_state_path = ks.STATE_FILE
with open(_state_path, "w") as _f:
    _f.write("{not json,,,")
check("corrupt state -> is_halted True (fail-closed)",
      ks.is_halted("main") is True)
check("corrupt state -> is_halted STAYS True on repeat call",
      ks.is_halted("main") is True)
check("corrupt state -> check_lane state_error",
      ks.check_lane("main")["action"] == "state_error")
check("corrupt file left untouched, never overwritten",
      open(_state_path).read() == "{not json,,,")

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
