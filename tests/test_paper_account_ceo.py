#!/usr/bin/env python3
"""Wiring tests: ceo lane <-> shared paper account (Stage 2).

Proves the actual runtime wiring, not just the account module:
with PAPER_ACCOUNT_ENABLED=1 the shared account is the authoritative
capital gate (an entry it cannot cover is REFUSED, transient, while
the lane's own $100 bankroll still has cash), the entry debits it,
every exit TRANCHE credits net proceeds immediately (the lane's own
bankroll still settles at finalize), a retried tranche op_id returns
the original outcome without double-crediting, a stale pos_id is
refused, and a legacy position opened with the flag off never touches
the account. With the flag off the lane never touches the account.

Harness mirrors tests/test_ceo.py (same fakes, same drive path).
Run: python3 tests/test_paper_account_ceo.py
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(_REPO_ROOT, "daemon", "launch_scanner"))
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests
sys.path.insert(0, _REPO_ROOT)

TMPD = tempfile.mkdtemp(prefix="paper-ceo-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="paper-ceo-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
os.environ["PAPER_ACCOUNT_GATES_ENABLED"] = "1"  # TEST FIXTURE (mandatory-guard edit): not owner values
os.environ["PAPER_ACCOUNT_MAX_DAILY_LOSS_USD"] = "1000"
os.environ["PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"] = "90"
os.environ["PAPER_ACCOUNT_MAX_EXPOSURE_PCT"] = "100"
os.environ["PAPER_ACCOUNT_MAX_SIMULTANEOUS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_POSITIONS"] = "1000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD"] = "1000000"  # TEST-FIXTURE value, not owner
os.environ["PAPER_ACCOUNT_START"] = "2"   # small: gate binds after 2

from daemon.launch_scanner import ceo as C
from daemon.launch_scanner import paper_account as PA
_gfb.install_price_feed(at_cost=True)  # mandatory-guard migration (disclosed): mechanics-only marks via real pipeline
from daemon.launch_scanner import signal_bus as BUS

C.LEDGER_PATH = os.path.join(TMPD, "ceo_ledger.json")
C.BUS_PATH = os.path.join(TMPD, "signal_bus.jsonl")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class FakeKS:
    def __init__(self):
        self.halted = set()
        self.recorded = []

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}


FAKE_KS = FakeKS()
_RUNNER_TAPE = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "usd_liquidity": 100_000.0, "usd_market_cap": 500_000.0,
                "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40,
                "dex": "pump"}


def _patch_all(price=0.01):
    p1 = patch.object(C, "_veto")
    p2 = patch.object(C, "_safety")
    p3 = patch.object(C, "_ks", FAKE_KS)
    p4 = patch.object(C, "quote_price", new=__import__("tests._guard_fixture", fromlist=["x"]).stamped_const(C, price))
    p5 = patch.object(C, "_fresh_tape", return_value=dict(_RUNNER_TAPE))
    p6 = patch.object(C, "RUNNER_CONFIRM_GAP_S", 0.0)
    m_veto, m_safety = p1.start(), p2.start()
    for p in (p3, p4, p5, p6):
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = {"passed": True, "unknown": False}
    return (p1, p2, p3, p4, p5, p6)


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.01):
    for p in (C.LEDGER_PATH, C.BUS_PATH):
        if os.path.exists(p):
            os.remove(p)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps = _patch_all(price=price)
    C.ensure_ledger()
    return ps


def _sig(mint, **kw):
    args = dict(chain="solana", source="test-feed", mint=mint,
                symbol="TKN", score=70.0, tier="pick",
                mcap=500_000.0, liquidity=50_000.0)
    args.update(kw)
    return BUS.make_signal(**args)


def _prices(seq, others=None):
    """Per-mint price feed: mints in `others` hold a flat price (no
    legs fire); the mint under test walks `seq` (last value sticks)."""
    it = iter(seq)
    others = others or {}

    def fn(chain, mint, spot_index=None):
        if mint in others:
            return others[mint]
        try:
            return next(it)
        except StopIteration:
            return seq[-1]
    return fn


def _reset_account(start="2"):
    home = tempfile.mkdtemp(prefix="paper-ceo-home-")
    os.environ["FOREX_AGENT_HOME"] = home
    os.environ["PAPER_ACCOUNT_START"] = start
    import importlib
    importlib.reload(PA)


# ---------------- flag ON: gate, debit, per-tranche settle ----------
ps = _fresh()
_reset_account()

ok, why = C.consider_card(_sig("LADP"))
snap = PA.snapshot()
check("ceo entry debits the shared account (2.00 - 1.00)",
      ok and abs(snap["cash"] - 1.0) < 1e-9)
pos = C.open_positions().get("LADP")
check("entry pins the shared position instance",
      pos is not None and pos.get("pa_pos_id", "").startswith("ceo/LADP#"))
check("lane bankroll still settles $1 as before",
      abs(C.account_cash() - 99.0) < 1e-9)

ok, why = C.consider_card(_sig("KEEP1"))
check("second entry drains the shared cash to 0",
      ok and abs(PA.snapshot()["cash"]) < 1e-9)

ok, why = C.consider_card(_sig("POOL1"))
check("third entry REFUSED by the shared account (transient)",
      not ok and "paper account" in why)
check("refused entry costs the lane nothing",
      abs(C.account_cash() - 98.0) < 1e-9
      and "POOL1" not in C.open_positions())

# Exit LADP through the real ladder: leg A at 2x settles its tranche
# into shared cash IMMEDIATELY, while the lane bankroll still waits
# for finalize.
entry = C.open_positions()["LADP"]["entry_price"]
tokens = C.open_positions()["LADP"]["tokens"]
keep_entry = C.open_positions()["KEEP1"]["entry_price"]
flat = {"KEEP1": keep_entry}
C.poll_exits(price_fn=_prices([entry * 2.0], others=flat))
pos = C.open_positions()["LADP"]
snap = PA.snapshot()
leg_a = pos["legs"]["a"]["realized_usd"]
check("leg A done at 2x", pos["legs"]["a"]["done"])
check("leg A tranche credited to shared cash immediately",
      abs(snap["cash"] - leg_a) < 1e-6 and leg_a > 0)
check("lane bankroll NOT settled before finalize (still 98)",
      abs(C.account_cash() - 98.0) < 1e-9)
apos = [p for p in snap["lanes"]["ceo"]["positions"]
        if p["mint"] == "LADP"][0]
check("shared position shrank to the remaining units + basis",
      abs(apos["qty"] - tokens * 0.5) < 1e-6
      and abs(apos["basis"] - 0.5) < 1e-9)

C.poll_exits(price_fn=_prices([entry * 3.0], others=flat))  # leg B
peak = entry * 3.4
C.poll_exits(price_fn=_prices([peak], others=flat))
closes = C.poll_exits(price_fn=_prices([peak * 0.69], others=flat))
check("ladder completes and finalizes the lane position",
      "LADP" not in C.open_positions() and len(closes) == 1)
rec = closes[0]
snap = PA.snapshot()
check("shared cash == total realized for the closed position",
      abs(snap["cash"] - rec["realized_usd"]) < 1e-6)
check("no ceo/LADP position left in the shared account",
      all(p["mint"] != "LADP"
          for p in snap["lanes"]["ceo"]["positions"]))
check("shared realized P/L matches the lane's booked pnl",
      abs(snap["lanes"]["ceo"]["realized_pnl"]
          - (rec["realized_usd"] - 1.0)) < 1e-6)
check("KEEP1 still open at full basis (untouched by LADP's close)",
      any(p["mint"] == "KEEP1" and abs(p["basis"] - 1.0) < 1e-9
          for p in snap["lanes"]["ceo"]["positions"]))

# Retry of an uncertain tranche response: same op_id returns the
# ORIGINAL outcome, no double credit, even with different amounts.
before = PA.snapshot()["cash"]
ok1, _ = PA.credit("ceo", "LADP", 999.0,
                   "ceo:leg:a:LADP:%s" % pos["pa_pos_id"],
                   pos_id=pos["pa_pos_id"], qty=1.0)
check("tranche op_id retry returns the original outcome",
      ok1 and abs(PA.snapshot()["cash"] - before) < 1e-9)

# Stale pos_id (a later reopen's instance) is refused, never settled.
ok2, why2 = PA.credit("ceo", "KEEP1", 5.0, "ceo:test:stale-pos",
                      pos_id="ceo/KEEP1#99", qty=1.0)
check("stale pos_id refused (position instance mismatch)",
      not ok2 and abs(PA.snapshot()["cash"] - before) < 1e-9)
_unpatch(ps)

# ---------------- flag OFF: the lane never touches the account ------
os.environ.pop("PAPER_ACCOUNT_ENABLED")
_reset_account()
ps = _fresh()
ok, why = C.consider_card(_sig("OFFM"))
snap = PA.snapshot()
# CONTRACT CHANGE (owner directive: one shared $10, no private capital):
# flag off REFUSES entry; there is no private position to close.
check("flag off: entry REFUSED by the mandatory guard, account pristine",
      (not ok) and "mandatory entry guard" in str(why)
      and "OFFM" not in C.open_positions() and snap["cash"] == 2.0
      and snap["lanes"].get("ceo", {}).get("open_count", 0) == 0
      and not C.open_positions())
_unpatch(ps)

# ---------------- exit/credit is NEVER blocked by the entry guard ----
# (replaces the exit coverage of the old private-position close check)
# A legitimately FUNDED position (opened with the mandatory gates on),
# then gates turned OFF: the hard-stop exit must still close it and credit
# the shared account. Entries are blocked fail-closed; exits never are.
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
_gfb.set_limits_env()
_reset_account()
ps = _fresh()
ok_e, why_e = C.consider_card(_sig("EXITM"))
funded = PA.snapshot()
entry = C.open_positions()["EXITM"]["entry_price"] if ok_e else None
check("control: funded entry opens through the guard (gates on)",
      ok_e and abs(funded["cash"] - 1.0) < 1e-9)
os.environ.pop("PAPER_ACCOUNT_GATES_ENABLED")
ok_b, why_b = C.consider_card(_sig("BLOCKM"))
check("gates now off: a NEW entry is refused fail-closed",
      (not ok_b) and "BLOCKM" not in C.open_positions())
C.poll_exits(price_fn=_prices([entry * 0.4]))   # hard stop
after = PA.snapshot()
check("gates off: exit still closes the funded position",
      "EXITM" not in C.open_positions()
      and after["lanes"].get("ceo", {}).get("open_count", 0) == 0)
check("gates off: exit credit reaches the shared account (cash = 2.00 + "
      "realized P/L, a loss at the 0.4x hard stop)",
      abs(after["cash"] - (2.0 + after["realized_pnl"])) < 1e-9
      and after["realized_pnl"] < 0 and after["cash"] > 1.0 + 1e-9)
_unpatch(ps)

if FAILS:
    print("\nFAILURES: %s" % FAILS)
    sys.exit(1)
print("\nALL PAPER-ACCOUNT CEO WIRING TESTS PASSED")
