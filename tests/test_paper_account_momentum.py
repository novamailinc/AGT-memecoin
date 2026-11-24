#!/usr/bin/env python3
"""Wiring tests: momentum lane <-> shared paper account.

Same contract proof as the ceo/watcher wiring tests: with
PAPER_ACCOUNT_ENABLED=1 the shared account is the authoritative
capital gate (refusal transient, lane bankroll untouched), entries
debit it, every exit tranche credits net proceeds immediately while
the lane's own bankroll still settles at finalize, tranche op_id
retries return the original outcome, and with the flag off the lane
never touches the account.

Harness mirrors tests/test_momentum_lane.py. Run:
python3 tests/test_paper_account_momentum.py
"""
import importlib
import os
import sys
import tempfile
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "daemon", "launch_scanner"))
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests
sys.path.insert(0, _REPO_ROOT)

TMPD = tempfile.mkdtemp(prefix="paper-mom-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="paper-mom-home-")
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
os.environ["PAPER_ACCOUNT_START"] = "2"

from daemon.launch_scanner import momentum_lane as M
from daemon.launch_scanner import paper_account as PA
_gfb.install_price_feed(at_cost=True)  # mandatory-guard migration (disclosed): mechanics-only marks via real pipeline

M.LEDGER_PATH = os.path.join(TMPD, "momentum_ledger.json")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


GOOD_TAPE = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0,
             "chg_m5": 5.0, "price_usd": 0.001,
             "tape_source": "test"}
QUOTES = {}


M._fresh_tape = lambda chain, mint: dict(GOOD_TAPE)
M.quote_price = lambda chain, mint, spot_index=None: \
    QUOTES.get(mint, 0.001)
import tests._guard_fixture as _GFQ  # after sys.path setup
M.quote_price = _GFQ.stamped_quote(M, M.quote_price)  # fixture: quote stamp at call time


class _SafetyStub:
    def check_token(self, chain, mint, timeout=10):
        return {"passed": True, "score": 80, "flags": [], "fails": [],
                "unknown": False}


class _VetoStub:
    def veto_check(self, mint, chain, symbol="", safety_report=None,
                   run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [],
                "details": {}}


M._safety = _SafetyStub()
M._veto = _VetoStub()


def runner(mint, **kw):
    d = {"pool_address": "Pool" + mint[:6], "mint": mint,
         "symbol": "RUN", "name": "RUN", "chain": "solana",
         "source": "gecko-trending", "lane": "lowcap", "url": "",
         "chg_24h_pct": 55.0, "chg_1h_pct": 8.0,
         "vol_24h_usd": 250000.0, "liq_usd": 100000.0,
         "usd_market_cap": 500000.0}
    d.update(kw)
    return d


def _reset_lane():
    if os.path.exists(M.LEDGER_PATH):
        os.remove(M.LEDGER_PATH)
    M.ensure_ledger()


def _reset_account(start="2"):
    os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
        prefix="paper-mom-home-")
    os.environ["PAPER_ACCOUNT_START"] = start
    importlib.reload(PA)


def MINT(i):
    return "MomMint%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d" % ((i,) * 16)


# ---------------- flag ON: gate, debit, per-tranche settle ----------
_reset_lane()
_reset_account()

opened, why, final = M.consider_candidate(runner(MINT(1)), time.time())
snap = PA.snapshot()
check("entry debits the shared account (2.00 - 1.00)",
      opened and abs(snap["cash"] - 1.0) < 1e-9)
pos = M.open_positions().get(MINT(1))
check("entry pins the shared position instance",
      pos is not None
      and pos.get("pa_pos_id", "").startswith("momentum/%s#" % MINT(1)))

opened2, _, _ = M.consider_candidate(runner(MINT(2)), time.time())
check("second entry drains the shared cash to 0",
      opened2 and abs(PA.snapshot()["cash"]) < 1e-9)

opened3, why3, final3 = M.consider_candidate(runner(MINT(3)),
                                             time.time())
check("third entry REFUSED by the shared account (transient)",
      not opened3 and "paper account" in why3 and not final3)
check("refused entry costs the lane nothing",
      abs(M.account_state()["bankroll"] - 98.0) < 1e-9
      and MINT(3) not in M.open_positions())

# Exit MINT(1) through the ladder: leg A at 2x settles its tranche
# into shared cash immediately; the lane bankroll waits for finalize.
entry = M.open_positions()[MINT(1)]["entry_price"]
tokens = M.open_positions()[MINT(1)]["tokens"]
keep_entry = M.open_positions()[MINT(2)]["entry_price"]
M.poll_exits(price_fn=lambda c, m, s:
             entry * 2.1 if m == MINT(1) else keep_entry)
pos = M.open_positions()[MINT(1)]
snap = PA.snapshot()
leg_a = pos["legs"]["a"]["realized_usd"]
check("leg A done at 2x", pos["legs"]["a"]["done"])
check("leg A tranche credited to shared cash immediately",
      abs(snap["cash"] - leg_a) < 1e-6 and leg_a > 0)
check("lane bankroll NOT settled before finalize (still 98)",
      abs(M.account_state()["bankroll"] - 98.0) < 1e-9)
mpos = [p for p in snap["lanes"]["momentum"]["positions"]
        if p["mint"] == MINT(1)][0]
check("shared position shrank to the remaining units + basis",
      abs(mpos["qty"] - tokens * 0.5) < 1e-6
      and abs(mpos["basis"] - 0.5) < 1e-9)

M.poll_exits(price_fn=lambda c, m, s:
             entry * 3.1 if m == MINT(1) else keep_entry)   # leg B
M.poll_exits(price_fn=lambda c, m, s:
             entry * 3.4 if m == MINT(1) else keep_entry)   # peak
closes = M.poll_exits(price_fn=lambda c, m, s:
                      entry * 3.4 * 0.69 if m == MINT(1)
                      else keep_entry)
check("ladder completes and finalizes the lane position",
      MINT(1) not in M.open_positions() and len(closes) == 1)
rec = closes[0]
snap = PA.snapshot()
check("shared cash == total realized for the closed position",
      abs(snap["cash"] - rec["realized_usd"]) < 1e-6)
check("no momentum/MINT(1) position left in the shared account",
      all(p["mint"] != MINT(1)
          for p in snap["lanes"]["momentum"]["positions"]))
check("MINT(2) still open at full basis (untouched)",
      any(p["mint"] == MINT(2) and abs(p["basis"] - 1.0) < 1e-9
          for p in snap["lanes"]["momentum"]["positions"]))

before = PA.snapshot()["cash"]
ok1, _ = PA.credit("momentum", MINT(1), 999.0,
                   "momentum:leg:a:%s:%s" % (MINT(1), pos["pa_pos_id"]),
                   pos_id=pos["pa_pos_id"], qty=1.0)
check("tranche op_id retry returns the original outcome",
      ok1 and abs(PA.snapshot()["cash"] - before) < 1e-9)

# ---------------- flag OFF: the lane never touches the account ------
os.environ.pop("PAPER_ACCOUNT_ENABLED")
_reset_account()
_reset_lane()
opened, why, final = M.consider_candidate(runner(MINT(20)),
                                          time.time())
snap = PA.snapshot()
check("flag off: entry leaves the shared account pristine",
      (not opened) and "mandatory entry guard" in str(why)
      and snap["cash"] == 2.0
      and snap["lanes"].get("momentum", {}).get("open_count", 0) == 0)

if FAILS:
    print("\nFAILURES: %s" % FAILS)
    sys.exit(1)
print("\nALL PAPER-ACCOUNT MOMENTUM WIRING TESTS PASSED")
