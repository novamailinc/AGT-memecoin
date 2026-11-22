#!/usr/bin/env python3
"""Steady lane tests: scoring, exits, risk limits, consistency gate."""
import sys
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
from steady import score_steady, SteadyLedger, CFG
import killswitch
_orig = killswitch.gate
killswitch.gate = lambda lane: True  # bypass for tests

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# --- Scoring ---
t = {"drawdown_7d_pct": 75, "smart_buyers_30m": 4, "vol24h": 100_000,
     "liquidity": 60_000, "bounce_pct": 12, "has_catalyst": True}
r = score_steady(t)
check("sweet spot scores high", r["score"] >= 80 and r["pass"])

t2 = dict(t); t2["drawdown_7d_pct"] = 30
r2 = score_steady(t2)
check("outside dd zone rejected", not r2["pass"])

t3 = dict(t); t3["drawdown_7d_pct"] = 95
check("too deep rejected", not score_steady(t3)["pass"])

t4 = dict(t); t4["liquidity"] = 5000
check("low liq hard block", not score_steady(t4)["pass"])

t4b = dict(t); t4b["liquidity"] = 24_999
check("RUNNER-ONLY rule: 24_999 liq blocked", not score_steady(t4b)["pass"])
t4c = dict(t); t4c["liquidity"] = 25_001
check("RUNNER-ONLY rule: 25_001 liq passes gate", score_steady(t4c)["pass"])
t4d = dict(t); del t4d["liquidity"]
check("unknown liq blocked fail-closed", not score_steady(t4d)["pass"])

# --- Ledger ---
T0 = 1_700_000_000
L = SteadyLedger()
p = L.enter("m1", "TKN", T0, 1.00, portfolio_usd=100.0)
check("enter ok", p is not None and p.size_usd == 1.0)
check("dup blocked", L.enter("m1", "TKN", T0, 1.00) is None)

# Max concurrent = 3
L.enter("m2", "A", T0, 1.0); L.enter("m3", "B", T0, 1.0)
check("max 3 concurrent", L.enter("m4", "C", T0, 1.0) is None)

# TP1 at 1.5x: half sold
L2 = SteadyLedger()
L2.enter("w1", "WIN", T0, 1.00, portfolio_usd=100)
ev = L2.on_price("w1", T0+3600, 1.55)
check("TP1 fires", any("TP1" in e for e in ev))
pos = L2.positions[0]
check("tp1 flag", pos.tp1_done and pos.exit_ts is None)

# TP2 at 2x: quarter more
ev = L2.on_price("w1", T0+7200, 2.05)
check("TP2 fires", any("TP2" in e for e in ev))

# Trail 25%: peak 2.05, trail at 1.5375
ev = L2.on_price("w1", T0+10800, 1.50)
check("trail fires", any("TRAIL" in e for e in ev))
check("trail closed", pos.exit_ts is not None)

# Hard stop -25%
L3 = SteadyLedger()
L3.enter("s1", "STP", T0, 1.00, portfolio_usd=100)
ev = L3.on_price("s1", T0+3600, 0.74)
check("hard stop", any("STOP" in e for e in ev))

# Time exit 24h
L4 = SteadyLedger()
L4.enter("e1", "OLD", T0, 1.00, portfolio_usd=100)
ev = L4.on_price("e1", T0 + 25*3600, 1.10)
check("time exit", any("TIME EXIT" in e for e in ev))

# Daily loss limit: 3%
L5 = SteadyLedger()
import datetime
today = datetime.datetime.fromtimestamp(T0).strftime("%Y-%m-%d")
L5.daily_pnl[today] = -3.5
ok, reason = L5.can_enter(T0)
check("daily loss blocks", not ok and "daily loss" in reason)

