"""Tests for risk.py — run: python3 tests/test_risk.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.config import TraderConfig
from advanced_trader.risk import RiskGovernor

CFG = TraderConfig(
    daily_loss_limit_lane=10.0,
    daily_loss_limit_global=20.0,
    max_open_positions=2,
    max_exposure_per_token=30.0,
    max_exposure_per_deployer=40.0,
    max_drawdown_pct=0.20,
)


def gov():
    return RiskGovernor(CFG, 1000.0)


def test_daily_loss_lane_halts_lane_only():
    g = gov()
    g.record_close(1, -12.0, "a", "D1", 988.0)
    ok, reason = g.allow_entry("a", "T1", "DEP1", 5.0, 988.0, "D1", 0)
    assert ok is False and reason == "daily_loss_lane"
    ok, _ = g.allow_entry("b", "T1", "DEP1", 5.0, 988.0, "D1", 0)
    assert ok is True  # other lanes unaffected
    # New day resets the limit.
    ok, _ = g.allow_entry("a", "T1", "DEP1", 5.0, 988.0, "D2", 0)
    assert ok is True
    print("PASS test_daily_loss_lane_halts_lane_only")


def test_daily_loss_global_halts_everything():
    g = gov()
    g.record_close(1, -12.0, "a", "D1", 988.0)
    g.record_close(2, -10.0, "b", "D1", 978.0)  # global -22 >= 20
    # Lanes "a" and "b" trip their own lane limits first (checked
    # before global); only an untouched lane reports the global halt.
    for lane, expected in (("a", "daily_loss_lane"), ("b", "daily_loss_lane")):
        ok, reason = g.allow_entry(lane, "T1", "DEP1", 5.0, 978.0, "D1", 0)
        assert ok is False and reason == expected, (lane, reason)
    ok, reason = g.allow_entry("c", "T1", "DEP1", 5.0, 978.0, "D1", 0)
    assert ok is False and reason == "daily_loss_global", reason
    ok, _ = g.allow_entry("a", "T1", "DEP1", 5.0, 978.0, "D2", 0)
    assert ok is True
    print("PASS test_daily_loss_global_halts_everything")


def test_max_positions_and_exposure():
    g = gov()
    ok, _ = g.allow_entry("a", "T1", "DEP1", 5.0, 1000.0, "D1", 2)
    assert ok is False  # max_open_positions = 2 already open
    g.record_open(1, "T1", "DEP1", 25.0)
    ok, reason = g.allow_entry("a", "T1", "DEP2", 10.0, 1000.0, "D1", 0)
    assert ok is False and reason == "token_exposure"  # 25+10 > 30
    g.record_open(2, "T2", "DEP1", 15.0)
    ok, reason = g.allow_entry("a", "T3", "DEP1", 5.0, 1000.0, "D1", 0)
    assert ok is False and reason == "deployer_exposure"  # 25+15+5 > 40
    # Closing releases exposure.
    g.record_close(1, 0.0, "a", "D1", 1000.0)
    ok, _ = g.allow_entry("a", "T1", "DEP2", 10.0, 1000.0, "D1", 0)
    assert ok is True
    print("PASS test_max_positions_and_exposure")


def test_drawdown_breaker_and_rebaseline():
    g = gov()
    g.record_close(1, -250.0, "a", "D1", 750.0)  # 25% drawdown from 1000
    assert g.breaker_tripped() is True
    ok, reason = g.allow_entry("a", "T1", "DEP1", 5.0, 750.0, "D1", 0)
    assert ok is False and reason == "drawdown_breaker"
    # Reset without re-baselining: still in drawdown, re-trips.
    # (Use a fresh day so the daily-loss limit doesn't answer first.)
    g.reset_breaker()
    ok, reason = g.allow_entry("a", "T1", "DEP1", 5.0, 750.0, "D2", 0)
    assert ok is False and reason == "drawdown_breaker"
    # Human re-enable with the new baseline: trading resumes.
    g.reset_breaker(current_equity=750.0)
    ok, _ = g.allow_entry("a", "T1", "DEP1", 5.0, 750.0, "D2", 0)
    assert ok is True
    print("PASS test_drawdown_breaker_and_rebaseline")


def test_lane_halt_and_human_reenable():
    g = gov()
    g.halt_lane("a", "negative_expectancy")
    assert g.is_lane_halted("a") is True
    ok, reason = g.allow_entry("a", "T1", "DEP1", 5.0, 1000.0, "D1", 0)
    assert ok is False and reason == "lane_halted"
    g.reenable_lane("a")
    ok, _ = g.allow_entry("a", "T1", "DEP1", 5.0, 1000.0, "D1", 0)
    assert ok is True
    print("PASS test_lane_halt_and_human_reenable")


if __name__ == "__main__":
    test_daily_loss_lane_halts_lane_only()
    test_daily_loss_global_halts_everything()
    test_max_positions_and_exposure()
    test_drawdown_breaker_and_rebaseline()
    test_lane_halt_and_human_reenable()
    print("test_risk.py: ALL PASS")
