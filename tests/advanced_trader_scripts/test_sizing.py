"""Tests for sizing.py — run: python3 tests/test_sizing.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.config import TraderConfig
from advanced_trader.sizing import KellySizer


def test_kelly_math():
    # p=0.6, b=2 -> f=(0.6*3-1)/2 = 0.4
    assert abs(KellySizer.kelly_fraction(0.6, 10.0, 5.0) - 0.4) < 1e-9
    # p=0.4, b=2 -> f=(0.4*3-1)/2 = 0.1: a real (small) edge.
    assert abs(KellySizer.kelly_fraction(0.4, 10.0, 5.0) - 0.1) < 1e-9
    # No edge -> 0 (needs p > 1/(b+1) = 1/3 here).
    assert KellySizer.kelly_fraction(0.3, 10.0, 5.0) == 0.0
    # No wins or no losses recorded -> 0.
    assert KellySizer.kelly_fraction(0.6, 0.0, 5.0) == 0.0
    assert KellySizer.kelly_fraction(0.6, 10.0, 0.0) == 0.0
    print("PASS test_kelly_math")


def test_quarter_kelly_and_caps():
    cfg = TraderConfig(
        kelly_fraction=0.25, max_stake=1000.0, max_bankroll_pct_per_trade=1.0
    )
    s = KellySizer(cfg)
    # f=0.4, bankroll 1000 -> 1000*0.4*0.25 = 100
    stake, halted = s.stake_for("a", 0.6, 10.0, 5.0, 1000.0)
    assert halted is False
    assert abs(stake - 100.0) < 1e-9
    # Default caps: max_stake 25 and 2.5% of bankroll bind first.
    s2 = KellySizer(TraderConfig())
    stake2, _ = s2.stake_for("a", 0.6, 10.0, 5.0, 1000.0)
    assert stake2 == 25.0
    # Small bankroll: the % cap binds (2.5% of 100 = 2.5).
    stake3, _ = s2.stake_for("a", 0.6, 10.0, 5.0, 100.0)
    assert abs(stake3 - 2.5) < 1e-9
    print("PASS test_quarter_kelly_and_caps")


def test_losing_streak_throttle():
    cfg = TraderConfig(
        kelly_fraction=0.25, max_stake=1000.0, max_bankroll_pct_per_trade=1.0
    )
    s = KellySizer(cfg)
    base, _ = s.stake_for("a", 0.6, 10.0, 5.0, 1000.0)
    for _ in range(3):
        s.on_loss("a")
    halved, halted = s.stake_for("a", 0.6, 10.0, 5.0, 1000.0)
    assert halted is False
    assert abs(halved - base * 0.5) < 1e-9
    s.on_win("a")
    assert s.streak("a") == 0
    again, _ = s.stake_for("a", 0.6, 10.0, 5.0, 1000.0)
    assert abs(again - base) < 1e-9
    print("PASS test_losing_streak_throttle")


def test_streak_halt_and_human_reenable():
    s = KellySizer(TraderConfig())
    for _ in range(5):
        s.on_loss("a")
    stake, halted = s.stake_for("a", 0.6, 10.0, 5.0, 1000.0)
    assert halted is True
    assert stake == 0.0
    s.reset_streak("a")
    stake, halted = s.stake_for("a", 0.6, 10.0, 5.0, 1000.0)
    assert halted is False and stake > 0
    print("PASS test_streak_halt_and_human_reenable")


if __name__ == "__main__":
    test_kelly_math()
    test_quarter_kelly_and_caps()
    test_losing_streak_throttle()
    test_streak_halt_and_human_reenable()
    print("test_sizing.py: ALL PASS")
