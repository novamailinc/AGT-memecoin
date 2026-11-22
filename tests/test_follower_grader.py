#!/usr/bin/env python3
"""Follower-P&L grader tests: exit-ladder math and per-wallet aggregation."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, _REPO_ROOT + "/daemon/launch_scanner")
from follower_grader import simulate_buy, grade_followers

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol

# --- Exit ladder math (entry_penalty=0.15, round_trip=0.04) ---
# peak 3.0: 0.5@(2x) + 0.25@(3x) + 0.25@(0.7*peak), minus 4%
check("peak 3x", approx(simulate_buy(3.0), 0.938261))
# peak 2.0: 0.5@(2x) + 0.5@(0.7*peak), minus 4%
check("peak 2x", approx(simulate_buy(2.0), 0.438261))
# peak 0.8: never hit TP; trail off peak floored at -50% hard exit
check("peak 0.8x floors at hard exit", approx(simulate_buy(0.8), -0.54))
# peak 1.0: small loss after penalty + costs
check("peak 1x small loss", approx(simulate_buy(1.0), -0.431304))
# peak 1.5: still a loss — entry penalty + costs eat the move
check("peak 1.5x still loses", simulate_buy(1.5) < 0)
# no data -> 0, never raises
check("peak 0 -> 0", simulate_buy(0.0) == 0.0)
check("negative peak -> 0", simulate_buy(-1.0) == 0.0)

# --- Aggregation ---
T0 = 1_700_000_000
grades = {
    "walletAAA111": {"buys": [
        {"mint": "m1", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 3.0},
        {"mint": "m2", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 0.8},
        {"mint": "m3", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 2.0},
        {"mint": "m4", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 1.0},
        {"mint": "m5", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 0.5},
        {"mint": "m6", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 4.0},
    ]},
    "walletBBB222": {"buys": [  # thin data: unranked, not dropped
        {"mint": "m1", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 3.0},
    ]},
    "walletCCC333": {"buys": [  # unanchored / no peak -> excluded
        {"mint": "m1", "buy_ts": T0, "anchor_mcap": 0.0, "peak_mult": 5.0},
        {"mint": "m2", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 0.0},
    ]},
    "WALLET1": {"buys": [  # test fixture -> skipped
        {"mint": "m1", "buy_ts": T0, "anchor_mcap": 1000.0, "peak_mult": 9.0},
    ]},
}
out = grade_followers(grades, now=T0 + 100)
a = out["walletAAA111"]
check("6 follows counted", a["n_follows"] == 6)
check("not thin", not a["thin_data"])
# nets: 3x->0.938261, 0.8x->-0.54, 2x->0.438261, 1x->-0.431304,
#       0.5x->max(0.7*0.5/1.15-1,-0.5)-0.04 = -0.54, 4x->?
n4 = simulate_buy(4.0)
exp_avg = (0.938261 - 0.54 + 0.438261 - 0.431304 - 0.54 + n4) / 6
check("avg_net", approx(a["avg_net"], round(exp_avg, 4)))
check("wins counted", a["wins"] == sum(
    1 for x in (0.938261, -0.54, 0.438261, -0.431304, -0.54, n4) if x > 0))
b = out["walletBBB222"]
check("thin data flagged", b["thin_data"] and b["n_follows"] == 1)
c = out["walletCCC333"]
check("unanchored excluded", c["n_follows"] == 0)
check("fixture skipped", "WALLET1" not in out)
check("never raises on garbage", grade_followers(None) is not None or True)

# --- follower_weight: 1.0 + shrunk avg_net, clamped [0.5, 1.5] ---
from follower_grader import follower_weight
T0 = 1_700_000_000
pw = {
    # avg_net -0.44 over 100 follows -> 1 + (100*-0.44)/110 = 0.6
    "loser1": {"n_follows": 100, "avg_net": -0.44},
    # avg_net +0.30 over 20 -> 1 + 6/30 = 1.2
    "winner1": {"n_follows": 20, "avg_net": 0.30},
    # thin data -> 1.0
    "thin1": {"n_follows": 3, "avg_net": 0.90},
    # extreme loss clamps at 0.5: 1 + (50*-0.9)/60 = 0.25 -> 0.5
    "dead1": {"n_follows": 50, "avg_net": -0.9},
}
check("loser weight ~0.6", abs(follower_weight("loser1", pw) - 0.6) < 0.01)
check("winner weight ~1.2", abs(follower_weight("winner1", pw) - 1.2) < 0.01)
check("thin -> 1.0", follower_weight("thin1", pw) == 1.0)
check("unknown -> 1.0", follower_weight("nope", pw) == 1.0)
check("clamped at 0.5", follower_weight("dead1", pw) == 0.5)
check("never raises", follower_weight(None, None) == 1.0)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
