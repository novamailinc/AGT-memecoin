#!/usr/bin/env python3
"""Promotion plumbing tests (section 4.6): research-only verdicts,
entry-time-only scorer schema, chronology by settled time, labeled
exclusions, durable storage. No promotion is ever approved here."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "..", "daemon", "launch_scanner"))

import promotion  # noqa: E402

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)
        raise AssertionError(name)


def _recs(mults, base_ts=1_700_000_000, step=3600,
          kind="current_price_simulated_fill"):
    return [{"entry_ts": base_ts + i * step - 600,
             "settled_ts": base_ts + i * step,
             "realized_mult": m,
             "features": {"band": "x"},
             "exit_price_kind": kind}
            for i, m in enumerate(mults)]


TAKE_ALL = lambda entry: True  # noqa: E731
TAKE_NONE = lambda entry: False  # noqa: E731

CRITERIA = {"approved": True, "min_settled": 30, "min_val": 10,
            "val_frac": 0.25}


def test_entry_schema_hygiene():
    recs = _recs([1.2, 0.8] * 20)
    recs.append({"settled_ts": 1_700_500_000,
                 "realized_mult": 1.3, "features": {"band": "x"},
                 "exit_price_kind": "current_price_simulated_fill"})
    recs.append({"entry_ts": 1_700_600_000,
                 "settled_ts": 1_700_500_000,
                 "realized_mult": 1.3, "features": {"band": "x"},
                 "exit_price_kind": "current_price_simulated_fill"})
    recs.append({"entry_ts": 1_700_400_000,
                 "settled_ts": 1_700_500_000,
                 "realized_mult": 1.3, "features": None,
                 "exit_price_kind": "current_price_simulated_fill"})
    dec = promotion.evaluate_promotion(
        "cand-schema", recs, TAKE_ALL, TAKE_ALL,
        criteria=dict(CRITERIA))
    check("promotion: invalid entry schema excluded with reasons, "
          "never scored",
          dec.get("n_settled") == 40
          and dec.get("excluded_reasons")
          == ["entry_after_settled", "missing_or_invalid_entry_ts",
              "missing_or_invalid_features"])


def test_no_criteria_is_not_eligible():
    dec = promotion.evaluate_promotion(
        "cand-nocrit", _recs([1.2, 0.8] * 20), TAKE_ALL, TAKE_ALL)
    check("promotion: no criteria -> NEED-CRITERIA and not "
          "eligible, never a recommendation",
          dec.get("verdict") == "NEED-CRITERIA"
          and dec.get("promotion_eligibility")
          == "not_eligible_research_only"
          and dec.get("research_only") is True
          and dec.get("activation") == "not_authorized"
          and "PROMOTE" not in str(dec.get("verdict")))


def test_approved_flag_grants_no_eligibility():
    recs = _recs([1.0 + (0.4 if i % 2 == 0 else -0.2)
                  for i in range(40)])
    dec = promotion.evaluate_promotion(
        "cand-eval", recs, TAKE_ALL,
        lambda entry: True, criteria=dict(CRITERIA))
    check("promotion: caller 'approved' flag grants nothing - "
          "EVALUATED evidence stays not eligible",
          dec.get("verdict") == "EVALUATED"
          and dec.get("promotion_eligibility")
          == "not_eligible_research_only"
          and dec.get("qualified_performance", False) is False
          and dec.get("val_candidate", {}).get("taken") == 10)
    small = promotion.evaluate_promotion(
        "cand-small", _recs([1.2, 0.8, 1.1]), TAKE_ALL, TAKE_ALL,
        criteria=dict(CRITERIA))
    check("promotion: too few settled records is NEED-DATA",
          small.get("verdict") == "NEED-DATA"
          and small.get("n_settled") == 3)


def test_scorer_sees_entry_time_only():
    seen = []

    def peek(entry):
        seen.append(sorted(entry.keys()))
        return entry["realized_mult"] > 1.0  # outcome peek: must fail

    raised = False
    try:
        promotion.evaluate_promotion(
            "cand-peek", _recs([1.2, 0.8] * 20), TAKE_ALL, peek,
            criteria=dict(CRITERIA))
    except KeyError:
        raised = True
    check("promotion: scorer input has no outcome fields; peeking "
          "fails loudly",
          raised and seen
          and all("realized_mult" not in keys for keys in seen))


def test_chronology_uses_settled_ts_not_archive_time():
    # Records carrying only archive bookkeeping time (no settled_ts)
    # are excluded, never silently treated as event time.
    recs = [{"archived_ts": 1_700_000_000 + i * 3600,
             "realized_mult": 1.1,
             "exit_price_kind": "current_price_simulated_fill"}
            for i in range(5)]
    dec = promotion.evaluate_promotion(
        "cand-arch", recs, TAKE_ALL, TAKE_ALL, criteria=dict(CRITERIA))
    check("promotion: archived_ts is not event time",
          dec.get("n_settled") == 0
          and dec.get("excluded_reasons")
          == ["missing_or_invalid_settled_ts"])
    # Shuffled input still splits by settled time: newest fold is OOS.
    mults = [2.0] * 30 + [0.5] * 10
    recs2 = _recs(mults)[::-1]
    dec2 = promotion.evaluate_promotion(
        "cand-chrono", recs2, TAKE_ALL, TAKE_ALL,
        criteria=dict(CRITERIA))
    check("promotion: validation fold is the newest settled records",
          dec2.get("val_incumbent", {}).get("taken") == 10
          and abs(dec2.get("val_incumbent", {}).get(
              "mean_mult", 0) - 0.5) < 1e-9)


def test_exclusions_counted_and_reported_separately():
    recs = _recs([1.5, 0.5] * 20)
    recs.append({"entry_ts": 1_700_500_000 - 600,
                 "settled_ts": 1_700_500_000,
                 "realized_mult": float("nan"),
                 "exit_price_kind": "current_price_simulated_fill"})
    recs.append({"entry_ts": 1_700_503_600 - 600,
                 "settled_ts": 1_700_503_600,
                 "realized_mult": 9.9,
                 "exit_price_kind": "stale_estimate"})
    dec = promotion.evaluate_promotion(
        "cand-excl", recs, TAKE_ALL, TAKE_ALL, criteria=dict(CRITERIA))
    check("promotion: excluded outcomes labeled and reported "
          "separately, never mixed",
          dec.get("n_settled") == 40
          and dec.get("n_excluded") == 2
          and dec.get("excluded_reasons")
          == ["nonfinite_or_missing_outcome", "not_current_price"]
          and (dec.get("excluded_economics") or {}).get("n") == 2
          and (dec.get("excluded_economics") or {}).get(
              "mean_mult") == 9.9
          and dec.get("provenance")
          == "simulated_current_price_only")


def test_decision_durable_append_and_read():
    home = tempfile.mkdtemp(prefix="promotion-")
    dec = promotion.evaluate_promotion(
        "cand-durable", _recs([1.1] * 40), TAKE_ALL, TAKE_ALL,
        criteria=dict(CRITERIA))
    check("promotion: decision append lands durably",
          promotion.append_decision(dec, home=home) is True)
    # A torn line is skipped, never misread.
    with open(promotion._path(home), "a") as f:
        f.write("{torn\n")
    loaded = promotion.load_decisions(home=home)
    check("promotion: durable record readable, torn line skipped",
          len(loaded) == 1
          and loaded[0].get("candidate_id") == "cand-durable"
          and loaded[0].get("activation") == "not_authorized"
          and loaded[0].get("research_only") is True)


def test_torn_tail_repaired_before_append():
    home = tempfile.mkdtemp(prefix="promotion-")
    os.makedirs(os.path.dirname(promotion._path(home)),
                exist_ok=True)
    # A fragment WITHOUT a trailing newline: a naive append would
    # glue the new record onto it and the read would lose both.
    with open(promotion._path(home), "w") as f:
        f.write("{torn-fragment")
    dec = promotion.evaluate_promotion(
        "cand-repair", _recs([1.1] * 40), TAKE_ALL, TAKE_ALL,
        criteria=dict(CRITERIA))
    check("promotion: append after torn tail still lands",
          promotion.append_decision(dec, home=home) is True)
    loaded = promotion.load_decisions(home=home)
    st = promotion.decisions_status(home=home)
    check("promotion: new record survives the torn tail, fragment "
          "counted unreadable",
          len(loaded) == 1
          and loaded[0].get("candidate_id") == "cand-repair"
          and st.get("unreadable_lines") == 1
          and st.get("records") == 1)


def test_lock_failure_fails_closed():
    home = tempfile.mkdtemp(prefix="promotion-")
    os.makedirs(os.path.dirname(promotion._path(home)),
                exist_ok=True)
    # Lock path as a directory: os.open fails -> append must NOT
    # proceed unlocked or claim durability.
    os.mkdir(promotion._lock_path(home))
    dec = promotion.evaluate_promotion(
        "cand-lockfail", _recs([1.1] * 40), TAKE_ALL, TAKE_ALL,
        criteria=dict(CRITERIA))
    check("promotion: lock failure fails closed, never an unlocked "
          "silent append",
          promotion.append_decision(dec, home=home) is False
          and promotion.load_decisions(home=home) == [])


def test_research_limits_recorded():
    dec = promotion.evaluate_promotion(
        "cand-limits", _recs([1.1] * 40), TAKE_ALL, TAKE_ALL,
        criteria=dict(CRITERIA))
    check("promotion: unresolved requirements ride the record",
          all(x in (dec.get("research_limits") or [])
              for x in ("capital_protection_not_modeled",
                        "selection_noise_uncontrolled",
                        "fresh_holdout_not_enforced",
                        "outcome_overlap_unhandled",
                        "missingness_bias_possible",
                        "counterfactual_coverage_unknown",
                        "callable_leakage_unprovable")))


_main_called = set()


def main():
    # Run every test_* exactly once, in sorted order, so the direct
    # script entry point covers the same set pytest collects.
    for name in sorted(n for n in globals()
                       if n.startswith("test_")
                       and callable(globals()[n])):
        _main_called.add(name)
        try:
            globals()[name]()
        except AssertionError:
            pass  # already recorded in FAILS by check()
        except Exception as e:
            print("FAIL %s raised %r" % (name, e))
            FAILS.append("%s raised %r" % (name, e))
    registered = {n for n in globals()
                  if n.startswith("test_") and callable(globals()[n])}
    try:
        check("registration: every test function ran in main",
              _main_called == registered)
    except AssertionError:
        pass
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL PROMOTION TESTS PASSED")


if __name__ == "__main__":
    main()
