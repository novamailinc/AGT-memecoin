#!/usr/bin/env python3
"""Nadir-v0 paper ledger tests: exits, costs, limits, determinism."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, _REPO_ROOT + "/daemon/launch_scanner")
from nadir_paper import Ledger, CFG

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

T0 = 1_700_000_000

# --- Entry ---
L = Ledger()
p = L.enter("m1", "TKN", T0, 1.00)
check("enter ok", p is not None)
check("buy slip applied", abs(p.entry_price_adj - 1.015) < 1e-9)
check("qty = 0.01/1.015", abs(p.qty_total - 0.01/1.015) < 1e-9)
check("dup blocked", L.enter("m1", "TKN", T0+1, 1.00) is None)

# daily cap: 3
L.enter("m2", "A", T0, 1.0); L.enter("m3", "B", T0, 1.0)
check("daily cap", L.enter("m4", "C", T0, 1.0) is None)
# max open: 5 (use next day to dodge daily cap)
L2 = Ledger()
for i in range(5): L2.enter("mx%d"%i, "S", T0+i*90000, 1.0)
check("max open", L2.enter("mx5", "S", T0+5*90000, 1.0) is None)

# --- Hard stop -35% ---
L3 = Ledger()
L3.enter("s1", "STP", T0, 1.00)          # adj entry 1.015
ev = L3.on_price("s1", T0+3600, 0.65)     # -36% -> stop
check("hard stop fires", any("HARD STOP" in e for e in ev))
check("stop closed", L3.positions[0].exit_ts is not None)
check("stop reason", L3.positions[0].exit_reason == "hard_stop")
# pnl ≈ (0.65*0.985 - 1.015) * qty  -> negative
check("stop pnl negative", L3.positions[0].realized_pnl < 0)

# --- Take profit 2x: sells half, trails rest ---
L4 = Ledger()
L4.enter("t1", "WIN", T0, 1.00)
ev = L4.on_price("t1", T0+3600, 2.05)    # > 2x adj (2.03)
check("TP fires", any("TP 2x" in e for e in ev))
pos = L4.positions[0]
check("half remains", abs(pos.qty_open - pos.qty_total/2) < 1e-12)
check("tp flag", pos.tp_hit and pos.exit_ts is None)
# trail: peak=2.05, drop 25% -> 1.5375
ev = L4.on_price("t1", T0+7200, 1.50)
check("trail fires", any("TRAIL" in e for e in ev))
check("trail closed", pos.exit_ts is not None)

# --- Time exit 14d ---
L5 = Ledger()
L5.enter("e1", "OLD", T0, 1.00)
ev = L5.on_price("e1", T0 + 14*86400 + 1, 1.10)
check("time exit", any("TIME EXIT" in e for e in ev))

# --- Liquidity exit ---
L6 = Ledger()
L6.enter("l1", "LIQ", T0, 1.00)
ev = L6.on_price("l1", T0+3600, 1.05, liquidity=10_000)
check("liq exit", any("LIQUIDITY" in e for e in ev))

# --- Safety exit ---
L7 = Ledger()
L7.enter("f1", "RUG", T0, 1.00)
ev = L7.on_price("f1", T0+3600, 1.20, safety_ok=False)
check("safety exit", any("SAFETY" in e for e in ev))

# --- Cooldown: re-enter blocked for 7d ---
check("cooldown", L7.enter("f1", "RUG", T0+7200, 1.00) is None)

# --- Expectancy math ---
L8 = Ledger()
L8.enter("w1", "W", T0, 1.00)
L8.on_price("w1", T0+3600, 2.05)   # TP half
L8.on_price("w1", T0+7200, 2.05)   # hold runner (no trail yet)
L8.on_price("w1", T0+14*86400+1, 2.05)  # time exit rest
m = L8.expectancy()
check("expectancy positive on win", m["expectancy"] > 0)
check("win rate 1.0", m["win_rate"] == 1.0)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
