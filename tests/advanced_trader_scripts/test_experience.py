"""Tests for experience.py — run: python3 tests/test_experience.py"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.config import TraderConfig
from advanced_trader.experience import ExperienceStore, hold_bucket


def store(tmp):
    return ExperienceStore(str(Path(tmp) / "experience.json"), TraderConfig())


def test_hold_buckets():
    assert hold_bucket(100) == "<1h"
    assert hold_bucket(3600) == "1-6h"
    assert hold_bucket(6 * 3600) == "6-24h"
    assert hold_bucket(25 * 3600) == ">24h"
    print("PASS test_hold_buckets")


def test_record_and_stats():
    with tempfile.TemporaryDirectory() as tmp:
        s = store(tmp)
        assert s.weight_for({"pattern": "grad_select"}) == 1.0  # no data: neutral
        s.record(
            {
                "pnl": 5.0,
                "lane": "grad_select",
                "source": "helius",
                "deployer": "DEP1",
                "pattern": "grad_select",
                "hold_seconds": 1800.0,
            }
        )
        stats = s.stats("pattern", "grad_select")
        assert stats is not None
        assert stats["n"] == 1 and stats["hit_rate"] == 1.0
        assert stats["expectancy"] == 5.0
        assert s.stats("hold_bucket", "1-6h") is None  # 1800s is <1h
        assert s.stats("hold_bucket", "<1h")["n"] == 1
    print("PASS test_record_and_stats")


def test_winning_pattern_earns_more_size():
    with tempfile.TemporaryDirectory() as tmp:
        s = store(tmp)
        for _ in range(5):
            s.record({"pnl": 5.0, "pattern": "grad_select"})
        assert s.weight_for({"pattern": "grad_select"}) == 1.25
        # A different pattern is unaffected.
        assert s.weight_for({"pattern": "momentum"}) == 1.0
    print("PASS test_winning_pattern_earns_more_size")


def test_losing_pattern_down_weighted_then_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        s = store(tmp)
        for _ in range(4):
            s.record({"pnl": -5.0, "pattern": "grad_raw"})
        # 4 losses: not enough data yet (min_trades=5) — still neutral.
        w = s.weight_for({"pattern": "grad_raw"})
        assert w == 1.0, w
        s.record({"pnl": -5.0, "pattern": "grad_raw"})  # 5th straight loss
        assert s.weight_for({"pattern": "grad_raw"}) == 0.0
        # A win breaks the streak.
        s.record({"pnl": 5.0, "pattern": "grad_raw"})
        assert s.weight_for({"pattern": "grad_raw"}) == 0.5  # still negative expectancy
    print("PASS test_losing_pattern_down_weighted_then_blocked")


def test_pessimistic_minimum_across_dimensions():
    with tempfile.TemporaryDirectory() as tmp:
        s = store(tmp)
        for _ in range(5):
            s.record({"pnl": 5.0, "pattern": "grad_select"})
        # DEP-RUG: 5 losses then 1 win -> streak broken, but expectancy
        # still negative with enough data, so it down-weights to 0.5.
        for _ in range(5):
            s.record({"pnl": -5.0, "deployer": "DEP-RUG"})
        s.record({"pnl": 5.0, "deployer": "DEP-RUG"})
        assert s.weight_for({"deployer": "DEP-RUG"}) == 0.5
        # Pattern is a winner (1.25) but the deployer is a loser (0.5):
        # the minimum wins.
        w = s.weight_for({"pattern": "grad_select", "deployer": "DEP-RUG"})
        assert w == 0.5, w
    print("PASS test_pessimistic_minimum_across_dimensions")


def test_persists_across_reload():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "experience.json")
        s1 = ExperienceStore(path, TraderConfig())
        for _ in range(5):
            s1.record({"pnl": -5.0, "pattern": "grad_raw"})
        s2 = ExperienceStore(path, TraderConfig())
        assert s2.weight_for({"pattern": "grad_raw"}) == 0.0
        assert s2.stats("pattern", "grad_raw")["n"] == 5
    print("PASS test_persists_across_reload")


def test_corrupt_state_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "experience.json"
        f.write_text("{broken")
        try:
            ExperienceStore(str(f), TraderConfig())
        except ValueError as exc:
            assert "corrupt" in str(exc)
        else:
            raise AssertionError("corrupt experience file must raise")
    print("PASS test_corrupt_state_fails_closed")


if __name__ == "__main__":
    test_hold_buckets()
    test_record_and_stats()
    test_winning_pattern_earns_more_size()
    test_losing_pattern_down_weighted_then_blocked()
    test_pessimistic_minimum_across_dimensions()
    test_persists_across_reload()
    test_corrupt_state_fails_closed()
    print("test_experience.py: ALL PASS")
