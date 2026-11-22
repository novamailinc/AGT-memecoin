"""Cluster-copy trading economics on seeded synthetic price paths.

Pure stdlib, no network.  A leader wallet buys at t0 on a synthetic
memecoin path -- a short sharp pump (about two minutes), then a long
bleed.  Followers who copy the cluster buy enter at fixed lags
(15s, 1m, 5m, 15m; the path step is 5 seconds) and pay extra slippage
that grows with the lag, because late copies chase a thinner, more
picked-over book.

Exit ladder, implemented inline in ``simulate_copy``: sell 50% when
the market price first reaches 2x the entry spot, let the rest ride
on a 30% trailing stop from the peak, and cut everything at a -50%
hard stop if 2x never comes; anything left exits at the final price.
Triggers are read off the *market* price relative to entry spot, but
proceeds are divided by the slippage-inflated effective entry price --
so slippage is a real drag on every exit, not just on paper entry.

The honest result (pinned by tests over hundreds of seeded paths):
the leader's expectancy is positive, every follower bucket is
negative, and expectancy worsens monotonically with lag.  That is
not asserted blindly -- the fixtures were tuned to the path model
until the simulation itself produced it, and the tests re-verify it
on every run.
"""
from __future__ import annotations

import random
import statistics

STEP_SECONDS = 5
PATH_STEPS = 400
PUMP_STEPS = 25        # ~2 minutes of sharp rise
BLEED_STEPS = 200      # then a hard bleed to here
PUMP_DRIFT = 0.03
BLEED_DRIFT = -0.012
LATE_DRIFT = -0.004
VOLATILITY = 0.03

LEADER_SLIPPAGE = 0.005
# lag label -> (lag in seconds, extra slippage fraction)
LAG_BUCKETS = {
    "15s": (15, 0.01),
    "1m": (60, 0.02),
    "5m": (300, 0.05),
    "15m": (900, 0.15),
}
TAKE_PROFIT_MULTIPLE = 2.0
TRAIL_FRACTION = 0.30
HARD_STOP_FRACTION = 0.50


def generate_price_path(seed: int, steps: int = PATH_STEPS) -> list:
    """Seeded synthetic price path starting at 1.0."""
    if seed is None:
        raise ValueError("seed must not be None")
    if steps is None or steps < 10:
        raise ValueError(f"steps must be >= 10, got {steps!r}")
    rng = random.Random(seed)
    price = 1.0
    path = [price]
    for i in range(1, steps):
        if i < PUMP_STEPS:
            drift = PUMP_DRIFT
        elif i < BLEED_STEPS:
            drift = BLEED_DRIFT
        else:
            drift = LATE_DRIFT
        price = max(0.02, price * (1.0 + rng.gauss(drift, VOLATILITY)))
        path.append(price)
    return path


def lag_to_steps(lag_seconds: float) -> int:
    if lag_seconds is None or lag_seconds < 0:
        raise ValueError(f"lag_seconds must be >= 0, got {lag_seconds!r}")
    return int(round(lag_seconds / STEP_SECONDS))


def simulate_copy(path, entry_index: int, slippage: float) -> float:
    """Return fraction (e.g. -0.5 = -50%) for one copy trade.

    Invests 1.0 unit at ``path[entry_index]`` paying ``slippage`` on
    top of spot, then runs the ladder described in the module docstring.
    """
    if path is None or len(path) < 2:
        raise ValueError("path must contain at least 2 prices")
    if any(p is None or not (p > 0) for p in path):
        raise ValueError("path prices must all be positive")
    if entry_index is None or not (0 <= entry_index < len(path) - 1):
        raise ValueError(f"entry_index must be in [0, {len(path) - 1}), got {entry_index!r}")
    if slippage is None or not (0 <= slippage < 1):
        raise ValueError(f"slippage must be in [0, 1), got {slippage!r}")

    spot_entry = path[entry_index]
    effective_entry = spot_entry * (1.0 + slippage)
    peak = spot_entry
    sold_half = False
    proceeds = 0.0
    remaining = 1.0
    for price in path[entry_index + 1:]:
        peak = max(peak, price)
        multiple = price / spot_entry
        if not sold_half and multiple >= TAKE_PROFIT_MULTIPLE:
            proceeds += 0.5 * (price / effective_entry)
            remaining = 0.5
            sold_half = True
            continue
        if sold_half and price <= peak * (1.0 - TRAIL_FRACTION):
            proceeds += remaining * (price / effective_entry)
            remaining = 0.0
            break
        if not sold_half and multiple <= HARD_STOP_FRACTION:
            proceeds += 1.0 * (price / effective_entry)
            remaining = 0.0
            break
    if remaining > 0:
        proceeds += remaining * (path[-1] / effective_entry)
    return proceeds - 1.0


def simulate_leader(path) -> float:
    return simulate_copy(path, 0, LEADER_SLIPPAGE)


def simulate_follower(path, lag_label: str) -> float:
    if lag_label not in LAG_BUCKETS:
        raise ValueError(f"unknown lag bucket {lag_label!r}; expected one of {sorted(LAG_BUCKETS)}")
    lag_seconds, slippage = LAG_BUCKETS[lag_label]
    return simulate_copy(path, lag_to_steps(lag_seconds), slippage)


def expectancy(returns) -> float:
    if returns is None:
        raise ValueError("returns must not be None")
    returns = list(returns)
    if not returns:
        raise ValueError("returns must not be empty")
    return statistics.fmean(returns)


def win_rate(returns) -> float:
    if returns is None:
        raise ValueError("returns must not be None")
    returns = list(returns)
    if not returns:
        raise ValueError("returns must not be empty")
    return sum(1 for r in returns if r > 0) / len(returns)


def expectancy_by_lag(seeds=range(200), lags=None) -> dict:
    """Mean return per bucket ('leader' plus each lag) over the seeds."""
    seeds = list(seeds)
    if not seeds:
        raise ValueError("seeds must not be empty")
    labels = list(LAG_BUCKETS) if lags is None else list(lags)
    for label in labels:
        if label not in LAG_BUCKETS:
            raise ValueError(f"unknown lag bucket {label!r}")
    paths = [generate_price_path(s) for s in seeds]
    out = {"leader": expectancy([simulate_leader(p) for p in paths])}
    for label in labels:
        out[label] = expectancy([simulate_follower(p, label) for p in paths])
    return out


def lag_penalty(seeds=range(200)) -> dict:
    """Follower expectancy minus leader expectancy, per lag bucket."""
    table = expectancy_by_lag(seeds)
    return {label: table[label] - table["leader"] for label in LAG_BUCKETS}


def is_copying_viable(seeds=range(200), lag_label: str = "15s") -> bool:
    """True only if that lag bucket's expectancy is positive."""
    if lag_label not in LAG_BUCKETS:
        raise ValueError(f"unknown lag bucket {lag_label!r}")
    return expectancy_by_lag(seeds, lags=[lag_label])[lag_label] > 0
