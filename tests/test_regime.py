#!/usr/bin/env python3
"""Regime-aware stake scaling + sit-out rule (advanced build,
piece 2, 2026-11-21).

Covers regime.py: assess (HOT/COLD/NORMAL/insufficient_data),
stake multipliers, effective stakes, effective MAX_OPEN, and the
sit-out rule. Pure unit tests on fixture closes — no network, no
live ledgers. Plain script — run directly with the venv python,
not pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

import tests._guard_fixture as _gfb  # noqa: E402
_gfb.block_network()  # before any daemon import
from daemon.launch_scanner import regime as R  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def _closes(pnls, start_ts=1700000000.0):
    return [{"pnl_usd": p, "close_ts": start_ts + i * 3600.0}
            for i, p in enumerate(pnls)]


# ---------------- assess -------------------------------------------
# HOT: 40 wins of +25 and 10 losses of -5 = net +950, mean +$19,
# win 80%, recent net positive — above the HOT bar (+$15, 55%).
hot2 = [25.0] * 40 + [-5.0] * 10
r = R.assess(_closes(hot2))
check("assess: hot regime detected (mean +$19, win 80%)",
      r["regime"] == "HOT" and r["reason"] == "hot_regime")
check("assess: hot win rate 0.80",
      abs(r["win_rate"] - 0.80) < 0.01)

# COLD via negative expectancy.
cold = [-10.0] * 30 + [5.0] * 20
r = R.assess(_closes(cold))
check("assess: cold via negative expectancy",
      r["regime"] == "COLD")

# COLD via recent bleed (last 20 net < -$50) even when the
# 50-close expectancy is positive.
cold_recent = [10.0] * 30 + [-10.0] * 20
r = R.assess(_closes(cold_recent))
check("assess: cold via recent bleed (last 20 net -$200)",
      r["regime"] == "COLD" and r["recent_net"] == -200.0)

# COLD via low win rate.
cold_wr = [1.0] * 15 + [-1.0] * 35
r = R.assess(_closes(cold_wr))
check("assess: cold via win rate 30%",
      r["regime"] == "COLD" and abs(r["win_rate"] - 0.30) < 0.01)

# NORMAL: alternating wins/losses, mean +$2.50, win 50%,
# recent 20 net +$50 — positive expectancy below the hot bar,
# no cold trigger.
normal = [10.0, -5.0] * 25
r = R.assess(_closes(normal))
check("assess: normal regime (mean +$2.50, win 50%)",
      r["regime"] == "NORMAL")

# Insufficient data: fewer than 20 closes -> NORMAL.
r = R.assess(_closes([-10.0] * 10))
check("assess: <20 closes is NORMAL insufficient_data",
      r["regime"] == "NORMAL" and r["reason"] == "insufficient_data")
r = R.assess([])
check("assess: empty closes is NORMAL",
      r["regime"] == "NORMAL" and r["n"] == 0)
r = R.assess(None)
check("assess: None closes never raises",
      r["regime"] == "NORMAL")

# ---------------- multipliers / stakes ----------------------------
check("multiplier: COLD 0.5", R.stake_multiplier("COLD") == 0.5)
check("multiplier: NORMAL 1.0",
      R.stake_multiplier("NORMAL") == 1.0)
check("multiplier: HOT 1.5", R.stake_multiplier("HOT") == 1.5)
check("multiplier: unknown fails safe to 1.0",
      R.stake_multiplier("BOGUS") == 1.0)
check("multiplier: None fails safe to 1.0",
      R.stake_multiplier(None) == 1.0)

check("stake: $25 COLD -> $12.50",
      R.effective_stake(25.00, "COLD") == 12.50)
check("stake: $25 NORMAL -> $25.00",
      R.effective_stake(25.00, "NORMAL") == 25.00)
check("stake: $25 HOT -> $37.50",
      R.effective_stake(25.00, "HOT") == 37.50)
check("stake: $1 COLD -> $0.50",
      R.effective_stake(1.00, "COLD") == 0.50)
check("stake: $1 HOT -> $1.50",
      R.effective_stake(1.00, "HOT") == 1.50)
check("stake: tiny base floors at $0.50",
      R.effective_stake(0.10, "COLD") == 0.50)
check("stake: zero base is 0.0",
      R.effective_stake(0.0, "HOT") == 0.0)

check("max_open: 20 COLD -> 10",
      R.effective_max_open(20, "COLD") == 10)
check("max_open: 20 NORMAL -> 20",
      R.effective_max_open(20, "NORMAL") == 20)
check("max_open: 20 HOT -> 20 (hot never adds capacity)",
      R.effective_max_open(20, "HOT") == 20)
check("max_open: COLD floors at 5",
      R.effective_max_open(8, "COLD") == 5)

# ---------------- sit-out -----------------------------------------
# COLD + 5 straight losses -> sit out (loss_streak).
so = _closes([-10.0] * 30 + [5.0] * 15 + [-10.0] * 5)
r = R.sit_out(so)
check("sit_out: COLD + 5 straight losses sits out",
      r["sit_out"] is True and r["reason"] == "loss_streak")
check("sit_out: loss streak counted",
      r["evidence"]["loss_streak"] == 5)

# COLD + last-10 net < -$100 -> sit out (recent_bleed), even
# when the last 5 are not all losses.
so2 = _closes([-10.0] * 30 + [5.0] * 10 + [-15.0] * 9 + [5.0])
r = R.sit_out(so2)
check("sit_out: COLD + last-10 net -$130 sits out",
      r["sit_out"] is True and r["reason"] == "recent_bleed")

# COLD but not bleeding -> no sit-out (halved size instead).
so3 = _closes([-10.0] * 30 + [5.0] * 19 + [5.0])
r = R.sit_out(so3)
check("sit_out: COLD but not bleeding does NOT sit out",
      r["sit_out"] is False
      and r["reason"] == "cold_but_not_bleeding")

# NORMAL + 5 straight losses -> no sit-out (regime not cold).
so4 = _closes([10.0] * 40 + [-5.0] * 5)
r = R.sit_out(so4)
check("sit_out: NORMAL + 5 losses does NOT sit out",
      r["sit_out"] is False and r["reason"] == "regime_not_cold")

# HOT -> no sit-out.
r = R.sit_out(_closes(hot2))
check("sit_out: HOT never sits out", r["sit_out"] is False)

# Insufficient data -> no sit-out.
r = R.sit_out(_closes([-10.0] * 10))
check("sit_out: <20 closes never sits out",
      r["sit_out"] is False)

# ---------------- bounded sit-out (fix 2026-11-22) -----------------
# A sit-out that has run past SIT_OUT_MAX_SECONDS (12h) lifts
# even though the evidence is unchanged: while sitting out no
# new closes can land, so the frozen evidence could never
# recover on its own (the 2026-11-21 latch).
_now = 1700000000.0 + 50 * 3600.0
check("sit_out: 12h bound constant",
      R.SIT_OUT_MAX_SECONDS == 12 * 3600)
r = R.sit_out(so, now=_now,
              sit_out_started_ts=_now - 13 * 3600)
check("sit_out: 13h-old sit-out expires (lifts)",
      r["sit_out"] is False and r["reason"] == "sit_out_expired")
r = R.sit_out(so, now=_now,
              sit_out_started_ts=_now - 12 * 3600)
check("sit_out: exactly 12h old expires (bound is >=)",
      r["sit_out"] is False and r["reason"] == "sit_out_expired")
r = R.sit_out(so, now=_now,
              sit_out_started_ts=_now - 11 * 3600)
check("sit_out: 11h-old sit-out still active",
      r["sit_out"] is True and r["reason"] == "loss_streak")
# No start given -> original behaviour (unbounded callers such
# as grad.py keep the old semantics).
r = R.sit_out(so, now=_now)
check("sit_out: no start given keeps original behaviour",
      r["sit_out"] is True and r["reason"] == "loss_streak")

# Fresh evidence: only closes after the baseline count toward a
# re-trigger, so the stale closes that caused an expired
# sit-out cannot re-engage it.
_old_last_ts = 1700000000.0 + 24 * 3600.0
r = R.sit_out(so, now=_now, fresh_after_ts=_old_last_ts + 25 * 3600.0)
check("sit_out: stale closes fenced off by baseline do NOT "
      "re-trigger",
      r["sit_out"] is False)
_old = _closes([5.0] * 25, start_ts=1700000000.0)
_fresh = _closes([-10.0] * 25,
                 start_ts=1700000000.0 + 25 * 3600.0)
r = R.sit_out(_old + _fresh, now=_now,
              fresh_after_ts=1700000000.0 + 24 * 3600.0)
check("sit_out: fresh losing closes after baseline DO re-trigger",
      r["sit_out"] is True and r["reason"] == "loss_streak")

print("")
if fails:
    print("FAILURES: %d — %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("ALL REGIME TESTS PASSED")


# ---------------- integration: grad.py stakes ----------------------
# Drive _consider_candidate with the patch pattern from
# tests/test_grad.py (FakeKS + stubbed veto/safety/quote/tape),
# seeding the ledger closes so the regime is known, and assert
# the position opens at the regime stake with the regime
# journaled into entry_features.
import tempfile  # noqa: E402
import time  # noqa: E402
from unittest.mock import patch  # noqa: E402
from importlib.machinery import SourceFileLoader  # noqa: E402
import importlib.util  # noqa: E402

# Mandatory-guard migration (DISCLOSED test edit, 2026-11-24): grad entries
# now require the shared paper account + gates. This integration section
# was never migrated and failed at baseline 28499f5/50367bb with "shared
# paper account not enabled". Fixture values only, NOT owner values; the
# starting balance is large so the regime stakes ($12.50/$25/$37.50) fit.
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="regime-home-")
os.environ["PAPER_ACCOUNT_START"] = "1000"  # TEST-FIXTURE value
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
for _k, _v in {"PAPER_ACCOUNT_GATES_ENABLED": "1", "PAPER_ACCOUNT_MAX_DAILY_LOSS_USD": "100000",
               "PAPER_ACCOUNT_MAX_DRAWDOWN_PCT": "99", "PAPER_ACCOUNT_MAX_EXPOSURE_PCT": "100",
               "PAPER_ACCOUNT_MAX_SIMULTANEOUS": "1000", "PAPER_ACCOUNT_MAX_POSITIONS": "1000", "PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD": "1000000",
               "PAPER_ACCOUNT_MAX_SIMULTANEOUS": "100000", "PAPER_ACCOUNT_MAX_DATA_AGE_S": "100000", "PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD": "1000000",
               "PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD": "1000000"}.items():
    os.environ[_k] = _v  # TEST-FIXTURE values, not owner
import importlib as _il  # noqa: E402
import daemon.launch_scanner.paper_account as _PA  # noqa: E402
_il.reload(_PA)
_gfb.install_price_feed(at_cost=True)
from daemon.launch_scanner import grad as G  # noqa: E402

_TMPD = tempfile.mkdtemp(prefix="regime-grad-")
_G_LEDGER = os.path.join(_TMPD, "grad_ledger.json")
G.LEDGER_PATH = _G_LEDGER
_ORIG_NOTIONAL = G.NOTIONAL_USD
_ORIG_MAX_OPEN = G.MAX_OPEN
G.NOTIONAL_USD = 25.00
G.MAX_OPEN = 20


class _FakeKS:
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


_FAKE_KS = _FakeKS()
_SAFETY_OK = {"passed": True, "unknown": False, "score": 7,
              "flags": [], "fails": [], "source": "rugcheck"}
_TAPE_OK = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "usd_liquidity": 8000.0, "usd_market_cap": 69000.0,
            "chg_m5": 3.0, "price_usd": 0.00007,
            "tape_source": "dexscreener"}


def _reg_fresh(price=0.00007):
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
    _FAKE_KS.halted = set()
    _FAKE_KS.recorded = []
    ps = []
    p1 = patch.object(G, "_veto")
    p2 = patch.object(G, "_safety")
    p3 = patch.object(G, "_ks", _FAKE_KS)
    p4 = patch.object(G, "quote_price", return_value=price)
    p5 = patch.object(G, "_fresh_tape", return_value=dict(_TAPE_OK))
    p6 = patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)
    m_veto, m_safety = p1.start(), p2.start()
    for p in (p3, p4, p5, p6):
        p.start()
    ps.extend([p1, p2, p3, p4, p5, p6])
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = dict(_SAFETY_OK)
    G.ensure_ledger()
    return ps


def _reg_unpatch(ps):
    for p in ps:
        p.stop()


def _reg_cand(mint="REG111pump", age_s=60.0):
    return {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
            "chain": "solana", "dex": "pumpswap",
            "created_ts": time.time() - age_s,
            "pool_address": "POOL1", "reserve_usd": 6000.0,
            "fdv_usd": 69000.0, "price_usd": 0.00007,
            "source": "gecko-new-pool"}


def _reg_seed_closes(pnls):
    with G._ledger_lock():
        d = G._read()
        d["closes"] = [
            {"mint": "seed%03d" % i, "pnl_usd": p,
             "close_ts": 1700000000.0 + i * 3600.0,
             "entry_ts": 1700000000.0 + i * 3600.0 - 60.0}
            for i, p in enumerate(pnls)]
        G._write(d)


def _reg_position(mint):
    return G._load()["positions"].get(mint)


# COLD regime (45 x -$10, 5 x +$5: expectancy -$8.50, last 5
# win so no sit-out): opens at $12.50.
ps = _reg_fresh()
try:
    _reg_seed_closes([-10.0] * 45 + [5.0] * 5)
    opened, why, final = G.consider_candidate(
        _reg_cand(mint="REG111pump"))
    check("integration: COLD regime opens at $12.50",
          opened is True)
    pos = _reg_position("REG111pump")
    check("integration: COLD position notional is $12.50",
          pos is not None and pos.get("notional_usd") == 12.50)
    check("integration: COLD regime journaled in entry_features",
          pos is not None
          and (pos.get("entry_features") or {}).get(
              "regime", {}).get("regime") == "COLD")
    check("integration: COLD stake journaled",
          pos is not None
          and (pos.get("entry_features") or {}).get(
              "stake_usd") == 12.50)
finally:
    _reg_unpatch(ps)

# HOT regime (40 x +$25, 10 x -$5): opens at $37.50.
ps = _reg_fresh()
try:
    _reg_seed_closes([25.0] * 40 + [-5.0] * 10)
    opened, why, final = G.consider_candidate(
        _reg_cand(mint="REG222pump"))
    check("integration: HOT regime opens at $37.50",
          opened is True)
    pos = _reg_position("REG222pump")
    check("integration: HOT position notional is $37.50",
          pos is not None and pos.get("notional_usd") == 37.50)
    check("integration: HOT regime journaled",
          pos is not None
          and (pos.get("entry_features") or {}).get(
              "regime", {}).get("regime") == "HOT")
finally:
    _reg_unpatch(ps)

# NORMAL regime: opens at the $25 base.
ps = _reg_fresh()
try:
    _reg_seed_closes([10.0, -5.0] * 25)
    opened, why, final = G.consider_candidate(
        _reg_cand(mint="REG333pump"))
    check("integration: NORMAL regime opens at $25.00",
          opened is True)
    pos = _reg_position("REG333pump")
    check("integration: NORMAL position notional is $25.00",
          pos is not None and pos.get("notional_usd") == 25.00)
finally:
    _reg_unpatch(ps)

# Sit-out (COLD + 5 straight losses): no entry, retryable.
ps = _reg_fresh()
try:
    _reg_seed_closes([-10.0] * 50)
    opened, why, final = G.consider_candidate(
        _reg_cand(mint="REG444pump"))
    check("integration: sit-out refuses entry",
          opened is False and "sit-out" in str(why))
    check("integration: sit-out is retryable (final=False)",
          final is False)
    check("integration: sit-out leaves no position",
          _reg_position("REG444pump") is None)
finally:
    _reg_unpatch(ps)

G.NOTIONAL_USD = _ORIG_NOTIONAL
G.MAX_OPEN = _ORIG_MAX_OPEN

# ---------------- integration: Live10 mirror -----------------------
# The demo stays exactly $10/trade: a $12.50 COLD paper loss of
# -$6.25 (-50%) mirrors as a -$5.00 demo loss (-50% of $10).
_spec = importlib.util.spec_from_loader(
    "live10_mirror",
    SourceFileLoader("live10_mirror",
                     os.path.join(REPO, "scripts",
                                  "live10-demo-mirror")))
MIR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MIR)

check("mirror: $25 paper pnl scales 0.4x",
      MIR.scaled_pnl(10.0, 25.00) == 4.0)
check("mirror: $12.50 paper pnl normalizes to $10 demo (x0.8)",
      MIR.scaled_pnl(-6.25, 12.50) == -5.0)
check("mirror: $37.50 paper pnl normalizes (x0.2667)",
      abs(MIR.scaled_pnl(15.0, 37.50) - 4.0) < 0.001)
check("mirror: legacy close without notional scales 0.4x",
      MIR.scaled_pnl(10.0) == 4.0)
check("mirror: None notional scales 0.4x",
      MIR.scaled_pnl(10.0, None) == 4.0)

_led = {"lanes": {"grad_select": {"balance": 10.0, "closes": 0,
                                  "funded_closes": 0, "wins": 0,
                                  "net": 0.0}},
        "combined": {"observed": 0, "closes": 0, "wins": 0,
                     "net": 0.0},
        "seen": [], "mirrored": []}
_close = {"mint": "MIR111pump", "symbol": "MIR",
          "close_ts": 1700000000.0, "pnl_usd": -6.25,
          "entry_notional_usd": 12.50,
          "closed_reason": "hard_stop"}
_entry = MIR.apply_close(_led, "grad_select", _close)
check("mirror: apply_close mirrors a COLD close at -$5.00",
      _entry is not None and _entry["scaled_pnl"] == -5.0
      and _entry["contributed"] == -5.0)
check("mirror: demo balance moves by the normalized amount",
      abs(_led["lanes"]["grad_select"]["balance"] - 5.0) < 0.001)

print("")
if fails:
    print("FAILURES: %d — %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("ALL REGIME + INTEGRATION TESTS PASSED")
