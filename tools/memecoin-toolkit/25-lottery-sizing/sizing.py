"""Position sizing for memecoin outcome distributions (synthetic lab).

Pure stdlib, no network.  A *distribution* is a list of
``(probability, gross_multiple)`` pairs: with that probability a $1
shot returns ``gross_multiple`` dollars (0 = total loss, 1 = break
even, 15 = a 15x).  Memecoin shots are lottery-like: most outcomes
are 0 and a few are large multiples.

Tools:
  * Expectancy -- the mean gross multiple and the net edge per $1.
  * Full Kelly -- the bankroll fraction maximising expected log
    growth for this discrete outcome set, found by a deterministic
    golden-section search over the expected-log curve.
  * Expected log growth at any fraction -- the quantity that shows
    why overbetting hurts: 2x Kelly on a positive-expectancy
    distribution grows *slower* than Kelly, and large enough
    fractions turn growth negative even though expectancy is positive.
  * Seeded Monte Carlo ruin probability for flat $1 shots on a $100
    bankroll: fully reproducible for a given seed.

Fail-closed: probabilities must each be in (0, 1] and sum to ~1
(tolerance 1e-6), multiples must be >= 0, fractions must be in
[0, 1], and any fraction that can produce a non-positive bankroll
factor for a possible outcome raises instead of returning log(<=0).
Nothing is silently renormalised.
"""
from __future__ import annotations

import math
import random

SUM_TOLERANCE = 1e-6


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_distribution(outcomes) -> list[tuple[float, float]]:
    """Validate and return a distribution as (prob, multiple) floats.

    Fail-closed: empty distributions, non-pair entries, probabilities
    outside (0, 1], negative multiples, and probability sums that
    differ from 1 by more than ``SUM_TOLERANCE`` all raise ValueError.
    """
    if outcomes is None:
        raise ValueError("outcomes must not be None")
    pairs = list(outcomes)
    if not pairs:
        raise ValueError("outcomes must be a non-empty distribution")
    cleaned: list[tuple[float, float]] = []
    total = 0.0
    for pair in pairs:
        try:
            prob, multiple = pair
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"each outcome must be a (probability, multiple) pair, "
                f"got {pair!r}") from exc
        if not _is_number(prob) or not (0 < prob <= 1):
            raise ValueError(f"probability must be in (0, 1], got {prob!r}")
        if not _is_number(multiple) or not (multiple >= 0):
            raise ValueError(f"multiple must be >= 0, got {multiple!r}")
        cleaned.append((float(prob), float(multiple)))
        total += float(prob)
    if abs(total - 1.0) > SUM_TOLERANCE:
        raise ValueError(
            f"probabilities must sum to ~1 (tolerance {SUM_TOLERANCE}), "
            f"got {total!r}")
    return cleaned


def expected_multiple(outcomes) -> float:
    """Mean gross multiple: sum of p * multiple."""
    pairs = validate_distribution(outcomes)
    return sum(p * m for p, m in pairs)


def expectancy(outcomes) -> float:
    """Net expectancy per $1 staked: expected_multiple - 1."""
    return expected_multiple(outcomes) - 1.0


def variance(outcomes) -> float:
    """Variance of the gross multiple."""
    pairs = validate_distribution(outcomes)
    mean = sum(p * m for p, m in pairs)
    return sum(p * (m - mean) ** 2 for p, m in pairs)


def prob_of_loss(outcomes) -> float:
    """Total probability of outcomes returning less than the stake."""
    pairs = validate_distribution(outcomes)
    return sum(p for p, m in pairs if m < 1.0)


def prob_of_ruin_outcome(outcomes) -> float:
    """Total probability of a total-loss outcome (multiple == 0)."""
    pairs = validate_distribution(outcomes)
    return sum(p for p, m in pairs if m == 0.0)


def _require_fraction(fraction) -> float:
    if not _is_number(fraction) or not (0 <= fraction <= 1):
        raise ValueError(f"fraction must be in [0, 1], got {fraction!r}")
    return float(fraction)


def expected_log_growth(fraction, outcomes) -> float:
    """Expected log bankroll growth per bet at ``fraction`` of bankroll.

    Each outcome multiplies the bankroll by ``1 - f + f * multiple``.
    Raises ValueError if any possible outcome would make that factor
    <= 0 (e.g. f == 1 with a total-loss outcome possible): the log is
    undefined there and the bettor is ruined with positive probability.
    """
    fraction = _require_fraction(fraction)
    pairs = validate_distribution(outcomes)
    total = 0.0
    for prob, multiple in pairs:
        factor = 1.0 - fraction + fraction * multiple
        if factor <= 0:
            raise ValueError(
                f"fraction {fraction} with multiple {multiple} gives a "
                f"non-positive bankroll factor; log growth is undefined "
                f"(ruin is possible)")
        total += prob * math.log(factor)
    return total


