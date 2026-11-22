#!/usr/bin/env python3
"""RETIRED 2026-10-23 — Stacker lane test suite (do not re-enable).

Rationale for retiring the whole suite instead of repairing it:
- The Stacker lane was KILLED by the 2026-10-22 lane verdict
  (7 closes, -$11.79, 0 wins) and its machinery was removed on
  2026-10-23: scripts/supervisor and scripts/forex-watchdog both record
  stacker-feed REMOVED from the daemon roster, killswitch lane
  "stacker" halted, do not restart.
- Everything below tests only that dead lane: score_stacker /
  StackerLedger in daemon/launch_scanner/stacker.py, whose sole caller
  was the removed scripts/stacker-feed. No live or shared code imports
  them (verified by grep 2026-10-23), so there are no still-live parts
  to keep.
- The suite's last 6 checks (stats / serialization round-trip) failed
  precisely because they run after killswitch.gate is restored to the
  live state file, where "stacker" is halted — i.e. the suite asserted
  a deliberately halted lane could still enter. "Fixing" that would
  mean permanently bypassing a deliberate halt to keep a killed lane's
  tests green, which is the opposite of isolation.
The original checks are preserved below the early exit for history;
they no longer execute. Original header: "Tests for the Stacker lane:
scoring, tiered exits, stops, risk limits."
"""
import sys
print("RETIRED 2026-10-23: Stacker lane killed by the 2026-10-22 "
      "verdict and removed from the roster 2026-10-23 — suite retired, "
      "see header rationale.")
sys.exit(0)

# --- Original suite below (retired 2026-10-23, not executed) --------------
import time
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")

# Bypass killswitch for ledger tests (test the gate separately)
import killswitch
_orig_gate = killswitch.gate
killswitch.gate = lambda lane: True

from stacker import (score_stacker, StackerLedger, SCORE_BAR,
                     MAX_CONCURRENT, HARD_STOP_PCT, TIME_STOP_SEC,
                     TP1_MULT, TP2_MULT, TRAIL_PCT)

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

def good_token(**kw):
    t = {"multiple_from_launch": 2.2, "mcap": 9_000, "liquidity": 30_000,
         "holders_per_min": 60, "buy_sell_ratio": 1.8, "curve_pct": 22,
         "safety": "clean", "eagle_momentum": 0, "age_min": 8}
    t.update(kw)
    return t

# ---- Scoring ----
r = score_stacker(good_token())
check("good token passes bar", r["pass"] and r["score"] >= SCORE_BAR)
check("good token score high", r["score"] >= 80)

r = score_stacker(good_token(safety="Honeypot risk"))
check("dirty safety hard-gated", not r["pass"] and r["score"] == 0)

r = score_stacker(good_token(liquidity=1_000))
check("low liq hard-gated", not r["pass"])

r = score_stacker(good_token(liquidity=24_999))
check("RUNNER-ONLY rule: 24_999 liq blocked", not r["pass"])
r = score_stacker(good_token(liquidity=25_001))
check("RUNNER-ONLY rule: 25_001 liq passes gate", r["pass"])
t_noliq = good_token(); del t_noliq["liquidity"]
check("unknown liq blocked fail-closed", not score_stacker(t_noliq)["pass"])

r = score_stacker(good_token(multiple_from_launch=1.2))
check("pre-2x rejected", not r["pass"])

r = score_stacker(good_token(multiple_from_launch=8.0))
check("already-pumped rejected", not r["pass"])

r = score_stacker(good_token(mcap=200_000))
check("mcap too high rejected", not r["pass"])

r = score_stacker(good_token(eagle_momentum=75))
r2 = score_stacker(good_token(eagle_momentum=0))
check("eagle bonus adds points", r["score"] > r2["score"])

r = score_stacker(good_token(buy_sell_ratio=0.8))
check("weak B/S scores lower", r["score"] < score_stacker(good_token())["score"])

