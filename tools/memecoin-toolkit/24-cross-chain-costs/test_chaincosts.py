"""Tests for chaincosts.py -- every pinned number hand-checkable."""
import pytest

from chaincosts import (CHAINS, FLAT_COST_USD, GATE_LIMIT_PCT,
                        PCT_COST_RATE, ROUND_TRIP_TXS, cost_gate,
                        cost_table, min_viable_notional, render_cost_table,
                        round_trip_cost_pct, round_trip_cost_usd,
                        tx_cost_usd)


# -- model constants ------------------------------------------------------------

def test_flat_fee_constants():
    assert FLAT_COST_USD == {"solana": 0.001, "base": 0.03, "ethereum": 1.00}


def test_pct_fee_constant():
    assert PCT_COST_RATE["hyperliquid"] == pytest.approx(0.00035)
    assert CHAINS == ("solana", "base", "ethereum", "hyperliquid")


def test_round_trip_is_four_txs_by_default():
    assert ROUND_TRIP_TXS == 4
    assert GATE_LIMIT_PCT == 25.0


# -- per-tx costs -----------------------------------------------------------------

def test_tx_cost_flat_chains_ignore_notional():
    assert tx_cost_usd("solana", 1.0) == pytest.approx(0.001)
    assert tx_cost_usd("solana", 10_000.0) == pytest.approx(0.001)
    assert tx_cost_usd("base", 1.0) == pytest.approx(0.03)
    assert tx_cost_usd("ethereum", 5.0) == pytest.approx(1.00)


def test_tx_cost_hyperliquid_scales_with_notional():
    assert tx_cost_usd("hyperliquid", 100.0) == pytest.approx(0.035)
    assert tx_cost_usd("hyperliquid", 1000.0) == pytest.approx(0.35)
    assert tx_cost_usd("hyperliquid", 1.0) == pytest.approx(0.00035)


# -- round trips ---------------------------------------------------------------------

def test_round_trip_flat_chains():
    assert round_trip_cost_usd("solana", 1.0) == pytest.approx(0.004)
    assert round_trip_cost_usd("base", 1.0) == pytest.approx(0.12)
    assert round_trip_cost_usd("ethereum", 1.0) == pytest.approx(4.00)


def test_round_trip_custom_tx_count():
    assert round_trip_cost_usd("ethereum", 1.0, txs=1) == pytest.approx(1.0)
    assert round_trip_cost_usd("ethereum", 1.0, txs=2) == pytest.approx(2.0)
    assert round_trip_cost_usd("base", 1.0, txs=10) == pytest.approx(0.30)


def test_round_trip_pct_flat_decreases_with_notional():
    assert round_trip_cost_pct("base", 1.0) == pytest.approx(12.0)
    assert round_trip_cost_pct("base", 10.0) == pytest.approx(1.2)
    assert round_trip_cost_pct("base", 100.0) < round_trip_cost_pct("base", 10.0)


def test_hl_pct_is_constant_across_notionals():
    pcts = [round_trip_cost_pct("hyperliquid", n)
            for n in (1.0, 5.0, 25.0, 1000.0, 1_000_000.0)]
    assert all(p == pytest.approx(0.14) for p in pcts)
    assert len(set(round(p, 10) for p in pcts)) == 1


# -- pinned gate results -----------------------------------------------------------------

def test_pinned_ethereum_one_dollar_blocked_at_400pct():
    result = cost_gate(1.0, "ethereum")
    assert result.allowed is False
    assert result.cost_usd == pytest.approx(4.00)
    assert result.cost_pct == pytest.approx(400.0)
    assert "blocked" in result.reason


def test_pinned_ethereum_25_dollars_allowed_at_16pct():
    result = cost_gate(25.0, "ethereum")
    assert result.allowed is True
    assert result.cost_usd == pytest.approx(4.00)
    assert result.cost_pct == pytest.approx(16.0)


def test_pinned_solana_and_base_pass_at_one_dollar():
    sol = cost_gate(1.0, "solana")
    assert sol.allowed is True and sol.cost_pct == pytest.approx(0.4)
    base = cost_gate(1.0, "base")
    assert base.allowed is True and base.cost_pct == pytest.approx(12.0)


def test_pinned_hyperliquid_passes_at_one_dollar():
    result = cost_gate(1.0, "hyperliquid")
    assert result.allowed is True
    assert result.cost_pct == pytest.approx(0.14)
    assert result.cost_usd == pytest.approx(0.0014)