def kelly_fraction(outcomes) -> float:
    """Full-Kelly fraction in [0, 1] maximising expected log growth.

    Deterministic golden-section search (200 iterations) over the
    concave expected-log curve.  A distribution with no positive
    expectancy has Kelly 0.  If a total-loss outcome is possible the
    search stays strictly below 1, where the log is defined.
    """
    pairs = validate_distribution(outcomes)
    if expectancy(pairs) <= 0:
        return 0.0
    has_total_loss = any(m == 0.0 for _, m in pairs)
    hi = 1.0 - 1e-9 if has_total_loss else 1.0
    lo = 0.0

    def growth(f: float) -> float:
        try:
            return expected_log_growth(f, pairs)
        except ValueError:
            return -math.inf

    inv_phi = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = lo, hi
    c = b - inv_phi * (b - a)
    d = a + inv_phi * (b - a)
    for _ in range(200):
        if growth(c) < growth(d):
            a = c
        else:
            b = d
        c = b - inv_phi * (b - a)
        d = a + inv_phi * (b - a)
    result = (a + b) / 2.0
    # Never return a fraction whose growth is worse than betting 0.
    if growth(result) <= 0:
        return 0.0
    return result


def growth_comparison(outcomes, flat_fraction=0.01) -> dict:
    """Expected log growth for flat vs Kelly vs 2x Kelly fractions.

    Returns ``{"flat": (f, g), "kelly": (f, g), "double_kelly": (f, g)}``
    where ``f`` is the fraction and ``g`` its expected log growth.
    The 2x Kelly fraction is capped at 1; if it would make a bankroll
    factor non-positive (genuinely ruinous overbetting) its growth is
    reported as ``-inf`` -- that case IS the pinned lesson.  Callers
    wanting strict fail-closed behaviour should call
    :func:`expected_log_growth` directly, which raises there.
    """
    kelly = kelly_fraction(outcomes)
    double = min(2.0 * kelly, 1.0)

    def safe_growth(f):
        try:
            return expected_log_growth(f, outcomes)
        except ValueError:
            return float("-inf")

    return {
        "flat": (flat_fraction, safe_growth(flat_fraction)),
        "kelly": (kelly, safe_growth(kelly)),
        "double_kelly": (double, safe_growth(double)),
    }


# -- Monte Carlo ruin -----------------------------------------------------

def _draw_multiple(rng: random.Random, pairs) -> float:
    roll = rng.random()
    cumulative = 0.0
    for prob, multiple in pairs:
        cumulative += prob
        if roll < cumulative:
            return multiple
    return pairs[-1][1]  # floating-point tail guard


def simulate_bankroll(outcomes, bankroll=100.0, bet=1.0, n_bets=1000,
                      seed=0) -> float:
    """Final bankroll of ONE seeded flat-bet path (deterministic)."""
    pairs = validate_distribution(outcomes)
    if not _is_number(bankroll) or not (bankroll > 0):
        raise ValueError(f"bankroll must be positive, got {bankroll!r}")
    if not _is_number(bet) or not (bet > 0):
        raise ValueError(f"bet must be positive, got {bet!r}")
    if not isinstance(n_bets, int) or isinstance(n_bets, bool) or n_bets < 1:
        raise ValueError(f"n_bets must be a positive int, got {n_bets!r}")
    rng = random.Random(seed)
    money = float(bankroll)
    for _ in range(n_bets):
        if money < bet:
            return money  # cannot place the next flat bet: ruined
        money += bet * (_draw_multiple(rng, pairs) - 1.0)
        if money <= 0:
            return money
    return money


def ruin_probability(outcomes, bankroll=100.0, bet=1.0, n_bets=1000,
                     n_sims=500, seed=0) -> float:
    """Seeded Monte Carlo probability of ruin for flat bets.

    Ruin = bankroll falls below one bet (or to <= 0) at any point in
    ``n_bets`` flat ``bet``-sized shots.  Each simulation uses its own
    stream derived deterministically from ``seed``, so the result is
    exactly reproducible for the same arguments.
    """
    pairs = validate_distribution(outcomes)
    if not _is_number(bankroll) or not (bankroll > 0):
        raise ValueError(f"bankroll must be positive, got {bankroll!r}")
    if not _is_number(bet) or not (bet > 0):
        raise ValueError(f"bet must be positive, got {bet!r}")
    if bet > bankroll:
        raise ValueError(f"bet {bet!r} exceeds bankroll {bankroll!r}")
    if not isinstance(n_bets, int) or isinstance(n_bets, bool) or n_bets < 1:
        raise ValueError(f"n_bets must be a positive int, got {n_bets!r}")
    if not isinstance(n_sims, int) or isinstance(n_sims, bool) or n_sims < 1:
        raise ValueError(f"n_sims must be a positive int, got {n_sims!r}")
    ruined = 0
    for sim in range(n_sims):
        final = simulate_bankroll(pairs, bankroll=bankroll, bet=bet,
                                  n_bets=n_bets, seed=seed * 1_000_003 + sim)
        if final < bet:
            ruined += 1
    return ruined / n_sims
