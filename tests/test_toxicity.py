#!/usr/bin/env python3
"""Toxicity veto tests."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
import time
sys.path.insert(0, _REPO_ROOT + "/daemon/launch_scanner")
from toxicity import check_toxicity, check_toxicity_detailed

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

# --- coverage reporting + legacy veto preservation (2026-11-23) ----
# Detailed API: per-check clear/veto/unverifiable labels.
v, r, cov = check_toxicity_detailed(
    info({"m5": {"buys": 60, "sells": 5}}, {"m5": 32.5},
         T0 * 1000 - 3600e3), T0)
check("detailed veto matches wrapper", v and "bump-farmed" in r)
check("coverage: bump_farm veto", cov["bump_farm"] == "veto")

v, r, cov = check_toxicity_detailed(
    info({"m5": {"buys": 60, "sells": 40}}, {"m5": 5000.0},
         T0 * 1000 - 3600e3), T0)
check("clean data: all checks clear",
      not v and all(c == "clear" for c in cov.values()))

# Missing data is journaled UNVERIFIABLE, never called clean.
v, r, cov = check_toxicity_detailed({}, T0)
check("empty info: no veto", not v)
check("empty info: every check unverifiable",
      all(c.startswith("unverifiable") for c in cov.values()))

# LEGACY OUTCOME PRESERVED: bump-farm veto still fires when volume
# is absent (avg not computable) - the label says so.
v, r, cov = check_toxicity_detailed(
    info({"m5": {"buys": 60, "sells": 5}}, None,
         T0 * 1000 - 3600e3), T0)
check("legacy: missing volume still vetoes bump-farm", v)
check("legacy label: volume absent",
      cov["bump_farm"].startswith("veto (legacy"))

# LEGACY OUTCOME PRESERVED: wash-skew veto still fires when sells
# are absent (read as zero) with volume present.
v, r, cov = check_toxicity_detailed(
    info({"m5": {"buys": 80}}, {"m5": 4000.0},
         T0 * 1000 - 3600e3), T0)
check("legacy: missing sells still vetoes wash-skew", v)
check("legacy label: sells absent",
      cov["wash_skew"].startswith("veto (legacy"))

# Below-threshold buys with missing volume is an honest clear.
v, r, cov = check_toxicity_detailed(
    info({"m5": {"buys": 10, "sells": 8}}, None,
         T0 * 1000 - 3600e3), T0)
check("below threshold + no volume: no veto", not v)
check("below threshold label honest",
      cov["bump_farm"].startswith("clear (buy count below threshold"))

# Wrapper stays a 2-tuple for existing callers.
res = check_toxicity(info({"m5": {"buys": 60, "sells": 5}},
                          {"m5": 32.5}, T0 * 1000 - 3600e3), T0)
check("wrapper still 2-tuple", len(res) == 2)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
