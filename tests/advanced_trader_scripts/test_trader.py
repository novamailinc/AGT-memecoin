"""Tests for trader.py — run: python3 tests/test_trader.py"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.config import TraderConfig
from advanced_trader.trader import AdvancedTrader


def make_trader(tmp, **overrides):
    cfg = TraderConfig(**overrides)
    return AdvancedTrader(
        config=cfg,
        ticket_state_file=str(Path(tmp) / "tickets.json"),
        experience_state_file=str(Path(tmp) / "experience.json"),
    )


def preload(trader, lane, wins, losses, win_pnl=6.0, loss_pnl=4.0):
    """Evenly interleaved history (W,L,W,L...) so both walk-forward
    halves see the same mix. A shuffled history can put all the losses
    in the test half and fail the trust check by construction."""
    pnls = []
    w, l = wins, losses
    while w or l:
        if w:
            pnls.append(win_pnl)
            w -= 1
        if l:
            pnls.append(-loss_pnl)
            l -= 1
    for pnl in pnls:
        trader.edge.record_close(lane, pnl)


def candidate(lane="good", token="TOK1", **extra):
    base = {
        "lane": lane,
        "token": token,
        "deployer": "DEP1",
        "quote": 0.001,
        "corroboration": 2,
        "evidence_score": 0.9,
    }
    base.update(extra)
    return base


def test_evaluate_enters_on_edge():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp)
        preload(t, "good", 33, 27)
        d = t.evaluate(candidate(), 1000.0, "D1")
        assert d["decision"] == "enter", d
        assert 0 < d["stake"] <= 25.0
        assert d["experience_weight"] == 1.0  # no experience yet
    print("PASS test_evaluate_enters_on_edge")


def test_evaluate_rejects_without_edge():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp)
        preload(t, "bad", 21, 39, win_pnl=4.0, loss_pnl=7.0)
        d = t.evaluate(candidate(lane="bad"), 1000.0, "D1")
        assert d["decision"] == "reject" and d["reason"] == "no_edge"
        assert t.rejected.get("no_edge", 0) == 1
    print("PASS test_evaluate_rejects_without_edge")


def test_evaluate_is_deterministic():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp)
        preload(t, "good", 33, 27)
        d1 = t.evaluate(candidate(), 1000.0, "D1")
        d2 = t.evaluate(candidate(), 1000.0, "D1")
        assert d1 == d2
    print("PASS test_evaluate_is_deterministic")


def test_evidence_bar_corroboration():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp)
        preload(t, "good", 33, 27)
        weak_lone = t.evaluate(candidate(corroboration=1, evidence_score=0.6), 1000.0, "D1")
        assert weak_lone["decision"] == "reject"
        assert weak_lone["reason"] == "weak_evidence"
        weak_corr = t.evaluate(candidate(corroboration=2, evidence_score=0.6), 1000.0, "D1")
        assert weak_corr["decision"] == "enter"
    print("PASS test_evidence_bar_corroboration")


def test_enter_allocates_unique_tickets():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp)
        preload(t, "good", 33, 27)
        p1 = t.enter(t.evaluate(candidate(token="T1"), 1000.0, "D1"), "D1", 0.0, 1000.0)
        p2 = t.enter(t.evaluate(candidate(token="T2"), 1000.0, "D1"), "D1", 0.0, 1000.0)
        assert p1 is not None and p2 is not None
        assert p1["ticket"] == 1 and p2["ticket"] == 2
        assert p1["ticket"] != p2["ticket"]
        assert len(t.open_positions()) == 2
    print("PASS test_enter_allocates_unique_tickets")


def test_manage_routes_bad_quote_to_dead_path():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp)
        preload(t, "good", 33, 27)
        pos = t.enter(t.evaluate(candidate(), 1000.0, "D1"), "D1", 0.0, 1000.0)
        actions = t.manage(pos, 5.0, 3600.0)  # 5000x spike
        assert actions == [], actions
        assert pos["closed"] is False
        assert t.guard.rejected_count == 1
        # Dead quote past the time stop still writes off.
        actions = t.manage(pos, None, 25 * 3600.0)
        assert len(actions) == 1 and actions[0]["action"] == "write_off"
    print("PASS test_manage_routes_bad_quote_to_dead_path")


def test_kill_switch_halts_negative_lane():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp)
        preload(t, "swing", 16, 14, win_pnl=5.0, loss_pnl=4.0)  # exp +0.8
        assert t.edge.is_tradable("swing") is True
        equity = 1000.0
        for i in range(2):
            equity -= 20.0
            t.on_close(
                {
                    "ticket": 100 + i,
                    "lane": "swing",
                    "token": "TOK-S",
                    "deployer": "DEP1",
                    "pnl": -20.0,
                    "day": "D1",
                    "equity_after": equity,
                    "reason": "hard_stop",
                }
            )
        assert t.edge.expectancy("swing") < 0
        assert t.risk.is_lane_halted("swing") is True
        d = t.evaluate(candidate(lane="swing"), equity, "D1")
        assert d["decision"] == "reject" and d["reason"] == "lane_halted"
    print("PASS test_kill_switch_halts_negative_lane")


def test_experience_blocks_losing_pattern_through_trader():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp)
        preload(t, "good", 33, 27)
        cand = candidate(pattern="grad_select", source="helius", deployer="DEP-X")
        assert t.evaluate(cand, 1000.0, "D1")["decision"] == "enter"
        for _ in range(5):
            t.experience.record(
                {
                    "pnl": -5.0,
                    "lane": "good",
                    "source": "helius",
                    "deployer": "DEP-X",
                    "pattern": "grad_select",
                    "hold_seconds": 3600.0,
                }
            )
        d = t.evaluate(cand, 1000.0, "D1")
        assert d["decision"] == "reject" and d["reason"] == "experience_blocked"
    print("PASS test_experience_blocks_losing_pattern_through_trader")


def test_risk_governor_cannot_be_bypassed():
    with tempfile.TemporaryDirectory() as tmp:
        t = make_trader(tmp, daily_loss_limit_lane=10.0)
        preload(t, "good", 33, 27)
        # Blow the lane's daily loss limit through closes.
        t.on_close(
            {
                "ticket": 1,
                "lane": "good",
                "token": "T",
                "deployer": "DEP1",
                "pnl": -12.0,
                "day": "D1",
                "equity_after": 988.0,
            }
        )
        d = t.evaluate(candidate(), 988.0, "D1")
        assert d["decision"] == "reject" and d["reason"] == "daily_loss_lane"
    print("PASS test_risk_governor_cannot_be_bypassed")


if __name__ == "__main__":
    test_evaluate_enters_on_edge()
    test_evaluate_rejects_without_edge()
    test_evaluate_is_deterministic()
    test_evidence_bar_corroboration()
    test_enter_allocates_unique_tickets()
    test_manage_routes_bad_quote_to_dead_path()
    test_kill_switch_halts_negative_lane()
    test_experience_blocks_losing_pattern_through_trader()
    test_risk_governor_cannot_be_bypassed()
    print("test_trader.py: ALL PASS")
