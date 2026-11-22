"""Tests for cardcheck.py -- one valid card per chain, one break per element."""
import copy

import pytest

from cardcheck import (Card, P_CA_INVALID, P_CA_MISSING, P_CONTEXT, P_COPY,
                       P_EXIT, P_EXIT_WRONG_CHAIN, P_LIQ, P_MCAP, P_PROVENANCE,
                       P_SAFETY, P_SUPPLY, P_TRACK, exit_hint_matches_chain,
                       has_copy_button, has_track_button, is_valid, is_valid_ca,
                       required_fields, summarize, validate)

MINTS = {
    "solana": "7xK9mQ2vR8tY4wZ6aB3cD5eF7gH9jK2mN4pQ6rS8tU1v",
    "base": "3bC5dE7fG9hJ2kM4nP6qR8sT1vW3xY5zA7bC9dE2fG4h",
    "ethereum": "5dE7fG9hJ2kM4nP6qR8sT1vW3xY5zA7bC9dE2fG4hJ6k",
    "hyperliquid": "9fG2hJ4kM6nP8qR1sT3vW5xY7zA9bC2dE4fG6hJ8kM1n",
}
HINTS = {
    "solana": "Exit: Solana ladder, sell on Raydium — 50% at 2x, rest trails",
    "base": "Exit: Base ladder, sell via Aerodrome — 50% at 2x, rest trails",
    "ethereum": "Exit: Ethereum ladder, mind the gas, sell via Uniswap",
    "hyperliquid": "Exit: Hyperliquid, exit at mark price on HL spot",
}


def valid_card(chain="solana", is_pick=False):
    buttons = ["📋 Copy CA"]
    if is_pick:
        buttons.append("📍 I bought this")
    return {
        "chain": chain,
        "mint": MINTS[chain],
        "mcap_usd": 250_000.0,
        "fdv_usd": 250_000.0,
        "liquidity_usd": 60_000.0,
        "context_line": "📊 $250k mcap · $60k liq · 120 holders, rising 5m",
        "safety_line": "🛡 Safety: PASS · score 42/100 · no hard fails",
        "supply_line": "Supply 1.0B",
        "provenance_line": f"⛓ {chain.capitalize()} runner feed",
        "exit_hint": HINTS[chain],
        "buttons": buttons,
        "is_pick": is_pick,
    }


def broken(chain="solana", **changes):
    card = valid_card(chain)
    card.update(changes)
    return card


# -- the four valid chain fixtures ----------------------------------------------
@pytest.mark.parametrize("chain", ["solana", "base", "ethereum", "hyperliquid"])
def test_fully_valid_card_per_chain(chain):
    card = valid_card(chain)
    assert validate(card) == []
    assert is_valid(card) is True


@pytest.mark.parametrize("chain", ["solana", "base", "ethereum", "hyperliquid"])
def test_fully_valid_pick_card_per_chain(chain):
    assert validate(valid_card(chain, is_pick=True)) == []


def test_valid_dataclass_card():
    d = valid_card()
    card = Card(**d)
    assert validate(card) == []


def test_fdv_alone_satisfies_mcap_requirement():
    assert validate(broken(mcap_usd=None)) == []


def test_mcap_alone_satisfies_mcap_requirement():
    assert validate(broken(fdv_usd=None)) == []


# -- CA rules ----------------------------------------------------------------------
def test_ca_length_boundaries():
    assert is_valid_ca("1" * 32) is True
    assert is_valid_ca("1" * 44) is True
    assert is_valid_ca("1" * 31) is False
    assert is_valid_ca("1" * 45) is False


def test_ca_rejects_non_base58_characters():
    for bad_char in ("0", "O", "I", "l"):
        assert is_valid_ca("1" * 20 + bad_char + "1" * 20) is False


def test_ca_rejects_non_strings_and_empty():
    assert is_valid_ca("") is False
    assert is_valid_ca(None) is False
    assert is_valid_ca(12345) is False


def test_missing_ca_fails_with_exactly_that_problem():
    card = valid_card()
    del card["mint"]
    assert validate(card) == [P_CA_MISSING]
    assert validate(broken(mint="")) == [P_CA_MISSING]
    assert validate(broken(mint="   ")) == [P_CA_MISSING]


