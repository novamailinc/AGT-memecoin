"""Statistical validity lab for memecoin strategy claims.

Pure stdlib, seeded and deterministic. Three questions, answered with
arithmetic instead of vibes:
  (i)   what is the expectancy of a set of realized multiples?
  (ii)  how great does pure noise look after you pick the best of many
        strategies in-sample (p-hacking), and what happens out-of-sample?
  (iii) how many closes do you need before a win-rate edge is even
        detectable (power / sample size)?
Plus the base rates any memecoin claim has to survive, and the
breakeven win rate for a given average win multiple.

Fail-closed behaviour: empty outcome lists, a payoff multiple <= 0,
probabilities outside (0, 1), and ``p1 <= p0`` in the power functions
all raise ``ValueError`` instead of returning a plausible-looking lie.
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist

# Published-order-of-magnitude base rates for Solana memecoin launches.
# graduation_rate: share of pump.fun launches that graduate (range).
# one_in_launches_to_1m: roughly 1 launch in this many reaches $1M mcap.
# wallets_profitable_90d: share of wallets profitable over a 90d window.
BASE_RATES: dict = {
    "graduation_rate": (0.0063, 0.017),
    "one_in_launches_to_1m": 4200,
    "wallets_profitable_90d": 0.0625,
}

_NORM = NormalDist()


def _validate_outcomes(outcomes: list[float]) -> list[float]:
    """Realized multiples must be a non-empty list of finite values >= 0."""
    if not outcomes:
        raise ValueError("empty outcome list is undefined: fail-closed")
    out = []
    for value in outcomes:
        if value is None or not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"realized multiples must be finite and >= 0, got {value!r}")
        out.append(float(value))
    return out


def expectancy(outcomes: list[float]) -> float:
    """Expectancy per $1 staked for realized multiples: mean - 1.

    expectancy([2.0, 0.0]) == 0.0 (one double, one total loss).
    Empty input, a negative multiple, or a non-finite value raises
    ValueError (fail-closed).
    """
    vals = _validate_outcomes(outcomes)
    return sum(vals) / len(vals) - 1.0


def win_rate(outcomes: list[float]) -> float:
    """Share of outcomes that actually made money: multiple strictly > 1.

    A multiple of exactly 1.0 returns the stake and is *not* a win —
    counting breakeven as a win is how mediocre lanes inflate their
    reported win rate. Empty/invalid input raises via the shared
    outcome validation.
    """
    vals = _validate_outcomes(outcomes)
    return sum(1 for v in vals if v > 1.0) / len(vals)


def median_multiple(outcomes: list[float]) -> float:
    """Median realized multiple — robust where the mean is not.

    One 20x outlier makes the mean of ten trades look healthy while
    the median still reports what a typical trade did.
    """
    vals = sorted(_validate_outcomes(outcomes))
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def profit_factor(outcomes: list[float]) -> float:
    """Gross profit over gross loss, in per-$1 profit units.

    Gains sum (m - 1) over winners, losses sum (1 - m) over losers;
    breakeven trades contribute to neither. No losses with gains
    returns ``math.inf``; no movement at all (every outcome 1.0, or
    gains and losses both zero) returns 0.0; no gains returns 0.0.
    """
    vals = _validate_outcomes(outcomes)
    gains = sum(v - 1.0 for v in vals if v > 1.0)
    losses = sum(1.0 - v for v in vals if v < 1.0)
    if losses == 0.0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def max_consecutive_losses(outcomes: list[float]) -> int:
    """Longest run of losing outcomes (multiple < 1) in trade order.

    A multiple of exactly 1.0 breaks a streak (the stake came back);
    the streak is a property of the sequence, so order matters here
    even though expectancy ignores it.
    """
    vals = _validate_outcomes(outcomes)
    best = run = 0
    for v in vals:
        run = run + 1 if v < 1.0 else 0
        best = max(best, run)
    return best


def max_profit_drawdown(outcomes: list[float]) -> float:
    """Largest peak-to-trough fall of the cumulative profit curve.

    The curve starts at 0 and adds (m - 1) per trade in order. The
    drawdown is measured from the running peak (never a future one),
    in dollars per $1-staked-per-trade. A monotone-winning sequence
    has drawdown 0.0.
    """
    vals = _validate_outcomes(outcomes)
    equity = peak = worst = 0.0
    for v in vals:
        equity += v - 1.0
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def standard_error(outcomes: list[float]) -> float:
    """Standard error of the mean multiple: sample std / sqrt(n).

    Needs at least two outcomes — the spread of a single trade is
    unknowable, so n < 2 raises ValueError (fail-closed).
    """
    vals = _validate_outcomes(outcomes)
    if len(vals) < 2:
        raise ValueError("standard error needs at least 2 outcomes: fail-closed")
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var) / math.sqrt(len(vals))


def wilson_interval(wins: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a win rate, as (low, high) in [0, 1].

    Better behaved than the normal approximation at small n and at
    the boundaries: 0 wins in 10 still yields a sensible (0, ~0.28),
    and n wins in n yields (~0.72, 1) instead of a degenerate point.
    ``wins`` outside [0, n], ``n <= 0``, or ``alpha`` outside (0, 1)
    raise ValueError.
    """
    if n <= 0:
        raise ValueError("n must be positive: fail-closed")
    if not (0 <= wins <= n):
        raise ValueError("wins must lie in [0, n]: fail-closed")
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha must be in (0, 1): fail-closed")
    z = _NORM.inv_cdf(1.0 - alpha / 2.0)
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def binomial_pvalue_at_least(k: int, n: int, p0: float) -> float:
    """Exact one-sided p-value P(X >= k) for X ~ Binomial(n, p0).

    The exact counterpart of the normal-approximation power math:
    how surprising is it to see at least ``k`` wins in ``n`` closes if
    the true win rate were only ``p0``? Computed with ``math.comb``
    (pure stdlib, exact coefficients). ``k = 0`` is 1.0 by definition.
    """
    if n <= 0:
        raise ValueError("n must be positive: fail-closed")
    if not (0 <= k <= n):
        raise ValueError("k must lie in [0, n]: fail-closed")
    if not (0.0 < p0 < 1.0):
        raise ValueError("p0 must be in (0, 1): fail-closed")
    if k == 0:
        return 1.0
    total = sum(math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i)
                for i in range(k, n + 1))
    return min(1.0, total)


