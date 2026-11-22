#!/usr/bin/env python3
"""Tests for the lane bandit (Thompson Sampling).

The bandit must: favor winning lanes, explore new lanes, decay old
evidence, and honestly report when it can't separate lanes (kill).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "daemon", "launch_scanner"))

from lane_bandit import LaneBandit

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" " + detail if detail and not cond else ""))


# --- cold start: uniform, all lanes explorable ---
b = LaneBandit()
check("cold start not trusted", not b.trusted())
check("cold start posterior = 50%", b.posterior_mean("stacker") == 0.5)
sel, samples = b.select()
check("cold start selects something", sel in samples)

# --- learns from rewards: winning lane favored ---
b2 = LaneBandit()
now = time.time()
for _ in range(30):
    b2.update("stacker", pnl=1.0, size=1.0, ts=now)   # 30 wins
for _ in range(30):
    b2.update("main", pnl=-1.0, size=1.0, ts=now)      # 30 losses
check("winner posterior > loser",
      b2.posterior_mean("stacker", now) > b2.posterior_mean("main", now))
# Thompson sample the winner many times: should win the majority
wins = sum(1 for _ in range(200)
           if b2.select(["stacker", "main"], now)[0] == "stacker")
check("Thompson favors winning lane", wins > 150, f"wins={wins}/200")

# --- pnl-scaled rewards: big win counts more than small win ---
b3 = LaneBandit()
b3.update("stacker", pnl=0.1, size=1.0, ts=now)   # small win
b3.update("steady", pnl=1.0, size=1.0, ts=now)    # big win (capped at 1.0)
check("magnitude matters",
      b3.arms["steady"]["w"] > b3.arms["stacker"]["w"])

# --- time decay: old evidence fades ---
b4 = LaneBandit()
old = now - 30 * 24 * 3600  # 30 days ago
b4.update("stacker", pnl=1.0, size=1.0, ts=old)
w_before = b4.arms["stacker"]["w"]
b4._decay("stacker", now)
w_after = b4.arms["stacker"]["w"]
# 30 days = ~2.14 half-lives -> factor ~0.23
check("old evidence decays", w_after < w_before * 0.5,
      f"{w_before:.3f} -> {w_after:.3f}")

# --- kill criterion: identical lanes -> should_kill ---
b5 = LaneBandit()
for i in range(80):
    b5.update("stacker", pnl=1.0 if i % 2 else -1.0, size=1.0, ts=now)
    b5.update("main", pnl=1.0 if i % 2 else -1.0, size=1.0, ts=now)
check("150+ closes, no separation -> kill",
      b5.should_kill(), f"n={b5.total_closes()}")
check("under 150 closes -> not killed", not b2.should_kill())

# --- trust threshold ---
b6 = LaneBandit()
for _ in range(49):
    b6.update("main", pnl=-0.01, size=0.025, ts=now)
check("49 closes: not trusted", not b6.trusted())
b6.update("main", pnl=-0.01, size=0.025, ts=now)
check("50 closes: trusted", b6.trusted())

# --- persistence round-trip ---
import tempfile
with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
    p = tf.name
b6.save(p)
b7 = LaneBandit.load(p)
check("save/load preserves counts",
      b7.arms["main"]["n"] == 50, str(b7.arms["main"]["n"]))
os.unlink(p)

# --- unknown lane ignored, zero pnl = close but no directional info ---
b8 = LaneBandit()
b8.update("nonexistent", pnl=1.0, size=1.0)
check("unknown lane ignored", b8.total_closes() == 0)
b8.update("stacker", pnl=0.0, size=1.0)
check("zero pnl counts as close", b8.total_closes() == 1)
check("zero pnl adds no directional info",
      b8.arms["stacker"]["w"] == 0 and b8.arms["stacker"]["l"] == 0
      and b8.posterior_mean("stacker") == 0.5)

print(f"\n{len(FAIL)} failures")
sys.exit(1 if FAIL else 0)
