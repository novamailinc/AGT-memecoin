#!/usr/bin/env python3
"""Promotion plumbing tests (section 4.6): criteria discipline,
labeled exclusions, durable decision records, no activation."""
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
    return [{"ts": base_ts + i * step, "realized_mult": m,
             "exit_price_kind": kind}
            for i, m in enumerate(mults)]


TAKE_ALL = lambda r: True  # noqa: E731
TAKE_NONE = lambda r: False  # noqa: E731


def test_need_data_below_min_settled():
    dec = promotion.evaluate_promotion(
        "cand-small", _recs([1.2, 0.8, 1.1]), TAKE_ALL, TAKE_ALL)
    check("promotion: too few settled records is NEED-DATA",
          dec.get("verdict") == "NEED-DATA"
          and dec.get("n_settled") == 3)


def test_promote_to_demo_requires_oos_improvement():
    mults = [1.0 + (0.4 if i % 2 == 0 else -0.2) for i in range(40)]
    recs = _recs(mults)
    better = promotion.evaluate_promotion(
        "cand-better", recs, TAKE_ALL,
        lambda r: r["realized_mult"] > 1.0)
    check("promotion: strictly better OOS is PROMOTE-TO-DEMO, "
          "never activation",
          better.get("verdict") == "PROMOTE-TO-DEMO"
          and better.get("activation") == "not_authorized"
          and better.get("qualified_performance") is False)
    worse = promotion.evaluate_promotion(
        "cand-worse", recs, TAKE_ALL, TAKE_NONE)
    check("promotion: candidate taking nothing OOS is REJECT",
          worse.get("verdict") == "REJECT"
          and worse.get("reason") == "candidate_takes_nothing_oos")
    flat = promotion.evaluate_promotion(
        "cand-flat", recs, TAKE_ALL, TAKE_ALL)
    check("promotion: no OOS improvement is REJECT",
          flat.get("verdict") == "REJECT"
          and flat.get("reason") == "no_oos_improvement")


def test_exclusions_counted_never_mixed():
    recs = _recs([1.5, 0.5] * 20)
    recs.append({"ts": 1_700_500_000, "realized_mult": float("nan"),
                 "exit_price_kind": "current_price_simulated_fill"})
    recs.append({"ts": 1_700_503_600, "realized_mult": 9.9,
                 "exit_price_kind": "stale_estimate"})
    dec = promotion.evaluate_promotion(
        "cand-excl", recs, TAKE_ALL, TAKE_ALL)
    check("promotion: non-finite and non-current-price outcomes are "
          "excluded and labeled, never mixed",
          dec.get("n_settled") == 40
          and dec.get("n_excluded") == 2
          and dec.get("excluded_reasons")
          == ["nonfinite_or_missing_core", "not_current_price"]
          and dec.get("provenance")
          == "simulated_current_price_only")


def test_chronological_split_uses_time_order():
    # Shuffled input must still split by time: the validation fold
    # is the NEWEST quarter.
    mults = [2.0] * 30 + [0.5] * 10
    recs = _recs(mults)
    shuffled = recs[::-1]
    dec = promotion.evaluate_promotion(
        "cand-chrono", shuffled, TAKE_ALL, TAKE_ALL)
    check("promotion: validation fold is the newest records",
          dec.get("val_incumbent", {}).get("taken") == 10
          and abs(dec.get("val_incumbent", {}).get("mean_mult", 0)
                  - 0.5) < 1e-9)


def test_decision_durable_append_and_read():
    home = tempfile.mkdtemp(prefix="promotion-")
    dec = promotion.evaluate_promotion(
        "cand-durable", _recs([1.1] * 40), TAKE_ALL, TAKE_ALL)
    check("promotion: decision append lands", 
          promotion.append_decision(dec, home=home) is True)
    # A torn line is skipped, never misread.
    with open(promotion._path(home), "a") as f:
        f.write("{torn\n")
    loaded = promotion.load_decisions(home=home)
    check("promotion: durable record readable, torn line skipped",
          len(loaded) == 1
          and loaded[0].get("candidate_id") == "cand-durable"
          and loaded[0].get("activation") == "not_authorized")


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