def kelly_fraction(wr: float, payoff_mult: float) -> float:
    """Full-Kelly stake fraction for the binary win/loss model.

    A win returns ``payoff_mult`` per $1, a loss returns 0 (the same
    model as :func:`breakeven_wr`). f = (wr*W - 1) / (W - 1): zero
    exactly at the breakeven win rate, 1.0 at a certain win, negative
    when the win rate is below breakeven (Kelly's answer: do not bet).
    ``payoff_mult <= 1`` raises — a 'win' that does not profit has no
    Kelly stake — as does a win rate outside [0, 1].
    """
    if not (0.0 <= wr <= 1.0):
        raise ValueError("win rate must be in [0, 1]: fail-closed")
    if payoff_mult is None or not math.isfinite(float(payoff_mult)) \
            or float(payoff_mult) <= 1.0:
        raise ValueError("payoff multiple must be finite and > 1: fail-closed")
    w = float(payoff_mult)
    return (wr * w - 1.0) / (w - 1.0)


def bootstrap_expectancy_ci(outcomes: list[float], seed: int = 42,
                            n_boot: int = 2000,
                            level: float = 0.95) -> tuple[float, float]:
    """Seeded bootstrap percentile CI for expectancy, as (low, high).

    Resamples the outcomes with replacement ``n_boot`` times from a
    single ``random.Random(seed)`` stream (so the interval reproduces
    exactly), computes expectancy per resample, and returns the
    central ``level`` percentile band. Constant outcomes collapse the
    band to a point — correctly: resampling cannot invent spread that
    is not in the data. ``n_boot < 100`` or ``level`` outside (0, 1)
    raise ValueError.
    """
    vals = _validate_outcomes(outcomes)
    if n_boot < 100:
        raise ValueError("n_boot must be >= 100 for a usable interval: fail-closed")
    if not (0.0 < level < 1.0):
        raise ValueError("level must be in (0, 1): fail-closed")
    rng = random.Random(seed)
    n = len(vals)
    stats = []
    for _ in range(n_boot):
        stats.append(sum(vals[rng.randrange(n)] for _ in range(n)) / n - 1.0)
    stats.sort()
    alpha = 1.0 - level
    lo_idx = int(alpha / 2.0 * n_boot)
    hi_idx = min(n_boot - 1, math.ceil((1.0 - alpha / 2.0) * n_boot) - 1)
    return (stats[lo_idx], stats[hi_idx])


