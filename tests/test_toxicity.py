#!/usr/bin/env python3
"""Toxicity veto tests."""
import sys
import time
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
from toxicity import check_toxicity

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

T0 = time.time()
def info(txns=None, volume=None, created_ms=None):
    d = {}
    if txns is not None: d["txns"] = txns
    if volume is not None: d["volume"] = volume
    if created_ms is not None: d["pairCreatedAt"] = created_ms
    return d

# 1. Bump farm: 60 buys/5m averaging $0.50 -> veto
v, r = check_toxicity(info({"m5": {"buys": 60, "sells": 5}},
                           {"m5": 32.5}, T0 * 1000 - 3600e3), T0)
check("bump farm vetoed", v and "bump-farmed" in r)
# healthy: 60 buys averaging $50 -> pass
v, _ = check_toxicity(info({"m5": {"buys": 60, "sells": 40}},
                           {"m5": 5000.0}, T0 * 1000 - 3600e3), T0)
check("healthy flow passes", not v)
# 2. Wash skew: 80 buys, 0 sells -> veto
v, r = check_toxicity(info({"m5": {"buys": 80, "sells": 0}},
                           {"m5": 4000.0}, T0 * 1000 - 3600e3), T0)
check("one-sided flow vetoed", v and "one-sided" in r)
# 3. Bot swarm: 200s-old pair, 150 h1 buys -> veto
v, r = check_toxicity(info({"m5": {"buys": 10, "sells": 8},
                            "h1": {"buys": 150, "sells": 60}},
                           {"m5": 500.0}, (T0 - 200) * 1000), T0)
check("bot swarm vetoed", v and "bot swarm" in r)
# old pair with same buys -> pass (age gate)
v, _ = check_toxicity(info({"m5": {"buys": 10, "sells": 8},
                            "h1": {"buys": 150, "sells": 60}},
                           {"m5": 500.0}, (T0 - 3600) * 1000), T0)
check("old pair passes", not v)
# fail-open: empty info never vetoes
check("empty info fail-open", check_toxicity({}) == (False, "ok"))
check("None fail-open", check_toxicity(None) == (False, "ok"))
check("missing txns fail-open", not check_toxicity({"volume": {}})[0])
# boundary: exactly 40 dust buys -> veto (>= rule)
v, _ = check_toxicity(info({"m5": {"buys": 40, "sells": 0}},
                           {"m5": 40.0}, None), T0)
check("boundary 40 buys vetoed", v)
# 39 buys -> pass
v, _ = check_toxicity(info({"m5": {"buys": 39, "sells": 0}},
                           {"m5": 39.0}, None), T0)
check("39 buys passes", not v)
check("never raises on garbage", check_toxicity({"txns": "x"}) == (False, "ok"))

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
