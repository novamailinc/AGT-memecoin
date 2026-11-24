#!/usr/bin/env python3
"""Degen v2 tests: scoring, filters, exits, breakeven math."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, _REPO_ROOT + "/daemon/launch_scanner")
from degen_v2 import score_token, breakeven_win_rate, expected_value, FILTERS
from degen_v2_ledger import DegenLedger

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# --- Scoring ---
# Perfect token
t = {"has_telegram": True, "holders": 200, "top10_pct": 20,
     "buy_sell_ratio": 2.0, "holder_velocity": 8, "fdv": 100_000,
     "deployer_clean": True, "age_sec": 300, "liquidity": 15000}
r = score_token(t)
check("perfect scores high", r["score"] >= 80 and r["pass"])

# No telegram penalty
t2 = dict(t); t2["has_telegram"] = False
r2 = score_token(t2)
check("no telegram penalized", r2["score"] < r["score"])

# Hard block: top10 >40%
t3 = dict(t); t3["top10_pct"] = 45
r3 = score_token(t3)
check("top10 hard block", not r3["pass"] and r3["score"] == 0)

# Hard block: low liq
t4 = dict(t); t4["liquidity"] = 1000
r4 = score_token(t4)
check("low liq hard block", not r4["pass"])

# Too young / too old
t5 = dict(t); t5["age_sec"] = 30
check("too young penalized", score_token(t5)["score"] < r["score"])
t6 = dict(t); t6["age_sec"] = 900
check("too old penalized", score_token(t6)["score"] < r["score"])

# Rugger deployer
t7 = dict(t); t7["deployer_clean"] = False; t7["deployer_rugger"] = True
check("rugger penalized", score_token(t7)["score"] < r["score"] - 20)

# --- Breakeven math ---
# At 3x avg win: need 1.25/(3-1+1.25) = 1.25/3.25 = 38.5%
be3 = breakeven_win_rate(3.0)
check("breakeven 3x ~38%", abs(be3 - 0.385) < 0.01)
# At 2x: 1.25/2.25 = 55.6%
be2 = breakeven_win_rate(2.0)
check("breakeven 2x ~56%", abs(be2 - 0.556) < 0.01)
# EV: 50% WR at 3x = 0.5*2 - 0.5*1.25 = 1.0 - 0.625 = +0.375
check("EV positive", expected_value(0.5, 3.0) > 0.3)
check("EV negative low WR", expected_value(0.2, 2.0) < 0)

# --- Ledger exits ---
# Note: degen lane is halted by killswitch, so we test ledger directly
# by temporarily disabling the gate
import killswitch
_orig_gate = killswitch.gate
killswitch.gate = lambda lane: True

T0 = 1_700_000_000
L = DegenLedger()
p = L.enter("m1", "TKN", T0, 1.00)
check("enter ok", p is not None)
# Kill switch gate tested separately; ledger enter works

# Hard stop -30%
ev = L.on_price("m1", T0+60, 0.69)
check("hard stop", any("HARD STOP" in e for e in ev))

# TP at 2x: half sold
L2 = DegenLedger()
L2.enter("m2", "WIN", T0, 1.00)
ev = L2.on_price("m2", T0+60, 2.05)
check("TP half", any("TP 2x" in e for e in ev))
# Trail: peak 2.05, 12% trail = 1.804
ev = L2.on_price("m2", T0+120, 1.79)
check("trail 12%", any("TRAIL" in e for e in ev))

# Time exit 45 min
L3 = DegenLedger()
L3.enter("m3", "OLD", T0, 1.00)
ev = L3.on_price("m3", T0 + 46*60, 1.10)
check("time exit", any("TIME EXIT" in e for e in ev))

# Stats
s = L2.stats()
check("stats n=1", s["n"] == 1)
# TP half at 2x: gross = (2.05/1 - 1)*0.5 = 0.525, cost 0.625 → net -0.10
# Trail rest at 1.79: gross = 0.79*0.5 = 0.395, cost 0.625 → net -0.23
# Total ≈ -0.33 (costs dominate — honest math)
check("stats computed", "expectancy" in s)

print("\n%d failures" % len(fails))
killswitch.gate = _orig_gate  # restore
sys.exit(1 if fails else 0)
