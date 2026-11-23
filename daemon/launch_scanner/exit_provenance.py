#!/usr/bin/env python3
"""Exit price provenance labels (2026-11-22, additive).

Every simulated settlement tranche and every close summary carries an
exit_price_kind classifying where the exit price CAME FROM:

- CURRENT   "current_price_simulated_fill": priced from a current
            observed quote in that cycle.
- STALE     "stale_price_estimate": priced from the last real observed
            quote (F2 last-real-price exits; time exits at the
            last-known price while every pricing source is dark).
            An ESTIMATE, not an observed fill.
- WRITEDOWN "write_down_zero": the position was unquotable at the
            stop and was written down at zero. A conservative
            write-down, NOT an executed sale and NOT observed
            proceeds.
- UNKNOWN   "unknown": legacy record with no label; treat as
            unclassified (contaminated until reclassified).

A close summary mixing kinds reads "mixed:<kind>,<kind>" (sorted).

Labels are provenance only: they never gate an exit, never change a
settlement amount, and no consumer may read a label as execution
proof. Qualification datasets that cannot distinguish these kinds are
contaminated and must be reported as not qualified.
"""

CURRENT = "current_price_simulated_fill"
STALE = "stale_price_estimate"
WRITEDOWN = "write_down_zero"
UNKNOWN = "unknown"


def classify_reason(reason, f2_reason=None):
    """Price kind for one settlement from its close reason.

    Reasons ending in "unquotable" are zero-price write-downs; the F2
    reason (or any f2-prefixed reason) is a stale-price estimate;
    anything else with a reason is a current-price simulated fill; an
    empty reason is UNKNOWN."""
    r = str(reason or "")
    if not r:
        return UNKNOWN
    if f2_reason and r == f2_reason:
        return STALE
    if r.startswith("f2"):
        return STALE
    if r.endswith("unquotable"):
        return WRITEDOWN
    return CURRENT


def aggregate(kinds):
    """Collapse per-tranche kinds into one close-summary label.

    A tranche with no recorded kind counts as UNKNOWN (a legacy
    tranche is not silently promoted to CURRENT). No tranches at all
    is UNKNOWN. More than one distinct kind is "mixed:" + sorted
    kinds."""
    ks = {(k or UNKNOWN) for k in kinds}
    if not ks:
        return UNKNOWN
    if len(ks) == 1:
        return next(iter(ks))
    return "mixed:" + ",".join(sorted(ks))
