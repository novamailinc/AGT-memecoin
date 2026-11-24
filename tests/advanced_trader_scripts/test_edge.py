"""Tests for edge.py — run: python3 tests/test_edge.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.config import TraderConfig
from advanced_trader.edge import EdgeValidator


def test_expectancy_and_win_rate():
    v = EdgeValidator(TraderConfig())
    assert v.expectancy("x") == 0.0
    assert v.win_rate("x") == 0.0
    assert v.is_tradable("x") is False  # no data at all
    for pnl in [6.0, 6.0, -4.0, 6.0, -4.0]:
        v.record_close("x", pnl)
    assert abs(v.expectancy("x") - 2.0) < 1e-9  # (18-8)/5
    assert abs(v.win_rate("x") - 0.6) < 1e-9
    assert abs(v.avg_win("x") - 6.0) < 1e-9
    assert abs(v.avg_loss("x") - 4.0) < 1e-9
    assert abs(v.profit_factor("x") - 18.0 / 8.0) < 1e-9
    print("PASS test_expectancy_and_win_rate")


def test_tradable_needs_enough_trades():
    v = EdgeValidator(TraderConfig(edge_min_trades=30))
    for _ in range(29):
        v.record_close("thin", 5.0)
    assert v.is_tradable("thin") is False
    v.record_close("thin", 5.0)
    assert v.is_tradable("thin") is True
    print("PASS test_tradable_needs_enough_trades")


def test_tradable_needs_positive_expectancy():
    v = EdgeValidator(TraderConfig())
    for _ in range(30):
        v.record_close("bad", -1.0)
    assert v.expectancy("bad") == -1.0
    assert v.is_tradable("bad") is False
    print("PASS test_tradable_needs_positive_expectancy")


def test_walk_forward_decay_blocks_trust():
    # Train half prints money, test half loses it: edge decayed.
    # Overall expectancy is still positive — trust must still fail.
    v = EdgeValidator(TraderConfig())
    for _ in range(42):
        v.record_close("decayed", 5.0)
    for _ in range(18):
        v.record_close("decayed", -5.0)
    assert v.expectancy("decayed") > 0  # (210-90)/60 = +2.0
    assert v.is_tradable("decayed") is False
    stats = v.lane_stats("decayed")
    assert stats["train_expectancy"] > 0
    assert stats["test_expectancy"] < 0
    print("PASS test_walk_forward_decay_blocks_trust")


def test_report_covers_all_lanes():
    v = EdgeValidator(TraderConfig())
    for _ in range(30):
        v.record_close("a", 2.0)
        v.record_close("b", -2.0)
    report = v.report()
    assert set(report) == {"a", "b"}
    assert report["a"]["tradable"] is True
    assert report["b"]["tradable"] is False
    print("PASS test_report_covers_all_lanes")


if __name__ == "__main__":
    test_expectancy_and_win_rate()
    test_tradable_needs_enough_trades()
    test_tradable_needs_positive_expectancy()
    test_walk_forward_decay_blocks_trust()
    test_report_covers_all_lanes()
    print("test_edge.py: ALL PASS")
