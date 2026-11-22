#!/usr/bin/env python3
"""Kill switch risk report: journaled pre-halt warnings (2026-11-21).

Covers risk_report() (empty / winning / deteriorating / improving /
losing lanes, loss-streak counting, read-only + never-raises on
corrupt/missing state) and check_lane() WARNING journaling (at most
one WARNING per lane per 24h, never halts an elevated-but-not-halting
lane, halt path unchanged with enriched HALT event).

No network, no live state (a temp home isolates everything).
Plain script — run directly with the venv python, not pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys, os, tempfile, json, time
# Isolate state
tmpd = tempfile.mkdtemp()
os.environ["FOREX_AGENT_HOME"] = tmpd
sys.path.insert(0, _REPO_ROOT + "/daemon/launch_scanner")
# Re-import after env set (module reads env at import)
import importlib
import killswitch
importlib.reload(killswitch)
ks = killswitch

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

def warnings_for(lane):
    return [e for e in ks.recent_events(100)
            if e.get("action") == "WARNING" and e.get("lane") == lane]

def halts_for(lane):
    return [e for e in ks.recent_events(100)
            if e.get("action") == "HALT" and e.get("lane") == lane]

# 0. Missing state file: risk_report never raises, reports low,
# would_halt False, and does NOT create/write any state file.
check("missing state file exists not yet",
      not os.path.exists(ks.STATE_FILE))
r = ks.risk_report("main")
check("empty lane -> risk low", r["risk"] == "low")
check("empty lane -> n 0", r["n"] == 0)
check("empty lane -> would_halt False", r["would_halt"] is False)
check("empty lane -> expectancy 0.0", r["expectancy"] == 0.0)
check("empty lane -> trend flat", r["trend"] == "flat")
check("empty lane -> loss_streak 0", r["loss_streak"] == 0)
check("empty lane -> recent_n 0", r["recent_n"] == 0)
check("empty lane -> min_trades 20 (main)", r["min_trades"] == 20)
check("risk_report wrote no state file",
      not os.path.exists(ks.STATE_FILE))
check("unknown lane -> low / would_halt False",
      ks.risk_report("nope_lane")["risk"] == "low"
      and ks.risk_report("nope_lane")["would_halt"] is False)

# 1. Winning lane -> low, would_halt False.
for i in range(20):
    ks.record_trade("degen", 10.0)
r = ks.risk_report("degen")
check("winning lane -> risk low", r["risk"] == "low")
check("winning lane -> would_halt False", r["would_halt"] is False)
check("winning lane -> expectancy 10.0", r["expectancy"] == 10.0)
check("winning lane -> win_rate 1.0", r["win_rate"] == 1.0)
check("winning lane -> recent_expectancy 10.0",
      r["recent_expectancy"] == 10.0)
check("winning lane -> recent_n 20", r["recent_n"] == 20)
check("winning lane -> trend flat", r["trend"] == "flat")
check("winning lane -> loss_streak 0", r["loss_streak"] == 0)
check("winning lane -> min_trades 15 (degen)", r["min_trades"] == 15)

# risk_report is read-only: state file bytes unchanged by the call.
_before = open(ks.STATE_FILE, "rb").read()
ks.risk_report("degen")
check("risk_report leaves state file untouched",
      open(ks.STATE_FILE, "rb").read() == _before)

# 2. Deteriorating lane: 20 wins @ +20 then 20 losses @ -15.
# Overall +2.5, recent -15 -> trend deteriorating, risk elevated,
# would_halt False (overall expectancy still positive).
for i in range(20):
    ks.record_trade("main", 20.0)
for i in range(20):
    ks.record_trade("main", -15.0)
r = ks.risk_report("main")
check("deteriorating lane -> n 40", r["n"] == 40)
check("deteriorating lane -> expectancy 2.5", r["expectancy"] == 2.5)
check("deteriorating lane -> recent_expectancy -15.0",
      r["recent_expectancy"] == -15.0)
check("deteriorating lane -> recent_net -300.0",
      r["recent_net"] == -300.0)
check("deteriorating lane -> recent_win_rate 0.0",
      r["recent_win_rate"] == 0.0)
check("deteriorating lane -> trend deteriorating",
      r["trend"] == "deteriorating")
check("deteriorating lane -> risk elevated", r["risk"] == "elevated")
check("deteriorating lane -> would_halt False",
      r["would_halt"] is False)
check("deteriorating lane -> loss_streak 20",
      r["loss_streak"] == 20)

# 3. Improving lane: 20 losses @ -15 then 20 wins @ +20.
# Overall +2.5, recent +20 -> trend improving, risk low.
for i in range(20):
    ks.record_trade("spark", -15.0)
for i in range(20):
    ks.record_trade("spark", 20.0)
r = ks.risk_report("spark")
check("improving lane -> trend improving", r["trend"] == "improving")
check("improving lane -> risk low", r["risk"] == "low")
check("improving lane -> would_halt False", r["would_halt"] is False)
check("improving lane -> recent_expectancy 20.0",
      r["recent_expectancy"] == 20.0)

# 4. Losing lane with n >= min_trades -> would_halt True, risk high.
for i in range(10):
    ks.record_trade("challenge", -5.0)
r = ks.risk_report("challenge")
check("losing lane -> would_halt True", r["would_halt"] is True)
check("losing lane -> risk high", r["risk"] == "high")
check("losing lane -> expectancy -5.0", r["expectancy"] == -5.0)
check("losing lane -> min_trades 10 (challenge)",
      r["min_trades"] == 10)
check("losing lane -> trend flat (recent == overall)",
      r["trend"] == "flat")

# 5. Loss-streak counting: 5 wins, then 3 losses -> 3; a win -> 0.
for i in range(5):
    ks.record_trade("stacker", 10.0)
for i in range(3):
    ks.record_trade("stacker", -5.0)
r = ks.risk_report("stacker")
check("loss streak -> 3 trailing losses", r["loss_streak"] == 3)
check("loss streak -> recent_n 8 (n < 20)", r["recent_n"] == 8)
ks.record_trade("stacker", 10.0)
r = ks.risk_report("stacker")
check("loss streak -> win resets to 0", r["loss_streak"] == 0)

# 6. check_lane WARNING: elevated-but-not-halting lane gets at most
# one WARNING per 24h and is never halted by it.
# steady (min 15, window 30): 10 wins @ +30 then 20 losses @ -12 ->
# overall +2.0, recent (last 20, all losses) -12.0 -> deteriorating
# + elevated, would_halt False. (The recent window must be a strict
# subset of the history, or recent == overall by construction.)
for i in range(10):
    ks.record_trade("steady", 30.0)
for i in range(20):
    ks.record_trade("steady", -12.0)
res = ks.check_lane("steady")
check("elevated lane -> check_lane action ok", res["action"] == "ok")
check("elevated lane -> not halted", not ks.is_halted("steady"))
check("elevated lane -> exactly one WARNING",
      len(warnings_for("steady")) == 1)
w = warnings_for("steady")[0]
check("WARNING carries expectancy", "expectancy" in w)
check("WARNING carries recent_expectancy",
      w.get("recent_expectancy") == -12.0)
check("WARNING carries loss_streak", w.get("loss_streak") == 20)
check("WARNING carries trend", w.get("trend") == "deteriorating")
res = ks.check_lane("steady")
check("elevated lane -> still ok on re-check", res["action"] == "ok")
check("elevated lane -> still not halted",
      not ks.is_halted("steady"))
check("WARNING dedup -> still exactly one after re-checks",
      len(warnings_for("steady")) == 1)
ks.check_lane("steady")
check("WARNING dedup -> still exactly one after 3rd check",
      len(warnings_for("steady")) == 1)
# Backdate the WARNING past 24h: the next check may warn again.
s = ks._load()
for e in s["events"]:
    if e.get("action") == "WARNING" and e.get("lane") == "steady":
        e["ts"] = time.time() - 90000
ks._save(s)
res = ks.check_lane("steady")
check("stale WARNING (>24h) -> second WARNING allowed",
      len(warnings_for("steady")) == 2)
check("stale WARNING -> lane still not halted",
      not ks.is_halted("steady") and res["action"] == "ok")

# 7. Halt path unchanged: a lane that would halt still halts with
# the same reason string, and the HALT event carries risk evidence.
for i in range(10):
    ks.record_trade("nadir", -5.0)
res = ks.check_lane("nadir")
check("losing lane -> HALTED", res["action"] == "HALTED")
check("losing lane -> is_halted true", ks.is_halted("nadir"))
check("halt reason string unchanged",
      res["halt_reason"] ==
      "rolling expectancy -5.000000 < 0 over last 10 trades "
      "(win rate 0.0%)")
h = halts_for("nadir")
check("HALT event logged", len(h) == 1)
check("HALT event carries recent_expectancy",
      h[0].get("recent_expectancy") == -5.0)
check("HALT event carries recent_net",
      h[0].get("recent_net") == -50.0)
check("HALT event carries loss_streak",
      h[0].get("loss_streak") == 10)
check("HALT event carries trend", h[0].get("trend") == "flat")
check("HALT event reason is the halt_reason",
      h[0].get("reason") == res["halt_reason"])
# A halted lane stays halted on re-check (no WARNING replaces it).
res = ks.check_lane("nadir")
check("halted lane stays halted", res["action"] == "halted")

# 8. Corrupt state (LAST — nothing after this touches state):
# risk_report never raises and reports low / would_halt False;
# fail-closed is_halted behaviour is unchanged.
with open(ks.STATE_FILE, "w") as _f:
    _f.write("{not json,,,")
try:
    r = ks.risk_report("main")
    _raised = False
except Exception:
    _raised = True
    r = {}
check("corrupt state -> risk_report never raises", not _raised)
check("corrupt state -> risk low", r.get("risk") == "low")
check("corrupt state -> would_halt False",
      r.get("would_halt") is False)
check("corrupt state -> n 0", r.get("n") == 0)
check("corrupt state -> is_halted still True (fail-closed)",
      ks.is_halted("main") is True)
check("corrupt state -> check_lane state_error",
      ks.check_lane("main")["action"] == "state_error")
check("corrupt file left untouched, never overwritten",
      open(ks.STATE_FILE).read() == "{not json,,,")

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
