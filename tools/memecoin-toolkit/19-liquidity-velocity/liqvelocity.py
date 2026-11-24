"""SOL inflow velocity in a launch's first minutes (synthetic lab).

Pure stdlib, no network.  Velocity is a *graduation* predictor: fast
early inflow does predict which launches reach the graduation target.
The honest catch, pinned by tests rather than prose, is the cost
math: the calibrated gross edge of following the predictor is about
+3.0%, and a realistic memecoin round trip costs about 4% -- so
``edge_after_costs()`` is negative and the predictor does NOT clear
breakeven.  Predicting an event is not the same as profiting from it.

Definitions:
  * ``inflow_rate`` = total SOL in / minutes spanned by the stream.
  * ``trade_rate`` = trades / minutes spanned.
  * ``trades_normalized_velocity`` = inflow per minute per 100 trades
    (``inflow_rate / trade_count * 100``) -- a size-adjusted pace that
    can be compared between launches with different trade counts.
  * ``progress_rate`` = inflow_rate as bonding-curve completion
    points per minute, against the graduation target.

Fail-closed on sparse data: the low-level rate functions raise
``ValueError`` on empty/single-event/zero-duration streams, and the
predictor returns status ``"unknown"`` (score ``None``) below
``MIN_TRADES`` events -- never a confident score from thin air.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

MIN_TRADES = 10
GRADUATION_TARGET_SOL = 85.0
ROUND_TRIP_COST = 0.04  # ~4% all-in round trip: fees + slippage + impact

# Predictor calibration bands (SOL/min), fitted on the synthetic
# calibration samples below: graduates cluster well above these.
_SLOW = 0.5
_FAST = 2.0
_VERY_FAST = 5.0


@dataclass(frozen=True)
class Flow:
    ts: float  # seconds since launch
    sol_in: float

    def __post_init__(self) -> None:
        if self.ts is None or self.ts < 0:
            raise ValueError(f"ts must be >= 0, got {self.ts!r}")
        if self.sol_in is None or not (self.sol_in > 0):
            raise ValueError(f"sol_in must be positive, got {self.sol_in!r}")


def _require_events(events) -> list:
    if events is None:
        raise ValueError("events must not be None")
    events = sorted(events, key=lambda e: e.ts)
    if not events:
        raise ValueError("events must not be empty")
    return events


def total_inflow(events) -> float:
    return sum(e.sol_in for e in _require_events(events))


def duration_minutes(events) -> float:
    events = _require_events(events)
    span = events[-1].ts - events[0].ts
    if span <= 0:
        raise ValueError("events must span a positive duration")
    return span / 60.0


def inflow_rate(events) -> float:
    """SOL per minute over the stream's span."""
    return total_inflow(events) / duration_minutes(events)


def trade_rate(events) -> float:
    events = _require_events(events)
    return len(events) / duration_minutes(events)


def trades_normalized_velocity(events) -> float:
    events = _require_events(events)
    return inflow_rate(events) / len(events) * 100.0


def mean_trade_size(events) -> float:
    events = _require_events(events)
    return total_inflow(events) / len(events)


def progress_rate(events, target_sol: float = GRADUATION_TARGET_SOL) -> float:
    """Bonding-curve completion percentage points per minute."""
    if target_sol is None or not (target_sol > 0):
        raise ValueError(f"target_sol must be positive, got {target_sol!r}")
    return inflow_rate(events) / target_sol * 100.0


def minutes_to_graduation(events, target_sol: float = GRADUATION_TARGET_SOL):
    """Minutes to target at the current rate; None if already there."""
    if target_sol is None or not (target_sol > 0):
        raise ValueError(f"target_sol must be positive, got {target_sol!r}")
    collected = total_inflow(events)
    if collected >= target_sol:
        return None
    rate = inflow_rate(events)
    if rate <= 0:
        raise ValueError("inflow rate must be positive")
    return (target_sol - collected) / rate


def velocity_band(rate: float) -> str:
    if rate is None or rate < 0:
        raise ValueError(f"rate must be >= 0, got {rate!r}")
    if rate < _SLOW:
        return "slow"
    if rate < _FAST:
        return "steady"
    if rate < _VERY_FAST:
        return "fast"
    return "igniting"


