#!/usr/bin/env python3
"""Venue fee propagation through the tranche lanes (2026-11-22).

Before this change watcher/ceo/momentum/mirror called
chain_costs.cost_gate / tx_cost_usd WITHOUT a venue, so venue swap
fees were modeled as ZERO while their docstrings claimed 1.25%. Now:

  - chain_costs.lane_dex_venue grounds the venue in the lane's own
    fresh-tape dex label ("pump" -> pumpfun_curve, "pumpswap" ->
    pumpswap); hyperliquid uses its own taker-fee model; ANY other
    chain or dex REFUSES the entry (fail-closed, never invented).
  - Market cap / quote mint are NOT threaded (lanes carry USD mcap,
    not quote-lamport mcap), so the fee module charges its maximum
    tier - the conservative direction.
  - Positions carry fee_venue from entry; exits charge the
    entry-stamped venue; a legacy position without one is charged
    the conservative pump maximum tier on solana.

Plain script - run directly (the pytest conftest ignores files
without pytest-style tests; scripts/run_script_tests.py executes
this file).
"""
import importlib
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TMPD = tempfile.mkdtemp(prefix="venue-fee-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="venue-fee-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"

from daemon.launch_scanner import chain_costs as CC
from daemon.launch_scanner import paper_account as PA
from daemon.launch_scanner import watcher_lane as W

importlib.reload(PA)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---- 1. lane_dex_venue mapping --------------------------------------
check("map: pump -> pumpfun_curve",
      CC.lane_dex_venue("solana", "pump") == ("pumpfun_curve", ""))
check("map: pumpswap -> pumpswap",
      CC.lane_dex_venue("solana", "pumpswap") == ("pumpswap", ""))
check("map: pump.fun alias -> pumpfun_curve",
      CC.lane_dex_venue("solana", "pump.fun") == ("pumpfun_curve", ""))
v, why = CC.lane_dex_venue("solana", "raydium")
check("map: raydium refused with reason", v is None and "raydium" in why)
v, why = CC.lane_dex_venue("solana", None)
check("map: missing dex refused", v is None and "unmodeled" in why)
v, why = CC.lane_dex_venue("solana", "")
check("map: empty dex refused", v is None and "unmodeled" in why)
v, why = CC.lane_dex_venue("base", "uniswap")
check("map: base refused (chain unmodeled)",
      v is None and "base" in why)
v, why = CC.lane_dex_venue("ethereum", "uniswap")
check("map: ethereum refused", v is None and "ethereum" in why)
check("map: hyperliquid uses own taker model",
      CC.lane_dex_venue("hyperliquid", None) == ("", ""))

# ---- 2. fee arithmetic -----------------------------------------------
gas_only = CC.tx_cost_usd("solana", 100.0)
with_curve = CC.tx_cost_usd("solana", 100.0, venue="pumpfun_curve")
check("fee: curve venue adds 1.25% on $100",
      abs((with_curve - gas_only) - 1.25) < 1e-9)
with_swap = CC.tx_cost_usd("solana", 100.0, venue="pumpswap")
check("fee: pumpswap unknown mcap charges max tier 1.25%",
      abs((with_swap - gas_only) - 1.25) < 1e-9)
blocked, _ = CC.cost_gate("solana", 1.0, venue="pumpfun_curve")
check("gate: $1 notional with 1.25% venue fee not cost-blocked",
      not blocked)

# ---- 3. watcher end-to-end -------------------------------------------
# Verifiably CLEAN tape for the entry-time toxicity re-check
# (2026-11-23): volume/h1 txns present, pairCreatedAt 1h old.
GOOD_TAPE = {"dex": "pump", "usd_liquidity": 100000.0,
             "usd_market_cap": 500000.0, "chg_m5": 5.0, "buys_m5": 100,
             "sells_m5": 40, "vol_m5": 9000.0, "buys_h1": 400,
             "sells_h1": 150,
             "pair_created_ms": (time.time() - 3600) * 1000,
             "price_usd": 0.001, "tape_source": "test"}


class _SafetyStub:
    def check_token(self, chain, mint, timeout=10):
        return {"passed": True, "score": 80, "flags": [],
                "fails": [], "unknown": False, "source": "test"}


class _KSStub:
    def gate(self, lane):
        return True

    def is_halted(self, lane):
        return False

    def record_trade(self, lane, pnl, ts=None):
        pass

    def check_lane(self, lane):
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        return {"lane": lane, "action": "HALTED"}


W.LEDGER_PATH = os.path.join(TMPD, "watcher_ledger.json")
W._safety = _SafetyStub()
W._ks = _KSStub()
W.quote_price = lambda chain, mint, spot_index=None: 0.001
W._convergence = lambda sig: (2, ["w1", "w2"], True)
W._fresh_tape = lambda chain, mint: dict(GOOD_TAPE)


def _sig(mint):
    return {"mint": mint, "symbol": "T", "chain": "solana"}


# 3a. Unmodeled dex -> entry REFUSED, nothing debited.
W._fresh_tape = lambda chain, mint: dict(GOOD_TAPE, dex="raydium")
cash0 = PA.snapshot()["cash_spendable"]
ok, why, final = W.consider_fire(_sig("MINT_RAY"))
check("watcher: raydium dex refused",
      not ok and "unmodeled" in why and "raydium" in why)
check("watcher: refusal is final (no retry churn)", final is True)
check("watcher: refusal debits nothing",
      PA.snapshot()["cash_spendable"] == cash0)
with W._ledger_lock():
    d = W._read()
check("watcher: refusal opens no position",
      "MINT_RAY" not in d["positions"])

# 3b. pump dex -> entry opens with fee_venue and venue-inclusive cost.
W._fresh_tape = lambda chain, mint: dict(GOOD_TAPE)
ok, why, final = W.consider_fire(_sig("MINT_PUMP"))
check("watcher: pump dex opens", ok)
with W._ledger_lock():
    d = W._read()
    pos = d["positions"]["MINT_PUMP"]
check("watcher: position stamped fee_venue=pumpfun_curve",
      pos.get("fee_venue") == "pumpfun_curve")
gas_only_1 = CC.GAS_USD.get("solana", 0.0)
check("watcher: entry cost includes 1.25% venue fee on $1",
      abs(pos["gas_in_usd"] - (gas_only_1 + 0.0125)) < 1e-6)

# 3c. Exit charges the entry-stamped venue (not zero).
with W._ledger_lock():
    d = W._read()
    pos = d["positions"]["MINT_PUMP"]
    px = pos["entry_price"] * 2.0
    W._close_leg(pos, "a", px, "target-a")
    leg = pos["legs"]["a"]
check("watcher: exit leg stamped fee_venue",
      leg.get("fee_venue") == "pumpfun_curve")
gross = pos["tokens"] * pos["legs"]["a"]["frac"] * \
    leg["exit_price"]
expected_net = gross - CC.tx_cost_usd("solana", gross,
                                      venue="pumpfun_curve")
check("watcher: exit net = gross - venue-inclusive cost",
      abs(leg["realized_usd"] - round(expected_net, 6)) < 1e-6)

# ---- 4. legacy position fallback -------------------------------------
check("exit helper: legacy solana position -> conservative pump max",
      W._exit_fee_venue({"chain": "solana"}) == "pump")
check("exit helper: stamped venue wins",
      W._exit_fee_venue({"chain": "solana",
                         "fee_venue": "pumpswap"}) == "pumpswap")
check("exit helper: hyperliquid own model -> None kwarg",
      W._exit_fee_venue({"chain": "hyperliquid",
                         "fee_venue": ""}) is None)
legacy_net_pos = {"chain": "solana", "tokens": 1000.0,
                  "entry_snapshot": {"liquidity": 100000.0},
                  "legs": {"a": {"frac": 1.0, "done": False},
                           "b": {"frac": 0.0, "done": True},
                           "c": {"frac": 0.0, "done": True}},
                  "realized_usd": 0.0, "entry_price": 0.001}
# Direct helper call: the legacy pos pays the pump max tier on exit.
_leg_fee = CC.tx_cost_usd("solana", 100.0,
                          venue=W._exit_fee_venue(legacy_net_pos))
check("exit helper: legacy exit fee equals pump max on $100",
      abs((_leg_fee - gas_only) - 1.25) < 1e-9)

print()
if FAILS:
    print("FAILURES: %s" % FAILS)
    sys.exit(1)
print("ALL %s CHECKS PASS" % "VENUE-FEE")
