#!/usr/bin/env python3
"""Nadir-v0 Phase 2 tests: boundary values and pure-function determinism."""
import sys
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
from nadir_signal import drawdown_pct, in_drawdown_band, check_universe, check_falling_knife

def mk_candles(n, base=100.0, trend=0.0):
    """n hourly candles, oldest->newest. trend = per-candle drift."""
    out, ts = [], 1_700_000_000
    p = base
    for i in range(n):
        o = p
        c = p * (1 + trend)
        h, l = max(o, c) * 1.01, min(o, c) * 0.99
        out.append((ts + i * 3600, o, h, l, c, 1000.0))
        p = c
    return out

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# Drawdown boundaries: 69.99 / 70 / 90 / 90.01
check("dd 69.99 blocked", not in_drawdown_band(69.99))
check("dd 70.00 allowed", in_drawdown_band(70.00))
check("dd 90.00 allowed", in_drawdown_band(90.00))
check("dd 90.01 blocked", not in_drawdown_band(90.01))
check("dd None blocked", not in_drawdown_band(None))
check("dd calc", abs(drawdown_pct(30.0, 100.0) - 70.0) < 1e-9)

# Universe boundaries
ok, _ = check_universe(100_000, 25_000, 10_000, 100, 30.0, 720)
check("universe min edge", ok)
ok, _ = check_universe(99_999, 25_000, 10_000, 100, 30.0, 720)
check("universe mcap low blocked", not ok)
ok, _ = check_universe(100_000, 25_000, 10_000, 100, 29.9, 720)
check("universe young pool blocked", not ok)
ok, _ = check_universe(100_000, 25_000, 10_000, 100, 30.0, 719)
check("universe short history blocked", not ok)

# Falling knife: construct passing candles
# Need: no fresh 24h low, 15%+ rebound off 7d low, close > close 6h ago
candles = mk_candles(720, base=100.0)
# Force a dip then recovery in the tail: set 7d low at 50, latest at 60 (+20%)
for i in range(-168, -24):
    ts, o, h, l, c, v = candles[i]
    candles[i] = (ts, o, h, 50.0, c, v)  # 7d low = 50
for i in range(-24, 0):
    ts, o, h, l, c, v = candles[i]
    candles[i] = (ts, o, h, 52.0, c, v)  # 24h low = 52 > 50, no fresh low
# latest close = 60, 6h ago close = 55
ts, o, h, l, c, v = candles[-1]
candles[-1] = (ts, o, h, l, 60.0, v)
ts, o, h, l, c, v = candles[-7]
candles[-7] = (ts, o, h, l, 55.0, v)
ok, reason, d = check_falling_knife(candles)
check("knife pass: " + reason, ok)

# Fresh low should block
candles2 = [list(c) for c in candles]
candles2[-1][3] = 40.0  # fresh 24h low below prior 48h
ok, _, _ = check_falling_knife([tuple(c) for c in candles2])
check("knife fresh-low blocked", not ok)

# Determinism: same input -> identical output
import json
r1 = check_falling_knife(candles)
r2 = check_falling_knife(candles)
check("deterministic", json.dumps(r1, default=str) == json.dumps(r2, default=str))

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
