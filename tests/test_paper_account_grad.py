#!/usr/bin/env python3
"""Wiring tests: grad lane <-> shared paper account (Stage 1).

Proves the actual runtime wiring, not just the account module:
with PAPER_ACCOUNT_ENABLED=1 the shared account is the
authoritative capital gate — an entry that the shared cash cannot
cover is REFUSED (fail-closed, transient), a covered entry debits
the shared account, and the close credits the exact realized
proceeds. With the flag off the lane never touches the account.

Harness mirrors tests/test_grad.py (same fakes, same drive path).
Run: python3 tests/test_paper_account_grad.py
"""
import importlib
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TMPD = tempfile.mkdtemp(prefix="paper-grad-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="paper-grad-home-")

import daemon.launch_scanner.grad as G
import daemon.launch_scanner.paper_account as PA

G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")

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

    def halt_lane(self, lane, reason=""):
        self.halted.add(lane)
        return {"lane": lane, "action": "HALTED"}


FAKE_KS = FakeKS()
SAFETY_OK = {"passed": True, "unknown": False, "score": 7,
             "flags": [], "fails": [], "source": "rugcheck"}
TAPE_OK = {"usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
           "chg_m5": 3.0, "price_usd": 0.00007,
           "tape_source": "dexscreener"}


def _patch_all(price=0.00007):
    p1 = patch.object(G, "_veto")
    p2 = patch.object(G, "_safety")
    p3 = patch.object(G, "_ks", FAKE_KS)
    p4 = patch.object(G, "quote_price", return_value=price)
    p5 = patch.object(G, "_fresh_tape", return_value=dict(TAPE_OK))
    p6 = patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)
    m_veto, m_safety = p1.start(), p2.start()
    for p in (p3, p4, p5, p6):
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = dict(SAFETY_OK)
    return (p1, p2, p3, p4, p5, p6), m_veto, m_safety


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.00007):
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps, m_veto, m_safety = _patch_all(price=price)
    G.ensure_ledger()
    return ps, m_veto, m_safety


def _cand(mint="AAA111pump", age_s=60.0, dex="pumpswap", **kw):
    c = {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
         "chain": "solana", "dex": dex,
         "created_ts": time.time() - age_s,
         "pool_address": "POOL1", "reserve_usd": 6_000.0,
         "fdv_usd": 69_000.0, "price_usd": 0.00007,
         "source": "gecko-new-pool"}
    c.update(kw)
    return c


def _close_open(mint, mult=2.0):
    """Drive a full close of the open position at `mult` x entry,
    the same path the lane's monitor uses."""
    with G._ledger_lock():
        d = G._read()
        pos = d["positions"][mint]
        exit_price = pos["entry_price"] * mult
        G._close_leg(pos, "a", exit_price, "test")
        G._close_leg(pos, "b", exit_price, "test")
        G._close_leg(pos, "c", exit_price, "test")
        G._finalize(d, mint, pos, "test")
        G._write(d)


# ---- flag OFF: lane never touches the account ---------------------
os.environ.pop("PAPER_ACCOUNT_ENABLED", None)
ps, _mv, _ms = _fresh()
opened, why, final = G.consider_candidate(_cand(mint="OFF111pump"))
check("flag off: entry works on the private bankroll",
      opened and "OFF111pump" in G._load()["positions"])
check("flag off: shared account untouched (no ledger dir)",
      not os.path.exists(PA.LEDGER_PATH))
_unpatch(ps)

# ---- flag ON: shared cash gates entries ----------------------------
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
importlib.reload(PA)  # picks up FOREX_AGENT_HOME paths as-is
ps, _mv, _ms = _fresh()
# Drain the shared account to $1.50 via a direct debit so the next
# $1 grad entry fits but a second one cannot.
ok, _, _pid = PA.try_debit("other-lane", "DRAIN1", 8.50)
check("fixture: shared account drained to $1.50",
      ok and PA.snapshot()["cash"] == 1.50)

opened1, why1, _f = G.consider_candidate(_cand(mint="PAY111pump"))
snap = PA.snapshot()
check("flag on: first entry enters and debits the shared account",
      opened1 and snap["cash"] == 0.50
      and snap["lanes"]["grad"]["open_count"] == 1)

opened2, why2, final2 = G.consider_candidate(_cand(mint="POOR11pump"))
check("flag on: entry refused when shared cash is short "
      "(fail-closed, transient, not final)",
      not opened2 and not final2 and "paper account" in why2)
check("flag on: refused entry never opened a lane position",
      "POOR11pump" not in G._load()["positions"])
check("flag on: private bankroll unchanged by the refusal",
      G._load()["bankroll"] == 100.00 - 1.00 or
      abs(G._load()["bankroll"] - (100.00 - 1.0)) < 1e-6)

# ---- close settles into the shared account -------------------------
before = PA.snapshot()["cash"]
_close_open("PAY111pump", mult=2.0)
snap = PA.snapshot()
check("flag on: close credits realized proceeds to the account",
      snap["cash"] > before
      and snap["lanes"]["grad"]["open_count"] == 0
      and snap["lanes"]["grad"]["n_closes"] == 1)
check("account/lane agree on the close direction (profitable)",
      snap["lanes"]["grad"]["realized_pnl"] > 0)
_unpatch(ps)

# ---- orphan close: position predating the flag ----------------------
ps, _mv, _ms = _fresh()
opened3, _w, _f = G.consider_candidate(_cand(mint="LEGACY1pump"))
check("fixture: legacy entry opened with flag on", opened3)
# Simulate a position opened before the flag existed by removing
# its shared-account record, then closing.
with PA._lock():
    led, healthy, _degraded = PA._load_locked()
    led["lanes"]["grad"]["open"].pop("LEGACY1pump", None)
    PA._save_locked(led)
_close_open("LEGACY1pump", mult=1.5)
journal = open(PA.JOURNAL_PATH).read()
check("orphan close: lane settles anyway, account journals it",
      "credit_orphan" in journal)
_unpatch(ps)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PAPER-ACCOUNT WIRING TESTS PASSED")
