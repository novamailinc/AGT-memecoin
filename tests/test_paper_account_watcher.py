#!/usr/bin/env python3
"""Wiring tests: watcher lanes (v1 + v2 engine variant) <-> shared
paper account.

Proves the actual runtime wiring: with PAPER_ACCOUNT_ENABLED=1 the
shared account is the authoritative capital gate for consider_fire
(a refusal is transient and costs the lane nothing), every exit
tranche credits net proceeds immediately, op_id retries are safe,
and watcher_v2 — an isolated engine instance of this same file with
LANE overridden — resolves the SAME account module (no silent
relative-import failure) and settles under its own lane identity.
With the flag off the lane never touches the account.

Harness mirrors tests/test_watcher_lane.py. Run:
python3 tests/test_paper_account_watcher.py
"""
import importlib
import os
import sys
import tempfile
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "daemon", "launch_scanner"))
sys.path.insert(0, _REPO_ROOT)

TMPD = tempfile.mkdtemp(prefix="paper-watcher-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="paper-watcher-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
os.environ["PAPER_ACCOUNT_START"] = "2"

from daemon.launch_scanner import watcher_lane as W
from daemon.launch_scanner import paper_account as PA
from daemon.launch_scanner import signal_bus as BUS

W.LEDGER_PATH = os.path.join(TMPD, "watcher_ledger.json")
_bus_tmp = tempfile.mkdtemp(prefix="paper-watcher-bus-")
BUS.BUS_PATH = os.path.join(_bus_tmp, "signal_bus.jsonl")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# Verifiably CLEAN DexScreener-shaped tape for the entry-time toxicity
# re-check (2026-11-23): see test_paper_e2e GOOD_TAPE comment.
GOOD_TAPE = {"dex": "pump", "usd_liquidity": 100_000.0, "usd_market_cap": 500_000.0,
             "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40,
             "vol_m5": 9000.0, "buys_h1": 400, "sells_h1": 150,
             "pair_created_ms": (time.time() - 3600) * 1000,
             "tape_source": "dexscreener"}
PRICES = {}


def _tape(chain, mint):
    return dict(GOOD_TAPE)


class _SafetyStub:
    def check_token(self, chain, mint, timeout=10):
        return {"passed": True, "score": 80, "flags": [], "fails": [],
                "unknown": False}


def _install_stubs(mod):
    mod._fresh_tape = _tape
    mod.quote_price = lambda chain, mint, spot_index=None: \
        PRICES.get(mint, 0.001)
    mod._safety = _SafetyStub()


_install_stubs(W)


def fire(mint, groups=3):
    ev = {"smart_wallets": groups,
          "wallets": ["Wal%d" % i for i in range(groups)],
          "wallet_groups": ["g%d" % i for i in range(groups)],
          "safety": "ok",
          "components": {"convergence_groups": groups,
                         "sightings": groups,
                         "sol_spent_total": 4.2,
                         "safety_score": 80}}
    return BUS.make_signal("solana", "smart-watcher", mint,
                           symbol="WATCH", tier="converged",
                           mcap=500000.0, liquidity=100000.0,
                           evidence=ev)


def _reset_lane():
    if os.path.exists(W.LEDGER_PATH):
        os.remove(W.LEDGER_PATH)
    W.ensure_ledger()


def _reset_account(start="2"):
    os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
        prefix="paper-watcher-home-")
    os.environ["PAPER_ACCOUNT_START"] = start
    importlib.reload(PA)


def M(i):
    return "WatchMint%037d" % i


# ---------------- flag ON: gate, debit, per-tranche settle ----------
_reset_lane()
_reset_account()

opened, why, final = W.consider_fire(fire(M(1)), time.time())
snap = PA.snapshot()
check("entry debits the shared account (2.00 - 1.00)",
      opened and abs(snap["cash"] - 1.0) < 1e-9)
pos = W.open_positions().get(M(1))
check("entry pins the shared position instance",
      pos is not None
      and pos.get("pa_pos_id", "").startswith("watcher/%s#" % M(1)))

opened2, _, _ = W.consider_fire(fire(M(2)), time.time())
check("second entry drains the shared cash to 0",
      opened2 and abs(PA.snapshot()["cash"]) < 1e-9)

opened3, why3, final3 = W.consider_fire(fire(M(3)), time.time())
check("third entry REFUSED by the shared account (transient)",
      not opened3 and "paper account" in why3 and not final3)
check("refused entry costs the lane nothing",
      abs(W.account_state()["bankroll"] - 98.0) < 1e-9
      and M(3) not in W.open_positions())

