"""Time-to-peak distribution lab (synthetic, pure stdlib).

Two populations that must never be conflated:
  (a) POP_PEAKED_2X  -- peaked>=2x paper closes, median ~53 min, mean ~74 min
  (b) POP_POST_GRAD  -- post-graduation peaks, median ~5 min

Fail-closed: empty samples, negative times, unknown populations and pooled
mixed samples without an explicit override all raise ValueError.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

POP_PEAKED_2X = "peaked_2x"
POP_POST_GRAD = "post_graduation"
POPULATIONS = (POP_PEAKED_2X, POP_POST_GRAD)


@dataclass(frozen=True)
class TaggedSamples:
    population: str
    values: tuple  # tuple[float, ...] minutes

    def __post_init__(self):
        if self.population not in POPULATIONS:
            raise ValueError(f"unknown population: {self.population!r}")
        if len(self.values) == 0:
            raise ValueError("values must be non-empty")
        for v in self.values:
            if v is None or v < 0:
                raise ValueError(f"times must be >= 0, got {v!r}")


def _require_values(values):
    if values is None or len(values) == 0:
        raise ValueError("values must be non-empty")
    for v in values:
        if v is None or v < 0:
            raise ValueError(f"times must be >= 0, got {v!r}")
    return sorted(float(v) for v in values)


def median(values) -> float:
    s = _require_values(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def mean(values) -> float:
    s = _require_values(values)
    return sum(s) / len(s)


def percentile(values, p: float) -> float:
    """Linear-interpolation percentile, p in [0, 100]."""
    if p is None or not (0 <= p <= 100):
        raise ValueError(f"p must be in [0, 100], got {p!r}")
    s = _require_values(values)
    if len(s) == 1:
        return s[0]
    rank = (p / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return s[int(rank)]
    frac = rank - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def histogram(values, bucket_edges) -> list:
    """Counts per bucket [edge[i], edge[i+1]); last bucket includes right edge overflow handling: values above last edge go in last bucket."""
    s = _require_values(values)
    if bucket_edges is None or len(bucket_edges) < 2:
        raise ValueError("bucket_edges must have >= 2 edges")
    edges = list(bucket_edges)
    for i in range(len(edges) - 1):
        if not (edges[i] < edges[i + 1]):
            raise ValueError("bucket_edges must be strictly increasing")
    counts = [0] * (len(edges) - 1)
    for v in s:
        if v < edges[0]:
            raise ValueError(f"value {v} below first bucket edge {edges[0]}")
        placed = False
        for i in range(len(counts)):
            if edges[i] <= v < edges[i + 1]:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return counts


def stats(values) -> dict:
    s = _require_values(values)
    return {
        "n": len(s),
        "median": median(s),
        "mean": mean(s),
        "min": s[0],
        "max": s[-1],
        "p25": percentile(s, 25),
        "p75": percentile(s, 75),
        "p90": percentile(s, 90),
    }


def stats_for_population(tagged: TaggedSamples) -> dict:
    if not isinstance(tagged, TaggedSamples):
        raise ValueError("expected TaggedSamples")
    out = stats(list(tagged.values))
    out["population"] = tagged.population
    return out


class MixedPopulationError(ValueError):
    pass


def mix_populations(tagged_list, *, allow_mix: bool = False) -> list:
    """Pool tagged samples. Raises MixedPopulationError if >1 population and not allow_mix."""
    if tagged_list is None or len(tagged_list) == 0:
        raise ValueError("tagged_list must be non-empty")
    pops = {t.population for t in tagged_list}
    if len(pops) > 1 and not allow_mix:
        raise MixedPopulationError(
            f"refusing to pool populations {sorted(pops)} without allow_mix=True; "
            "peaked>=2x closes and post-graduation peaks are different distributions"
        )
    out = []
    for t in tagged_list:
        out.extend(float(v) for v in t.values)
    return out


def mix_warning(tagged_list) -> str | None:
    if tagged_list is None or len(tagged_list) <= 1:
        return None
    pops = {t.population for t in tagged_list}
    if len(pops) > 1:
        return f"MIXED POPULATIONS: {sorted(pops)} pooled — medians are not comparable"
    return None


def prob_peak_after(values, t_minutes: float) -> float:
    """Empirical P(peak time > T). For time-stop choice."""
    if t_minutes is None or t_minutes < 0:
        raise ValueError(f"t_minutes must be >= 0, got {t_minutes!r}")
    s = _require_values(values)
    return sum(1 for v in s if v > t_minutes) / len(s)


def prob_peak_after_population(tagged: TaggedSamples, t_minutes: float) -> float:
    return prob_peak_after(list(tagged.values), t_minutes)


def survival_curve(values, thresholds) -> list:
    if thresholds is None or len(thresholds) == 0:
        raise ValueError("thresholds must be non-empty")
    return [prob_peak_after(values, t) for t in thresholds]


def generate_peaked_2x_samples(seed: int = 46, n: int = 200) -> TaggedSamples:
    """Lognormal seeded to land near median 53, mean ~74."""
    rng = random.Random(seed)
    # lognormal median = exp(mu); choose mu=ln(53), sigma chosen so mean=median*exp(sigma^2/2)~74 => sigma~0.82
    mu = math.log(53.0)
    sigma = 0.82
    vals = tuple(max(0.5, rng.lognormvariate(mu, sigma)) for _ in range(n))
    return TaggedSamples(population=POP_PEAKED_2X, values=vals)


def generate_post_grad_samples(seed: int = 43, n: int = 200) -> TaggedSamples:
    rng = random.Random(seed)
    mu = math.log(5.0)
    sigma = 0.7
    vals = tuple(max(0.1, rng.lognormvariate(mu, sigma)) for _ in range(n))
    return TaggedSamples(population=POP_POST_GRAD, values=vals)


def recommend_time_stop(values, max_miss_rate: float = 0.10) -> float:
    """Smallest T (from sample quantiles) with P(peak after T) <= max_miss_rate."""
    if max_miss_rate is None or not (0 < max_miss_rate < 1):
        raise ValueError(f"max_miss_rate must be in (0, 1), got {max_miss_rate!r}")
    return percentile(values, (1 - max_miss_rate) * 100)