# ---- Ledger: tiered exits ----
T0 = 1_700_000_000
led = StackerLedger()
p = led.enter("m1", "TKN", T0, 1.00)
check("enter ok", p is not None)
check("dup mint blocked", led.enter("m1", "TKN", T0, 1.00) is None)

# TP1 at 2.2x: 50% sold
ev = led.on_price("m1", T0 + 60, 2.25)
check("TP1 fires", any("TP1" in e for e in ev))
check("tp1 flag set", p.tp1_done)
check("50% remaining", abs(p.frac_remaining - 0.5) < 1e-9)
check("not exited yet", not p.exit_ts)

# TP2 at 3.5x: 30% of original sold (60% of remaining)
ev = led.on_price("m1", T0 + 120, 3.6)
check("TP2 fires", any("TP2" in e for e in ev))
check("tp2 flag set", p.tp2_done)
check("20% remaining", abs(p.frac_remaining - 0.2) < 1e-9)

# Trail 25%: peak 3.6, trail triggers at <= 2.7
ev = led.on_price("m1", T0 + 180, 2.69)
check("trail fires", any("TRAIL" in e for e in ev))
check("fully exited", p.exit_ts > 0)
check("profitable", p.realized_pnl > 0)

# ---- Hard stop ----
led2 = StackerLedger()
led2.enter("m2", "DUMP", T0, 1.00)
ev = led2.on_price("m2", T0 + 60, 0.74)
check("hard stop -25%", any("HARD STOP" in e for e in ev))
check("stop exited", led2.positions[0].exit_ts > 0)
check("stop lost money", led2.positions[0].realized_pnl < 0)

# ---- Time stop ----
led3 = StackerLedger()
led3.enter("m3", "SLOW", T0, 1.00)
ev = led3.on_price("m3", T0 + TIME_STOP_SEC + 10, 1.50)
check("time stop 2h", any("TIME STOP" in e for e in ev))

# ---- Max concurrent ----
led4 = StackerLedger()
for i in range(MAX_CONCURRENT):
    check(f"enter {i+1} ok", led4.enter(f"c{i}", "C", T0, 1.0) is not None)
check("4th blocked", led4.enter("cX", "X", T0, 1.0) is None)

# ---- Daily loss halt ----
led5 = StackerLedger(bankroll=150.0)
today = time.strftime("%Y-%m-%d", time.gmtime(T0))
led5.daily_pnl[today] = -5.0  # > 3% of 150 = 4.5
ok, why = led5.can_enter(T0)
check("daily loss halts", not ok and "halt" in why)
led5.daily_pnl[today] = -4.0  # under threshold
ok, why = led5.can_enter(T0)
check("under threshold allows", ok)

# ---- Killswitch fail-closed ----
killswitch.gate = lambda lane: (_ for _ in ()).throw(Exception("boom"))
led6 = StackerLedger()
ok, why = led6.can_enter(T0)
check("killswitch error fail-closed", not ok)
killswitch.gate = _orig_gate  # restore (may be halted; just check callable)
ok, _ = led6.can_enter(T0)
check("gate callable after restore", isinstance(ok, bool))

# ---- Stats ----
led7 = StackerLedger()
led7.enter("w1", "WIN", T0, 1.0)
led7.on_price("w1", T0 + 60, 2.3)   # TP1
led7.on_price("w1", T0 + 120, 3.6)  # TP2
led7.on_price("w1", T0 + 180, 2.6)  # trail
led7.enter("l1", "LOSS", T0, 1.0)
led7.on_price("l1", T0 + 60, 0.70)  # hard stop
s = led7.stats()
check("stats 2 trades", s["total_trades"] == 2)
check("stats 1 win", s["wins"] == 1)
check("stats wr 0.5", s["win_rate"] == 0.5)
check("stats pnl tracked", isinstance(s["total_pnl_usd"], float))

# ---- Serialization ----
d = led7.to_dict()
led8 = StackerLedger.from_dict(d)
check("roundtrip positions", len(led8.positions) == 2)
check("roundtrip pnl", led8.stats()["total_trades"] == 2)

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