# Exit M(1) through the ladder: leg A at 2x settles its tranche into
# shared cash immediately; the lane bankroll waits for finalize.
entry = W.open_positions()[M(1)]["entry_price"]
tokens = W.open_positions()[M(1)]["tokens"]
keep_entry = W.open_positions()[M(2)]["entry_price"]
W.poll_exits(price_fn=lambda c, m, s:
             entry * 2.1 if m == M(1) else keep_entry)
pos = W.open_positions()[M(1)]
snap = PA.snapshot()
leg_a = pos["legs"]["a"]["realized_usd"]
check("leg A done at 2x", pos["legs"]["a"]["done"])
check("leg A tranche credited to shared cash immediately",
      abs(snap["cash"] - leg_a) < 1e-6 and leg_a > 0)
check("lane bankroll NOT settled before finalize (still 98)",
      abs(W.account_state()["bankroll"] - 98.0) < 1e-9)
wpos = [p for p in snap["lanes"]["watcher"]["positions"]
        if p["mint"] == M(1)][0]
check("shared position shrank to the remaining units + basis",
      abs(wpos["qty"] - tokens * 0.5) < 1e-6
      and abs(wpos["basis"] - 0.5) < 1e-9)

W.poll_exits(price_fn=lambda c, m, s:
             entry * 3.1 if m == M(1) else keep_entry)   # leg B
W.poll_exits(price_fn=lambda c, m, s:
             entry * 3.4 if m == M(1) else keep_entry)   # peak
closes = W.poll_exits(price_fn=lambda c, m, s:
                      entry * 3.4 * 0.69 if m == M(1) else keep_entry)
check("ladder completes and finalizes the lane position",
      M(1) not in W.open_positions() and len(closes) == 1)
rec = closes[0]
snap = PA.snapshot()
check("shared cash == total realized for the closed position",
      abs(snap["cash"] - rec["realized_usd"]) < 1e-6)
check("no watcher/M(1) position left in the shared account",
      all(p["mint"] != M(1)
          for p in snap["lanes"]["watcher"]["positions"]))
check("M(2) still open at full basis (untouched by M(1)'s close)",
      any(p["mint"] == M(2) and abs(p["basis"] - 1.0) < 1e-9
          for p in snap["lanes"]["watcher"]["positions"]))

# Tranche op_id retry returns the ORIGINAL outcome, no double credit.
before = PA.snapshot()["cash"]
ok1, _ = PA.credit("watcher", M(1), 999.0,
                   "watcher:leg:a:%s:%s" % (M(1), pos["pa_pos_id"]),
                   pos_id=pos["pa_pos_id"], qty=1.0)
check("tranche op_id retry returns the original outcome",
      ok1 and abs(PA.snapshot()["cash"] - before) < 1e-9)

# ---------------- watcher_v2 engine instance: same account ----------
from daemon.launch_scanner import watcher_v2_lane as V2

_reset_account()
check("v2 engine instance resolves the SAME account module",
      V2._eng._paper_account() is PA)
check("v2 lane identity is watcher_v2", V2._eng.LANE == "watcher_v2")
_install_stubs(V2._eng)
if os.path.exists(V2._eng.LEDGER_PATH):
    os.remove(V2._eng.LEDGER_PATH)
V2._eng.ensure_ledger()
opened_v2, why_v2, _ = V2._eng.consider_fire(fire(M(9)), time.time())
snap = PA.snapshot()
check("v2 entry debits under lane watcher_v2 (not watcher)",
      opened_v2 and abs(snap["cash"] - 1.0) < 1e-9
      and snap["lanes"].get("watcher_v2", {}).get("open_count") == 1
      and snap["lanes"].get("watcher", {}).get("open_count", 0) == 0)
pos_v2 = V2._eng.open_positions().get(M(9))
check("v2 entry pins a watcher_v2 position instance",
      pos_v2 is not None
      and pos_v2.get("pa_pos_id", "").startswith("watcher_v2/"))

# ---------------- flag OFF: the lane never touches the account ------
os.environ.pop("PAPER_ACCOUNT_ENABLED")
_reset_account()
_reset_lane()
opened, why, final = W.consider_fire(fire(M(20)), time.time())
snap = PA.snapshot()
check("flag off: entry leaves the shared account pristine",
      opened and snap["cash"] == 2.0
      and snap["lanes"].get("watcher", {}).get("open_count", 0) == 0)

if FAILS:
    print("\nFAILURES: %s" % FAILS)
    sys.exit(1)
print("\nALL PAPER-ACCOUNT WATCHER WIRING TESTS PASSED")
