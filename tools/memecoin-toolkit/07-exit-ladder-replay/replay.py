"""Candle-path replay engine for Solana memecoin exit ladders.

Pure stdlib, no network, no look-ahead: bars are evaluated strictly in
time order and a fill can only use information from bars up to and
including the current bar. Entry is at t=0 at price ``p0 = path[0][1]``.

Fill-price conventions (documented, deterministic):
  * take-profit tranche  -> filled at its target price (target * p0)
  * trailing tranche     -> filled at peak * (1 - trail_pct)
  * hard stop            -> filled at the hard-stop price (stop * p0),
                            and exits ALL remaining tranches at once
  * time cap / path end  -> filled at that bar's actual price

Fail-closed behaviour: an empty path, a non-positive entry price, a
path whose first timestamp is not 0, non-increasing timestamps, or an
unknown ladder name all raise ``ValueError`` instead of guessing.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

# A price path is a list of (t_minutes, price) points.
PricePath = list[tuple[float, float]]


@dataclass(frozen=True)
class Tranche:
    """One slice of a position with its own exit rule."""

    fraction: float
    kind: str  # "tp" | "trail" | "hold"
    target_mult: float | None = None  # for kind == "tp"
    trail_pct: float | None = None  # for kind == "trail"
    arm_mult: float | None = None  # trail arms only at/above this mult


@dataclass(frozen=True)
class Ladder:
    name: str
    tranches: tuple[Tranche, ...]
    hard_stop_mult: float  # price <= this * p0 exits everything
    time_cap_min: float


USER_LADDER = Ladder(
    name="user_ladder",
    tranches=(
        Tranche(0.50, "tp", target_mult=2.0),
        Tranche(0.25, "tp", target_mult=3.0),
        # The trailing remainder only arms once the position has doubled:
        # before 2.0x the hard stop alone protects it, otherwise a coin
        # that never ran would "trail" out at e.g. 0.77x on its first dip
        # and the hard-stop test on a straight rug could never fire cleanly.
        Tranche(0.25, "trail", trail_pct=0.30, arm_mult=2.0),
    ),
    hard_stop_mult=0.5,
    time_cap_min=1440.0,
)

FAST_HARVEST = Ladder(
    name="fast_harvest",
    tranches=(
        Tranche(0.50, "tp", target_mult=1.5),
        Tranche(0.25, "tp", target_mult=2.0),
        Tranche(0.25, "trail", trail_pct=0.20, arm_mult=1.5),
    ),
    hard_stop_mult=0.6,
    time_cap_min=240.0,
)

HOLD_TO_STOP = Ladder(
    name="hold_to_stop",
    tranches=(Tranche(1.0, "hold"),),
    hard_stop_mult=0.5,
    time_cap_min=1440.0,
)

LADDERS: dict[str, Ladder] = {lad.name: lad for lad in (USER_LADDER, FAST_HARVEST, HOLD_TO_STOP)}


@dataclass
class Leg:
    fraction: float
    exit_price: float
    exit_time: float
    reason: str  # tp | trail | hard_stop | time_cap | path_end


@dataclass
class ReplayResult:
    ladder: str
    realized_mult: float
    peak_mult: float
    legs: list[Leg] = field(default_factory=list)

    @property
    def filled_fraction(self) -> float:
        return sum(leg.fraction for leg in self.legs)


def _validate(path: PricePath) -> float:
    if not path:
        raise ValueError("empty path: fail-closed, no replay")
    if path[0][0] != 0:
        raise ValueError("path must start at t=0 (entry bar)")
    p0 = float(path[0][1])
    if not math.isfinite(p0) or p0 <= 0:
        raise ValueError("entry price p0 must be positive")
    prev_t = None
    for t, price in path:
        if not math.isfinite(float(t)) or not math.isfinite(float(price)):
            raise ValueError("timestamps and prices must be finite")
        if price <= 0:
            raise ValueError("prices must be positive")
        if prev_t is not None and t <= prev_t:
            raise ValueError("timestamps must be strictly increasing")
        prev_t = t
    return p0


def _require_mult(mult: float) -> float:
    if mult is None or not math.isfinite(float(mult)) or float(mult) <= 0:
        raise ValueError(f"multiple must be positive and finite, got {mult!r}")
    return float(mult)


def validate_ladder(ladder: Ladder) -> Ladder:
    """Validate a (possibly custom) ladder, fail-closed.

    A ladder is tradeable only if its tranches are well-formed and
    their fractions sum to exactly the whole position (1.0, within
    1e-6): a ladder that silently trades 80% — or 120% — of a position
    would fabricate or lose money in exactly the way a replay exists
    to catch. Returns the ladder unchanged on success.
    """
    if not isinstance(ladder, Ladder):
        raise ValueError(f"not a Ladder: {ladder!r}")
    if not isinstance(ladder.name, str) or not ladder.name:
        raise ValueError("ladder name must be a non-empty string")
    if not ladder.tranches:
        raise ValueError("ladder must have at least one tranche")
    total = 0.0
    for tr in ladder.tranches:
        if not (0.0 < tr.fraction <= 1.0):
            raise ValueError(f"tranche fraction must be in (0, 1], got {tr.fraction!r}")
        total += tr.fraction
        if tr.kind not in ("tp", "trail", "hold"):
            raise ValueError(f"unknown tranche kind {tr.kind!r}")
        if tr.kind == "tp":
            if tr.target_mult is None or not math.isfinite(tr.target_mult) \
                    or tr.target_mult <= 1.0:
                raise ValueError("tp tranche needs a finite target_mult > 1.0")
        if tr.kind == "trail":
            if tr.trail_pct is None or not (0.0 < tr.trail_pct < 1.0):
                raise ValueError("trail tranche needs trail_pct in (0, 1)")
            if tr.arm_mult is not None and (
                    not math.isfinite(tr.arm_mult) or tr.arm_mult <= 0):
                raise ValueError("trail arm_mult must be positive and finite")
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise ValueError(f"tranche fractions must sum to 1.0, got {total!r}")
    if not (0.0 < ladder.hard_stop_mult < 1.0):
        raise ValueError("hard_stop_mult must be in (0, 1)")
    if not math.isfinite(ladder.time_cap_min) or ladder.time_cap_min <= 0:
        raise ValueError("time_cap_min must be positive and finite")
    return ladder


def _resolve_ladder(ladder: str | Ladder) -> Ladder:
    if isinstance(ladder, Ladder):
        return validate_ladder(ladder)
    if isinstance(ladder, str) and ladder in LADDERS:
        return LADDERS[ladder]
    raise ValueError(f"unknown ladder {ladder!r}: fail-closed")


def replay(path: PricePath, ladder: str | Ladder = "user_ladder") -> ReplayResult:
    """Replay ``path`` bar-by-bar under ``ladder``. No look-ahead."""
    lad = _resolve_ladder(ladder)
    p0 = _validate(path)
    # Mutable per-tranche state; index-aligned with lad.tranches.
    open_tranches: list[Tranche | None] = list(lad.tranches)
    legs: list[Leg] = []
    peak = p0
    last_t, last_price = path[0]

    def close(idx: int, price: float, t: float, reason: str) -> None:
        tr = open_tranches[idx]
        assert tr is not None
        legs.append(Leg(tr.fraction, price, t, reason))
        open_tranches[idx] = None

    def close_all(price: float, t: float, reason: str) -> None:
        for i in range(len(open_tranches)):
            if open_tranches[i] is not None:
                close(i, price, t, reason)

    def any_open() -> bool:
        return any(tr is not None for tr in open_tranches)

    for t, price in path:
        if t > lad.time_cap_min:
            break  # beyond the cap: this bar is never seen by the ladder
        last_t, last_price = t, price
        peak = max(peak, price)
        # 1) Hard stop: global, exits everything at the stop price.
        if any_open() and price <= lad.hard_stop_mult * p0:
            close_all(lad.hard_stop_mult * p0, t, "hard_stop")
            break
        # 2) Take-profits, in tranche order, at the target price.
        for i, tr in enumerate(open_tranches):
            if tr is not None and tr.kind == "tp" and price >= tr.target_mult * p0:
                close(i, tr.target_mult * p0, t, "tp")
        # 3) Trailing tranches, at the trailing price off the running peak.
        for i, tr in enumerate(open_tranches):
            if tr is not None and tr.kind == "trail":
                armed = tr.arm_mult is None or peak >= tr.arm_mult * p0
                trail_price = peak * (1.0 - tr.trail_pct)
                if armed and peak > p0 and price <= trail_price:
                    close(i, trail_price, t, "trail")
        # 4) Time cap: exit the remainder at this bar's price.
        if any_open() and t >= lad.time_cap_min:
            close_all(price, t, "time_cap")
            break
        if not any_open():
            break

    if any_open():  # path ended (or ran past the cap) with size left
        past_cap = path[-1][0] > lad.time_cap_min
        reason = "time_cap" if (last_t >= lad.time_cap_min or past_cap) else "path_end"
        close_all(last_price, last_t, reason)

    realized = sum(leg.fraction * leg.exit_price for leg in legs) / p0
    peak_mult = max(price for _, price in path) / p0
    return ReplayResult(ladder=lad.name, realized_mult=realized,
                        peak_mult=peak_mult, legs=legs)


def time_to_peak(path: PricePath) -> float:
    """Minutes from entry to the first bar at the maximum price."""
    _validate(path)
    best_t, best_price = path[0]
    for t, price in path:
        if price > best_price:
            best_t, best_price = t, price
    return float(best_t)


def peaked_ge(path: PricePath, mult: float) -> bool:
    """True iff the path ever reached ``mult`` x entry price."""
    p0 = _validate(path)
    return max(price for _, price in path) >= _require_mult(mult) * p0


def replay_batch(paths: list[PricePath], ladder: str | Ladder = "user_ladder") -> dict:
    """Aggregate stats over ``paths``. Empty input -> zeros / 0.0 (no crash)."""
    if not paths:
        return {"mean_mult": 0.0, "win_rate": 0.0, "round_trippers": 0,
                "median_time_to_peak_min": 0.0}
    results = [replay(p, ladder) for p in paths]
    mults = [r.realized_mult for r in results]
    return {
        "mean_mult": sum(mults) / len(mults),
        "win_rate": sum(1 for m in mults if m >= 1.0) / len(mults),
        "round_trippers": sum(
            1 for r in results if r.realized_mult < 1.0 and r.peak_mult >= 2.0),
        "median_time_to_peak_min": float(statistics.median(
            time_to_peak(p) for p in paths)),
    }


def peak_multiple(path: PricePath) -> float:
    """Highest price on the path as a multiple of the entry price."""
    p0 = _validate(path)
    return max(price for _, price in path) / p0


def first_crossing_time(path: PricePath, mult: float) -> float | None:
    """First timestamp at which price reaches ``mult`` x entry, or None.

    Only bars at or before the crossing are consulted to decide it —
    the answer is the first such bar, never an interpolated or
    future-informed one.
    """
    p0 = _validate(path)
    mult = _require_mult(mult)
    for t, price in path:
        if price >= mult * p0:
            return float(t)
    return None


def time_above_multiple(path: PricePath, mult: float) -> float:
    """Minutes spent at or above ``mult`` x entry.

    Discrete-bar convention: the interval from bar i to bar i+1 counts
    iff bar i's own price is at/above the threshold; the final bar
    contributes nothing (no following interval is known).
    """
    p0 = _validate(path)
    mult = _require_mult(mult)
    total = 0.0
    for (t, price), (next_t, _) in zip(path, path[1:]):
        if price >= mult * p0:
            total += float(next_t) - float(t)
    return total


def max_drawdown(path: PricePath) -> float:
    """Largest peak-to-trough fall as a fraction of the running peak.

    0.0 for a path that never falls from a prior peak; 0.5 for a path
    that halves from its highest point so far. Uses only the running
    peak up to each bar (no future peak can deepen a past drawdown).
    """
    _validate(path)
    peak = path[0][1]
    worst = 0.0
    for _, price in path:
        peak = max(peak, price)
        if peak > 0:
            worst = max(worst, (peak - price) / peak)
    return worst


def is_round_tripper(result: ReplayResult) -> bool:
    """True iff the path doubled but the position still closed a loser."""
    return result.peak_mult >= 2.0 and result.realized_mult < 1.0


def exit_reason_breakdown(result: ReplayResult) -> dict[str, float]:
    """Fraction of the position closed for each exit reason (sums to 1)."""
    out: dict[str, float] = {}
    for leg in result.legs:
        out[leg.reason] = out.get(leg.reason, 0.0) + leg.fraction
    return out


def weighted_exit_time(result: ReplayResult) -> float:
    """Position-weighted mean exit time in minutes.

    Raises ValueError on a result with no legs (fail-closed: a mean
    over nothing would be a plausible-looking lie).
    """
    if not result.legs:
        raise ValueError("no legs: weighted exit time is undefined")
    total = sum(leg.fraction for leg in result.legs)
    if total <= 0:
        raise ValueError("legs carry no fraction: fail-closed")
    return sum(leg.fraction * leg.exit_time for leg in result.legs) / total


def last_exit_time(result: ReplayResult) -> float:
    """Timestamp of the final leg's exit; ValueError if there are no legs."""
    if not result.legs:
        raise ValueError("no legs: last exit time is undefined")
    return max(leg.exit_time for leg in result.legs)


