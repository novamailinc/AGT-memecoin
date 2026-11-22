"""Deployer (token creator) history forensics (synthetic lab).

Pure stdlib, no network.  Given a synthetic history of launches, this
lab answers one question per creator: what does their record actually
support saying about them?

Verdicts, fail-closed:
  * UNKNOWN       -- no history at all, or history whose outcomes are
                     all unknown.  Unknown is NOT clean: absence of
                     evidence is not evidence of a clean creator.
  * SERIAL_RUGGER -- >= 3 launches and >= 2 known rugs.
  * WATCH         -- some signal worth attention: any known rug that
                     does not meet the serial bar, a rug rate >= 25%
                     on known outcomes, heavy funding-source reuse,
                     heavy name-pattern reuse, or too little known
                     history to clear the creator.
  * CLEAN         -- only with real evidence: >= 2 launches, every
                     outcome known, zero rugs, and no fingerprint
                     reuse flags.  Anything less stays WATCH/UNKNOWN.

Fingerprint features (all computed, none trusted alone):
  * funding-source reuse -- share of launches whose funding wallet
    also funded another of this creator's launches;
  * launch cadence -- median hours between consecutive launches;
  * name-pattern reuse -- names normalised (lowercased, trailing
    digits/punctuation stripped) and compared across launches.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from enum import Enum

KNOWN_OUTCOMES = frozenset({"rug", "runner", "flat"})
SERIAL_MIN_LAUNCHES = 3
SERIAL_MIN_RUGS = 2
WATCH_RUG_RATE = 0.25
WATCH_FUNDING_REUSE = 0.5
WATCH_NAME_REUSE = 0.5
CLEAN_MIN_LAUNCHES = 2


class Verdict(str, Enum):
    UNKNOWN = "UNKNOWN"
    CLEAN = "CLEAN"
    WATCH = "WATCH"
    SERIAL_RUGGER = "SERIAL_RUGGER"


@dataclass(frozen=True)
class Launch:
    """One synthetic launch.  outcome: 'rug' | 'runner' | 'flat' | 'unknown'."""

    deployer: str
    mint: str
    name: str
    timestamp_hours: float
    funded_by: str
    outcome: str

    def __post_init__(self) -> None:
        if not self.deployer:
            raise ValueError("deployer must be non-empty")
        if not self.mint:
            raise ValueError("mint must be non-empty")
        if self.timestamp_hours is None or self.timestamp_hours < 0:
            raise ValueError(f"timestamp_hours must be >= 0, got {self.timestamp_hours!r}")
        if self.outcome not in KNOWN_OUTCOMES | {"unknown"}:
            raise ValueError(f"outcome must be rug/runner/flat/unknown, got {self.outcome!r}")


def is_rug(launch: Launch) -> bool:
    return launch.outcome == "rug"


def has_known_outcome(launch: Launch) -> bool:
    return launch.outcome in KNOWN_OUTCOMES


def launches_by_deployer(records: list[Launch]) -> dict[str, list[Launch]]:
    """Group records by deployer, each group sorted by time."""
    grouped: dict[str, list[Launch]] = {}
    for record in records:
        grouped.setdefault(record.deployer, []).append(record)
    for group in grouped.values():
        group.sort(key=lambda r: r.timestamp_hours)
    return grouped


def rug_count(records: list[Launch]) -> int:
    return sum(1 for r in records if is_rug(r))


def known_count(records: list[Launch]) -> int:
    return sum(1 for r in records if has_known_outcome(r))


def rug_rate(records: list[Launch]) -> float | None:
    """Rugs / known outcomes.  None when nothing is known (fail-closed:
    a caller must not read None as 0%)."""
    known = [r for r in records if has_known_outcome(r)]
    if not known:
        return None
    return rug_count(records) / len(known)


def is_serial_rugger(records: list[Launch]) -> bool:
    """>= 3 launches and >= 2 rugs."""
    return len(records) >= SERIAL_MIN_LAUNCHES and rug_count(records) >= SERIAL_MIN_RUGS


def name_pattern(name: str) -> str:
    """Normalised name pattern: lowercase alphanumerics, trailing
    digits/separators stripped ('MoonCat 2!!' and 'mooncat' collide)."""
    cleaned = re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()
    cleaned = re.sub(r"[\s\d]+$", "", cleaned)
    return cleaned or "<empty>"


@dataclass(frozen=True)
class Fingerprint:
    launch_count: int
    known_outcomes: int
    rug_count: int
    distinct_funding_sources: int
    funding_reuse_ratio: float
    name_reuse_ratio: float
    median_interval_hours: float | None
    distinct_name_patterns: int


def fingerprint(records: list[Launch]) -> Fingerprint:
    """Fingerprint features for one deployer's launches (any order)."""
    ordered = sorted(records, key=lambda r: r.timestamp_hours)
    n = len(ordered)
    if n == 0:
        return Fingerprint(0, 0, 0, 0, 0.0, 0.0, None, 0)
    funding_counts: dict[str, int] = {}
    for r in ordered:
        funding_counts[r.funded_by] = funding_counts.get(r.funded_by, 0) + 1
    reused = sum(c for c in funding_counts.values() if c > 1)
    patterns = [name_pattern(r.name) for r in ordered]
    intervals = [ordered[i + 1].timestamp_hours - ordered[i].timestamp_hours
                 for i in range(n - 1)]
    return Fingerprint(
        launch_count=n,
        known_outcomes=known_count(ordered),
        rug_count=rug_count(ordered),
        distinct_funding_sources=len(funding_counts),
        funding_reuse_ratio=reused / n,
        name_reuse_ratio=1.0 - len(set(patterns)) / n,
        median_interval_hours=statistics.median(intervals) if intervals else None,
        distinct_name_patterns=len(set(patterns)),
    )


