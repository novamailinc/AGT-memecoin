"""Token lifecycle phases for a memecoin launch (synthetic lab).

Pure stdlib, no network.  A token is observed through snapshots of
measurable quantities (curve completion, age, liquidity, market cap,
momentum, holder trend) and classified into exactly one phase:

    BONDING -> GRADUATED -> RUNNER -> FADING -> DEAD
                   ^          |  ^       |
                   |          +--+       +-> RUNNER (revival, from FADING)
                   +-- DEAD -> RUNNER is legal but *suspicious*

plus UNKNOWN, the fail-closed answer whenever a required metric is
missing or invalid.  UNKNOWN is never quietly treated as a real phase:
a lane that only hunts RUNNER must not fire on UNKNOWN.

Classification rules (documented so tests can hand-check them, applied
in this order):
  1. Any required metric missing / NaN / out of range -> UNKNOWN.
  2. Collapse: liquidity < 1000 USD, or (momentum <= -85% and holder
     trend <= -30%) -> DEAD, at any age, graduated or not.
  3. Curve completion < 100% -> BONDING.
  4. Graduated and momentum >= 15%, liquidity >= 25000 USD,
     mcap >= 50000 USD, holder trend >= -5% -> RUNNER.
  5. Graduated and (momentum <= -10% or holder trend <= -15%
     or liquidity < 10000 USD) -> FADING.
  6. Otherwise (graduated, alive, not yet running, not fading)
     -> GRADUATED.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    BONDING = "BONDING"
    GRADUATED = "GRADUATED"
    RUNNER = "RUNNER"
    FADING = "FADING"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


# Thresholds (module constants so tests and readers share one truth).
RUNNER_MIN_LIQUIDITY_USD = 25_000.0
RUNNER_MIN_MCAP_USD = 50_000.0
RUNNER_MIN_MOMENTUM_PCT = 15.0
RUNNER_MIN_HOLDER_TREND_PCT = -5.0
DEAD_MAX_LIQUIDITY_USD = 1_000.0
COLLAPSE_MOMENTUM_PCT = -85.0
COLLAPSE_HOLDER_TREND_PCT = -30.0
FADING_MOMENTUM_PCT = -10.0
FADING_HOLDER_TREND_PCT = -15.0
GRADUATED_MIN_LIQUIDITY_USD = 10_000.0

REQUIRED_FIELDS = (
    "curve_completion_pct", "age_minutes", "liquidity_usd",
    "mcap_usd", "momentum_pct", "holder_trend_pct",
)


@dataclass(frozen=True)
class Snapshot:
    """One observation.  Any field may be None (feed gap) -> UNKNOWN."""

    curve_completion_pct: float | None = None
    age_minutes: float | None = None
    liquidity_usd: float | None = None
    mcap_usd: float | None = None
    momentum_pct: float | None = None
    holder_trend_pct: float | None = None


@dataclass(frozen=True)
class Classification:
    phase: Phase
    reasons: tuple[str, ...]


def snapshot_issues(snapshot: Snapshot) -> tuple[str, ...]:
    """Human-readable problems with a snapshot; empty means usable."""
    if snapshot is None:
        return ("snapshot is None",)
    issues: list[str] = []
    for name in REQUIRED_FIELDS:
        value = getattr(snapshot, name)
        if value is None:
            issues.append(f"{name} is missing")
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(f"{name} is not a number: {value!r}")
        elif isinstance(value, float) and math.isnan(value):
            issues.append(f"{name} is NaN")
    if issues:
        return tuple(issues)
    if not (0.0 <= snapshot.curve_completion_pct <= 100.0):
        issues.append("curve_completion_pct outside [0, 100]")
    if snapshot.age_minutes < 0:
        issues.append("age_minutes is negative")
    if snapshot.liquidity_usd < 0:
        issues.append("liquidity_usd is negative")
    if snapshot.mcap_usd < 0:
        issues.append("mcap_usd is negative")
    return tuple(issues)


def classify_with_reasons(snapshot: Snapshot) -> Classification:
    """Classify one snapshot, keeping the reasons for the verdict."""
    issues = snapshot_issues(snapshot)
    if issues:
        return Classification(Phase.UNKNOWN, issues)
    s = snapshot
    if s.liquidity_usd < DEAD_MAX_LIQUIDITY_USD:
        return Classification(Phase.DEAD, ("liquidity below dead floor",))
    if (s.momentum_pct <= COLLAPSE_MOMENTUM_PCT
            and s.holder_trend_pct <= COLLAPSE_HOLDER_TREND_PCT):
        return Classification(Phase.DEAD, ("price and holders collapsed",))
    if s.curve_completion_pct < 100.0:
        return Classification(Phase.BONDING, ("curve not complete",))
    if (s.momentum_pct >= RUNNER_MIN_MOMENTUM_PCT
            and s.liquidity_usd >= RUNNER_MIN_LIQUIDITY_USD
            and s.mcap_usd >= RUNNER_MIN_MCAP_USD
            and s.holder_trend_pct >= RUNNER_MIN_HOLDER_TREND_PCT):
        return Classification(Phase.RUNNER, ("graduated, liquid, rising, holders holding",))
    if (s.momentum_pct <= FADING_MOMENTUM_PCT
            or s.holder_trend_pct <= FADING_HOLDER_TREND_PCT
            or s.liquidity_usd < GRADUATED_MIN_LIQUIDITY_USD):
        return Classification(Phase.FADING, ("graduated but momentum/holders/liquidity fading",))
    return Classification(Phase.GRADUATED, ("graduated, alive, not yet a runner",))


def classify(snapshot: Snapshot) -> Phase:
    """The phase for one snapshot; UNKNOWN on any missing/invalid metric."""
    return classify_with_reasons(snapshot).phase


# -- legal transitions -------------------------------------------------------
# UNKNOWN may be observed before/after anything (it is a feed state, not
# a token state).  DEAD -> RUNNER is in the table because dormant-token
# revivals do happen, but it is flagged suspicious by transition_flag().
LEGAL_TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    Phase.UNKNOWN: frozenset(Phase),
    Phase.BONDING: frozenset({Phase.BONDING, Phase.GRADUATED, Phase.DEAD, Phase.UNKNOWN}),
    Phase.GRADUATED: frozenset({Phase.GRADUATED, Phase.RUNNER, Phase.FADING, Phase.DEAD, Phase.UNKNOWN}),
    Phase.RUNNER: frozenset({Phase.RUNNER, Phase.FADING, Phase.DEAD, Phase.UNKNOWN}),
    Phase.FADING: frozenset({Phase.FADING, Phase.RUNNER, Phase.DEAD, Phase.UNKNOWN}),
    Phase.DEAD: frozenset({Phase.DEAD, Phase.RUNNER, Phase.UNKNOWN}),
}

SUSPICIOUS_TRANSITIONS: frozenset[tuple[Phase, Phase]] = frozenset({
    (Phase.DEAD, Phase.RUNNER),
})


def is_legal_transition(before: Phase, after: Phase) -> bool:
    """True iff ``before -> after`` is in the legal-transition table."""
    if not isinstance(before, Phase) or not isinstance(after, Phase):
        raise ValueError(f"phases must be Phase values, got {before!r} -> {after!r}")
    return after in LEGAL_TRANSITIONS[before]


def transition_flag(before: Phase, after: Phase) -> str:
    """'ok', 'suspicious' (legal but noteworthy), or 'illegal'."""
    if (before, after) in SUSPICIOUS_TRANSITIONS:
        return "suspicious"
    return "ok" if is_legal_transition(before, after) else "illegal"


def validate_timeline_phases(phases: list[Phase]) -> list[tuple[int, Phase, Phase, str]]:
    """All non-ok transitions as (index_of_after, before, after, flag)."""
    out = []
    for i in range(1, len(phases)):
        flag = transition_flag(phases[i - 1], phases[i])
        if flag != "ok":
            out.append((i, phases[i - 1], phases[i], flag))
    return out


# -- timelines -----------------------------------------------------------------
@dataclass(frozen=True)
class TimelinePoint:
    timestamp_minutes: float
    snapshot: Snapshot

    def __post_init__(self) -> None:
        if self.timestamp_minutes is None or self.timestamp_minutes < 0:
            raise ValueError(f"timestamp_minutes must be >= 0, got {self.timestamp_minutes!r}")
        if self.snapshot is None:
            raise ValueError("snapshot must not be None")


def classify_timeline(points: list[TimelinePoint]) -> list[Phase]:
    """Phase of every point, in order."""
    _check_monotonic(points)
    return [classify(p.snapshot) for p in points]


def _check_monotonic(points: list[TimelinePoint]) -> None:
    for i in range(1, len(points)):
        if points[i].timestamp_minutes < points[i - 1].timestamp_minutes:
            raise ValueError("timeline timestamps must be non-decreasing")


def time_in_phase(points: list[TimelinePoint],
                  end_minutes: float | None = None) -> dict[Phase, float]:
    """Minutes spent in each phase.

    Each point owns the interval until the next point.  The last point
    owns nothing unless ``end_minutes`` (which must be >= its timestamp)
    is given.  Phases never observed are absent from the dict.
    """
    _check_monotonic(points)
    totals: dict[Phase, float] = {}
    for i, point in enumerate(points):
        start = point.timestamp_minutes
        if i + 1 < len(points):
            stop = points[i + 1].timestamp_minutes
        elif end_minutes is not None:
            if end_minutes < start:
                raise ValueError("end_minutes is before the last point")
            stop = end_minutes
        else:
            stop = start
        if stop > start:
            phase = classify(point.snapshot)
            totals[phase] = totals.get(phase, 0.0) + (stop - start)
    return totals


def phase_counts(points: list[TimelinePoint]) -> dict[Phase, int]:
    """How many observations fell in each phase."""
    counts: dict[Phase, int] = {}
    for phase in classify_timeline(points):
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def first_phase_at(points: list[TimelinePoint], phase: Phase) -> float | None:
    """Timestamp (minutes) when ``phase`` is first observed, else None."""
    for point, seen in zip(points, classify_timeline(points)):
        if seen is phase:
            return point.timestamp_minutes
    return None


def is_runner_phase(phase: Phase) -> bool:
    """The only phase the hunting lanes are allowed to act on."""
    return phase is Phase.RUNNER


# -- synthetic fixtures ---------------------------------------------------------
def _snap(curve, age, liq, mcap, mom, holders):
    return Snapshot(curve_completion_pct=curve, age_minutes=age,
                    liquidity_usd=liq, mcap_usd=mcap,
                    momentum_pct=mom, holder_trend_pct=holders)


def fast_rug_timeline() -> list[TimelinePoint]:
    """Birth to dead in minutes: never graduates, liquidity vanishes."""
    return [
        TimelinePoint(0.0, _snap(5, 0, 3000, 8000, 40, 20)),
        TimelinePoint(2.0, _snap(25, 2, 6000, 20000, 60, 30)),
        TimelinePoint(5.0, _snap(30, 5, 400, 3000, -92, -60)),
        TimelinePoint(9.0, _snap(30, 9, 100, 1500, -95, -70)),
    ]


def dac_timeline() -> list[TimelinePoint]:
    """Bonding -> graduated -> runner -> dead in under two hours."""
    return [
        TimelinePoint(0.0, _snap(10, 0, 4000, 12000, 30, 15)),
        TimelinePoint(20.0, _snap(70, 20, 9000, 40000, 45, 20)),
        TimelinePoint(38.0, _snap(100, 38, 30000, 90000, 8, 10)),
        TimelinePoint(55.0, _snap(100, 55, 80000, 300000, 80, 25)),
        TimelinePoint(80.0, _snap(100, 80, 40000, 150000, -30, -20)),
        TimelinePoint(105.0, _snap(100, 105, 500, 6000, -90, -55)),
    ]


def slow_steady_runner_timeline() -> list[TimelinePoint]:
    """Graduates, then runs for hours with holders intact."""
    return [
        TimelinePoint(0.0, _snap(20, 0, 5000, 15000, 20, 10)),
        TimelinePoint(30.0, _snap(100, 30, 20000, 60000, 8, 5)),
        TimelinePoint(60.0, _snap(100, 60, 60000, 180000, 35, 12)),
        TimelinePoint(180.0, _snap(100, 180, 90000, 260000, 28, 8)),
        TimelinePoint(360.0, _snap(100, 360, 110000, 320000, 22, 6)),
    ]


def zombie_timeline() -> list[TimelinePoint]:
    """Graduates, never runs, fades forever without quite dying."""
    return [
        TimelinePoint(0.0, _snap(100, 0, 15000, 45000, 2, 0)),
        TimelinePoint(120.0, _snap(100, 120, 12000, 38000, -18, -8)),
        TimelinePoint(480.0, _snap(100, 480, 9000, 30000, -25, -20)),
        TimelinePoint(1440.0, _snap(100, 1440, 6000, 22000, -12, -18)),
        TimelinePoint(2880.0, _snap(100, 2880, 4500, 18000, -20, -22)),
    ]
