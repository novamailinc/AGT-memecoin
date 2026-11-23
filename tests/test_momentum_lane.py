#!/usr/bin/env python3
"""Momentum paper lane tests (2026-10-23).

Covers momentum_lane.py: killswitch registration, pre-registered
terms + divergence stamp, the momentum-feed qualify->enter path
(existing MomentumScanner + sources pool shape), runner-rule
re-check from fresh tape, fail-closed safety, veto as a HARD gate
with 24h counterfactual journaling, the 25% cost gate, spec-filter
applicability journaling, dedupe-ever, MAX_OPEN=3, the user's ladder
exits on synthetic price paths, and the pre-registered 100-close
verdict halting the lane. No network: every feed is stubbed. Plain
script — run directly, not via pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import momentum_lane as M
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import trials as T

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()

# ---------------- killswitch registration ----------------
check("killswitch: momentum registered (window/min 100, halt)",
      "momentum" in KS.CFG and KS.CFG["momentum"]["window"] == 100
      and KS.CFG["momentum"]["min_trades"] == 100
      and KS.CFG["momentum"]["halt"] is True)
check("momentum: verdict sample constant is 100",
      M.VERDICT_SAMPLE_CLOSES == 100)
check("momentum: MAX_OPEN is 3 (spec decision)",
      M.MAX_OPEN == 3)
check("momentum: $1 shots on a $100 bankroll",
      M.NOTIONAL_USD == 1.0 and M.BANKROLL_START == 100.0)

_ks_tmp = tempfile.mkdtemp(prefix="momentum-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")

_tmp = tempfile.mkdtemp(prefix="momentum-ledger-")
M.LEDGER_PATH = os.path.join(_tmp, "momentum_ledger.json")
led = M.ensure_ledger()
pre = led["preregistered_verdict"]
check("preregistration: lane momentum, sample 100",
      pre["lane"] == "momentum" and pre["sample_closes"] == 100)
check("preregistration: killswitch cfg stamped {100,100,halt}",
      pre["killswitch_cfg"] == {"window": 100, "min_trades": 100,
                                "halt": True})
check("preregistration: terms carry veto HARD gate + counterfactuals",
      "HARD" in pre["terms"]["veto"]
      and "24h" in pre["terms"]["counterfactuals"]
      and pre["terms"]["max_open"] == 3
      and pre["terms"]["daily_entry_cap"] is None)
check("preregistration: divergence note stamped honestly",
      "win over" in pre["spec_divergence"]
      and "graduated-only" in pre["spec_divergence"])

# ---------------- trial registration ----------------
_reg_tmp = tempfile.mkdtemp(prefix="momentum-trials-")
T.REGISTRY_PATH = os.path.join(_reg_tmp, "trial_registry.json")
rec = M.register_trial()
check("trials: register_trial returns an active momentum trial",
      rec is not None and rec["lane"] == "momentum"
      and rec["status"] == "active")
M.register_trial()
check("trials: registration is idempotent",
      len(T.trials_for("momentum")) == 1)

# ---------------- stubs (no network) ----------------
GOOD_TAPE = {"dex": "pump", "usd_liquidity": 100000.0, "usd_market_cap": 500000.0,
             "chg_m5": 5.0, "price_usd": 0.001,
             "tape_source": "test"}
TAPES = {}
QUOTES = {}
SAFETY = {}
VETOES = {}


def _tape(chain, mint):
    return TAPES.get(mint, dict(GOOD_TAPE))


M._fresh_tape = _tape
M.quote_price = lambda chain, mint, spot_index=None: \
    QUOTES.get(mint, 0.001)


class _SafetyStub:
    def check_token(self, chain, mint, timeout=10):
        return SAFETY.get(mint, {"passed": True, "score": 80,
                                 "flags": [], "fails": [],
                                 "unknown": False})


class _VetoStub:
    def veto_check(self, mint, chain, symbol="", safety_report=None,
                   run_bundle=False, lane="unknown"):
        return VETOES.get(mint, {"passed": True, "vetoes": [],
                                 "flags": [], "details": {}})


M._safety = _SafetyStub()
M._veto = _VetoStub()


def runner(mint, symbol="RUN", chain="solana", **kw):
    d = {"pool_address": "Pool" + mint[:6], "mint": mint,
         "symbol": symbol, "name": symbol, "chain": chain,
         "source": "gecko-trending", "lane": "lowcap", "url": "",
         "chg_24h_pct": 55.0, "chg_1h_pct": 8.0,
         "vol_24h_usd": 250000.0, "liq_usd": 100000.0,
         "usd_market_cap": 500000.0}
    d.update(kw)
    return d


def raw_pool(mint, symbol="RUN"):
    return {"pool_address": "Pool" + mint[:6], "mint": mint,
            "symbol": symbol, "name": symbol + " / SOL",
            "chain": "solana", "chg_1h_pct": 8.0, "chg_24h_pct": 55.0,
            "vol_24h_usd": 250000.0, "liq_usd": 100000.0,
            "usd_market_cap": 500000.0, "url": ""}


M1 = "MomMint1111111111111111111111111111111111111"
M2 = "MomMint2222222222222222222222222222222222222"
M3 = "MomMint3333333333333333333333333333333333333"
M4 = "MomMint4444444444444444444444444444444444444"
M5 = "MomMint5555555555555555555555555555555555555"
M6 = "MomMint6666666666666666666666666666666666666"
M7 = "MomMint7777777777777777777777777777777777777"
M8 = "MomMint8888888888888888888888888888888888888"
M9 = "MomMint9999999999999999999999999999999999999"
M10 = "MomMint1010101010101010101010101010101010101"
M11 = "MomMint1111111111111111111111111111111111112"
M12 = "MomMint1212121212121212121212121212121212121"

# ---------------- qualify -> enter via the momentum feed ----------------
res = M.scan_once(now=NOW, pools=[raw_pool(M1)])
check("feed: raw trending pool qualified + opened",
      res["pools"] == 1 and res["qualified"] == 1 and res["opened"] == 1)
d = M._read()
pos = d["positions"][M1]
check("ledger: 1 open, bankroll $99 after the $1 shot",
      len(d["positions"]) == 1 and abs(d["bankroll"] - 99.0) < 1e-6)
check("entry: user ladder legs present (50/25/25)",
      pos["legs"]["a"]["frac"] == 0.50 and pos["legs"]["b"]["frac"] == 0.25
      and pos["legs"]["c"]["frac"] == 0.25
      and pos["legs"]["a"]["target"] == 2.0
      and pos["legs"]["b"]["target"] == 3.0
      and pos["legs"]["c"]["trail"] == 0.30)
check("entry_features: source is the momentum feed, lane journaled",
      pos["entry_features"]["source"] == "momentum-feed"
      and pos["entry_features"]["momentum_lane"] == "lowcap")
sf = pos["entry_features"]["spec_filters"]
check("spec_filters: no graduation data -> none applicable, none faked",
      all(v["applicable"] is False and v["passed"] is None
          for v in sf.values()))

# dedupe-ever (while open)
opened, why, final = M.consider_candidate(runner(M1), NOW + 10)
check("dedupe: same mint never opens twice", not opened)

# ---------------- runner-rule re-check from fresh tape ----------------
TAPES[M2] = dict(GOOD_TAPE, usd_liquidity=10000.0)
opened, why, final = M.consider_candidate(runner(M2), NOW)
check("runner gate: liq $10k < $25k blocks", not opened and "liq" in why)
TAPES[M3] = dict(GOOD_TAPE, chg_m5=-2.0)
opened, why, final = M.consider_candidate(runner(M3), NOW)
check("runner gate: negative 5m momentum blocks",
      not opened and "not rising" in why)
TAPES[M4] = dict(GOOD_TAPE, usd_market_cap=10000.0)
opened, why, final = M.consider_candidate(runner(M4), NOW)
check("runner gate: mcap $10k < $50k blocks",
      not opened and "mcap" in why)
d = M._read()
check("runner gate: blocked mints entered nothing, journaled nothing",
      all(m not in d["positions"] for m in (M2, M3, M4))
      and not any(v["mint"] in (M2, M3, M4) for v in d["vetoed"]))

# ---------------- safety gate fail-closed ----------------
SAFETY[M5] = {"passed": False, "score": 10, "flags": [],
              "fails": ["honeypot"], "unknown": False}
opened, why, final = M.consider_candidate(runner(M5), NOW)
check("safety: hard fail blocks, final",
      not opened and final and M5 in M._read()["dead_mints"])
SAFETY[M6] = {"passed": True, "score": 0, "flags": [], "fails": [],
              "unknown": True}
opened, why, final = M.consider_candidate(runner(M6), NOW)
check("safety: unknown blocks (fail-closed), not final, not dead",
      not opened and not final and M6 not in M._read()["dead_mints"])

# ---------------- veto HARD gate + counterfactual ----------------
QUOTES[M7] = 0.002
VETOES[M7] = {"passed": False,
              "vetoes": ["wash:bump-bot pattern (txns up, holders flat)"],
              "flags": [], "details": {}}
opened, why, final = M.consider_candidate(runner(M7), NOW)
d = M._read()
check("veto: vetoed candidate NOT entered", not opened
      and M7 not in d["positions"])
check("veto: journaled to counterfactual tracker with price at veto",
      len(d["vetoed"]) == 1 and d["vetoed"][0]["mint"] == M7
      and abs(d["vetoed"][0]["price_at_veto"] - 0.002) < 1e-9
      and "wash" in d["vetoed"][0]["veto_reason"]
      and d["vetoed"][0]["checked_24h"] is False)
opened, why, final = M.consider_candidate(runner(M7), NOW)
check("veto: vetoed mint is never reconsidered", not opened and final)
# 24h counterfactual check stamps when due
with M._ledger_lock():
    d = M._read()
    d["vetoed"][0]["ts"] = NOW - 25 * 3600
    M._write(d)
stamped = M.update_counterfactuals(
    now=NOW, price_fn=lambda c, m, s: 0.003)
d = M._read()
check("counterfactual: 24h price stamped (+50% vs price at veto)",
      stamped == 1 and d["vetoed"][0]["checked_24h"] is True
      and abs(d["vetoed"][0]["price_24h"] - 0.003) < 1e-9
      and abs(d["vetoed"][0]["change_24h_pct"] - 50.0) < 0.01)

# ---------------- cost gate ----------------
opened, why, final = M.consider_candidate(
    runner(M8, chain="ethereum"), NOW)
check("cost gate: $1 ETH entry COST-BLOCKED (>25% round-trip)",
      not opened and "COST-BLOCKED" in why
      and M8 not in M._read()["positions"])

# ---------------- spec filters where graduation data exists ----------------
opened, why, final = M.consider_candidate(
    runner(M9, graduation_ts=NOW - 3600, graduation_price=0.0005), NOW)
d = M._read()
check("spec filters: graduated 1h ago + above grad price -> opens",
      opened and M9 in d["positions"])
sf = d["positions"][M9]["entry_features"]["spec_filters"]
check("spec filters: applicability + pass journaled per entry",
      all(v["applicable"] is True and v["passed"] is True
          for v in sf.values()))
opened, why, final = M.consider_candidate(
    runner(M10, graduation_ts=NOW - 60), NOW)
check("spec filters: 1m since graduation (<30m) blocks",
      not opened and "30m-6h" in why and M10 not in M._read()["positions"])

# ---------------- MAX_OPEN = 3 ----------------
opened, why, final = M.consider_candidate(runner(M11), NOW)
check("max_open: third position opens", opened)
opened, why, final = M.consider_candidate(runner(M12), NOW)
check("max_open: fourth position blocked at MAX_OPEN=3",
      not opened and "max open" in why)
check("max_open: exactly 3 open", len(M._read()["positions"]) == 3)

# ---------------- ladder exits on synthetic price paths ----------------
entry1 = M._read()["positions"][M1]["entry_price"]


def _path(target, mult):
    # Price path for ONE mint; every other open position sits at 1.0x.
    return lambda c, m, s: entry1 * mult if m == target else entry1


M.poll_exits(price_fn=_path(M1, 2.1))
d = M._read()
check("ladder: leg A (50% @2x) fired at 2.1x, position still open",
      d["positions"][M1]["legs"]["a"]["done"] and M1 in d["positions"])
M.poll_exits(price_fn=_path(M1, 3.1))
d = M._read()
check("ladder: leg B (25% @3x) fired at 3.1x",
      d["positions"][M1]["legs"]["b"]["done"])
closes = M.poll_exits(price_fn=_path(M1, 2.0))
d = M._read()
check("ladder: leg C trailed out after peak -> ladder_complete close",
      M1 not in d["positions"] and len(d["closes"]) == 1
      and d["closes"][0]["closed_reason"] == "ladder_complete"
      and d["closes"][0]["lane"] == "momentum"
      and d["closes"][0]["peak_mult"] is not None
      and d["closes"][0]["peak_mult"] >= 3.0)

entry9 = M._read()["positions"][M9]["entry_price"]
closes = M.poll_exits(price_fn=lambda c, m, s: entry9 * 0.4
                      if m == M9 else entry9)
d = M._read()
check("ladder: hard stop -50% closes everything",
      M9 not in d["positions"]
      and any(c["mint"] == M9 and c["closed_reason"] == "hard_stop_-50pct"
              for c in d["closes"]))

with M._ledger_lock():
    d = M._read()
    d["positions"][M11]["entry_ts"] = time.time() - 25 * 3600
    M._write(d)
entry11 = M._read()["positions"][M11]["entry_price"]
closes = M.poll_exits(price_fn=lambda c, m, s: entry11 * 1.0)
d = M._read()
check("ladder: 24h time stop closes the last position",
      M11 not in d["positions"]
      and any(c["mint"] == M11 and c["closed_reason"] == "time_stop_24h"
              for c in d["closes"]))
check("closes so far: 3, no verdict yet",
      len(d["closes"]) == 3 and d["verdict"] is None
      and not KS.is_halted("momentum"))

# dedupe-ever (after close)
opened, why, final = M.consider_candidate(runner(M1), time.time())
check("dedupe: a closed mint never re-enters", not opened and final)

# ---------------- pre-registered verdict at 100 closes ----------------
base = len(M._read()["closes"])
for i in range(100 - base):
    mint = "VerdictMint%040d" % i
    opened, why, final = M.consider_candidate(runner(mint), time.time())
    if not opened:
        check("verdict loop: entry %d opened (%s)" % (i, why), False)
        break
    ep = M._read()["positions"][mint]["entry_price"]
    M.poll_exits(price_fn=lambda c, m, s, _ep=ep: _ep * 0.4)
d = M._read()
check("verdict: 100 closes reached", len(d["closes"]) == 100)
check("verdict: recorded in-ledger, expectancy <= 0 -> HALT",
      d["verdict"] is not None and d["verdict"]["n_closes"] == 100
      and d["verdict"]["expectancy"] <= 0
      and "HALT" in d["verdict"]["action"])
check("verdict: killswitch lane halted",
      KS.is_halted("momentum"))
opened, why, final = M.consider_candidate(
    runner("VerdictMintAfterHalt000000000000000000000"), time.time())
check("verdict: halted lane takes no new entries", not opened)

print()
if fails:
    print("FAILURES (%d): %s" % (len(fails), fails))
    sys.exit(1)
print("ALL MOMENTUM LANE TESTS PASSED")
