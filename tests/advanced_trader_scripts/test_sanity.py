"""Tests for sanity.py — run: python3 tests/test_sanity.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.config import TraderConfig
from advanced_trader.sanity import PriceGuard


def test_accepts_sane_quotes():
    g = PriceGuard(TraderConfig())
    assert g.check(0.001, 0.001) == (True, "ok")
    assert g.check(0.05, 0.001) == (True, "ok")  # 50x is wild but allowed
    assert g.check(0.1, 0.001) == (True, "ok")  # exactly 100x: allowed
    assert g.rejected_count == 0
    print("PASS test_accepts_sane_quotes")


def test_rejects_5000x_spike():
    g = PriceGuard(TraderConfig())
    ok, reason = g.check(5.0, 0.001)  # 5000x entry
    assert ok is False and reason == "quote_spike"
    assert g.rejected_count == 1
    rej = g.rejections()[0]
    assert rej["reason"] == "quote_spike"
    assert rej["multiple"] == 5000.0
    print("PASS test_rejects_5000x_spike")


def test_rejects_nonpositive_and_missing():
    g = PriceGuard(TraderConfig())
    assert g.check(0.0, 0.001) == (False, "nonpositive_quote")
    assert g.check(-1.0, 0.001) == (False, "nonpositive_quote")
    assert g.check(None, 0.001) == (False, "no_quote")
    assert g.check(0.001, 0.0) == (False, "bad_entry_price")
    assert g.rejected_count == 4
    print("PASS test_rejects_nonpositive_and_missing")


def test_custom_multiplier():
    g = PriceGuard(TraderConfig(max_quote_multiple=10.0))
    assert g.check(0.011, 0.001) == (False, "quote_spike")  # 11x > 10x
    assert g.check(0.009, 0.001) == (True, "ok")
    print("PASS test_custom_multiplier")


if __name__ == "__main__":
    test_accepts_sane_quotes()
    test_rejects_5000x_spike()
    test_rejects_nonpositive_and_missing()
    test_custom_multiplier()
    print("test_sanity.py: ALL PASS")