def compare_ladders(path: PricePath,
                    ladders: list[str] | None = None) -> dict[str, ReplayResult]:
    """Replay one path under several ladders, keyed by ladder name.

    Defaults to every ladder in ``LADDERS`` (in definition order).
    An empty ladder list raises ValueError (fail-closed), and any
    unknown name raises via :func:`replay`.
    """
    names = list(LADDERS) if ladders is None else list(ladders)
    if not names:
        raise ValueError("no ladders to compare: fail-closed")
    return {name: replay(path, name) for name in names}


# --- Synthetic fixtures (p0 = 1.0, prices are multiples of entry) ---

def make_round_tripper() -> PricePath:
    """Peaks 2.6x, fades through the trail and the hard stop to 0.3x."""
    return [(0, 1.0), (10, 1.2), (20, 1.6), (30, 2.0), (40, 2.4), (50, 2.6),
            (60, 2.2), (70, 1.8), (80, 1.4), (90, 1.0), (100, 0.6),
            (110, 0.4), (120, 0.3)]


def make_clean_runner() -> PricePath:
    """Peaks 5.0x, settles at 3.5x (the trailing price off the peak)."""
    return [(0, 1.0), (10, 1.3), (20, 1.8), (30, 2.2), (40, 3.0), (50, 4.0),
            (60, 5.0), (70, 4.5), (80, 4.0), (90, 3.6), (100, 3.5)]


def make_straight_rug() -> PricePath:
    """Never above 1.1x, dies to 0.05x; the hard stop must fire first."""
    return [(0, 1.0), (10, 1.1), (20, 1.05), (30, 0.9), (40, 0.7),
            (50, 0.55), (60, 0.45), (70, 0.2), (80, 0.05)]


def make_slow_grinder() -> PricePath:
    """Grinds to a 2.05x peak at ~1200 min, still inside the 24h cap."""
    return [(0, 1.0), (120, 1.1), (240, 1.2), (360, 1.35), (480, 1.5),
            (600, 1.65), (720, 1.75), (840, 1.85), (960, 1.92),
            (1080, 1.98), (1200, 2.05), (1320, 1.9), (1440, 1.8)]


DEMO_PATHS: dict[str, PricePath] = {
    "round_tripper": make_round_tripper(),
    "clean_runner": make_clean_runner(),
    "straight_rug": make_straight_rug(),
    "slow_grinder": make_slow_grinder(),
}
