"""Tests for exits.py — run: python3 tests/test_exits.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.config import TraderConfig
from advanced_trader.exits import ExitManager, new_position


def pos(entry=0.001, ts=0.0, volume=25.0):
    return new_position(1, "TOK", entry, ts, volume, lane="good")


def test_leg_a_and_b_fire_at_multiples():
    m = ExitManager(TraderConfig())
    p = pos()
    actions = m.manage(p, 0.0015, 3600.0)  # exactly 1.5x
    assert len(actions) == 1 and actions[0]["reason"] == "leg_a"
    assert actions[0]["fraction"] == 0.25
    assert p["breakeven_armed"] is True
    actions = m.manage(p, 0.003, 7200.0)  # 3x
    assert len(actions) == 1 and actions[0]["reason"] == "leg_b"
    assert p["remaining"] == 0.5
    print("PASS test_leg_a_and_b_fire_at_multiples")


def test_both_legs_fire_on_one_big_tick():
    m = ExitManager(TraderConfig())
    p = pos()
    actions = m.manage(p, 0.0035, 3600.0)  # jumps past both legs
    reasons = [a["reason"] for a in actions]
    assert reasons == ["leg_a", "leg_b"], reasons
    print("PASS test_both_legs_fire_on_one_big_tick")


def test_hard_stop_unconditional():
    m = ExitManager(TraderConfig())
    p = pos()
    actions = m.manage(p, 0.0005, 3600.0)  # exactly -50%
    assert len(actions) == 1
    assert actions[0]["action"] == "close_all"
    assert actions[0]["reason"] == "hard_stop"
    assert p["closed"] is True
    # Even mid-crash below the stop, and even after legs banked.
    p2 = pos()
    m.manage(p2, 0.0015, 3600.0)  # leg A banks
    actions = m.manage(p2, 0.0001, 7200.0)  # -90% flash crash
    assert actions[0]["reason"] == "hard_stop"
    print("PASS test_hard_stop_unconditional")


def test_breakeven_stop_after_leg_a():
    m = ExitManager(TraderConfig())
    p = pos()
    m.manage(p, 0.0015, 3600.0)  # leg A banks, breakeven arms
    actions = m.manage(p, 0.0009, 7200.0)  # falls back below entry
    assert len(actions) == 1
    assert actions[0]["reason"] == "breakeven_stop"
    assert p["closed"] is True
    print("PASS test_breakeven_stop_after_leg_a")


def test_time_stop_closes_everything():
    m = ExitManager(TraderConfig())
    p = pos()
    actions = m.manage(p, 0.0012, 25 * 3600.0)  # 25h, below leg A
    assert len(actions) == 1
    assert actions[0]["reason"] == "time_stop"
    assert p["closed"] is True
    print("PASS test_time_stop_closes_everything")


def test_trailing_stop_and_dip_buyback():
    m = ExitManager(TraderConfig())
    p = pos()
    m.manage(p, 0.0016, 3600.0)  # leg A banks, peak 0.0016
    # 31% retrace from peak, still above entry, age < 24h.
    actions = m.manage(p, 0.0011, 7200.0)
    reasons = [a["reason"] for a in actions]
    assert reasons == ["trailing_stop", "dip_buyback"], reasons
    assert actions[0]["fraction"] == 0.75  # remainder closes
    assert actions[1]["fraction"] == 0.25  # buyback re-entry size
    assert p["buyback_used"] is True
    # Only one buyback per position.
    p2 = pos()
    m.manage(p2, 0.0016, 3600.0)
    m.manage(p2, 0.0011, 7200.0)
    assert p2["buyback_used"] is True and p2["closed"] is True
    print("PASS test_trailing_stop_and_dip_buyback")


def test_no_trailing_before_leg_a():
    # A 30% drop before any leg banks is the hard stop's job, not the trail.
    m = ExitManager(TraderConfig())
    p = pos()
    actions = m.manage(p, 0.0007, 3600.0)  # -30%, no legs banked
    assert actions == []
    assert p["closed"] is False
    print("PASS test_no_trailing_before_leg_a")


def test_quote_dead_write_off_at_zero():
    m = ExitManager(TraderConfig())
    p = pos()
    assert m.manage(p, None, 23 * 3600.0) == []  # hold before time stop
    assert p["closed"] is False
    actions = m.manage(p, None, 25 * 3600.0)  # past time stop, feed dead
    assert len(actions) == 1
    assert actions[0]["action"] == "write_off"
    assert actions[0]["reason"] == "quote_dead_time_stop"
    assert actions[0]["close_price"] == 0.0
    assert p["closed"] is True
    # A zero quote is a dead quote too.
    p2 = pos()
    actions = m.manage(p2, 0.0, 25 * 3600.0)
    assert actions[0]["action"] == "write_off"
    print("PASS test_quote_dead_write_off_at_zero")


def test_closed_position_stays_closed():
    m = ExitManager(TraderConfig())
    p = pos()
    m.manage(p, 0.0005, 3600.0)
    assert m.manage(p, 0.01, 7200.0) == []
    print("PASS test_closed_position_stays_closed")


if __name__ == "__main__":
    test_leg_a_and_b_fire_at_multiples()
    test_both_legs_fire_on_one_big_tick()
    test_hard_stop_unconditional()
    test_breakeven_stop_after_leg_a()
    test_time_stop_closes_everything()
    test_trailing_stop_and_dip_buyback()
    test_no_trailing_before_leg_a()
    test_quote_dead_write_off_at_zero()
    test_closed_position_stays_closed()
    print("test_exits.py: ALL PASS")