def test_gate_boundary_exactly_25pct_is_allowed():
    # Ethereum: $4 round trip on a $16 notional is exactly 25%.
    result = cost_gate(16.0, "ethereum")
    assert result.cost_pct == pytest.approx(25.0)
    assert result.allowed is True


def test_gate_just_above_boundary_is_blocked():
    result = cost_gate(15.99, "ethereum")
    assert result.cost_pct > 25.0
    assert result.allowed is False


def test_gate_result_unpacks_as_four_tuple():
    allowed, cost, pct, reason = cost_gate(1.0, "ethereum")
    assert allowed is False and cost == pytest.approx(4.0)
    assert pct == pytest.approx(400.0) and isinstance(reason, str)


def test_gate_reason_names_chain_and_limit():
    result = cost_gate(1.0, "ethereum")
    assert "ethereum" in result.reason and "25%" in result.reason
    ok = cost_gate(25.0, "ethereum")
    assert "allowed" in ok.reason


def test_gate_scales_block_threshold_with_notional():
    # Base round trip is $0.12: blocked only below a $0.48 notional.
    assert cost_gate(0.47, "base").allowed is False
    assert cost_gate(0.48, "base").allowed is True


def test_solana_passes_even_tiny_notionals():
    assert cost_gate(0.10, "solana").allowed is True  # 4% of $0.10


def test_hyperliquid_never_blocked_by_gate_at_any_size():
    for notional in (0.01, 1.0, 100.0, 1e6):
        assert cost_gate(notional, "hyperliquid").allowed is True


# -- minimum viable notional ----------------------------------------------------------------

def test_min_viable_notional_flat_chains():
    assert min_viable_notional("ethereum") == pytest.approx(16.0)
    assert min_viable_notional("base") == pytest.approx(0.48)
    assert min_viable_notional("solana") == pytest.approx(0.016)


def test_min_viable_notional_hyperliquid_is_zero():
    # Percentage costs scale with the trade, so no floor exists.
    assert min_viable_notional("hyperliquid") == 0.0


def test_min_viable_notional_actually_passes_gate():
    for chain in ("solana", "base", "ethereum"):
        minimum = min_viable_notional(chain)
        assert cost_gate(minimum, chain).allowed is True
        assert cost_gate(minimum * 0.99, chain).allowed is False


# -- table --------------------------------------------------------------------------------------

def test_cost_table_shape_and_rows():
    rows = cost_table()
    assert len(rows) == 12  # 3 notionals x 4 chains
    eth_1 = [r for r in rows if r["chain"] == "ethereum"
             and r["notional"] == 1.0][0]
    assert eth_1["allowed"] is False and eth_1["cost_pct"] == pytest.approx(400.0)


def test_cost_table_custom_inputs():
    rows = cost_table(notionals=(2.0,), chains=("solana",))
    assert len(rows) == 1 and rows[0]["cost_usd"] == pytest.approx(0.004)


def test_rendered_table_contains_all_chains_and_verdicts():
    text = render_cost_table()
    for chain in CHAINS:
        assert chain in text
    assert "BLOCKED" in text and "ALLOWED" in text
    assert text.count("\n") == 12  # header + 12 rows, no trailing newline


def test_rendered_table_eth_1_row_shows_400pct():
    lines = [ln for ln in render_cost_table().splitlines()
             if "ethereum" in ln and "$    1.00" in ln]
    assert len(lines) == 1 and "400.00%" in lines[0] and "BLOCKED" in lines[0]


# -- fail-closed -------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad_chain", ["dogechain", "", None, "SOLANA", "eth"])
def test_unknown_chain_rejected(bad_chain):
    with pytest.raises(ValueError):
        cost_gate(1.0, bad_chain)
    with pytest.raises(ValueError):
        tx_cost_usd(bad_chain, 1.0)


@pytest.mark.parametrize("bad_notional", [0, -1.0, None, "10"])
def test_bad_notional_rejected(bad_notional):
    with pytest.raises(ValueError):
        cost_gate(bad_notional, "solana")


def test_bad_tx_count_rejected():
    with pytest.raises(ValueError):
        round_trip_cost_usd("solana", 1.0, txs=0)
    with pytest.raises(ValueError):
        round_trip_cost_usd("solana", 1.0, txs=2.5)


def test_bad_limit_rejected():
    with pytest.raises(ValueError):
        cost_gate(1.0, "solana", limit_pct=0)
    with pytest.raises(ValueError):
        min_viable_notional("nope")