# --- Daily loss tally: portfolio-percent units (2026-10-17 fix) ---
# A single -25% stop on a $1 position ($100 portfolio) is ~-0.3% of the
# portfolio, NOT -30%. The tally must use portfolio-percent units to match
# daily_loss_limit_pct, or one close blocks the lane for the whole day.
L14 = SteadyLedger()
p14 = L14.enter("u1", "U1", T0, 1.0, portfolio_usd=100)
L14.on_price("u1", T0+60, 0.70)  # hard stop fires
check("daily tally in portfolio units",
      abs(L14.daily_pnl[today] - p14.pnl) < 1e-9)
ok14, _ = L14.can_enter(T0+61)
check("one stop does not trip daily limit", ok14)

# --- Consistency gate ---
L6 = SteadyLedger()
# 20 trades: 12 wins (+$2), 8 losses (-$1) → 60% WR, profitable ex-top-3
for i in range(20):
    p = L6.enter(f"g{i}", "G", T0+i, 1.0, portfolio_usd=100)
    pnl = 2.0 if i < 12 else -1.0
    p.pnl = pnl
    p.exit_ts = T0+i+1
    L6.closed.append(p)
g = L6.consistency_check()
check("good lane passes gate", g["pass"])

# Bad: 30% WR
L7 = SteadyLedger()
for i in range(20):
    p = L7.enter(f"b{i}", "B", T0+i, 1.0, portfolio_usd=100)
    p.pnl = 2.0 if i < 6 else -1.0  # 30% WR
    p.exit_ts = T0+i+1
    L7.closed.append(p)
g = L7.consistency_check()
check("low WR fails gate", not g["pass"])

# Fragile: profitable only due to top-3
L8 = SteadyLedger()
for i in range(20):
    p = L8.enter(f"f{i}", "F", T0+i, 1.0, portfolio_usd=100)
    # 3 huge wins, 17 small losses → profitable overall, not ex-top-3
    p.pnl = 10.0 if i < 3 else -1.0
    p.exit_ts = T0+i+1
    L8.closed.append(p)
g = L8.consistency_check()
# WR = 3/20 = 15% < 40%, so fails on WR first
check("fragile fails gate", not g["pass"])

# --- Ledger persistence round-trip ---
L9 = SteadyLedger()
p9 = L9.enter("mint9", "SYM9", T0, 0.5, portfolio_usd=100, dd_zone="75-85")
p9.pnl = 2.5
p9.exit_ts = T0 + 100
p9.exit_reason = "tp1"
L9.closed.append(p9)
d9 = L9.to_dict()
L10 = SteadyLedger.from_dict(d9)
check("persistence: closed count", len(L10.closed) == 1)
check("persistence: pnl", L10.closed[0].pnl == 2.5)
check("persistence: dd_zone", L10.closed[0].dd_zone == "75-85")
check("persistence: exit_reason", L10.closed[0].exit_reason == "tp1")

# --- Bucket stats ---
L11 = SteadyLedger()
for i in range(10):
    p = L11.enter(f"b{i}", "B", T0+i, 1.0, portfolio_usd=100,
                  dd_zone="60-75" if i < 6 else "75-85")
    p.pnl = 1.0 if i % 2 == 0 else -0.5
    p.exit_ts = T0+i+1
    L11.closed.append(p)
bs = L11.bucket_stats()
check("buckets: 60-75 n=6", bs.get("60-75", {}).get("n") == 6)
check("buckets: 75-85 n=4", bs.get("75-85", {}).get("n") == 4)
check("buckets: wr computed", "wr" in bs.get("60-75", {}))

# --- Exit logic: hard stop fires ---
L12 = SteadyLedger()
p12 = L12.enter("s12", "S12", T0, 1.0, portfolio_usd=100)
evs = L12.on_price("s12", T0+60, 0.70)  # -30% < -25% stop
check("hard stop fires", any("STOP" in e for e in evs))
check("hard stop closes", p12.exit_ts is not None)

# --- Exit logic: TP1 partial ---
L13 = SteadyLedger()
p13 = L13.enter("s13", "S13", T0, 1.0, portfolio_usd=100)
evs = L13.on_price("s13", T0+60, 1.6)  # +60% > 1.5x TP1
check("TP1 fires", any("TP1" in e for e in evs))
check("TP1 partial (still open)", p13.exit_ts is None and p13.tp1_done)

