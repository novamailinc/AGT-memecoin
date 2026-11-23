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


# The qualification status every simulated performance number
# carries (owner-reviewed 2026-11-22): a simulated fill is never
# execution proof, and the fill model is UNVALIDATED until a
# timestamp-correct fill model is reviewed in. Price-source labels
# classify where a price CAME FROM; fill-model readiness is a
# separate axis and stays False regardless of labels.
QUALIFICATION_STATUS = "simulation_unvalidated_not_qualified"
FILL_MODEL_VALIDATED = False


def sample_breakdown(closes):
    """Per-kind provenance tally for a sample of close records.

    counts every close under its aggregate exit_price_kind label
    (missing label = UNKNOWN, never silently promoted), then:
      unverified - closes NOT priced at a current observed quote
                   (stale, write-down, unknown, and any mixed close
                   all count; mixed current+stale is still
                   contaminated)
      mixed      - closes aggregating more than one kind
      unknown    - closes with no usable kind at all
    Visibility only: these numbers must never gate or re-weight an
    evaluation."""
    per_kind = {}
    for c in closes:
        k = (c or {}).get("exit_price_kind") or UNKNOWN
        per_kind[k] = per_kind.get(k, 0) + 1
    unverified = sum(n for k, n in per_kind.items() if k != CURRENT)
    mixed = sum(n for k, n in per_kind.items()
                if k.startswith("mixed:"))
    unknown = sum(n for k, n in per_kind.items()
                  if k == UNKNOWN or "unknown" in k)
    return {"total": len(closes), "per_kind": per_kind,
            "unverified": unverified, "mixed": mixed,
            "unknown": unknown}


def verdict_visibility(closes):
    """The additive verdict-record fields (owner-approved 2026-11-22):
    the registered halt math and existing verdict fields are
    untouched; these fields make the sample's provenance and
    qualification status explicit so a legacy expectancy number can
    never be READ as qualified performance. Any change to the
    registered evaluation itself is an owner decision, not a
    technical default."""
    # Diagnostic contamination measurement (2026-11-23): the
    # registered expectancy ingests stale-estimated closes BY FROZEN
    # DESIGN. This DIAGNOSTIC puts a current-price-only number next to
    # it so the gap is measured, not hidden. It is NOT qualified
    # performance: the current-price-only subset can carry missingness
    # bias (the closes that lack a current quote may not be random), so
    # it is a diagnostic of how much the frozen number depends on
    # estimates, nothing more. It never feeds HALT, excludes nothing
    # from the registered sample, and re-registers nothing; changing
    # the registered evaluation itself stays an owner decision.
    current_only = [float((c or {}).get("pnl_usd") or 0) for c in closes
                    if (c or {}).get("exit_price_kind") == CURRENT]
    return {
        "price_kind_breakdown": sample_breakdown(closes),
        "qualification_status": QUALIFICATION_STATUS,
        "fill_model_validated": FILL_MODEL_VALIDATED,
        "expectancy_presentation":
            "reproducibility_only_not_qualified_performance",
        "stale_estimate_diagnostic": {
            "current_price_closes": len(current_only),
            "expectancy_current_price_only_diagnostic": (
                round(sum(current_only) / len(current_only), 6)
                if current_only else None),
            "note": ("diagnostic only, NOT qualified performance - "
                     "the current-price subset can carry missingness "
                     "bias; the registered verdict math is UNCHANGED "
                     "(all closes, frozen) and keeps ingesting "
                     "stale-estimate P/L until the owner changes it; "
                     "this figure never feeds HALT, excludes nothing, "
                     "re-registers nothing"),
        },
    }


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
