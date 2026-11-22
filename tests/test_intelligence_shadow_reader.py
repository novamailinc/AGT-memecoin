#!/usr/bin/env python3
"""Read-only shadow reader tests (Phase 13 finalize, Phase 11
live-wiring follow-up).

Covers daemon/intelligence/signals/shadow_reader.py:
  * the standalone loader guard (veto.py is refused — it has
    relative imports that only resolve inside the monolith;
    conviction / regime / instant_safety load without importing
    daemon.launch_scanner);
  * the pure translators (veto result, safety report, grad_select
    filters, conviction tiers, deployer band, regime sit-out);
  * signals_for_close on a stored close record (read-only);
  * close_signal_provider reading lane ledgers read-only.

No network (live_safety_observation is never called here), no
live files, no launch_scanner import. Plain script, run directly
with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_intelligence_shadow_reader.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.intelligence.signals import shadow_reader as SR

FAILS = []
PASSES = [0]


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)


GOOD_VALUES = {"grad_age_s": 0.0, "mcap": 20000.0, "liquidity": 9000.0,
               "deployer_launches_24h": 1, "curve_trades": 4,
               "ttg_s": 0.2, "ticker_others_24h": 0}


# --- loader guard --------------------------------------------------------
try:
    SR._load_standalone("veto")
    check("loader: veto.py is refused (relative imports)", False)
except SR.ShadowReaderError as exc:
    check("loader: veto.py is refused (relative imports)", True)
    check("loader: refusal names the monolith guard",
          "monolith" in str(exc))
try:
    SR._load_standalone("bundle")
    check("loader: non-allow-listed module is refused", False)
except SR.ShadowReaderError:
    check("loader: non-allow-listed module is refused", True)

conv = SR._load_standalone("conviction")
regime = SR._load_standalone("regime")
check("loader: conviction loads standalone",
      hasattr(conv, "score_candidate") and hasattr(conv, "gate"))
check("loader: regime loads standalone",
      hasattr(regime, "sit_out"))
check("loader: loading them did NOT import the monolith package",
      "daemon.launch_scanner" not in sys.modules
      and "daemon.launch_scanner.veto" not in sys.modules
      and "daemon.launch_scanner.conviction" not in sys.modules)

# --- veto translator (results only — veto_check is never called) ---------
obs = SR.veto_observation({"passed": False,
                           "vetoes": ["wash_trade"],
                           "flags": [], "details": {}})
check("veto: failed result -> fail + hard_veto",
      obs["name"] == "veto" and obs["outcome"] == "fail"
      and obs["hard_veto"] is True)
obs = SR.veto_observation({"passed": True, "vetoes": [],
                           "flags": ["bundle:unknown"], "details": {}})
check("veto: passed result -> pass, flags kept as evidence",
      obs["outcome"] == "pass"
      and obs["evidence"]["flags"] == ["bundle:unknown"])
obs = SR.veto_observation(None)
check("veto: no result -> unknown, never a pass",
      obs["outcome"] == "unknown")

# --- safety translator ---------------------------------------------------
obs = SR.safety_observation({"passed": False, "score": 0, "flags": [],
                             "fails": ["transfer_hook"], "unknown": False})
check("safety: hard fail -> fail + security_hard_fail",
      obs["outcome"] == "fail"
      and obs["security_hard_fail"] is True)
obs = SR.safety_observation({"passed": True, "score": 0, "flags": [],
                             "fails": [], "unknown": False})
check("safety: passed report -> pass", obs["outcome"] == "pass")
obs = SR.safety_observation({"passed": True, "score": 0, "flags": [],
                             "fails": [], "unknown": True})
check("safety: unknown report -> unknown, never a pass",
      obs["outcome"] == "unknown")
obs = SR.safety_observation(None)
check("safety: no report -> unknown", obs["outcome"] == "unknown")

# --- grad_select filters -------------------------------------------------
obs = SR.grad_select_filters_observation(dict(GOOD_VALUES))
check("filters: all four pass -> pass",
      obs["name"] == "grad_select_filters"
      and obs["outcome"] == "pass")
obs = SR.grad_select_filters_observation(
    dict(GOOD_VALUES, deployer_launches_24h=5))
check("filters: serial deployer -> fail naming the filter",
      obs["outcome"] == "fail"
      and "serial_deployer>=3" in obs["evidence"]["failed"])
obs = SR.grad_select_filters_observation(
    {k: v for k, v in GOOD_VALUES.items() if k != "curve_trades"})
check("filters: unknown curve_trades -> fail (fail-closed)",
      obs["outcome"] == "fail"
      and "curve_trades" in obs["evidence"]["unknown"])
obs = SR.grad_select_filters_observation(
    dict(GOOD_VALUES, ticker_others_24h=4))
check("filters: copycat tickers -> fail",
      obs["outcome"] == "fail"
      and "copycat_tickers>=3" in obs["evidence"]["failed"])

# --- conviction ----------------------------------------------------------
obs = SR.conviction_observation(dict(GOOD_VALUES), freshness_s=10)
check("conviction: strong values -> pass, tier STRONG",
      obs["outcome"] == "pass"
      and obs["evidence"]["tier"] == "STRONG")
obs = SR.conviction_observation(
    dict(GOOD_VALUES, deployer_launches_24h=5), freshness_s=10)
check("conviction: serial deployer BLOCK -> fail + contradictory "
      "(verified fact)",
      obs["outcome"] == "fail" and obs["contradictory"] is True)
obs = SR.conviction_observation(
    dict(GOOD_VALUES, grad_age_s=45.0), freshness_s=10)
check("conviction: slow entry BLOCK -> fail, NOT contradictory "
      "(score judgement)",
      obs["outcome"] == "fail" and obs["contradictory"] is False)
weak = {"grad_age_s": 0.0, "mcap": 100000.0, "liquidity": 9000.0,
        "deployer_launches_24h": 0, "curve_trades": 150,
        "ttg_s": 300, "ticker_others_24h": 1}
obs = SR.conviction_observation(weak, freshness_s=10)
check("conviction: WEAK values -> flag (never a qualifying "
      "positive)", obs["outcome"] == "flag")

# --- deployer band -------------------------------------------------------
check("band: 1-2 -> pass",
      SR.deployer_band_observation(
          {"deployer_band": "1-2"})["outcome"] == "pass")
check("band: derived from launches (1) -> pass",
      SR.deployer_band_observation(
          {"deployer_launches_24h": 1})["outcome"] == "pass")
check("band: 0 -> flag",
      SR.deployer_band_observation(
          {"deployer_band": "0"})["outcome"] == "flag")
check("band: 3+ -> fail",
      SR.deployer_band_observation(
          {"deployer_band": "3+"})["outcome"] == "fail")
check("band: unknown -> unknown",
      SR.deployer_band_observation({})["outcome"] == "unknown")

# --- regime --------------------------------------------------------------
closes = [{"pnl_usd": -25.0, "close_ts": time.time() - 60 * i}
          for i in range(12)]
info = SR.regime_observation(closes)
check("regime: returns observation + restrictions",
      set(info) == {"observation", "restrictions"}
      and info["observation"]["name"] == "regime")
direct = regime.sit_out(closes)
check("regime: sit-out flag matches regime.sit_out directly",
      info["restrictions"]["regime_sit_out"] == direct["sit_out"]
      and (info["observation"]["outcome"] == "flag")
      == bool(direct["sit_out"]))
winning = [{"pnl_usd": 25.0, "close_ts": time.time() - 60 * i}
           for i in range(12)]
info2 = SR.regime_observation(winning)
check("regime: winning tape -> no sit-out, observation pass",
      info2["restrictions"]["regime_sit_out"] is False
      and info2["observation"]["outcome"] == "pass")

# --- signals_for_close + file provider -----------------------------------
close = {
    "mint": "TESTMINT111111111111111111111111111111111",
    "close_ts": time.time() - 120,
    "pnl_usd": 12.5,
    "entry_features": {
        "deployer_band": "1-2",
        "grad_age_s": 0.0,
        "select": {"deployer_launches_24h": 1, "curve_trades": 4,
                   "ttg_s": 0.2, "ticker_others_24h": 0}},
    "entry_snapshot": {"mcap": 20000.0, "liquidity": 9000.0,
                       "safety": {"score": 0, "flags": [],
                                  "source": ""}},
}
obs_list = SR.signals_for_close(close, freshness_s=10)
names = {o["name"] for o in obs_list}
check("close: five observations with the expected names",
      names == {"veto", "instant_safety", "grad_select_filters",
                "conviction", "deployer_band"})
by_name = {o["name"]: o for o in obs_list}
check("close: filters + conviction + band pass on good values",
      by_name["grad_select_filters"]["outcome"] == "pass"
      and by_name["conviction"]["outcome"] == "pass"
      and by_name["deployer_band"]["outcome"] == "pass")
check("close: veto + safety are unknown (nothing stamped on a "
      "close — never invented)",
      by_name["veto"]["outcome"] == "unknown"
      and by_name["instant_safety"]["outcome"] == "unknown")
check("close: every observation carries verification provenance",
      all(o.get("verification") == "verified"
          for o in obs_list))

with tempfile.TemporaryDirectory() as tmp:
    home = os.path.join(tmp, ".forex-agent")
    os.makedirs(home)
    with open(os.path.join(home, "grad_select_ledger.json"), "w") as fh:
        json.dump({"bankroll": 25.0, "positions": {},
                   "closes": [close]}, fh)
    provider = SR.close_signal_provider(home)
    got = provider(close["mint"], "grad_select")
    check("provider: stored close -> its five translated signals",
          len(got) == 5
          and {o["name"] for o in got} == names)
    check("provider: unknown mint -> [] (no signal, never a pass)",
          provider("NOMINT222222222222222222222222222222222",
                   "grad_select") == [])
    check("provider: unknown lane -> []",
          provider(close["mint"], "grad_raw") == [])
    restr = SR.regime_restrictions(home, "grad_select")
    check("provider: regime restrictions read from the lane closes",
          restr == {"regime_sit_out": False})

print("\n%d passed, %d failed" % (PASSES[0], len(FAILS)))
if FAILS:
    for name in FAILS:
        print("  FAILED: %s" % name)
sys.exit(1 if FAILS else 0)
