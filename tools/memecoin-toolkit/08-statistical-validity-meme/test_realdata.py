"""Tests for realdata.py -- run against sample_paper_closes.jsonl, a
small fixture built from real journaled paper closes (fields preserved,
whitelisted: mint/symbol/source/leg + outcome numbers only; no wallet
data of any kind).

Fixture multiples, in file order: 1.838, 3.027, 0.1493, 0.024, 0.6525,
1.0716 (sum 6.7624). One recorded rug (0.024). Every expectation below
is computed by hand from those six numbers.
"""
import os

import pytest

from realdata import (full_report, iter_dataset_paper_closes,
                      load_stacker_closes, summarise_closes)
from statslab import required_n

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "sample_paper_closes.jsonl")
CLOSES = list(iter_dataset_paper_closes(FIXTURE))


def test_fixture_loads_six_real_closes():
    assert len(CLOSES) == 6
    assert all(c["lane"] == "paper" for c in CLOSES)
    assert all(c["rug_basis"] == "recorded" for c in CLOSES)
    assert [c["realized_mult"] for c in CLOSES] == [
        1.838, 3.027, 0.1493, 0.024, 0.6525, 1.0716]


def test_summary_hand_computed_from_fixture():
    s = summarise_closes(CLOSES)
    assert s["n"] == 6
    assert s["mean_mult"] == pytest.approx(6.7624 / 6)
    assert s["expectancy"] == pytest.approx(6.7624 / 6 - 1)  # +0.12707
    assert s["wr_ge_1_5"] == pytest.approx(2 / 6)   # 1.838 and 3.027
    assert s["wr_gt_1"] == pytest.approx(3 / 6)     # plus 1.0716
    assert s["share_ge_1_0"] == pytest.approx(3 / 6)
    assert s["rug_rate"] == pytest.approx(1 / 6)    # the recorded rug flag
    assert s["n_negative_multiples"] == 0


def test_required_n_uses_the_toolkit_convention():
    s = summarise_closes(CLOSES)
    # Observed WR 1/3 > 0.30 null, so required_n applies directly.
    assert s["required_n"] == required_n(2 / 6, 0.30) == 1192


def test_required_n_is_none_when_wr_does_not_beat_the_null():
    losers = [dict(c, realized_mult=0.5) for c in CLOSES]
    s = summarise_closes(losers)
    assert s["wr_ge_1_5"] == 0.0
    assert s["required_n"] is None
    assert "does not exceed" in s["required_n_note"]


def test_empty_group_fails_closed_with_nones():
    s = summarise_closes([])
    assert s["n"] == 0 and s["expectancy"] is None and s["required_n"] is None


def test_negative_multiple_is_flagged_not_clamped(tmp_path):
    # A stacker-style ledger whose recorded pnl exceeds the stake:
    # realized = 1 + (-3.15)/3.0 = -0.05, kept as recorded.
    import json
    path = tmp_path / "stacker.json"
    path.write_text(json.dumps({"positions": [{
        "mint": "M", "entry_price": 1.0, "size_usd": 3.0, "peak_price": 1.0,
        "exit_reason": "time_stop_noprice", "realized_pnl": -3.15,
        "frac_remaining": 0.0, "exit_ts": 1.0}], "daily_pnl": {}, "bankroll": 0}))
    closes = load_stacker_closes(str(path))
    assert closes[0]["realized_mult"] == pytest.approx(-0.05)
    s = summarise_closes(closes)
    assert s["n_negative_multiples"] == 1
    assert s["expectancy"] == pytest.approx(-1.05)  # arithmetic, unclamped


def test_open_stacker_positions_are_not_closes(tmp_path):
    import json
    path = tmp_path / "stacker.json"
    path.write_text(json.dumps({"positions": [{
        "mint": "M", "entry_price": 1.0, "size_usd": 3.0, "peak_price": 1.0,
        "exit_reason": "", "realized_pnl": 0.0,
        "frac_remaining": 1.0, "exit_ts": 0}], "daily_pnl": {}, "bankroll": 0}))
    assert load_stacker_closes(str(path)) == []


def test_full_report_groups_by_lane_and_source():
    report = full_report(CLOSES)
    assert report["overall"]["n"] == 6
    assert set(report["per_lane"]) == {"paper"}
    assert report["per_source"]["(blank)"]["n"] == 6


def test_loader_skips_judge_records_and_bad_lines(tmp_path):
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        '{"outcome_kind": "judge", "realized_mult": 9.9}\n'
        "not json\n"
        '{"outcome_kind": "paper", "mint": "M", "realized_mult": 2.0,'
        ' "peak_mult": 2.0, "rug": false, "exit_reason": "tp"}\n')
    closes = list(iter_dataset_paper_closes(str(path)))
    assert len(closes) == 1 and closes[0]["realized_mult"] == 2.0
