#!/usr/bin/env python3
"""Thompson Sampling bandit for lane selection.

Which lane gets to trade? Instead of vibes, use the math.

Each lane (stacker, steady, degen_v2, main, nadir) is a bandit arm with a
Beta(wins+1, losses+1) posterior. Before each entry opportunity, sample
from each arm's posterior and favor the lane that's actually winning.
[VERIFIED] Zhu et al., arXiv 1911.05309 — regret bounds: performs nearly
as well as the best fixed lane in hindsight.

Design (from 2026-10-15 AI-tools research):
- Rewards are PNL-SCALED, not binary win/loss. A +5x win counts more
  than a +0.1x win. reward = sign(pnl) * min(1, |pnl|/size).
- Time decay: old observations fade (half-life 14 days) so a lane
  that was good in September doesn't coast on stale glory in October.
- Shadow mode FIRST: log what it would allocate for 50+ closes before
  it gates anything. The kill switch keeps its veto always — the bandit
  allocates opportunity, the kill switch can still say no.
- Cold start: uniform Beta(1,1) — new lanes get exploration automatically.

Kill criteria: can't separate lanes after 150 closes -> kill it,
keep kill-switch-only.
"""

import json
import math
import os
import random
import time
from pathlib import Path

HOME = Path(os.environ.get("FOREX_AGENT_HOME", str(Path.home() / ".forex-agent")))
BANDIT_PATH = HOME / "launch_scanner" / "lane_bandit.json"

# Half-life for observation decay (seconds): 14 days
HALF_LIFE_SEC = 14 * 24 * 3600
# Minimum closes before the bandit is trusted (shadow mode until then)
MIN_CLOSES_TRUSTED = 50
# Kill criterion
KILL_CLOSES = 150

LANES = ["stacker", "steady", "degen_v2", "main", "nadir"]


class LaneBandit:
    """Thompson Sampling over paper-trading lanes."""

    def __init__(self):
        # lane -> {"w": decayed_wins, "l": decayed_losses,
        #          "n": total_closes, "last_update": ts}
        self.arms = {lane: {"w": 0.0, "l": 0.0, "n": 0,
                            "last_update": 0.0}
                     for lane in LANES}
        self.shadow_log = []  # (ts, selected, samples) for audit

    def _decay(self, lane, now):
        """Apply time decay to an arm's counts."""
        arm = self.arms[lane]
        dt = now - (arm["last_update"] or now)
        if dt > 0:
            factor = 0.5 ** (dt / HALF_LIFE_SEC)
            arm["w"] *= factor
            arm["l"] *= factor
            arm["last_update"] = now

    def update(self, lane, pnl, size, ts=None):
        """Record a closed trade. pnl/size in same units.

        reward in [-1, +1]: sign(pnl) * min(1, |pnl|/size).
        Wins add to w, losses add to l, scaled by magnitude.
        """
        if lane not in self.arms:
            return
        now = ts or time.time()
        self._decay(lane, now)
        arm = self.arms[lane]
        size = abs(size) or 1.0
        mag = min(1.0, abs(pnl) / size)
        if pnl > 0:
            arm["w"] += mag
        elif pnl < 0:
            arm["l"] += mag
        # pnl == 0: no information, skip
        arm["n"] += 1
        arm["last_update"] = now

    def _sample(self, lane, now):
        """One Thompson sample from the arm's Beta posterior."""
        self._decay(lane, now)
        arm = self.arms[lane]
        alpha = arm["w"] + 1.0
        beta = arm["l"] + 1.0
        return random.betavariate(alpha, beta)

    def select(self, lanes=None, now=None):
        """Thompson Sampling: sample each arm, return the winner.

        Returns (selected_lane, {lane: sample}).
        In shadow mode the caller logs this instead of acting on it.
        """
        now = now or time.time()
        lanes = lanes or [l for l in LANES if self.arms[l]["n"] > 0]
        if not lanes:
            lanes = list(LANES)  # cold start: all uniform
        samples = {l: self._sample(l, now) for l in lanes}
        best = max(samples, key=samples.get)
        return best, samples

    def posterior_mean(self, lane, now=None):
        """E[win rate] for a lane. For display/diagnostics."""
        now = now or time.time()
        self._decay(lane, now)
        arm = self.arms[lane]
        return (arm["w"] + 1.0) / (arm["w"] + arm["l"] + 2.0)

    def total_closes(self):
        return sum(a["n"] for a in self.arms.values())

    def trusted(self):
        """Has the bandit seen enough to gate entries?"""
        return self.total_closes() >= MIN_CLOSES_TRUSTED

    def should_kill(self):
        """Kill criterion: 150+ closes and posteriors haven't separated.

        Separation = max posterior mean - min posterior mean > 0.1
        among lanes with 10+ closes. If they're all the same, the
        bandit adds nothing over kill-switch-only.
        """
        if self.total_closes() < KILL_CLOSES:
            return False
        now = time.time()
        means = [self.posterior_mean(l, now) for l in LANES
                 if self.arms[l]["n"] >= 10]
        if len(means) < 2:
            return False
        return (max(means) - min(means)) <= 0.10

    def log_shadow(self, selected, samples):
        """Record a shadow-mode decision for audit."""
        self.shadow_log.append({
            "ts": time.time(),
            "selected": selected,
            "samples": {k: round(v, 3) for k, v in samples.items()},
        })
        # bound the log
        self.shadow_log = self.shadow_log[-1000:]

    def to_dict(self):
        return {
            "arms": self.arms,
            "shadow_log": self.shadow_log[-100:],
            "updated_at": time.time(),
        }

    @classmethod
    def from_dict(cls, d):
        b = cls()
        if isinstance(d, dict):
            for lane, arm in (d.get("arms") or {}).items():
                if lane in b.arms and isinstance(arm, dict):
                    b.arms[lane] = {
                        "w": float(arm.get("w", 0)),
                        "l": float(arm.get("l", 0)),
                        "n": int(arm.get("n", 0)),
                        "last_update": float(arm.get("last_update", 0)),
                    }
            b.shadow_log = d.get("shadow_log") or []
        return b

    def save(self, path=None):
        p = Path(path) if path else BANDIT_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1))
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass

    @classmethod
    def load(cls, path=None):
        p = Path(path) if path else BANDIT_PATH
        try:
            return cls.from_dict(json.loads(p.read_text()))
        except Exception:
            return cls()
