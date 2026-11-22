#!/usr/bin/env python3
"""Watcher v2 lane tests (2026-10-24).

Covers watcher_v2_lane.py: the v2 engine is an isolated instance
of watcher_lane.py with ONLY the first ladder rung changed
(1.5x instead of 2x); v1's module is untouched; the v2 ledger
stamps v2 terms; leg A actually banks at 1.5x on v2 and does NOT
bank at 1.6x on v1. No network: exits run on a stub price_fn.
Plain script — run directly, not via pytest.
"""
import json
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import watcher_lane as V1
from daemon.launch_scanner import watcher_v2_lane as V2

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- isolation + terms ----------------
check("v2 lane is watcher_v2", V2.LANE == "watcher_v2")
check("v2 first rung is 1.5x", V2.LEG_A_TARGET == 1.5)
check("v2 ledger is watcher_v2_ledger.json",
      V2.LEDGER_PATH.endswith("watcher_v2_ledger.json"))
check("v1 untouched: lane still watcher, rung still 2.0",
      V1.LANE == "watcher" and V1.LEG_A_TARGET == 2.0)
pre = V2._preregistration()
check("v2 terms stamp the 1.5x ladder + watcher_v2 killswitch",
      "1.5x" in pre["terms"]["ladder"]
      and "watcher_v2" in pre["rule"]
      and pre["killswitch_cfg"] == {"window": 100, "min_trades": 100,
                                    "halt": True}
      and pre["terms"]["notional_usd"] == 1.0)


def _blank_ledger():
    return {"bankroll_start": 100.0, "bankroll": 100.0,
            "positions": {}, "closes": [],
            "created_ts": time.time(), "state": "active",
            "preregistered_verdict": {}}


def _pos(entry=0.001):
    return {"mint": "V2Mint1111111111111111111111111111111pump",
            "symbol": "V2", "chain": "solana",
            "source": "smart-watcher", "lane": "watcher_v2",
            "entry_ts": time.time(), "entry_price": entry,
            "peak_price": entry, "peak_ts": time.time(),
            "tokens": 1000.0, "realized_usd": 0.0, "status": "open",
            "spot_index": None, "last_liquidity": 50000.0,
            "entry_snapshot": {"liquidity": 50000.0},
            "legs": {
                "a": {"frac": 0.50, "target": 1.5, "done": False,
                      "exit_price": 0, "realized_usd": 0},
                "b": {"frac": 0.25, "target": 3.0, "done": False,
                      "exit_price": 0, "realized_usd": 0},
                "c": {"frac": 0.25, "trail": 0.30, "peak": entry,
                      "done": False, "exit_price": 0,
                      "realized_usd": 0},
            }}


def _run_exit(module, eng_path_attr, entry_mult):
    """Fabricate one open position on a temp ledger, run poll_exits
    with a stub price at entry_mult x entry, return the position."""
    tmp = tempfile.mkdtemp(prefix="watcherv2-")
    path = os.path.join(tmp, "ledger.json")
    d = _blank_ledger()
    d["positions"] = {"V2Mint1111111111111111111111111111111pump":
                      _pos()}
    with open(path, "w") as f:
        json.dump(d, f)
    if module is V2:
        V2._eng.LEDGER_PATH = path
        module.poll_exits(
            price_fn=lambda chain, mint, spot: 0.001 * entry_mult)
        out = json.load(open(path))
    else:
        V1.LEDGER_PATH = path
        module.poll_exits(
            price_fn=lambda chain, mint, spot: 0.001 * entry_mult)
        out = json.load(open(path))
    return out["positions"][
        "V2Mint1111111111111111111111111111111pump"]


# ---------------- leg-A behavior ----------------
p = _run_exit(V2, None, 1.4)
check("v2: leg A does NOT bank at 1.4x",
      p["legs"]["a"]["done"] is False)
p = _run_exit(V2, None, 1.6)
check("v2: leg A banks at 1.6x with the honest 1.5x reason",
      p["legs"]["a"]["done"] is True
      and p["legs"]["a"]["exit_reason"] == "leg_a_1.5x"
      and p["legs"]["a"]["exit_price"] > 0
      and abs(p["legs"]["a"]["exit_price"] - 0.0016) / 0.0016 < 0.02)
p = _run_exit(V1, None, 1.6)
check("v1 contrast: leg A does NOT bank at 1.6x (needs 2.0)",
      p["legs"]["a"]["done"] is False)
p = _run_exit(V1, None, 2.1)
check("v1 contrast: leg A banks at 2.1x",
      p["legs"]["a"]["done"] is True)

print()
if fails:
    print("FAILURES (%d): %s" % (len(fails), fails))
    sys.exit(1)
print("ALL WATCHER-V2 TESTS PASSED")
