#!/usr/bin/env python3
"""Staleness + convergence filter tests."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, _REPO_ROOT + "/daemon/launch_scanner")
from smartwatch_filters import (wallet_group, is_stale, eval_convergence,
                                is_expired, add_sighting)

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

T0 = 1_700_000_000.0
reg = {
    "AAA111": {"label": "pack-sniper-1"},
    "BBB222": {"label": "pack-sniper-2"},
    "CCC333": {"label": "grad-sniper-1"},
    "DDD444": {"label": "smart-flow"},
    "EEE555": {"label": ""},
}

# --- wallet_group: pack members are ONE actor ---
check("pack-sniper-1 -> pack-sniper", wallet_group("AAA111", reg) == "pack-sniper")
check("pack-sniper-2 -> pack-sniper", wallet_group("BBB222", reg) == "pack-sniper")
check("grad-sniper-1 -> grad-sniper", wallet_group("CCC333", reg) == "grad-sniper")
check("smart-flow stays", wallet_group("DDD444", reg) == "smart-flow")
check("empty label -> addr prefix", wallet_group("EEE555", reg) == "EEE555")
check("unknown wallet -> prefix", wallet_group("ZZZ999xxx", reg) == "ZZZ999xx")
check("never raises", wallet_group(None, None) == "")

# --- is_stale: 50% pump rule on the wallet's REAL buy price ---
# buy at 0.001 SOL/token, now 0.0016 SOL/token -> 1.6x -> stale
check("1.6x pump is stale", is_stale(0.001, 0.16, 100.0))
# 1.2x -> not stale
check("1.2x pump not stale", not is_stale(0.001, 0.12, 100.0))
# exactly 1.5x -> stale (>= rule)
check("1.5x boundary stale", is_stale(0.001, 0.15, 100.0))
# fail-open: unknown buy price never blocks
check("unknown buy price fail-open", not is_stale(0, 0.5, 100.0))
check("zero sol price fail-open", not is_stale(0.001, 0.5, 0))
check("never raises on garbage", is_stale("x", None, -1) is False)

# --- eval_convergence: 2+ INDEPENDENT groups within 30 min ---
s1 = [{"group": "pack-sniper", "buy_ts": T0},
      {"group": "pack-sniper", "buy_ts": T0 + 60}]
n, ok = eval_convergence(s1)
check("same pack x2 = 1 group, no fire", n == 1 and not ok)
s2 = [{"group": "pack-sniper", "buy_ts": T0},
      {"group": "smart-flow", "buy_ts": T0 + 240}]  # 4 min later
n, ok = eval_convergence(s2)
check("two groups converge", n == 2 and ok)
s3 = [{"group": "pack-sniper", "buy_ts": T0},
      {"group": "smart-flow", "buy_ts": T0 + 600}]  # 10 min later: outside 5-min window
n, ok = eval_convergence(s3)
check("second buy outside window ignored", n == 1 and not ok)
check("empty -> (0, False)", eval_convergence([]) == (0, False))
check("single -> no fire", eval_convergence([{"group": "a", "buy_ts": T0}]) == (1, False))

# --- is_expired ---
check("fresh not expired", not is_expired(T0, T0 + 100))
check("old expired", is_expired(T0, T0 + 2000))

# --- add_sighting: dedup same wallet, keep earliest ---
ledger = {}
rec = add_sighting(ledger, "MINT1", "AAA111", "pack-sniper", T0, 0.001, 1.0, T0 + 5)
rec = add_sighting(ledger, "MINT1", "AAA111", "pack-sniper", T0 + 50, 0.002, 1.0, T0 + 55)
check("same wallet not double-counted", len(rec["sightings"]) == 1)
check("kept earliest buy price", rec["sightings"][0]["buy_price_sol"] == 0.001)
rec = add_sighting(ledger, "MINT1", "DDD444", "smart-flow", T0 + 100, 0.0012, 0.5, T0 + 105)
n, ok = eval_convergence(rec["sightings"])
check("two actors -> converged", n == 2 and ok)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
