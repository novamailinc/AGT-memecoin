"""Tests for risk.py -- kill switch, cost gate, and bankroll."""

import pytest

from risk import Bankroll, KillSwitch, cost_gate


# ------------------------------------------------------------- kill switch

def test_halt_after_exactly_15_negative_closes():
    ks = KillSwitch()
    for _ in range(14):
        assert ks.record_close(0.5) is False
    assert ks.record_close(0.5) is True
    assert ks.is_halted() is True
    assert ks.expectancy() == pytest.approx(-0.5)


def test_no_halt_at_14_negative_closes():
    ks = KillSwitch()
    for _ in range(14):
        ks.record_close(0.0)
    assert ks.is_halted() is False


def test_no_halt_when_expectancy_positive():
    ks = KillSwitch()
    for _ in range(20):
        ks.record_close(1.5)
    assert ks.is_halted() is False
    assert ks.expectancy() == pytest.approx(0.5)


def test_no_halt_at_zero_expectancy():
    ks = KillSwitch()
    for _ in range(15):
        ks.record_close(1.0)
    assert ks.is_halted() is False


def test_rolling_window_uses_only_recent_closes():
    ks = KillSwitch(window=5, min_trades=5)
    for _ in range(5):
        ks.record_close(3.0)
    for _ in range(4):
        ks.record_close(0.5)
    assert ks.is_halted() is False  # last 5 still include one 3.0
    assert ks.record_close(0.5) is True  # window is now all 0.5


def test_rearm_requires_operator_and_reason():
    ks = KillSwitch()
    for _ in range(15):
        ks.record_close(0.5)
    assert ks.is_halted() is True
    with pytest.raises(ValueError):
        ks.rearm("", "reviewed the ledger")
    with pytest.raises(ValueError):
        ks.rearm("nova", "   ")
    with pytest.raises(ValueError):
        ks.rearm("", "")
    assert ks.is_halted() is True
    assert len([j for j in ks.journal if j["event"] == "rearm_attempt"]) == 3


def test_rearm_success_journals_and_unhalts():
    ks = KillSwitch()
    for _ in range(15):
        ks.record_close(0.5)
    assert ks.rearm("nova", "reviewed the ledger") is True
    assert ks.is_halted() is False
    attempt = ks.journal[-1]
    assert attempt["operator"] == "nova"
    assert attempt["success"] is True


def test_no_auto_rearm_method():
    ks = KillSwitch()
    assert not hasattr(ks, "auto_rearm")
    assert not hasattr(ks, "auto_re_arm")