def verdict_for_records(records: list[Launch]) -> Verdict:
    """The verdict one deployer's record supports.  Fail-closed."""
    if not records:
        return Verdict.UNKNOWN
    if is_serial_rugger(records):
        return Verdict.SERIAL_RUGGER
    fp = fingerprint(records)
    if fp.known_outcomes == 0:
        return Verdict.UNKNOWN
    rate = rug_rate(records)
    if fp.rug_count >= 1:
        return Verdict.WATCH
    if rate is not None and rate >= WATCH_RUG_RATE:
        return Verdict.WATCH
    if fp.funding_reuse_ratio >= WATCH_FUNDING_REUSE and fp.launch_count >= 2:
        return Verdict.WATCH
    if fp.name_reuse_ratio >= WATCH_NAME_REUSE and fp.launch_count >= 2:
        return Verdict.WATCH
    if fp.launch_count < CLEAN_MIN_LAUNCHES or fp.known_outcomes < fp.launch_count:
        # Not enough *complete* evidence to call anyone clean.
        return Verdict.WATCH
    return Verdict.CLEAN


@dataclass(frozen=True)
class DeployerReport:
    deployer: str
    verdict: Verdict
    fingerprint: Fingerprint
    rug_rate: float | None


def analyze(records: list[Launch]) -> dict[str, DeployerReport]:
    """A report per deployer found in ``records``."""
    return {
        deployer: DeployerReport(
            deployer=deployer,
            verdict=verdict_for_records(group),
            fingerprint=fingerprint(group),
            rug_rate=rug_rate(group),
        )
        for deployer, group in launches_by_deployer(records).items()
    }


def verdict_for(deployer: str, records: list[Launch]) -> Verdict:
    """Verdict for one deployer; UNKNOWN if they have no history."""
    if not deployer:
        raise ValueError("deployer must be non-empty")
    return verdict_for_records(launches_by_deployer(records).get(deployer, []))


def blacklist_candidates(records: list[Launch]) -> list[str]:
    """Deployers whose verdict is SERIAL_RUGGER, sorted for determinism."""
    return sorted(d for d, report in analyze(records).items()
                  if report.verdict is Verdict.SERIAL_RUGGER)


def should_veto(deployer: str, records: list[Launch]) -> bool:
    """The repeat-rug veto: only SERIAL_RUGGER vetoes.  WATCH and
    UNKNOWN do not veto here -- they inform, they do not convict --
    but callers must never read a non-veto as 'clean'."""
    return verdict_for(deployer, records) is Verdict.SERIAL_RUGGER


# -- synthetic fixtures ---------------------------------------------------------
def _launch(deployer, mint, name, t, funded, outcome):
    return Launch(deployer=deployer, mint=mint, name=name,
                  timestamp_hours=t, funded_by=funded, outcome=outcome)


def serial_rugger_history() -> list[Launch]:
    """One funder, one name pattern, four launches, three rugs."""
    return [
        _launch("SerialSam", "M1", "MoonCat", 0.0, "FundWalletX", "rug"),
        _launch("SerialSam", "M2", "MoonCat 2", 5.0, "FundWalletX", "rug"),
        _launch("SerialSam", "M3", "MoonCat 3!", 11.0, "FundWalletX", "flat"),
        _launch("SerialSam", "M4", "MoonCat IV", 20.0, "FundWalletX", "rug"),
    ]


def clean_creator_history() -> list[Launch]:
    """Three launches, all known, no rugs, fresh funding and names."""
    return [
        _launch("HonestAda", "M10", "SolarPanda", 0.0, "FundA", "runner"),
        _launch("HonestAda", "M11", "QuietRiver", 100.0, "FundB", "flat"),
        _launch("HonestAda", "M12", "PaperPlane", 260.0, "FundC", "runner"),
    ]


def mixed_history() -> list[Launch]:
    """Several deployers: serial, clean, watch, and a total unknown."""
    records = serial_rugger_history() + clean_creator_history()
    records += [
        _launch("OneRugRita", "M20", "LuckyFrog", 0.0, "FundR", "runner"),
        _launch("OneRugRita", "M21", "LuckyFrog 2", 30.0, "FundR", "rug"),
        _launch("GhostGary", "M30", "SilentPeak", 0.0, "FundG", "unknown"),
    ]
    return records
