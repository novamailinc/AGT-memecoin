"""Real-data adapter for the funnel autopsy lab.

Runs the existing ``autopsy.classify`` over the real journaled cases
the recorded data actually supports, and is explicit about the cases
it does not support -- those are counted as *unclassifiable from
recorded data*, never forced into a class.

What the live journal (``backtest_dataset.jsonl``) actually records:

  * ``judge`` records (the large majority) carry a snapshot list
    (``t``, ``liq_usd``, ``mcap_usd``, ``buys5m``, ``sells5m``), one
    record-level ``safety`` object (``score`` / ``unknown`` / ``fails``),
    a ``verdict`` (confirm / reject) and an ``outcome`` (``peak_mult``,
    ``rug``). That is enough to build an autopsy case, with two
    documented derivations:
      - momentum: no per-snapshot momentum flag is recorded, so
        ``momentum_positive`` is proxied as ``buys5m > sells5m``.
      - safety: recorded once per record, not per snapshot, so the
        same mapped status (unknown / failed / known_ok) is applied
        to every snapshot of that record.
    No gate-log reasons are recorded anywhere in the journal, so
    ``gate_log`` is empty by construction -- which makes two classes
    *unreachable* from this data: ``invented-ceiling-block`` (needs a
    recorded ceiling reason) and ``burned-one-shot-convergence`` (needs
    a recorded convergence block). A zero count for them means "not
    observable in the recorded data", NOT "never happened".
    Additionally, in the journal as recorded, judge outcomes never set
    ``rug`` and safety ``fails`` is always empty, so
    ``gate-correctly-blocked-rug`` has no supporting evidence in judge
    records either (the adapter counts the evidence:
    ``judge_rug_outcomes`` / ``judge_safety_failed``). Rug flags DO
    exist -- on paper closes, which carry no snapshots to classify.

  * ``paper`` closes carry NO snapshots (the field is an empty list on
    every recorded close) and no gate log. The funnel classifier
    therefore has nothing to classify: every paper close is counted as
    unclassifiable. As a supplementary, clearly separate tally, paper
    closes are also counted by *outcome pattern* (round-tripper,
    rug-flagged, winner, plain loss) from their recorded peak/realized
    multiples -- pattern tallies are not funnel classes and are never
    merged into the autopsy distribution.

Privacy: the adapter whitelists fields. Wallet addresses, wallet
names/grades, registry and follower data are never read or emitted --
only public token mints and recorded market/outcome numbers.
"""
from __future__ import annotations

import json

import autopsy

DEFAULT_DATASET = "/home/hatch/.forex-agent/launch_scanner/backtest_dataset.jsonl"


def _safety_status(safety) -> str:
    """Map a record-level safety object to the classifier's vocabulary."""
    safety = safety or {}
    if safety.get("unknown"):
        return "unknown"
    if safety.get("fails"):
        return "failed"
    return "known_ok"


def case_from_judge_record(rec: dict) -> dict:
    """Build an autopsy case from one judge record (see module docs)."""
    t0 = float(rec.get("t0") or 0.0)
    status = _safety_status(rec.get("safety"))
    timeline = []
    for snap in rec.get("snapshots") or []:
        timeline.append({
            "t_min": (float(snap.get("t") or t0) - t0) / 60.0,
            "liq_usd": float(snap.get("liq_usd") or 0.0),
            "mcap_usd": float(snap.get("mcap_usd") or 0.0),
            # Proxy: no momentum flag is recorded per snapshot.
            "momentum_positive": (snap.get("buys5m") or 0) > (snap.get("sells5m") or 0),
            "safety": status,
        })
    outcome = rec.get("outcome") or {}
    if outcome.get("rug"):
        label = "rug"
    elif (outcome.get("peak_mult") or 0.0) >= 2.0:
        label = "runner"
    else:
        label = "flat"
    # No gate-log reasons are recorded in the journal: empty by design.
    return {"timeline": timeline, "gate_log": [], "outcome": label}


def paper_close_pattern(rec: dict) -> str:
    """Outcome pattern of one paper close (NOT a funnel class)."""
    peak = float(rec.get("peak_mult") or 0.0)
    realized = float(rec.get("realized_mult") or 0.0)
    if peak >= 2.0 and realized < 1.0:
        return "round-tripper"
    if rec.get("rug"):
        return "rug-flagged"
    if realized >= 1.5:
        return "winner"
    if realized < 1.0:
        return "plain-loss"
    return "breakeven-band"


def autopsy_real(path: str = DEFAULT_DATASET, limit: int | None = None) -> dict:
    """Stream the journal once and classify everything classifiable.

    Returns judge-record class counts (all six autopsy classes, zeros
    included), the paper-close classifiability count, supplementary
    paper-close pattern tallies, and record totals.
    """
    distribution = {name: 0 for name in autopsy.CLASSES}
    patterns = {"round-tripper": 0, "rug-flagged": 0, "winner": 0,
                "plain-loss": 0, "breakeven-band": 0}
    counts = {"judge_records": 0, "paper_closes": 0,
              "paper_classifiable": 0, "paper_unclassifiable": 0,
              "other_records": 0, "judge_rug_outcomes": 0,
              "judge_safety_failed": 0}
    seen = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen += 1
            kind = rec.get("outcome_kind")
            if kind == "judge":
                counts["judge_records"] += 1
                if (rec.get("outcome") or {}).get("rug"):
                    counts["judge_rug_outcomes"] += 1
                if (rec.get("safety") or {}).get("fails"):
                    counts["judge_safety_failed"] += 1
                distribution[autopsy.classify(case_from_judge_record(rec))] += 1
            elif kind == "paper":
                counts["paper_closes"] += 1
                # Paper closes record no snapshots and no gate log:
                # the funnel classifier has nothing to work on.
                if rec.get("snapshots") or rec.get("gate_log"):
                    counts["paper_classifiable"] += 1
                else:
                    counts["paper_unclassifiable"] += 1
                if rec.get("realized_mult") is not None:
                    patterns[paper_close_pattern(rec)] += 1
            else:
                counts["other_records"] += 1
            if limit is not None and seen >= limit:
                break
    return {
        "judge_class_distribution": distribution,
        "paper_close_patterns": patterns,
        **counts,
        "unreachable_classes": ["invented-ceiling-block",
                                "burned-one-shot-convergence"],
        "unreachable_reason": (
            "the journal records no gate-log reasons and no convergence "
            "events, so these two classes cannot be observed in the "
            "recorded data; a zero count is absence of evidence, not "
            "evidence of absence"),
    }


if __name__ == "__main__":  # pragma: no cover - manual real-data run
    print(json.dumps(autopsy_real(), indent=2))