def test_corrupt_state_file_loads_halted(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json at all", encoding="utf-8")
    ks = KillSwitch(state_path=path)
    assert ks.is_halted() is True


def test_missing_state_file_loads_halted(tmp_path):
    ks = KillSwitch(state_path=tmp_path / "does-not-exist.json")
    assert ks.is_halted() is True


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    first = KillSwitch(state_path=path)  # missing file: halted (fail-closed)
    first.rearm("nova", "first boot, state reviewed")
    for _ in range(15):
        first.record_close(0.5)
    second = KillSwitch(state_path=path)
    assert second.is_halted() is True
    assert len(second.closes) == 15


# ---------------------------------------------------------------- cost gate

def test_cost_gate_ethereum_blocked_at_400_percent():
    allowed, cost, pct = cost_gate("ethereum", 1.0)
    assert allowed is False
    assert cost == pytest.approx(4.00)
    assert pct == pytest.approx(400.0)


def test_cost_gate_solana_values():
    allowed, cost, pct = cost_gate("solana", 1.0)
    assert allowed is True
    assert cost == pytest.approx(0.004)
    assert pct == pytest.approx(0.4)


def test_cost_gate_base_passes():
    allowed, cost, pct = cost_gate("base", 1.0)
    assert allowed is True
    assert cost == pytest.approx(0.12)
    assert pct == pytest.approx(12.0)


def test_cost_gate_hyperliquid_passes():
    allowed, cost, pct = cost_gate("hyperliquid", 1.0)
    assert allowed is True
    assert cost == pytest.approx(0.0014)
    assert pct == pytest.approx(0.14)


def test_cost_gate_scales_with_notional_for_percentage_chains():
    allowed, cost, pct = cost_gate("hyperliquid", 100.0)
    assert allowed is True
    assert cost == pytest.approx(0.14)
    assert pct == pytest.approx(0.14)


def test_cost_gate_fixed_chain_blocked_even_at_larger_notional_check():
    # Fixed $1/tx on Ethereum: at $10 notional cost is still $4 = 40%.
    allowed, cost, pct = cost_gate("ethereum", 10.0)
    assert allowed is False
    assert cost == pytest.approx(4.00)
    assert pct == pytest.approx(40.0)


def test_cost_gate_unknown_chain_fails_closed():
    with pytest.raises(ValueError):
        cost_gate("some-new-chain", 1.0)


# ----------------------------------------------------------------- bankroll

def test_bankroll_open_and_settle_math():
    bank = Bankroll()
    assert bank.cash == 100.00 and bank.state == "active"
    bank.open_position()
    assert bank.cash == 99.00 and bank.open_count == 1
    credited = bank.settle(2.0)
    assert credited == 2.00
    assert bank.cash == 101.00 and bank.open_count == 0


def test_bankroll_total_loss_settle():
    bank = Bankroll()
    bank.open_position()
    assert bank.settle(0.0) == 0.0
    assert bank.cash == 99.00


def test_bankroll_max_open_enforced():
    bank = Bankroll()
    for _ in range(Bankroll.MAX_OPEN):
        bank.open_position()
    with pytest.raises(RuntimeError):
        bank.open_position()
    assert bank.open_count == 10


def test_bankroll_settle_without_open_raises():
    with pytest.raises(RuntimeError):
        Bankroll().settle(2.0)


def test_bankroll_exhaustion_flips_state():
    bank = Bankroll()
    for _ in range(99):
        bank.open_position()
        bank.settle(0.0)
    assert bank.cash == 1.00 and bank.state == "active"
    bank.open_position()
    bank.settle(0.0)
    assert bank.cash == 0.00
    assert bank.state == "bankroll_exhausted"
    with pytest.raises(RuntimeError):
        bank.open_position()


def test_bankroll_exhaustion_is_permanent():
    bank = Bankroll(start=2.00)
    bank.open_position()
    bank.open_position()  # cash 0.00, two shots still open
    assert bank.state == "bankroll_exhausted"
    bank.settle(5.0)  # a winner lands afterwards...
    assert bank.cash == 5.00
    assert bank.state == "bankroll_exhausted"  # ...state never reverses


def test_bankroll_has_no_top_up_or_reset():
    bank = Bankroll()
    assert hasattr(bank, "top_up") is False
    assert hasattr(bank, "reset") is False
    assert not hasattr(Bankroll, "top_up")
    assert not hasattr(Bankroll, "reset")

# ------------------------------------------------------- kill switch extras

def test_expectancy_is_none_before_any_close():
    ks = KillSwitch()
    assert ks.expectancy() is None
    assert ks.recent_closes() == []
    assert ks.loss_streak() == 0


def test_expectancy_ignores_closes_outside_window():
    ks = KillSwitch(window=3, min_trades=3)
    for value in (9.0, 9.0, 0.0, 0.0, 0.0):
        ks.record_close(value)
    # Only the last three closes (all total losses) are in the window.
    assert ks.recent_closes() == [0.0, 0.0, 0.0]
    assert ks.expectancy() == pytest.approx(-1.0)
    assert ks.is_halted() is True



def test_halt_is_sticky_until_a_human_rearms():
    ks = KillSwitch()
    for _ in range(15):
        ks.record_close(0.5)
    assert ks.is_halted() is True
    # One huge winner lifts the window mean above zero, but a halted
    # switch never clears itself -- only rearm() can do that.
    ks.record_close(50.0)
    assert ks.expectancy() > 0
    assert ks.is_halted() is True


def test_rearmed_switch_rehalts_on_next_negative_close():
    ks = KillSwitch()
    for _ in range(15):
        ks.record_close(0.5)
    ks.rearm("nova", "probation after review")
    assert ks.is_halted() is False
    # Closes were preserved by rearm, so the window is still negative.
    assert ks.record_close(0.5) is True



def test_invalid_window_or_min_trades_raise():
    with pytest.raises(ValueError):
        KillSwitch(window=0)
    with pytest.raises(ValueError):
        KillSwitch(min_trades=0)
    with pytest.raises(ValueError):
        KillSwitch(window=-3)


def test_record_close_rejects_negative_multiple():
    ks = KillSwitch()
    with pytest.raises(ValueError):
        ks.record_close(-0.1)
    assert ks.closes == []


def test_loss_streak_counts_only_the_trailing_run():
    ks = KillSwitch(min_trades=100)  # keep the halt out of this test
    for value in (0.5, 0.5, 2.0, 0.0, 0.9, 0.1):
        ks.record_close(value)
    assert ks.loss_streak() == 3
    ks.record_close(1.0)  # exactly breakeven breaks the streak
    assert ks.loss_streak() == 0


def test_would_halt_if_is_predictive_and_non_mutating():
    ks = KillSwitch()
    for _ in range(14):
        ks.record_close(0.5)
    assert ks.would_halt_if(0.5) is True   # the 15th loser would trip it
    assert ks.would_halt_if(30.0) is False  # a huge winner would not
    assert len(ks.closes) == 14            # nothing was recorded
    assert ks.is_halted() is False


def test_would_halt_if_below_min_trades_and_when_already_halted():
    ks = KillSwitch()
    ks.record_close(0.0)
    assert ks.would_halt_if(0.0) is False  # only 2 closes, below minimum
    halted = KillSwitch()
    for _ in range(15):
        halted.record_close(0.5)
    assert halted.would_halt_if(99.0) is True  # already halted stays halted


def test_corrupt_state_journals_the_load_failure(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, bad shape
    ks = KillSwitch(state_path=path)
    assert ks.is_halted() is True
    assert ks.journal[0]["event"] == "load_failed"



def test_cost_gate_at_exactly_25_percent_is_allowed():
    # The block is strict (> 25%): Ethereum at $16 costs $4 = exactly 25%.
    allowed, cost, pct = cost_gate("ethereum", 16.0)
    assert allowed is True
    assert cost == pytest.approx(4.00)
    assert pct == pytest.approx(25.0)
    # One cent below that notional the same $4 cost tips over the cap.
    assert cost_gate("ethereum", 15.99)[0] is False



def test_per_tx_cost_normalises_chain_name():
    from risk import per_tx_cost
    assert per_tx_cost(" Ethereum ") == pytest.approx(1.00)
    assert per_tx_cost("SOLANA") == pytest.approx(0.001)



def test_min_notional_to_pass_values():
    from risk import min_notional_to_pass
    assert min_notional_to_pass("ethereum") == pytest.approx(16.0)
    assert min_notional_to_pass("base") == pytest.approx(0.48)
    assert min_notional_to_pass("solana") == pytest.approx(0.016)
    assert min_notional_to_pass("hyperliquid") == 0.0
    with pytest.raises(ValueError):
        min_notional_to_pass("no-such-chain")


def test_min_notional_boundary_actually_passes_the_gate():
    from risk import min_notional_to_pass
    for chain in ("ethereum", "base", "solana"):
        threshold = min_notional_to_pass(chain)
        assert cost_gate(chain, threshold)[0] is True
        assert cost_gate(chain, threshold / 2)[0] is False


def test_chains_passing_at_one_dollar_and_at_sixteen():
    from risk import chains_passing
    assert chains_passing(1.0) == ["base", "hyperliquid", "solana"]
    assert "ethereum" in chains_passing(16.0)
    with pytest.raises(ValueError):
        chains_passing(0)


# ------------------------------------------------------------- bankroll extras

def test_bankroll_can_open_predicate_tracks_all_three_blocks():
    bank = Bankroll(start=1.00)
    assert bank.can_open() is True
    bank.open_position()               # cash hits 0 -> exhausted
    assert bank.can_open() is False
    full = Bankroll()
    for _ in range(Bankroll.MAX_OPEN):
        full.open_position()
    assert full.cash == 90.00          # cash is fine; the cap is the block
    assert full.can_open() is False


def test_bankroll_starting_below_one_shot_is_born_exhausted():
    bank = Bankroll(start=0.50)
    assert bank.state == "bankroll_exhausted"
    assert bank.shots_affordable() == 0
    with pytest.raises(RuntimeError):
        bank.open_position()


def test_bankroll_shots_affordable_floors_partial_shots():
    bank = Bankroll(start=7.99)
    assert bank.shots_affordable() == 7
    bank.open_position()
    assert bank.shots_affordable() == 6


def test_bankroll_summary_and_net_pnl_account_for_open_stakes():
    bank = Bankroll()
    bank.open_position()
    bank.open_position()
    credited = bank.settle(3.0)
    assert credited == 3.0
    summary = bank.summary()
    assert summary["cash"] == pytest.approx(101.00)
    assert summary["open"] == 1
    assert summary["total_debited"] == pytest.approx(2.00)
    assert summary["total_credited"] == pytest.approx(3.00)
    assert summary["positions_settled"] == 1
    # +$2 of profit is locked in cash; the still-open $1 stake is not P/L.
    assert summary["net_pnl"] == pytest.approx(2.00)
    assert summary["state"] == "active"


def test_bankroll_settle_rejects_negative_multiple_without_closing():
    bank = Bankroll()
    bank.open_position()
    with pytest.raises(ValueError):
        bank.settle(-1.0)
    assert bank.open_count == 1
    assert bank.cash == 99.00