def test_invalid_ca_fails_with_exactly_that_problem():
    assert validate(broken(mint="short")) == [P_CA_INVALID]
    assert validate(broken(mint="0" * 40)) == [P_CA_INVALID]


def test_dict_card_accepts_ca_key():
    card = valid_card()
    card["ca"] = card.pop("mint")
    assert validate(card) == []


# -- one fixture per missing element, exactly that element named ----------------------
def test_missing_mcap_fdv():
    assert validate(broken(mcap_usd=None, fdv_usd=None)) == [P_MCAP]
    assert validate(broken(mcap_usd=0, fdv_usd=-5)) == [P_MCAP]


def test_missing_liquidity():
    assert validate(broken(liquidity_usd=None)) == [P_LIQ]
    assert validate(broken(liquidity_usd=0)) == [P_LIQ]


def test_missing_context_line():
    assert validate(broken(context_line="")) == [P_CONTEXT]
    assert validate(broken(context_line="   ")) == [P_CONTEXT]


def test_missing_safety_line():
    assert validate(broken(safety_line="")) == [P_SAFETY]


def test_missing_copy_button():
    assert validate(broken(buttons=[])) == [P_COPY]
    assert validate(broken(buttons=["Some other button"])) == [P_COPY]


def test_missing_supply_line():
    assert validate(broken(supply_line="")) == [P_SUPPLY]


def test_missing_chain_provenance():
    assert validate(broken(provenance_line="")) == [P_PROVENANCE]
    no_chain = valid_card()
    no_chain["chain"] = ""
    assert validate(no_chain) == [P_PROVENANCE]
    wrong_chain = valid_card()
    wrong_chain["chain"] = "polygon"
    assert validate(wrong_chain) == [P_PROVENANCE]


def test_missing_exit_hint():
    assert validate(broken(exit_hint="")) == [P_EXIT]


def test_exit_hint_for_wrong_chain():
    card = valid_card("solana")
    card["exit_hint"] = HINTS["ethereum"]
    assert validate(card) == [P_EXIT_WRONG_CHAIN]


def test_missing_track_button_on_pick():
    card = valid_card(is_pick=False)
    card["is_pick"] = True
    assert validate(card) == [P_TRACK]


def test_non_pick_needs_no_track_button():
    card = valid_card(is_pick=False)
    assert has_track_button(card) is False
    assert validate(card) == []


# -- buttons / helpers ------------------------------------------------------------------
def test_button_markers_detected_inside_button_lists():
    card = valid_card(is_pick=True)
    assert has_copy_button(card) is True
    assert has_track_button(card) is True


def test_copy_marker_as_plain_string_buttons():
    assert validate(broken(buttons="📋 Copy CA")) == []


def test_exit_hint_matching_per_chain():
    for chain, hint in HINTS.items():
        assert exit_hint_matches_chain(chain, hint) is True
    assert exit_hint_matches_chain("solana", HINTS["base"]) is False
    assert exit_hint_matches_chain("bogus", HINTS["solana"]) is False
    assert exit_hint_matches_chain("solana", "") is False


def test_bool_is_not_a_positive_amount():
    assert validate(broken(liquidity_usd=True)) == [P_LIQ]


def test_empty_card_reports_every_element():
    problems = validate({})
    for expected in (P_MCAP, P_LIQ, P_CONTEXT, P_SAFETY, P_CA_MISSING,
                     P_COPY, P_SUPPLY, P_PROVENANCE, P_EXIT):
        assert expected in problems


def test_validate_none_raises():
    with pytest.raises(ValueError):
        validate(None)


def test_multiple_problems_all_reported_in_order():
    card = broken(liquidity_usd=None, supply_line="")
    assert validate(card) == [P_LIQ, P_SUPPLY]


def test_summarize_valid_and_invalid():
    assert summarize(valid_card()) == {
        "valid": True, "problems": [], "chain": "solana", "is_pick": False}
    bad = summarize(broken(safety_line=""))
    assert bad["valid"] is False and bad["problems"] == [P_SAFETY]


def test_required_fields_lists_the_contract():
    fields = required_fields()
    assert len(fields) == 9
    assert "CA" in fields and "copy-CA button" in fields


def test_mutating_a_copy_does_not_break_the_original():
    card = valid_card()
    clone = copy.deepcopy(card)
    clone["liquidity_usd"] = None
    assert validate(card) == [] and validate(clone) == [P_LIQ]
