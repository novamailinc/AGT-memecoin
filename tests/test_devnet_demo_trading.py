#!/usr/bin/env python3
"""Plain-script tests for the SOL-denominated demo trading mirror
(scripts/devnet-demo-trader, trade_mirror_once).

Run with the venv python from the repo root:
    ../.venv/bin/python tests/test_devnet_demo_trading.py
No network, no files: the mirror mutates only the passed state dict.
The no-write property of dry-run mode is enforced by main() (it never
calls save_trade_state when dry-run); here we verify the dry-run action
prefix and that mirror logic itself touches no filesystem paths.

Accounting under test (stake ratio, not an exchange rate):
  demo P&L SOL = paper P&L USD x (0.1 / paper notional USD).
  Demo balance = starting balance + cumulative net demo P&L.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

from importlib.machinery import SourceFileLoader
_loader = SourceFileLoader(
    "devnet_demo_trader",
    os.path.join(_HERE, "..", "scripts", "devnet-demo-trader"))
_spec = importlib.util.spec_from_loader("devnet_demo_trader", _loader)
mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(mod)

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def approx(a, b, eps=1e-9):
    return abs(a - b) <= eps


def live_state(start=12.0):
    """Activated trading state with a known starting balance."""
    st = mod.default_trade_state()
    st["starting_balance_sol"] = start
    st["balance_sol"] = start
    st["wallet"] = "DemoWallet111111111111111111111111111111"
    st["activated"] = True
    st["seen_mints"] = {}
    return st


def open_pos(mint, entry_price=0.0001, notional=25.0, legs=None):
    return {"mint": mint, "symbol": "TST", "entry_ts": 1000.0,
            "entry_price": entry_price, "notional_usd": notional,
            "legs": legs or {}}


def leg(frac, done, realized):
    return {"frac": frac, "done": done, "realized_usd": realized,
            "exit_reason": "leg_test" if done else None}


# 1. Activation seeds preexisting mints; nothing opens or books.
st = mod.default_trade_state()
acts = mod.trade_mirror_once(
    st, {"A": open_pos("A"), "B": open_pos("B")},
    [{"mint": "C", "pnl_usd": 5.0}], 1000.0)
check("activation: seeded flag", st["activated"] is True)
check("activation: seen = positions+closes", set(st["seen_mints"]) == {"A", "B", "C"})
check("activation: no opens", st["counts"]["opened"] == 0 and st["open_positions"] == {})
check("activation: balance untouched", st["balance_sol"] is None)

# 2. New paper position opens a demo position; balance unchanged.
st = live_state()
acts = mod.trade_mirror_once(st, {"M": open_pos("M")}, [], 2000.0)
check("entry: demo opened", "M" in st["open_positions"])
check("entry: stake 0.1", st["open_positions"]["M"]["stake_sol"] == 0.1)
check("entry: balance unchanged at open", st["balance_sol"] == 12.0)
check("entry: counts", st["counts"]["opened"] == 1)

# 3. Partial leg booking while open: leg a (0.5 frac, $25 gross on $25
#    notional) nets +$12.50 paper -> +0.05 SOL demo.
st = live_state()
mod.trade_mirror_once(st, {"M": open_pos("M")}, [], 2000.0)
acts = mod.trade_mirror_once(
    st, {"M": open_pos("M", legs={"a": leg(0.5, True, 25.0),
                                  "b": leg(0.25, False, 0),
                                  "c": leg(0.25, False, 0)})}, [], 2100.0)
check("leg: booked", "a" in st["open_positions"]["M"]["legs_booked"])
check("leg: demo pnl +0.05", approx(st["open_positions"]["M"]["booked_demo_pnl_sol"], 0.05))
check("leg: balance 12.05", approx(st["balance_sol"], 12.05))
check("leg: counts", st["counts"]["legs_booked"] == 1)
# Same leg seen again is not double-booked.
mod.trade_mirror_once(
    st, {"M": open_pos("M", legs={"a": leg(0.5, True, 25.0)})}, [], 2200.0)
check("leg: no double-book", approx(st["balance_sol"], 12.05)
      and st["counts"]["legs_booked"] == 1)

# 4. Full close reconciles exactly: paper pnl +$10 -> demo +$0.04.
st = live_state()
mod.trade_mirror_once(st, {"M": open_pos("M")}, [], 2000.0)
mod.trade_mirror_once(
    st, {"M": open_pos("M", legs={"a": leg(0.5, True, 25.0)})}, [], 2100.0)
close = {"mint": "M", "close_ts": 2300.0, "pnl_usd": 10.0,
         "realized_usd": 35.0, "entry_notional_usd": 25.0,
         "closed_reason": "ladder_complete",
         "legs": {"a": leg(0.5, True, 25.0),
                  "b": leg(0.25, True, 7.5),
                  "c": leg(0.25, True, 2.5)}}
mod.trade_mirror_once(st, {}, [close], 2300.0)
check("close: position closed", "M" not in st["open_positions"]
      and len(st["closed_positions"]) == 1)
check("close: total demo pnl == paper pnl x 0.004",
      approx(st["closed_positions"][0]["demo_pnl_sol"], 0.04))
check("close: balance == start + demo pnl", approx(st["balance_sol"], 12.04))
check("close: win counted", st["counts"]["closed"] == 1 and st["counts"]["wins"] == 1)

# 5. Loss close: paper pnl -$12.50 -> demo -$0.05, loss counted.
st = live_state()
mod.trade_mirror_once(st, {"M": open_pos("M")}, [], 2000.0)
close = {"mint": "M", "close_ts": 2300.0, "pnl_usd": -12.5,
         "realized_usd": 12.5, "entry_notional_usd": 25.0,
         "closed_reason": "hard_stop_-50pct", "legs": {}}
mod.trade_mirror_once(st, {}, [close], 2300.0)
check("loss: balance 11.95", approx(st["balance_sol"], 11.95))
check("loss: loss counted", st["counts"]["losses"] == 1 and st["counts"]["wins"] == 0)

# 6. Preexisting mint's close never touches the demo ledger.
st = mod.default_trade_state()
mod.trade_mirror_once(st, {"A": open_pos("A")}, [], 1000.0)
mod.trade_mirror_once(st, {}, [{"mint": "A", "pnl_usd": 999.0,
                                "realized_usd": 1024.0,
                                "entry_notional_usd": 25.0,
                                "closed_reason": "ladder_complete"}], 2000.0)
check("preexisting: close ignored", st["counts"]["closed"] == 0
      and st["closed_positions"] == [] and st["open_positions"] == {})

# 7. Between-polls open+close: close record for an unseen mint is
#    ignored, never backfilled, but marked seen.
st = live_state()
mod.trade_mirror_once(st, {}, [{"mint": "X", "pnl_usd": 50.0,
                                "realized_usd": 75.0,
                                "entry_notional_usd": 25.0,
                                "closed_reason": "ladder_complete"}], 2000.0)
check("missed: no backfill", st["counts"]["closed"] == 0
      and approx(st["balance_sol"], 12.0))
check("missed: marked seen", "X" in st["seen_mints"]
      and st["seen_mints"]["X"].get("missed_between_polls") is True)

# 8. Vanish without close: unresolved after 2 polls, bookings kept.
st = live_state()
mod.trade_mirror_once(st, {"M": open_pos("M")}, [], 2000.0)
mod.trade_mirror_once(
    st, {"M": open_pos("M", legs={"a": leg(0.5, True, 25.0)})}, [], 2100.0)
mod.trade_mirror_once(st, {}, [], 2200.0)
check("vanish: still open after 1 absent poll", "M" in st["open_positions"])
mod.trade_mirror_once(st, {}, [], 2300.0)
check("vanish: unresolved after 2 polls", "M" not in st["open_positions"]
      and len(st["unresolved"]) == 1
      and st["unresolved"][0]["reason"] == "vanished_without_close")
check("vanish: booked pnl kept", approx(st["balance_sol"], 12.05))

# 9. Close fallback: no pnl_usd -> realized - notional.
st = live_state()
mod.trade_mirror_once(st, {"M": open_pos("M")}, [], 2000.0)
close = {"mint": "M", "close_ts": 2300.0, "realized_usd": 40.0,
         "entry_notional_usd": 25.0, "closed_reason": "ladder_complete"}
mod.trade_mirror_once(st, {}, [close], 2300.0)
check("fallback: pnl 15 -> demo 0.06",
      approx(st["closed_positions"][0]["demo_pnl_sol"], 0.06))

# 10. Leg with missing frac: skipped while open, captured at close.
st = live_state()
mod.trade_mirror_once(st, {"M": open_pos("M")}, [], 2000.0)
nofrac = {"done": True, "realized_usd": 25.0, "exit_reason": "leg_a_2x"}
mod.trade_mirror_once(st, {"M": open_pos("M", legs={"a": nofrac})}, [], 2100.0)
check("nofrac: skipped while open", st["counts"]["legs_booked"] == 0
      and approx(st["balance_sol"], 12.0))
close = {"mint": "M", "close_ts": 2300.0, "pnl_usd": 10.0,
         "realized_usd": 35.0, "entry_notional_usd": 25.0,
         "closed_reason": "ladder_complete",
         "legs": {"a": nofrac, "b": leg(0.25, True, 7.5),
                  "c": leg(0.25, True, 2.5)}}
mod.trade_mirror_once(st, {}, [close], 2300.0)
check("nofrac: captured at close", approx(st["balance_sol"], 12.04))

# 11. Scale uses the actual notional: $50 paper stake -> 0.002 factor.
st = live_state()
mod.trade_mirror_once(st, {"M": open_pos("M", notional=50.0)}, [], 2000.0)
close = {"mint": "M", "close_ts": 2300.0, "pnl_usd": 20.0,
         "realized_usd": 70.0, "entry_notional_usd": 50.0,
         "closed_reason": "ladder_complete"}
mod.trade_mirror_once(st, {}, [close], 2300.0)
check("scale: 20 x 0.1/50 = 0.04",
      approx(st["closed_positions"][0]["demo_pnl_sol"], 0.04))

# 12. Dry-run actions are prefixed and state stays in-memory shaped.
st = live_state()
acts = mod.trade_mirror_once(st, {"M": open_pos("M")}, [], 2000.0, dryrun=True)
check("dryrun: actions prefixed",
      all(a.startswith("DRYRUN trade ") for a in acts) and len(acts) > 0)
check("dryrun: state mutated in memory only",
      "M" in st["open_positions"] and st["counts"]["opened"] == 1)

# 13. Scale constant: 0.1 SOL / 25 USD = 0.004.
check("scale constant", approx(mod.DEMO_STAKE_SOL / 25.0, 0.004)
      and mod.DEMO_STAKE_SOL == 0.1)

print()
if FAILURES:
    print("FAILURES:", FAILURES)
    sys.exit(1)
print("ALL DEVNET DEMO TRADING TESTS PASS")
