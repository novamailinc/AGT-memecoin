#!/usr/bin/env python3
"""Plain-script tests for the devnet demo lane (scripts/devnet-demo-trader).

Run with the venv python from the repo root:
    ../.venv/bin/python tests/test_devnet_demo.py
No network. The executor is faked except for the RPC host-guard test,
which uses the real DevnetRPC constructor (it validates the URL before
any request).
"""
import importlib.util
import json
import os
import sys
import tempfile

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


class FakeExec:
    def __init__(self, fail_n=0):
        self.wallet_pubkey = "FakePubkey111111111111111111111111111111"
        self.calls = []
        self.fail_n = fail_n

    def transfer_sol(self, to, sol):
        self.calls.append((to, sol))
        if len(self.calls) <= self.fail_n:
            raise RuntimeError("fake devnet failure")
        return "FakeSig%d" % len(self.calls)


def fresh_state():
    st = mod.default_state()
    st["seeded"] = True
    return st


# 1. Seeding marks known mints pre-existing, fires nothing.
st = mod.default_state()
fired = mod.mirror_once(st, {"A": {}, "B": {}}, [{"mint": "C"}],
                        FakeExec(), 1000.0)
check("seed: no fires on first run", fired == [])
check("seed: journal has positions+closes", set(st["journal"]) == {"A", "B", "C"})
check("seed: all preexisting", all(v.get("preexisting") for v in st["journal"].values()))
check("seed: seeded flag set", st["seeded"] is True)

# 2. Only genuinely new positions fire, with the fixed amount, to self.
st = fresh_state()
st["journal"] = {"A": {"sig": "x", "ts": 1.0}}
fx = FakeExec()
fired = mod.mirror_once(st, {"A": {}, "B": {}, "C": {}}, [], fx, 2000.0)
check("fresh: only new mints fire", fired == ["B", "C"])
check("fresh: fixed 0.0001 to self", all(c == (fx.wallet_pubkey, 0.0001) for c in fx.calls))
check("fresh: journal updated", set(st["journal"]) == {"A", "B", "C"})
check("fresh: counts", st["counts"]["confirmed"] == 2 and st["counts"]["transfers"] == 2)
check("fresh: streak reset", st["fail_streak"] == 0)

# 3. Failure streak halts at 5 and stays halted across calls.
st = fresh_state()
fx = FakeExec(fail_n=99)
for _ in range(4):
    mod.mirror_once(st, {"M": {}}, [], fx, 3000.0)
check("halt: not halted before 5", st["halted"] is False)
mod.mirror_once(st, {"M": {}}, [], fx, 3000.0)
check("halt: halted at 5", st["halted"] is True)
n_calls = len(fx.calls)
mod.mirror_once(st, {"N": {}}, [], fx, 3000.0)
check("halt: no calls while halted", len(fx.calls) == n_calls)
check("halt: failed count", st["counts"]["failed"] == 5)

# 4. A failure then a success resets the streak.
st = fresh_state()
fx = FakeExec(fail_n=1)
mod.mirror_once(st, {"M": {}}, [], fx, 4000.0)
check("streak: 1 after failure", st["fail_streak"] == 1)
mod.mirror_once(st, {"N": {}}, [], fx, 4000.0)
check("streak: 0 after success", st["fail_streak"] == 0)

# 5. Ledger save/load round-trip (tmp path, never the live ledger).
st = fresh_state()
st["journal"] = {"Z": {"sig": "s", "ts": 5.0}}
with tempfile.TemporaryDirectory() as td:
    mod.DEMO_LEDGER = os.path.join(td, "demo.json")
    mod.save_state(st)
    loaded = mod.load_state()
check("ledger: round-trip", loaded["journal"] == st["journal"] and loaded["seeded"] is True)

# 6. RPC host guard: real code, no network.
from broker.memecoin.execution import rpc as rpc_mod
try:
    rpc_mod.DevnetRPC("https://api.mainnet-beta.solana.com")
    check("guard: mainnet refused", False)
except Exception as e:
    check("guard: mainnet refused", "devnet-only" in str(e) or "non-devnet" in str(e))
try:
    rpc_mod.DevnetRPC("https://api.devnet.solana.com")
    check("guard: devnet accepted", True)
except Exception:
    check("guard: devnet accepted", False)

# 7. Lane scoping: one coin open in two lanes mirrors once per lane.
st = fresh_state()
st["journal"] = {"A": {"sig": "x", "ts": 1.0}}
fx = FakeExec()
fired = mod.mirror_once(st, {"A": {}, "B": {}}, [], fx, 6000.0,
                        lane="tranche")