def breakeven_wr(payoff_mult: float) -> float:
    """Win rate at which expectancy is exactly 0.

    A win returns ``payoff_mult`` (profit W-1 per $1), a loss returns 0
    (profit -1 per $1). EV = wr*(W-1) - (1-wr)*1 = wr*W - 1 >= 0
    iff wr >= 1/W. So breakeven_wr(2.0) == 0.5, breakeven_wr(4.0) == 0.25.
    """
    if payoff_mult is None or not math.isfinite(float(payoff_mult)) \
            or float(payoff_mult) <= 0:
        raise ValueError("payoff multiple must be positive: fail-closed")
    return 1.0 / payoff_mult


def simulate_phacking(seed: int = 42, n_strategies: int = 500,
                      n_in_sample: int = 40, n_out_sample: int = 200,
                      true_wr: float = 0.30) -> dict:
    """Pick the best of ``n_strategies`` pure-noise strategies in-sample.

    Every strategy is coin flips with win probability ``true_wr`` — none
    has any edge. The winner's in-sample win rate looks like a discovery;
    its fresh out-of-sample win rate regresses toward ``true_wr``.
    Same seed -> identical result (single ``random.Random`` stream).
    """
    if n_strategies <= 0 or n_in_sample <= 0 or n_out_sample <= 0:
        raise ValueError("counts must be positive: fail-closed")
    if not 0.0 < true_wr < 1.0:
        raise ValueError("true_wr must be in (0, 1): fail-closed")
    rng = random.Random(seed)

    def draw_wr(n: int) -> float:
        return sum(1 for _ in range(n) if rng.random() < true_wr) / n

    in_sample = [draw_wr(n_in_sample) for _ in range(n_strategies)]
    best_idx = max(range(n_strategies), key=lambda i: in_sample[i])
    best_is = in_sample[best_idx]
    best_oos = draw_wr(n_out_sample)  # fresh data, same (non-)strategy
    return {
        "seed": seed,
        "n_strategies": n_strategies,
        "true_wr": true_wr,
        "best_index": best_idx,
        "best_in_sample_wr": best_is,
        "best_out_of_sample_wr": best_oos,
        "mean_in_sample_wr": sum(in_sample) / len(in_sample),
        "inflation": best_is - best_oos,
    }


def required_n(p1: float = 0.35, p0: float = 0.30, alpha: float = 0.05,
               power: float = 0.8) -> int:
    """Closes needed to distinguish WR ``p1`` from ``p0`` (one-sided).

    Normal approximation:
        n = (z_a*sqrt(p0(1-p0)) + z_b*sqrt(p1(1-p1)))^2 / (p1-p0)^2
    with z_a = Phi^-1(1-alpha), z_b = Phi^-1(power). Rounded up.
    For p1=0.35 vs p0=0.30 at alpha 0.05 / power 0.8 this is 534.
    """
    if not (0.0 < p0 < p1 < 1.0):
        raise ValueError("need 0 < p0 < p1 < 1: fail-closed")
    if not (0.0 < alpha < 1.0 and 0.0 < power < 1.0):
        raise ValueError("alpha and power must be in (0, 1): fail-closed")
    z_a = _NORM.inv_cdf(1.0 - alpha)
    z_b = _NORM.inv_cdf(power)
    num = (z_a * math.sqrt(p0 * (1 - p0)) + z_b * math.sqrt(p1 * (1 - p1))) ** 2
    return math.ceil(num / (p1 - p0) ** 2)


def achieved_power(n: int, p0: float = 0.30, p1: float = 0.35,
                   alpha: float = 0.05) -> float:
    """Power of a one-sided WR test with ``n`` closes (inverse of required_n).

    z_b = (sqrt(n)*(p1-p0) - z_a*sqrt(p0(1-p0))) / sqrt(p1(1-p1)),
    power = Phi(z_b). achieved_power(required_n(...)) ~= the target power.
    """
    if n <= 0:
        raise ValueError("n must be positive: fail-closed")
    if not (0.0 < p0 < p1 < 1.0):
        raise ValueError("need 0 < p0 < p1 < 1: fail-closed")
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha must be in (0, 1): fail-closed")
    z_a = _NORM.inv_cdf(1.0 - alpha)
    z_b = (math.sqrt(n) * (p1 - p0) - z_a * math.sqrt(p0 * (1 - p0))) / math.sqrt(
        p1 * (1 - p1))
    return _NORM.cdf(z_b)
