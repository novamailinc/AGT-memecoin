"""Latency / age economics of launch entries (synthetic lab).

Pure stdlib, no network.  The model, kept deliberately simple so every
number can be checked by hand:

  * A launch's price multiple rises geometrically from 1x to
    ``peak_multiple`` over ``rise_seconds``:
    ``multiple(t) = peak ** (t / rise)`` for ``t <= rise``, then peaks.
  * After the peak the price decays with a half-life:
    ``multiple(t) = peak * 0.5 ** ((t - rise) / half_life)``.
  * Entering at delay ``d`` (detection + execution, seconds after
    launch) and exiting at the peak is the *best case* for a late
    entrant: nobody times the peak.  Even under that generosity, the
    fraction of the move left after a polling delay is tiny, and after
    the rise is over the entrant is buying the top and holding through
    decay -- the 2-10 minute copy window.
  * Costs: a round trip costs ``round_trip_cost_pct`` (default 4%:
    fees + slippage + adverse fills), charged on every trade.

Seeded synthetic launches add reality back: a share of launches rug
(exit multiple ~0.02x), peaks and rise times vary, and the exit is the
peak only if you were in before it -- late entries exit after a fixed
hold, deep into the decay.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

DEFAULT_PEAK_MULTIPLE = 10.0
DEFAULT_RISE_SECONDS = 120.0
DEFAULT_HALF_LIFE_SECONDS = 180.0
DEFAULT_ROUND_TRIP_COST_PCT = 4.0
SNIPER_MAX_DELAY_SECONDS = 2.0
POLLING_MIN_DELAY_SECONDS = 15.0


def _require_positive(name, value):
    if value is None or not (value > 0):
        raise ValueError(f"{name} must be positive, got {value!r}")
    return float(value)


def _require_non_negative(name, value):
    if value is None or not (value >= 0):
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return float(value)


def multiple_at_time(t_seconds: float, peak_multiple: float = DEFAULT_PEAK_MULTIPLE,
                     rise_seconds: float = DEFAULT_RISE_SECONDS,
                     half_life_seconds: float = DEFAULT_HALF_LIFE_SECONDS) -> float:
    """Price multiple (vs launch) at ``t_seconds`` after launch."""
    _require_non_negative("t_seconds", t_seconds)
    _require_positive("peak_multiple", peak_multiple)
    _require_positive("rise_seconds", rise_seconds)
    _require_positive("half_life_seconds", half_life_seconds)
    if peak_multiple < 1.0:
        raise ValueError("peak_multiple must be >= 1 for a rising launch")
    if t_seconds <= rise_seconds:
        return peak_multiple ** (t_seconds / rise_seconds)
    return peak_multiple * 0.5 ** ((t_seconds - rise_seconds) / half_life_seconds)


def entry_multiple(delay_seconds: float, **kwargs) -> float:
    """Multiple you actually pay when your fill lands ``delay`` in."""
    _require_non_negative("delay_seconds", delay_seconds)
    return multiple_at_time(delay_seconds, **kwargs)


def fraction_of_move_remaining(delay_seconds: float,
                               peak_multiple: float = DEFAULT_PEAK_MULTIPLE,
                               rise_seconds: float = DEFAULT_RISE_SECONDS) -> float:
    """Share of the (log) move still ahead of an entrant at ``delay``.

    1.0 at launch, 0.0 at/after the peak.  Log scale is the honest one
    for multiples: half the time to the peak means half the *multiple
    exponent*, i.e. sqrt(peak) is already gone.
    """
    _require_non_negative("delay_seconds", delay_seconds)
    _require_positive("rise_seconds", rise_seconds)
    if peak_multiple <= 1.0:
        raise ValueError("peak_multiple must be > 1")
    if delay_seconds >= rise_seconds:
        return 0.0
    return 1.0 - delay_seconds / rise_seconds


def gross_return_pct(delay_seconds: float, exit_multiple_value: float,
                     **kwargs) -> float:
    """% return buying at ``delay`` and exiting at a given multiple."""
    entry = entry_multiple(delay_seconds, **kwargs)
    return (exit_multiple_value / entry - 1.0) * 100.0


def net_return_at_peak_pct(delay_seconds: float,
                           peak_multiple: float = DEFAULT_PEAK_MULTIPLE,
                           rise_seconds: float = DEFAULT_RISE_SECONDS,
                           round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT) -> float:
    """Best-case net %: enter at ``delay``, exit exactly at the peak.

    Only meaningful for delay <= rise; after the rise the 'peak exit'
    is in the past, so this raises ValueError instead of lying.
    """
    _require_non_negative("delay_seconds", delay_seconds)
    _require_non_negative("round_trip_cost_pct", round_trip_cost_pct)
    if delay_seconds > rise_seconds:
        raise ValueError("cannot exit at a peak that happened before entry")
    gross = gross_return_pct(delay_seconds, peak_multiple,
                             peak_multiple=peak_multiple, rise_seconds=rise_seconds)
    return (1.0 + gross / 100.0) * (1.0 - round_trip_cost_pct / 100.0) * 100.0 - 100.0


def late_entry_return_pct(delay_seconds: float, hold_seconds: float = 60.0,
                          peak_multiple: float = DEFAULT_PEAK_MULTIPLE,
                          rise_seconds: float = DEFAULT_RISE_SECONDS,
                          half_life_seconds: float = DEFAULT_HALF_LIFE_SECONDS,
                          round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT) -> float:
    """Net % for entering at ``delay`` and exiting ``hold`` seconds later.

    This is the copy-window trade: entry on the way down, exit further
    down.  Works for any delay, before or after the peak.
    """
    _require_non_negative("hold_seconds", hold_seconds)
    kwargs = dict(peak_multiple=peak_multiple, rise_seconds=rise_seconds,
                  half_life_seconds=half_life_seconds)
    entry = entry_multiple(delay_seconds, **kwargs)
    exit_value = multiple_at_time(delay_seconds + hold_seconds, **kwargs)
    return (exit_value / entry) * (1.0 - round_trip_cost_pct / 100.0) * 100.0 - 100.0


def delay_bucket(delay_seconds: float) -> str:
    """Name the entrant: sniper (0-2s), polling (15-30s+), copy window..."""
    _require_non_negative("delay_seconds", delay_seconds)
    if delay_seconds <= SNIPER_MAX_DELAY_SECONDS:
        return "sniper"
    if delay_seconds < POLLING_MIN_DELAY_SECONDS:
        return "fast_polling"
    if delay_seconds < 120.0:
        return "polling"
    if delay_seconds <= 600.0:
        return "copy_window_2_10min"
    return "late"


@dataclass(frozen=True)
class BucketResult:
    delay_seconds: float
    bucket: str
    trades: int
    mean_net_return_pct: float
    win_rate_pct: float


def _one_launch(rng: random.Random, delay_seconds: float,
                hold_seconds: float, round_trip_cost_pct: float) -> float:
    """Net % for one seeded synthetic launch entered at ``delay``."""
    peak = min(50.0, max(1.5, rng.lognormvariate(1.6, 0.8)))
    rise = rng.uniform(30.0, 120.0)
    kwargs = dict(peak_multiple=peak, rise_seconds=rise,
                  half_life_seconds=DEFAULT_HALF_LIFE_SECONDS)
    entry = entry_multiple(delay_seconds, **kwargs)
    if rng.random() < 0.35:  # rug: exit near zero regardless of timing
        exit_value = 0.02
    elif delay_seconds < rise:
        exit_value = peak  # generous: early entrants are granted the peak
    else:
        exit_value = multiple_at_time(delay_seconds + hold_seconds, **kwargs)
    return (exit_value / entry) * (1.0 - round_trip_cost_pct / 100.0) * 100.0 - 100.0


def expectancy_by_delay(delay_seconds: float, seed: int = 7, n_launches: int = 400,
                        hold_seconds: float = 60.0,
                        round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT) -> BucketResult:
    """Mean net % and win rate over seeded synthetic launches at one delay."""
    _require_non_negative("delay_seconds", delay_seconds)
    if n_launches <= 0:
        raise ValueError(f"n_launches must be positive, got {n_launches!r}")
    rng = random.Random(seed)
    returns = [_one_launch(rng, delay_seconds, hold_seconds, round_trip_cost_pct)
               for _ in range(n_launches)]
    wins = sum(1 for r in returns if r > 0)
    return BucketResult(
        delay_seconds=float(delay_seconds), bucket=delay_bucket(delay_seconds),
        trades=n_launches, mean_net_return_pct=sum(returns) / len(returns),
        win_rate_pct=wins / len(returns) * 100.0,
    )


def expectancy_table(delays=(1.0, 20.0, 60.0, 180.0, 300.0, 600.0),
                     seed: int = 7, n_launches: int = 400) -> list[BucketResult]:
    """One BucketResult per delay, same seed sequence size per delay."""
    return [expectancy_by_delay(d, seed=seed, n_launches=n_launches) for d in delays]


def copy_window_expectancy(seed: int = 7, n_launches: int = 400) -> float:
    """Mean net % across the 2-10 minute window (delays 120/300/600s)."""
    results = [expectancy_by_delay(d, seed=seed, n_launches=n_launches)
               for d in (120.0, 300.0, 600.0)]
    return sum(r.mean_net_return_pct for r in results) / len(results)


def break_even_delay(peak_multiple: float = DEFAULT_PEAK_MULTIPLE,
                     rise_seconds: float = DEFAULT_RISE_SECONDS,
                     round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT,
                     tolerance_seconds: float = 0.01) -> float | None:
    """Latest entry delay whose best-case peak exit still breaks even.

    Binary search on the (monotonically falling) net_return_at_peak_pct.
    None if even an instant entry cannot cover costs; ``rise_seconds``
    if even a peak-time entry would (impossible with positive costs,
    but the solver reports it rather than assuming).
    """
    _require_positive("tolerance_seconds", tolerance_seconds)
    if net_return_at_peak_pct(0.0, peak_multiple, rise_seconds, round_trip_cost_pct) < 0:
        return None
    if net_return_at_peak_pct(rise_seconds, peak_multiple, rise_seconds,
                              round_trip_cost_pct) >= 0:
        return float(rise_seconds)
    lo, hi = 0.0, float(rise_seconds)
    while hi - lo > tolerance_seconds:
        mid = (lo + hi) / 2.0
        if net_return_at_peak_pct(mid, peak_multiple, rise_seconds,
                                  round_trip_cost_pct) >= 0:
            lo = mid
        else:
            hi = mid
    return lo
