"""Missed-runner funnel autopsy (lab, synthetic fixtures only).

Pure stdlib, no network. Given a post-mortem ``case`` for one token,
classify *why* the funnel did not card a runner -- or honestly say the
funnel was right, or that the token was caught / something else did it.

A case is::

    {
      "timeline": [(t_min, liq_usd, mcap_usd, momentum_positive, safety)],
      "gate_log": [(t_min, lane, action, reason), ...],   # or dict rows
      "outcome":  "runner" / "rug" / ...                    # optional
    }

where ``safety`` is one of ``"known_ok"``, ``"unknown"``, ``"failed"``.

Classification precedence (checked in exactly this order, first hit
wins -- the order is the contract, because real cases overlap):

1. ``never-met-bar``                -- no snapshot ever clears the bar.
2. ``gate-correctly-blocked-rug``   -- rug outcome, or safety failed
                                       at a bar-meeting snapshot.
3. ``invented-ceiling-block``       -- a gate-log reason cites a gain
                                       ceiling / max-gain rule.
4. ``burned-one-shot-convergence``  -- an early convergence signal was
                                       blocked once and never retried,
                                       while safety was ok at some point.
5. ``safety-unknown-lag``           -- bar met, but safety was unknown
                                       at every bar-meeting snapshot.
6. ``caught-or-other``              -- none of the above.
"""

from __future__ import annotations

LIQ_MIN = 25_000
MCAP_MIN = 50_000

CLASSES = (
    "never-met-bar",
    "gate-correctly-blocked-rug",
    "invented-ceiling-block",
    "burned-one-shot-convergence",
    "safety-unknown-lag",
    "caught-or-other",
)

_CEILING_WORDS = ("ceiling", "max-gain", "max_gain", "max gain", "gain cap")
_RETRY_ACTIONS = {"retry", "retried", "fired", "allowed", "carded", "card"}
_BLOCK_ACTIONS = {"blocked", "block", "fired_and_blocked", "convergence_blocked"}
_RUG_OUTCOMES = {"rug", "rugged", "flagged_rug", "rug_flagged"}


def _snapshot(row) -> dict:
    if isinstance(row, dict):
        return {
            "t": row.get("t_min", row.get("t", 0)),
            "liq": float(row.get("liq_usd", row.get("liq", 0))),
            "mcap": float(row.get("mcap_usd", row.get("mcap", 0))),
            "momentum": bool(
                row.get("momentum_positive", row.get("momentum", False))
            ),
            "safety": str(row.get("safety", "unknown")),
        }
    t, liq, mcap, momentum, safety = row
    return {
        "t": t,
        "liq": float(liq),
        "mcap": float(mcap),
        "momentum": bool(momentum),
        "safety": str(safety),
    }


def _event(row) -> dict:
    if isinstance(row, dict):
        return {
            "t": row.get("t_min", row.get("t", 0)),
            "lane": str(row.get("lane", "")),
            "action": str(row.get("action", "")),
            "reason": str(row.get("reason", "")),
        }
    t, lane, action, reason = row
    return {"t": t, "lane": str(lane), "action": str(action), "reason": str(reason)}


def meets_bar(snapshot: dict) -> bool:
    """The runner bar: liquidity, market cap, and positive momentum."""
    return (
        snapshot["liq"] >= LIQ_MIN
        and snapshot["mcap"] >= MCAP_MIN
        and snapshot["momentum"]
    )


def bar_meeting_snapshots(case: dict) -> list[dict]:
    """Normalised snapshots that clear the bar, in timeline order."""
    return [
        snap
        for snap in (_snapshot(row) for row in case.get("timeline", []))
        if meets_bar(snap)
    ]


def first_bar_time(case: dict):
    """Earliest t at which the token cleared the bar; None if never."""
    meeting = bar_meeting_snapshots(case)
    return min((snap["t"] for snap in meeting), default=None)


def bar_duration_minutes(case: dict) -> float:
    """Span between the first and last bar-meeting snapshots.

    0.0 when the bar was met at exactly one snapshot (or never): a
    one-print runner gave the funnel a single instant, not a window.
    """
    meeting = bar_meeting_snapshots(case)
    if len(meeting) < 2:
        return 0.0
    times = [snap["t"] for snap in meeting]
    return float(max(times) - min(times))


def safety_at_first_bar(case: dict) -> str | None:
    """Safety status at the earliest bar-meeting snapshot; None if none."""
    meeting = bar_meeting_snapshots(case)
    if not meeting:
        return None
    first_t = min(snap["t"] for snap in meeting)
    for snap in meeting:
        if snap["t"] == first_t:
            return snap["safety"]
    return None  # pragma: no cover - first_t came from meeting itself


