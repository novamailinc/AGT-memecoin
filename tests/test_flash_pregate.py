#!/usr/bin/env python3
"""Tests for the flash traction pre-gate (2026-11-16, RPC-burn fix,
Lever 1).

The flash safety screen used to run on EVERY birth (~4,600/day, up
to 3 Helius calls each). The pre-gate runs first, on stream data
already in hand (zero RPC): only births showing traction are
screened. Held births are routed exactly like safety-unknown holds
(hourly UNVETTED digest, never instant, never paper) and can still
be screened later by the confirm path. The instant-CA gate
semantics are unchanged: instant drops ONLY for coins with a known
safety report, no hard fails, score >= 1/100.

Run: python3 tests/test_flash_pregate.py
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.launch_scanner import LaunchScanner
from daemon.launch_scanner import safety

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# --- A. The pre-gate itself: pure, no RPC ---------------------------
def _degen(**kw):
    base = dict(enabled=True, poll_interval_seconds=30, entry_sol=0.05,
                max_positions=5, min_usd_market_cap=10000.0,
                max_age_minutes=10.0, velocity_multiplier=3.0,
                velocity_window_minutes=10.0, stop_loss_pct=30.0,
                take_profit_pct=200.0, max_hold_hours=4.0, auto_enter=True,
                early_safety_flash=True, early_flash_timeout_s=4.0,
                early_min_score=30.0,
                early_alert_max_per_window=10,
                early_alert_window_seconds=300.0,
                feedback_enabled=False,
                digest_include_unknown_safety=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _birth(**kw):
    c = {"mint": "MINT1", "symbol": "TKN", "name": "Token",
         "chain": "solana", "source": "pumpportal-new",
         "created_ts": time.time(), "usd_market_cap": 0.0,
         "extra": {"complete": False, "reply_count": 0, "banned": False,
                   "initial_buy_sol": 0.0, "market_cap_sol": 28.0},
         "url": "https://pump.fun/coin/MINT1"}
    c.update(kw)
    return c


D = _degen()
check("pregate: fresh birth held (no buy, curve mcap ~28 SOL)",
      not LaunchScanner._flash_pregate_pass(D, _birth()))
check("pregate: creator buy 2.0 SOL passes",
      LaunchScanner._flash_pregate_pass(
          D, _birth(extra={"initial_buy_sol": 2.0,
                           "market_cap_sol": 30.0})))
check("pregate: curve mcap 40 SOL passes (no big single buy)",
      LaunchScanner._flash_pregate_pass(
          D, _birth(extra={"initial_buy_sol": 0.2,
                           "market_cap_sol": 40.0})))
check("pregate: curve mcap 35 SOL held (below the 40 bar)",
      not LaunchScanner._flash_pregate_pass(
          D, _birth(extra={"initial_buy_sol": 0.2,
                           "market_cap_sol": 35.0})))
check("pregate: curve mcap 42 SOL passes",
      LaunchScanner._flash_pregate_pass(
          D, _birth(extra={"initial_buy_sol": 0.0,
                           "market_cap_sol": 42.0})))
check("pregate: median real dev buy 0.04 SOL held (units fix)",
      not LaunchScanner._flash_pregate_pass(
          D, _birth(extra={"initial_buy_sol": 0.04,
                           "market_cap_sol": 30.0})))
check("pregate: migration always passes",
      LaunchScanner._flash_pregate_pass(
          D, _birth(source="pumpportal-migration",
                    extra={"initial_buy_sol": 0.0,
                           "market_cap_sol": 28.0})))
check("pregate: priced mcap $30k passes (non-stream source)",
      LaunchScanner._flash_pregate_pass(
          D, _birth(source="pumpfun", usd_market_cap=30000.0,
                    extra={})))
check("pregate: priced mcap $5k held",
      not LaunchScanner._flash_pregate_pass(
          D, _birth(source="pumpfun", usd_market_cap=5000.0,
                    extra={})))
check("pregate: logstream shape with no economics held",
      not LaunchScanner._flash_pregate_pass(
          D, {"source": "pumpfun-log", "extra": {},
              "usd_market_cap": 0.0}))
check("pregate: disabled knob passes everything",
      LaunchScanner._flash_pregate_pass(
          _degen(flash_pregate_enabled=False), _birth()))
check("pregate: custom thresholds honored",
      LaunchScanner._flash_pregate_pass(
          _degen(flash_pregate_min_initial_buy_sol=5.0),
          _birth(extra={"initial_buy_sol": 2.0,
                        "market_cap_sol": 28.0})) is False
      and LaunchScanner._flash_pregate_pass(
          _degen(flash_pregate_min_initial_buy_sol=5.0),
          _birth(extra={"initial_buy_sol": 6.0,
                        "market_cap_sol": 28.0})))


# --- B. _consider integration: the screen is skipped, not faked ----
def _state():
    return {"seen": {}, "degen_positions": {}, "candidates": {}}


def _consider(c, verdict=None):
    """Run _consider with _instant_verdict mocked; return
    (state, verdict_mock, stage_mock, publish_mock)."""
    from agent import events as event_bus
    sc = LaunchScanner.__new__(LaunchScanner)
    state = _state()
    cfg = SimpleNamespace(degen=_degen())
    verdict_mock = patch.object(
        LaunchScanner, "_instant_verdict",
        return_value=verdict or (True, "ok",
                                 {"passed": True, "score": 90,
                                  "fails": [], "flags": []}))
    stage_mock = patch.object(LaunchScanner, "_sniper_stage")
    rank_mock = patch.object(
        LaunchScanner, "_sniper_rank",
        return_value={"tier": "sniper", "score": 90.0, "threshold": 50.0})
    pub_mock = patch.object(event_bus, "publish")
    with verdict_mock as vm, stage_mock as sm, rank_mock, \
            pub_mock as pm:
        sc._consider(cfg, None, state, c)
    return state, vm, sm, pm


# Held birth: the screen never runs, the coin never reaches an
# instant card, it joins the UNVETTED digest instead.
state, vm, sm, pm = _consider(_birth())
check("consider: held birth never calls the flash screen",
      vm.call_count == 0)
check("consider: held birth never reaches instant staging",
      sm.call_count == 0)
check("consider: held birth never publishes an instant alert",
      pm.call_count == 0)
digest = state.get("early_digest") or []
check("consider: held birth joins the UNVETTED digest",
      len(digest) == 1 and digest[0].get("unvetted")
      and digest[0].get("reason", "").startswith("safety-unknown"))
check("consider: held birth opens no paper position",
      state.get("degen_positions") == {})
check("consider: held birth still registered as a candidate",
      "MINT1" in state.get("candidates", {}))

# Traction birth: the screen runs and the coin proceeds.
state, vm, sm, pm = _consider(
    _birth(extra={"initial_buy_sol": 2.0, "market_cap_sol": 30.0}))
check("consider: traction birth runs the flash screen",
      vm.call_count == 1)
check("consider: traction birth proceeds to instant staging",
      sm.call_count == 1)


# --- C. The confirm path still screens (pre-gate never applies) ---
def _confirm(report):
    sc = LaunchScanner.__new__(LaunchScanner)
    cand = {"chain": "solana", "mint": "MINT1", "symbol": "TKN"}
    with patch.object(safety, "check_token",
                      return_value=report) as ct:
        ok, reason, rep = sc._confirm_safety_ok(_degen(), cand)
    return ok, reason, rep, ct


ok, reason, rep, ct = _confirm({"passed": True, "unknown": False,
                                "score": 90, "fails": [], "flags": []})
check("confirm: known good report still confirms",
      ok and reason == "ok" and ct.call_count == 1)
ok, reason, rep, ct = _confirm({"passed": True, "unknown": True,
                                "score": None, "fails": [], "flags": []})
check("confirm: unknown report still screened, returned not-ok",
      (not ok) and reason == "safety-unknown (no score)"
      and ct.call_count == 1)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL FLASH PRE-GATE TESTS PASSED")
