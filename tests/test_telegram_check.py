#!/usr/bin/env python3
"""Tests for the Telegram /check lane-lookup (daemon/telegram/commands_check.py).

Plain-script style like the rest of this repo: builds a fake
FOREX_AGENT_HOME with small ledgers, asserts reply substrings.
Run: /home/hatch/workspace/Forex-agent/.venv/bin/python tests/test_telegram_check.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main")
from daemon.telegram import commands_check as cc

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


MINT = "3SVjZ7BfT9FxiR49nZc7UdBmLgzRLJY7XBZDKhEFpump"
OTHER = "OtherMint111111111111111111111111111111111111"

home = tempfile.mkdtemp()
# grad_raw: a closed win
json.dump({"closes": [{
    "mint": MINT, "entry_ts": 1792871068, "close_ts": 1792872607,
    "entry_price": 0.000297, "pnl_usd": 1.2355, "realized_mult": 2.2355,
    "closed_reason": "ladder_complete"}], "positions": []},
    open(os.path.join(home, "grad_raw_ledger.json"), "w"))
# watcher_v2: an open position
json.dump({"closes": [], "positions": [{
    "mint": MINT, "entry_ts": 1792872000, "entry_price": 0.0005,
    "peak_mult": 1.4}]},
    open(os.path.join(home, "watcher_v2_ledger.json"), "w"))
# steady: a closed loss in steady's own shape
json.dump({"closed": [{
    "mint": MINT, "entry_ts": 1792871000, "exit_ts": 1792873000,
    "entry_price": 0.01, "pnl": -0.5, "exit_reason": "hard_stop"}],
    "positions": []},
    open(os.path.join(home, "steady_ledger.json"), "w"))
# grad_select: no entry for this mint (a different coin only)
json.dump({"closes": [{
    "mint": OTHER, "entry_ts": 1792871000, "close_ts": 1792872000,
    "entry_price": 0.001, "pnl_usd": 2.0, "realized_mult": 3.0,
    "closed_reason": "ladder_complete"}], "positions": []},
    open(os.path.join(home, "grad_select_ledger.json"), "w"))
# watcher/ceo/momentum/degen_mirror ledgers absent: skipped, not fatal.

r = cc.check_mint(MINT, home)
check("close hit names lane + pnl", "grad_raw: CLOSED +$1.24" in r)
check("close hit shows multiple", "2.24x" in r)
check("open hit reported", "watcher_v2: OPEN" in r)
check("steady shape handled", "steady: CLOSED -$0.50" in r)
check("absent lane not reported", "grad_select" not in r)
check("exact match only (other mint excluded)",
      "OtherMint" not in r)

r2 = cc.check_mint("NoSuchMint999999999999999999999999999999", home)
check("miss says not in book", "Not in our paper book" in r2)
check("miss lists checked lanes", "grad_raw" in r2 and "steady" in r2)

r3 = cc.check_mint("", home)
check("empty mint gives usage", r3.startswith("Usage:"))

# Case-sensitivity: a differently-cased mint must NOT match.
r4 = cc.check_mint(MINT.lower(), home)
check("case-sensitive, no false hit", "Not in our paper book" in r4)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