def blocking_events(case: dict) -> list[dict]:
    """Normalised gate-log events whose action is a block, in log order."""
    return [
        event
        for event in (_event(row) for row in case.get("gate_log", []))
        if event["action"].lower() in _BLOCK_ACTIONS
    ]


def has_ceiling_block(case: dict) -> bool:
    """True iff any gate-log reason cites a gain ceiling / max-gain rule."""
    return any(
        word in _event(row)["reason"].lower()
        for row in case.get("gate_log", [])
        for word in _CEILING_WORDS
    )


def retry_after_first_block(case: dict) -> bool:
    """True iff any event after the earliest convergence block is a retry.

    Mirrors the classifier's rule exactly: the retry must be strictly
    later than the first convergence block; a same-minute retry does
    not un-burn a one-shot signal.
    """
    events = [_event(row) for row in case.get("gate_log", [])]
    blocked_at = [
        event["t"]
        for event in events
        if "convergence" in event["lane"].lower()
        and event["action"].lower() in _BLOCK_ACTIONS
    ]
    if not blocked_at:
        return False
    first_block = min(blocked_at)
    return any(
        event["t"] > first_block and event["action"].lower() in _RETRY_ACTIONS
        for event in events
    )


def _is_rug_outcome(case: dict) -> bool:
    outcome = case.get("outcome")
    if isinstance(outcome, dict):
        return bool(outcome.get("rug")) or str(
            outcome.get("status", "")
        ).lower() in _RUG_OUTCOMES
    return str(outcome or "").lower() in _RUG_OUTCOMES


def classify(case: dict) -> str:
    """Classify one missed-runner case; see module docstring for order."""
    snapshots = [_snapshot(row) for row in case.get("timeline", [])]
    events = [_event(row) for row in case.get("gate_log", [])]
    bar_meeting = [snap for snap in snapshots if meets_bar(snap)]

    # 1. The bar comes first: a token that never cleared it was never
    #    a miss at all, even if it later turned out to be a rug.
    if not bar_meeting:
        return "never-met-bar"

    # 2. A rug the gates kept out is a correct block, not a miss.
    if _is_rug_outcome(case) or any(
        snap["safety"] == "failed" for snap in bar_meeting
    ):
        return "gate-correctly-blocked-rug"

    # 3. A home-grown gain ceiling killing a real runner outranks the
    #    other gate stories, because it is a rule bug, not bad luck.
    for event in events:
        reason = event["reason"].lower()
        if any(word in reason for word in _CEILING_WORDS):
            return "invented-ceiling-block"

    # 4. One-shot convergence: fired+blocked early, never retried,
    #    even though safety was known-ok at some point in the timeline.
    safety_ok_somewhere = any(snap["safety"] == "known_ok" for snap in snapshots)
    if safety_ok_somewhere:
        blocked_at = [
            event["t"]
            for event in events
            if "convergence" in event["lane"].lower()
            and event["action"].lower() in _BLOCK_ACTIONS
        ]
        if blocked_at:
            first_block = min(blocked_at)
            retried = any(
                event["t"] > first_block
                and event["action"].lower() in _RETRY_ACTIONS
                for event in events
            )
            if not retried:
                return "burned-one-shot-convergence"

    # 5. The bar was met while rising, but safety never caught up.
    if all(snap["safety"] == "unknown" for snap in bar_meeting):
        return "safety-unknown-lag"

    # 6. Caught, carded late, or a story this classifier does not know.
    return "caught-or-other"


def explain(case: dict) -> dict:
    """The classification plus the evidence a human would cite for it.

    Keys: ``classification``, ``bar_met``, ``n_bar_snapshots``,
    ``first_bar_t`` (None if never), ``bar_duration_minutes``,
    ``safety_at_first_bar``, ``n_blocking_events``,
    ``has_ceiling_block`` and ``retried_after_block``. Purely a
    reporting view -- :func:`classify` remains the single verdict.
    """
    meeting = bar_meeting_snapshots(case)
    return {
        "classification": classify(case),
        "bar_met": bool(meeting),
        "n_bar_snapshots": len(meeting),
        "first_bar_t": first_bar_time(case),
        "bar_duration_minutes": bar_duration_minutes(case),
        "safety_at_first_bar": safety_at_first_bar(case),
        "n_blocking_events": len(blocking_events(case)),
        "has_ceiling_block": has_ceiling_block(case),
        "retried_after_block": retry_after_first_block(case),
    }


def summarise(cases) -> dict:
    """Count classifications over many cases, keyed by every class.

    All six classes are always present (zero when unseen), so a report
    can show the full verdict space instead of only what happened.
    """
    counts = {name: 0 for name in CLASSES}
    for case in cases:
        counts[classify(case)] += 1
    return counts
