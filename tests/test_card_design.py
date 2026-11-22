#!/usr/bin/env python3
"""Tests for Nova's 3-second card design."""
import sys
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
from card_design import build_card, build_runner_update, signal_color, status_tag

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# Signal colors
check("green for high score", signal_color(85) == "🟢")
check("green for smart money", signal_color(72, has_smart=True) == "🟢")
check("yellow for medium", signal_color(65) == "🟡")
check("red for low", signal_color(55) == "🔴")
check("green for runner", signal_color(50, is_runner=True) == "🟢")

# Status tags
check("runner tag", "RUNNER" in status_tag(is_runner=True, multiple=2.5))
check("smart tag", "SMART" in status_tag(has_smart=True))
check("fresh tag", "FRESH" in status_tag(is_fresh=True))
check("steady tag", "STEADY" in status_tag(is_steady=True))

# Full card
card = build_card(symbol="TEST", score=75, mint="abc123",
    entry_price=0.001, current_price=0.002,
    mcap=500000, liquidity=25000, buyers=45,
    age_min=5, buys_5m=20, sells_5m=5,
    smart_wallets=1, safety="clean", is_fresh=True)
check("card has symbol", "$TEST" in card)
check("card has CA", "abc123" in card)
check("card has prices", "$0.001" in card and "$0.002" in card)
check("card has mcap", "$500K" in card)
check("card has buyers", "45 buyers" in card)
check("card has smart", "🧠" in card)
check("card has safety", "🛡️" in card)
check("card scannable (<=12 lines)", len(card.split("\n")) <= 12)

# Phase 0: chain/source tagging + exit tempo + tap nudge
check("card has chain tag", "⛓ Solana" in card)
card2 = build_card(symbol="T2", score=70, mint="xyz",
                   chain="ethereum", source="eth-runner")
check("eth chain tag", "⛓ Ethereum" in card2)
check("eth source tag", "eth-runner" in card2)
check("eth exit hint (slow decay)", "breathe" in card2)
check("solana exit hint (fast decay)", "Fast decay" in card)
check("tap nudge present", "trains your feed" in card)
# backward compat: defaults still work
card3 = build_card(symbol="T3", score=60, mint="qrs")
check("default chain tag", "⛓ Solana" in card3)

# Runner update
r = build_runner_update("TEST", "abc123", 0.001, 0.003, 0.004)
check("runner shows 3x", "3.0x" in r)
check("runner off peak", "Off peak" in r)

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