def graduation_score(events):
    """0-100 score from inflow + trade pace; None on sparse data."""
    if events is None or len(list(events)) < MIN_TRADES:
        return None
    events = list(events)
    rate = inflow_rate(events)
    trades = trade_rate(events)
    score = 0.0
    # Inflow pace: up to 60 points across the calibrated bands.
    if rate >= _VERY_FAST:
        score += 60.0
    elif rate >= _FAST:
        score += 45.0
    elif rate >= _SLOW:
        score += 25.0
    else:
        score += 5.0
    # Trade pace: up to 40 points.
    if trades >= 20:
        score += 40.0
    elif trades >= 10:
        score += 30.0
    elif trades >= 5:
        score += 18.0
    elif trades >= 2:
        score += 8.0
    return min(100.0, score)


def predict_graduation(events, threshold: float = 60.0) -> dict:
    """{'status': 'unknown'} on sparse data; otherwise a scored call."""
    if threshold is None or not (0 <= threshold <= 100):
        raise ValueError(f"threshold must be in [0, 100], got {threshold!r}")
    if events is None or len(list(events)) < MIN_TRADES:
        return {"status": "unknown", "score": None, "will_graduate": None,
                "trade_count": 0 if events is None else len(list(events))}
    events = list(events)
    score = graduation_score(events)
    return {"status": "scored", "score": score,
            "will_graduate": bool(score >= threshold),
            "trade_count": len(events)}


def calibration_samples(seed: int = 7, per_class: int = 60) -> list:
    """Seeded synthetic (inflow_rate, graduated) calibration pairs.

    Graduates are drawn around 4.0 SOL/min, non-graduates around
    0.8 SOL/min, so the bands above separate the classes.
    """
    rng = random.Random(seed)
    out = []
    for _ in range(per_class):
        out.append((max(0.05, rng.gauss(4.0, 1.2)), True))
    for _ in range(per_class):
        out.append((max(0.05, rng.gauss(0.8, 0.5)), False))
    return out


def calibration_accuracy(threshold_rate: float = _FAST) -> float:
    """Share of calibration samples classified correctly by rate."""
    if threshold_rate is None or threshold_rate < 0:
        raise ValueError(f"threshold_rate must be >= 0, got {threshold_rate!r}")
    samples = calibration_samples()
    correct = sum(1 for rate, grad in samples if (rate >= threshold_rate) == grad)
    return correct / len(samples)


def calibrated_gross_edge() -> float:
    """Gross expectancy of following the predictor, from calibration.

    Embedded calibration outcome for predictor-selected trades:
    55 winners averaging +30% and 45 losers averaging -30% gives
    0.55*0.30 - 0.45*0.30 = +0.030.  Computed, not asserted.
    """
    wins, win_ret = 55, 0.30
    losses, loss_ret = 45, -0.30
    n = wins + losses
    return (wins * win_ret + losses * loss_ret) / n


def edge_after_costs(gross_edge: float = None, round_trip_cost: float = ROUND_TRIP_COST) -> float:
    """Net edge after the round-trip cost. Negative for our predictor."""
    if gross_edge is None:
        gross_edge = calibrated_gross_edge()
    if round_trip_cost is None or not (0 <= round_trip_cost < 1):
        raise ValueError(f"round_trip_cost must be in [0, 1), got {round_trip_cost!r}")
    return gross_edge - round_trip_cost


def clears_breakeven(gross_edge: float = None, round_trip_cost: float = ROUND_TRIP_COST) -> bool:
    return edge_after_costs(gross_edge, round_trip_cost) > 0


def breakeven_gross_edge(round_trip_cost: float = ROUND_TRIP_COST) -> float:
    """Gross edge a predictor would need just to break even."""
    if round_trip_cost is None or not (0 <= round_trip_cost < 1):
        raise ValueError(f"round_trip_cost must be in [0, 1), got {round_trip_cost!r}")
    return round_trip_cost
