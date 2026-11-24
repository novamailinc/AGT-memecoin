"""Backtesting-rigor toolkit for memecoin signal research.

Pure stdlib. Five defences against fooling yourself:
  (i)   PreRegistration — write the gate down and freeze it before you
        look at results; mutating it after the freeze raises.
  (ii)  walk_forward — expanding-window train/test folds with an
        embargo gap so neighbouring bars cannot leak across the split.
  (iii) parameter_plateau — a real edge is a plateau, not a spike:
        measure the width of the good region around the best parameter.
  (iv)  deflated_expectancy — haircut an observed expectancy for how
        many variants you tried before picking it.
  (v)   lookahead_experiment — a dishonesty injector showing how much
        expectancy a next-bar (cheating) signal fabricates from noise.

Fail-closed behaviour: unfrozen registrations refuse to certify,
negative embargoes / fold counts raise ValueError, empty parameter
results raise ValueError, and n_samples <= 0 raises ValueError.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterator, Sequence


@dataclass(slots=True)
class PreRegistration:
    """A gate written down before the experiment runs.

    Fields: ``gate`` (description), ``thresholds`` (dict of named
    cut-offs), ``registered_at`` (ISO date/time string).
    ``freeze()`` locks it — comprehensively:

      * assigning *any* attribute (``gate`` / ``thresholds`` /
        ``registered_at`` / ``_frozen`` itself / a brand-new name)
        raises RuntimeError via ``__setattr__``;
      * deleting any attribute raises RuntimeError via ``__delattr__``;
      * item assignment or deletion into ``thresholds`` raises
        TypeError (it becomes a mapping proxy), and the proxy exposes
        no ``update`` / ``pop`` / ``clear`` / ``setdefault`` mutators
        (AttributeError);
      * ``set_threshold()`` raises RuntimeError;
      * the class uses ``__slots__``, so there is no ``__dict__`` to
        rewrite behind ``__setattr__``'s back.

    Construction is fail-closed and defensive: ``gate`` and
    ``registered_at`` must be non-empty strings, ``thresholds`` must
    be a dict of non-empty string keys to finite numbers, and the
    dict is *copied* — mutating the caller's original dict afterwards
    (before or after the freeze) never touches the registration.
    ``frozen()`` reports the lock state. Before freezing, mutation
    through the normal routes is allowed.
    """

    gate: str
    thresholds: dict
    registered_at: str
    _frozen: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.gate, str) or not self.gate:
            raise ValueError("gate must be a non-empty string: fail-closed")
        if not isinstance(self.registered_at, str) or not self.registered_at:
            raise ValueError("registered_at must be a non-empty string: fail-closed")
        if not isinstance(self.thresholds, dict) or not self.thresholds:
            raise ValueError("thresholds must be a non-empty dict: fail-closed")
        for key, value in self.thresholds.items():
            if not isinstance(key, str) or not key:
                raise ValueError("threshold keys must be non-empty strings")
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(float(value)):
                raise ValueError(f"threshold {key!r} must be a finite number")
        # Defensive copy: the registration owns its thresholds outright.
        object.__setattr__(self, "thresholds", dict(self.thresholds))

    # Note: explicit object.__setattr__ / __delattr__, not zero-arg
    # super() — @dataclass(slots=True) rebuilds the class, which breaks
    # the __class__ cell zero-arg super() depends on.
    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False):
            raise RuntimeError(f"pre-registration is frozen: cannot set {name!r}")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if getattr(self, "_frozen", False):
            raise RuntimeError(f"pre-registration is frozen: cannot delete {name!r}")
        object.__delattr__(self, name)

    @property
    def description(self) -> str:
        return self.gate

    def frozen(self) -> bool:
        """True once freeze() has been called."""
        return self._frozen

    def freeze(self) -> "PreRegistration":
        if self._frozen:  # idempotent: freezing twice changes nothing
            return self
        object.__setattr__(self, "thresholds", MappingProxyType(dict(self.thresholds)))
        object.__setattr__(self, "_frozen", True)
        return self

    def set_threshold(self, key: str, value: float) -> None:
        if self._frozen:
            raise RuntimeError("pre-registration is frozen: cannot set thresholds")
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)):
            raise ValueError("threshold value must be a finite number")
        self.thresholds[key] = value

    def certify(self) -> bool:
        """Fail-closed: only a frozen registration can certify a result."""
        if not self._frozen:
            raise RuntimeError("cannot certify with an unfrozen pre-registration")
        return True


def registration_fingerprint(reg: PreRegistration) -> str:
    """SHA-256 fingerprint of a registration's contents (hex digest).

    Canonicalised (sorted threshold keys, JSON), so two registrations
    with identical gate / thresholds / timestamp fingerprint the same,
    and changing any one of them — gate text, a single threshold, the
    timestamp — changes the fingerprint. A cheap way to prove in a
    report that the gate being certified is the gate that was frozen.
    """
    if not isinstance(reg, PreRegistration):
        raise ValueError(f"not a PreRegistration: {reg!r}")
    payload = {
        "gate": reg.gate,
        "registered_at": reg.registered_at,
        "thresholds": {k: float(v) for k, v in reg.thresholds.items()},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def evaluate_gate(reg: PreRegistration, metrics: dict) -> bool:
    """True iff every frozen threshold is met by ``metrics``.

    Each threshold is a minimum: the metric of the same name must be
    >= the frozen value. The registration must be frozen (an unfrozen
    gate certifying a pass would be exactly the failure mode this
    class exists to prevent, so it raises RuntimeError). A missing or
    non-finite metric raises ValueError (fail-closed: an unmeasured
    criterion is not a passed criterion).
    """
    if not isinstance(reg, PreRegistration):
        raise ValueError(f"not a PreRegistration: {reg!r}")
    reg.certify()  # raises RuntimeError when unfrozen
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be a dict: fail-closed")
    for key, threshold in reg.thresholds.items():
        if key not in metrics:
            raise ValueError(f"missing metric {key!r}: fail-closed")
        value = metrics[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)):
            raise ValueError(f"metric {key!r} must be a finite number")
        if float(value) < float(threshold):
            return False
    return True


def walk_forward(data: Sequence, n_folds: int = 3,
                 embargo: int = 0) -> Iterator[tuple[list, list]]:
    """Expanding-window walk-forward folds with an embargo gap.

    The data is cut into ``n_folds + 1`` equal segments. Fold i trains
    on segments [0, i) and tests on the next segment, except the first
    ``embargo`` samples after the train end are dropped from both sides
    (they belong to neither train nor test). Yields (train, test) lists
    in time order. Raises ValueError on bad parameters (fail-closed).
    """
    if n_folds <= 0:
        raise ValueError("n_folds must be positive: fail-closed")
    if embargo < 0:
        raise ValueError("embargo must be >= 0: fail-closed")
    items = list(data)
    segment = len(items) // (n_folds + 1)
    if segment <= embargo:
        raise ValueError("segment too small for the embargo: fail-closed")
    for i in range(1, n_folds + 1):
        train_end = segment * i
        test_start = train_end + embargo
        test_end = test_start + (segment - embargo)
        if test_start >= len(items):
            break
        yield items[:train_end], items[test_start:test_end]


def chronological_split(data: Sequence, train_fraction: float = 0.7,
                        embargo: int = 0) -> tuple[list, list]:
    """One chronological train/test split with an embargo gap.

    The first ``floor(len * train_fraction)`` samples train; the next
    ``embargo`` samples belong to neither side; the rest test. Raises
    ValueError when the fraction is outside (0, 1), the embargo is
    negative, or either side would be empty (fail-closed — a split
    with an empty side proves nothing and usually hides a bug).
    """
    if not (0.0 < train_fraction < 1.0):
        raise ValueError("train_fraction must be in (0, 1): fail-closed")
    if embargo < 0:
        raise ValueError("embargo must be >= 0: fail-closed")
    items = list(data)
    cut = int(len(items) * train_fraction)
    train, test = items[:cut], items[cut + embargo:]
    if not train or not test:
        raise ValueError("split would leave an empty side: fail-closed")
    return train, test


def check_no_leakage(train_indices: Sequence, test_indices: Sequence) -> int:
    """Verify two index sets are disjoint and time-ordered; return the gap.

    The gap is ``min(test) - max(train) - 1``: the number of samples
    sitting between the sets (the embargo width, when one was used).
    Overlapping sets, a test sample that does not come strictly after
    every train sample, or an empty set all raise ValueError — a
    leakage check that passes bad input quietly is worse than none.
    """
    train, test = list(train_indices), list(test_indices)
    if not train or not test:
        raise ValueError("index sets must be non-empty: fail-closed")
    if set(train) & set(test):
        raise ValueError("train and test overlap: leakage")
    if max(train) >= min(test):
        raise ValueError("test must come strictly after train: leakage / disorder")
    return min(test) - max(train) - 1


def plateau_region(results: dict[float, float]) -> list[float]:
    """The contiguous good parameter region around the best, sorted.

    Same rule as :func:`parameter_plateau` (neighbours must keep at
    least 80% of the best expectancy), but returns the parameters
    themselves, so a report can show *which* region is robust, not
    just how wide it is. Empty when the best expectancy is <= 0.
    Empty input or a non-finite expectancy raises ValueError.
    """
    if not results:
        raise ValueError("no parameter results: fail-closed")
    for value in results.values():
        if not math.isfinite(float(value)):
            raise ValueError("expectancies must be finite: fail-closed")
    params = sorted(results)
    best_param = max(params, key=lambda p: results[p])
    best = results[best_param]
    if best <= 0:
        return []
    threshold = 0.8 * best
    idx = params.index(best_param)
    lo = idx
    while lo - 1 >= 0 and results[params[lo - 1]] >= threshold:
        lo -= 1
    hi = idx
    while hi + 1 < len(params) and results[params[hi + 1]] >= threshold:
        hi += 1
    return params[lo:hi + 1]


def parameter_plateau(results: dict[float, float]) -> int:
    """Width of the contiguous good region around the best parameter.

    Parameters are sorted; the threshold is 80% of the best expectancy.
    Expanding left/right from the argmax while neighbours clear the
    threshold gives the width. A lone spike returns 1 (fragile); a
    broad plateau returns a large width. If the best expectancy is
    <= 0 there is no good region and the width is 0.
    Empty input raises ValueError (fail-closed).
    """
    return len(plateau_region(results))


def is_robust_plateau(results: dict[float, float], min_width: int = 2) -> bool:
    """True iff the good region is at least ``min_width`` parameters wide.

    The one-line verdict form of :func:`parameter_plateau`: width 1
    (the default ``min_width=2`` fails it) means the 'edge' lives at a
    single magic setting. ``min_width < 1`` raises ValueError.
    """
    if min_width < 1:
        raise ValueError("min_width must be >= 1: fail-closed")
    return parameter_plateau(results) >= min_width


def multiple_testing_penalty(n_trials: int, n_samples: int = 100) -> float:
    """The multiple-testing penalty alone, in expectancy points.

        penalty = sqrt(2 * ln(max(n_trials, 1))) / sqrt(n_samples)

    Zero at one trial, growing like sqrt(ln(trials)) and shrinking
    like 1/sqrt(samples). Same validation as
    :func:`deflated_expectancy`, which is ``observed - penalty``.
    """
    if n_samples <= 0:
        raise ValueError("n_samples must be positive: fail-closed")
    if n_trials < 0:
        raise ValueError("n_trials must be >= 0: fail-closed")
    return math.sqrt(2.0 * math.log(max(n_trials, 1))) / math.sqrt(n_samples)


def bonferroni_alpha(alpha: float, n_trials: int) -> float:
    """Per-test significance level under a Bonferroni correction.

    ``alpha / n_trials``: the level each individual variant must beat
    for the family-wise error rate to stay at ``alpha``. ``alpha``
    outside (0, 1) or ``n_trials < 1`` raises ValueError.
    """
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha must be in (0, 1): fail-closed")
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1: fail-closed")
    return alpha / n_trials


def deflated_expectancy(observed: float, n_trials: int, n_samples: int = 100) -> float:
    """Expectancy haircut for having tried ``n_trials`` variants.

        penalty  = sqrt(2 * ln(max(n_trials, 1))) * (1 / sqrt(n_samples))
        deflated = observed - penalty

    Assumes per-sample expectancy has std ~1.0 (documented assumption);
    with one trial the penalty is 0. More trials or fewer samples ->
    larger haircut. n_samples <= 0 raises ValueError (fail-closed).
    The deflated value may go negative: that is the point.
    """
    if observed is None or not math.isfinite(float(observed)):
        raise ValueError("observed expectancy must be finite: fail-closed")
    return float(observed) - multiple_testing_penalty(n_trials, n_samples)


def lookahead_experiment(seed: int = 42, n_bars: int = 5000,
                         sigma: float = 0.5) -> tuple[float, float]:
    """Fabricate an edge from noise by peeking at the next bar.

    Generates ``n_bars`` iid bar returns ~ N(0, sigma). Both strategies
    earn the *next* bar's return on bars they select:
      * cheating selects bar i iff return[i+1] > 0 (uses the future);
      * honest   selects bar i iff return[i]   > 0 (momentum, no edge).
    Returns (cheating_expectancy, honest_expectancy) where expectancy
    is the mean selected next-bar return. With sigma=0.5 the cheating
    figure is ~+0.40 and the honest figure is ~0. Same seed -> same pair.
    """
    if n_bars < 100:
        raise ValueError("need at least 100 bars for a meaningful demo: fail-closed")
    if sigma is None or not math.isfinite(float(sigma)) or float(sigma) <= 0:
        raise ValueError("sigma must be positive and finite: fail-closed")
    rng = random.Random(seed)
    returns = [rng.gauss(0.0, sigma) for _ in range(n_bars)]
    cheat = [returns[i + 1] for i in range(n_bars - 1) if returns[i + 1] > 0]
    honest = [returns[i + 1] for i in range(n_bars - 1) if returns[i] > 0]
    cheat_exp = sum(cheat) / len(cheat)
    honest_exp = sum(honest) / len(honest)
    return cheat_exp, honest_exp


def lookahead_bias(seed: int = 42, n_bars: int = 5000,
                   sigma: float = 0.5) -> float:
    """Cheating minus honest expectancy: the fabricated edge, one number.

    For iid normal noise the cheating figure is the mean of the
    positive half of the distribution, ``sigma * sqrt(2 / pi)``, so
    the bias grows linearly in ``sigma`` while the honest figure stays
    at ~0 — doubling the noise doubles the fake edge, which is exactly
    why volatility makes look-ahead bugs look *better*, not worse.
    """
    cheat, honest = lookahead_experiment(seed=seed, n_bars=n_bars, sigma=sigma)
    return cheat - honest
