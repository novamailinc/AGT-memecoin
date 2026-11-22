"""Tests for realdata.py -- run against small fixtures built from real
journaled records (fields preserved, whitelisted; no wallet data).

Fixtures:
  * sample_judge_records.jsonl -- 3 real judge records chosen so the
    existing classifier returns, in file order: never-met-bar
    (liq $0), safety-unknown-lag (bar met, safety unknown),
    caught-or-other (bar met, safety known-ok, 8.8x outcome peak).
  * sample_paper_closes.jsonl -- 2 real paper closes; paper closes
    record no snapshots and no gate log, so both must be counted
    unclassifiable, with patterns round-tripper + rug-flagged.
"""
import json
import os

import pytest

import autopsy
import realdata

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGE = os.path.join(HERE, "sample_judge_records.jsonl")
PAPER = os.path.join(HERE, "sample_paper_closes.jsonl")


def _records(path):
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_judge_cases_classify_as_recorded():
    classes = [autopsy.classify(realdata.case_from_judge_record(r))
               for r in _records(JUDGE)]
    assert classes == ["never-met-bar", "safety-unknown-lag", "caught-or-other"]


def test_case_construction_uses_the_documented_proxies():
    rec = _records(JUDGE)[1]  # the safety-lag record
    case = realdata.case_from_judge_record(rec)
    snap = case["timeline"][0]
    assert snap["liq_usd"] == pytest.approx(36192.18)
    assert snap["momentum_positive"] is True   # buys 35 > sells 13 (proxy)
    assert snap["safety"] == "unknown"          # record-level safety, mapped
    assert case["gate_log"] == []               # nothing recorded: empty, honestly
    assert case["outcome"] == "flat"            # peak 1.0, not a rug


def test_combined_run_counts_unclassifiable_paper_closes(tmp_path):
    combined = tmp_path / "combined.jsonl"
    with open(combined, "w") as out:
        for path in (JUDGE, PAPER):
            out.write(open(path).read())
    result = realdata.autopsy_real(str(combined))
    assert result["judge_records"] == 3
    assert result["judge_class_distribution"] == {
        "never-met-bar": 1, "gate-correctly-blocked-rug": 0,
        "invented-ceiling-block": 0, "burned-one-shot-convergence": 0,
        "safety-unknown-lag": 1, "caught-or-other": 1}
    assert result["paper_closes"] == 2
    assert result["paper_classifiable"] == 0
    assert result["paper_unclassifiable"] == 2
    assert result["paper_close_patterns"]["round-tripper"] == 1
    assert result["paper_close_patterns"]["rug-flagged"] == 1
    assert result["judge_rug_outcomes"] == 0
    assert result["judge_safety_failed"] == 0


def test_unreachable_classes_are_named_with_a_reason(tmp_path):
    result = realdata.autopsy_real(JUDGE)
    assert result["unreachable_classes"] == [
        "invented-ceiling-block", "burned-one-shot-convergence"]
    assert "no gate-log" in result["unreachable_reason"]


def test_paper_close_pattern_precedence():
    rt = {"peak_mult": 2.5, "realized_mult": 0.4, "rug": True}
    assert realdata.paper_close_pattern(rt) == "round-tripper"  # peak story first
    assert realdata.paper_close_pattern(
        {"peak_mult": 1.2, "realized_mult": 0.02, "rug": True}) == "rug-flagged"
    assert realdata.paper_close_pattern(
        {"peak_mult": 3.0, "realized_mult": 2.0, "rug": False}) == "winner"
    assert realdata.paper_close_pattern(
        {"peak_mult": 1.1, "realized_mult": 1.2, "rug": False}) == "breakeven-band"