check("lane: tranche seeds, fires nothing", fired == [])
check("lane: tranche keys scoped",
      "tranche:A" in st["journal"] and "tranche:B" in st["journal"])
fired = mod.mirror_once(st, {"A": {}, "B": {}}, [], fx, 6000.0,
                        lane="grad_select")
check("lane: select fires only unmirrored B", fired == ["B"])
fired = mod.mirror_once(st, {"A": {}, "B": {}, "C": {}}, [], fx, 6000.0,
                        lane="tranche")
check("lane: tranche fires only new C", fired == ["C"])
check("lane: transfer records carry lane",
      st["transfers"][-1].get("lane") == "tranche"
      and st["transfers"][-2].get("lane") == "grad_select")
check("lane: counts", st["counts"]["confirmed"] == 2
      and st["counts"]["transfers"] == 2)

# 8. Per-lane seeding: a new lane seeds without firing, without
# touching the select seeded flag.
st = mod.default_state()
fired = mod.mirror_once(st, {"X": {}}, [{"mint": "Y"}], FakeExec(), 7000.0,
                        lane="tranche")
check("lane-seed: fires nothing", fired == [])
check("lane-seed: scoped preexisting",
      st["journal"].get("tranche:X", {}).get("preexisting") is True
      and st["journal"].get("tranche:Y", {}).get("preexisting") is True)
check("lane-seed: select flag untouched", st.get("seeded") is False)
fired = mod.mirror_once(st, {"X": {}, "Y": {}, "Z": {}}, [], FakeExec(),
                        7000.0, lane="tranche")
check("lane-seed: only new Z fires after seed", fired == ["Z"])

# 9. Trading mirror per lane: tranche sub-account opens, books a leg
# and closes without touching the select top-level account.
tst = mod.default_trade_state()
sub = mod._lane_trade_state(tst, "tranche")
check("trade-lane: sub-account created",
      tst["lanes"]["tranche"] is sub)
check("trade-lane: select keeps top-level",
      mod._lane_trade_state(tst, "grad_select") is tst)
acts = mod.trade_mirror_once(sub, {"P": {}}, [], 8000.0)
check("trade-lane: activation seeds", sub["activated"] is True)
acts = mod.trade_mirror_once(sub, {"Q": {"entry_price": 1.0,
                                         "notional_usd": 25.0,
                                         "entry_ts": 8000.0,
                                         "symbol": "Q",
                                         "legs": {}}}, [], 8000.0)
check("trade-lane: demo opened", sub["counts"]["opened"] == 1
      and "Q" in sub["open_positions"])
check("trade-lane: select untouched", tst["counts"]["opened"] == 0)
acts = mod.trade_mirror_once(sub, {"Q": {"entry_price": 1.0,
                                         "notional_usd": 25.0,
                                         "entry_ts": 8000.0,
                                         "symbol": "Q",
                                         "legs": {"a": {"done": True,
                                                       "realized_usd": 12.5,
                                                       "frac": 0.25,
                                                       "exit_reason": "leg_a_2x"}}}},
                               [], 8000.0)
check("trade-lane: leg booked",
      "a" in sub["open_positions"]["Q"]["legs_booked"]
      and abs(sub["open_positions"]["Q"]["booked_demo_pnl_sol"] - 0.025) < 1e-9)
acts = mod.trade_mirror_once(sub, {}, [{"mint": "Q", "pnl_usd": 10.0,
                                        "close_ts": 8000.0,
                                        "closed_reason": "ladder_complete",
                                        "entry_notional_usd": 25.0}], 8000.0)
check("trade-lane: closed with scaled pnl",
      sub["counts"]["closed"] == 1 and sub["counts"]["wins"] == 1
      and abs(sub["closed_positions"][0]["demo_pnl_sol"] - 0.04) < 1e-9)

# 10. Failure halt is global across lanes.
st = fresh_state()
fx = FakeExec(fail_n=99)
mod.mirror_once(st, {"M0": {}}, [], fx, 5000.0, lane="tranche")
for i in range(1, 6):
    mod.mirror_once(st, {"M%d" % i: {}}, [], fx, 5000.0, lane="tranche")
check("halt-global: halted at 5 failures", st["halted"] is True)
check("halt-global: failed count 5", st["counts"]["failed"] == 5)
n_calls = len(fx.calls)
mod.mirror_once(st, {"N": {}}, [], fx, 5000.0, lane="grad_select")
check("halt-global: other lane also stops", len(fx.calls) == n_calls)

print()
if FAILURES:
    print("FAILURES:", FAILURES)
    sys.exit(1)
print("ALL DEVNET DEMO TESTS PASS")
