#!/usr/bin/env python3
"""Deployer Tracker: track pump.fun deployers with runner history.

From the 2026-10-14 research:
- Deployer = address paying fees for the token's creation transaction
- Retrieve creations via pump.fun Token Mint Authority:
  TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM
  (signs only create txs on bonding-curve program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P)
- Predictive metrics: previous runner count, launch cadence, holder retention
- Top-100 creators on trailing 2-week windows (validated predictive feature)
- Base rate: only ~1.41% of pump.fun tokens graduate — deployer history
  is one of the few filters that beats this

Factory detection: >2000 launches/month = fee-farming factory, negative signal.

Never raises. Paper-only.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("deployer_tracker")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_STATE_FILE = _HOME / "launch_scanner" / "deployer_tracker.json"

# pump.fun Token Mint Authority (signs only create transactions)
PUMP_MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Thresholds
FACTORY_LAUNCHES_PER_MONTH = 2000  # > this = fee-farming factory
RUNNER_MCAP_USD = 1_000_000        # sustained $1M+ = runner
TOP_CREATORS_WINDOW_D = 14        # trailing 2-week window
TOP_CREATORS_N = 100


def _load() -> Dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {"deployers": {}, "top_creators": [], "top_computed_ts": 0}


def _save(s: Dict):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(_STATE_FILE) + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, _STATE_FILE)
    try:
        os.chmod(_STATE_FILE, 0o600)
    except Exception:
        pass


def record_launch(deployer: str, mint: str, ts: float = None,
                  name: str = "", symbol: str = ""):
    """Record a token launch by a deployer."""
    ts = ts or time.time()
    try:
        s = _load()
        d = s["deployers"].setdefault(deployer, {
            "launches": [], "graduations": 0, "runners": 0,
            "first_seen_ts": ts, "is_factory": False,
            "total_launches": 0,
            "launch_ts_log": [],  # unbounded timestamps for cadence check
        })
        d["total_launches"] = d.get("total_launches", 0) + 1
        d.setdefault("launch_ts_log", []).append(ts)
        # Bound the timestamp log (keep 1 year worth, ~3000 max for factories)
        d["launch_ts_log"] = d["launch_ts_log"][-5000:]
        d["launches"].append({
            "mint": mint, "ts": ts, "name": name, "symbol": symbol,
            "graduated": False, "became_runner": False,
            "peak_mcap": 0,
        })
        # Bound launches per deployer (detail list; count tracked separately)
        d["launches"] = d["launches"][-500:]
        _check_factory(d)
        _save(s)
    except Exception:
        pass


def record_graduation(deployer: str, mint: str):
    """Mark a deployer's token as graduated (migrated to Raydium)."""
    try:
        s = _load()
        d = s["deployers"].get(deployer)
        if not d:
            return
        for launch in d["launches"]:
            if launch["mint"] == mint and not launch["graduated"]:
                launch["graduated"] = True
                d["graduations"] += 1
                break
        _save(s)
    except Exception:
        pass


def record_runner(deployer: str, mint: str, peak_mcap: float):
    """Mark a deployer's token as a runner (sustained $1M+ mcap)."""
    try:
        s = _load()
        d = s["deployers"].get(deployer)
        if not d:
            return
        for launch in d["launches"]:
            if launch["mint"] == mint:
                launch["peak_mcap"] = max(launch.get("peak_mcap", 0), peak_mcap)
                if peak_mcap >= RUNNER_MCAP_USD and not launch["became_runner"]:
                    launch["became_runner"] = True
                    d["runners"] += 1
                break
        _save(s)
    except Exception:
        pass


def _check_factory(d: Dict):
    """Flag as factory if launch cadence exceeds threshold."""
    now = time.time()
    month_ago = now - 30 * 86400
    # Use unbounded timestamp log (the detail list is capped at 500)
    ts_log = d.get("launch_ts_log", [])
    if ts_log:
        recent = sum(1 for t in ts_log if t >= month_ago)
    else:
        # Legacy: fall back to detail list
        recent = sum(1 for l in d["launches"] if l["ts"] >= month_ago)
    d["is_factory"] = recent >= FACTORY_LAUNCHES_PER_MONTH
    d["launches_last_30d"] = recent


def deployer_score(deployer: str) -> Dict:
    """Score a deployer 0-100 based on track record.

    Positive: runners, graduations, reasonable cadence
    Negative: factory behavior, zero runners across many launches
    """
    s = _load()
    d = s["deployers"].get(deployer)
    if not d:
        return {"score": 0, "reason": "unknown deployer", "is_factory": False}

    if d.get("is_factory"):
        return {"score": 0, "reason": "factory (fee-farming)",
                "is_factory": True}

    n = len(d["launches"])
    runners = d.get("runners", 0)
    grads = d.get("graduations", 0)

    score = 0
    reasons = []

    # Runner history is the strongest signal
    if runners >= 3:
        score += 40
        reasons.append(f"+40 {runners} runners")
    elif runners >= 1:
        score += 25
        reasons.append(f"+25 {runners} runner")

    # Graduation rate above base rate (1.41%)
    if n >= 10:
        grad_rate = grads / n
        if grad_rate > 0.05:  # 5%+ = well above base rate
            score += 20
            reasons.append(f"+20 grad rate {grad_rate:.1%}")
        elif grad_rate > 0.0141:
            score += 10
            reasons.append(f"+10 grad rate {grad_rate:.1%}")

    # Experience (but not factory-level)
    if 10 <= n <= 200:
        score += 10
        reasons.append(f"+10 {n} launches")

    # Penalty: many launches, zero runners
    if n >= 20 and runners == 0:
        score = max(0, score - 20)
        reasons.append("-20 no runners in 20+ launches")

    return {
        "score": min(100, score),
        "reasons": reasons,
        "is_factory": False,
        "launches": n,
        "runners": runners,
        "graduations": grads,
    }


def compute_top_creators() -> List[Dict]:
    """Top-100 creators on trailing 2-week window by runner count.

    Cached; recomputed at most once per hour.
    """
    s = _load()
    now = time.time()
    if now - s.get("top_computed_ts", 0) < 3600 and s.get("top_creators"):
        return s["top_creators"]

    window_start = now - TOP_CREATORS_WINDOW_D * 86400
    scored = []
    for deployer, d in s["deployers"].items():
        if d.get("is_factory"):
            continue
        recent_runners = sum(
            1 for l in d["launches"]
            if l["ts"] >= window_start and l.get("became_runner")
        )
        recent_launches = sum(1 for l in d["launches"] if l["ts"] >= window_start)
        if recent_launches > 0:
            scored.append({
                "deployer": deployer,
                "recent_runners": recent_runners,
                "recent_launches": recent_launches,
            })

    scored.sort(key=lambda x: (-x["recent_runners"], -x["recent_launches"]))
    top = scored[:TOP_CREATORS_N]
    s["top_creators"] = top
    s["top_computed_ts"] = now
    _save(s)
    return top


def is_top_creator(deployer: str) -> bool:
    """Check if deployer is in the trailing-2-week Top-100."""
    return any(c["deployer"] == deployer for c in compute_top_creators())


def get_deployer_stats(deployer: str) -> Optional[Dict]:
    s = _load()
    return s["deployers"].get(deployer)