# --- Accounting fix regression (2026-10-24): partial exits must bank PnL
# only on the fraction ACTUALLY closed. The old _close_frac computed the
# final leg on 100% of the ORIGINAL size after TP1 had already banked
# 50% — a TP1+trail position banked 1.5x its size. Hand-computed:
# entry 1.00, size $1. TP1 poll 1.55 -> leg (0.55)(1.0)(0.5) - 0.04*0.5
# = 0.255. Trail poll 1.16 (25.2% off the 1.55 peak) -> final leg on the
# REMAINING 0.5: (0.16)(1.0)(0.5) - 0.04*0.5 = 0.06. Total = 0.315.
# (The buggy code produced 0.375: final leg on the full original size.)
L15 = SteadyLedger()
p15 = L15.enter("a15", "A15", T0, 1.00, portfolio_usd=100)
L15.on_price("a15", T0+3600, 1.55)  # TP1
check("acct: TP1 leg pnl", abs(p15.pnl - 0.255) < 1e-9)
check("acct: remaining after TP1", p15.remaining == 0.5)
evs = L15.on_price("a15", T0+7200, 1.16)  # trail off 1.55 peak
check("acct: trail fires", any("TRAIL" in e for e in evs))
check("acct: TP1+trail total pnl == 0.315 (not buggy 0.375)",
      abs(p15.pnl - 0.315) < 1e-9)
check("acct: fraction banked never exceeds size (remaining == 0)",
      p15.remaining == 0.0 and p15.exit_ts is not None)

# TP1 + TP2 + final: TP2 banks 25% of ORIGINAL (CFG tp2_frac), the final
# leg banks only the last 25%. Hand-computed: TP1 leg 0.255 (as above);
# TP2 poll 2.05 -> (1.05)(1.0)(0.25) - 0.04*0.25 = 0.2525; time-exit poll
# 2.05 at +7h -> (1.05)(1.0)(0.25) - 0.04*0.25 = 0.2525. Total = 0.76.
L16 = SteadyLedger()
p16 = L16.enter("a16", "A16", T0, 1.00, portfolio_usd=100)
L16.on_price("a16", T0+3600, 1.55)  # TP1
L16.on_price("a16", T0+5400, 2.05)  # TP2
check("acct: remaining after TP2", p16.remaining == 0.25)
check("acct: pnl after TP1+TP2", abs(p16.pnl - 0.5075) < 1e-9)
evs = L16.on_price("a16", T0+7*3600, 2.05)  # time exit (max_hold 6h)
check("acct: time exit fires", any("TIME EXIT" in e for e in evs))
check("acct: TP1+TP2+final total pnl == 0.76", abs(p16.pnl - 0.76) < 1e-9)
check("acct: fully closed, remaining 0",
      p16.remaining == 0.0 and p16.exit_ts is not None)

# --- Persistence: `remaining` round-trips; legacy saves derive it ---
L17 = SteadyLedger()
p17 = L17.enter("a17", "A17", T0, 1.00, portfolio_usd=100)
L17.on_price("a17", T0+3600, 1.55)  # TP1 -> remaining 0.5
d17 = L17.to_dict()
check("persist: remaining in dict", d17["positions"][0].get("remaining") == 0.5)
L18 = SteadyLedger.from_dict(d17)
check("persist: remaining round-trip", L18.positions[0].remaining == 0.5)
# Legacy save (no "remaining" key): derive from banked fractions
d17b = L17.to_dict()
del d17b["positions"][0]["remaining"]
L19 = SteadyLedger.from_dict(d17b)
check("persist: legacy derives remaining from tp1 flag",
      L19.positions[0].remaining == 0.5)
d17c = L17.to_dict()
del d17c["positions"][0]["remaining"]
d17c["positions"][0]["tp2_done"] = True
L20 = SteadyLedger.from_dict(d17c)
check("persist: legacy derives remaining from tp1+tp2 flags",
      L20.positions[0].remaining == 0.25)

killswitch.gate = _orig
print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
