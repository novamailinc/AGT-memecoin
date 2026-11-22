#!/usr/bin/env python3
"""Bear scorer tests (2026-11-17).

Covers daemon/launch_scanner/bear_score.py, scripts/bear-calibrate,
and their wiring into the shared grad evaluation path:
  * score computation from known features (hand-computed totals)
  * missing-feature handling: unknown, never a placeholder 0
    (all-missing scores 0 with max_known 0; liquidity 0 is the
    "no quote" marker, not thin; junk/booleans are unknown)
  * per-feature contributions, exact
  * journal field present on evaluations (pass AND reject), and
    the score NEVER gates: a high-bear candidate that passes the
    filters still passes (journal-only in v1 — no "bear_score"
    reject reason exists)
  * fail-soft wiring: a raising scorer leaves the verdict
    byte-identical
  * calibration bands on a fixture ledger with known outcomes
    (exact band closes/wins/net)
No network. Plain script — run directly, not via pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import importlib.util
import json
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import bear_score as B
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import saturation as S

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ================= score computation =================
clean = B.score_features({
    "deployer_launches_24h": 1, "ticker_others_24h": 0,
    "ttg_s": 35.0, "curve_trades": 80, "grad_age_s": 0.2})
check("score: all-clean features score 0",
      clean["bear_score"] == 0 and clean["contributions"] == {
          "deployer": 0, "copycat": 0, "curve_speed": 0,
          "curve_trades": 0, "entry_speed": 0}
      and clean["unknown"] == ["liquidity", "top10"]
      and clean["known"] == 5 and clean["max_known"] == 85)

bearish = B.score_features({
    "deployer_launches_24h": 0, "ticker_others_24h": 2,
    "ttg_s": 700.0, "curve_trades": 200, "grad_age_s": 120.0})
check("score: bearish features sum exactly (25+10+15+5+15=70)",
      bearish["bear_score"] == 70
      and bearish["contributions"] == {
          "deployer": 25, "copycat": 10, "curve_speed": 15,
          "curve_trades": 5, "entry_speed": 15})

mid = B.score_features({
    "deployer_launches_24h": 0, "ticker_others_24h": 0,
    "ttg_s": 300.0, "curve_trades": 50, "grad_age_s": 10.0})
check("score: deployer-0 + mid ttg = 25+7 = 32",
      mid["bear_score"] == 32
      and mid["contributions"]["deployer"] == 25
      and mid["contributions"]["curve_speed"] == 7)

serial = B.score_features({
    "deployer_launches_24h": 5, "ticker_others_24h": 0,
    "ttg_s": 30.0, "curve_trades": 50, "grad_age_s": 5.0})
check("score: serial deployer (>=3) scores the full deployer weight",
      serial["bear_score"] == 25
      and serial["contributions"]["deployer"] == 25)

# ================= missing features: unknown, never 0 =================
empty = B.score_features({})
check("unknown: all-missing scores 0 but max_known 0 (not an all-clear)",
      empty["bear_score"] == 0 and empty["known"] == 0
      and empty["max_known"] == 0
      and sorted(empty["unknown"]) == sorted(
          ["deployer", "copycat", "curve_speed", "curve_trades",
           "entry_speed", "liquidity", "top10"])
      and empty["contributions"] == {})

only_dep = B.score_features({"deployer_launches_24h": 0})
check("unknown: one known feature scores alone, rest unknown",
      only_dep["bear_score"] == 25 and only_dep["known"] == 1
      and only_dep["max_known"] == 25
      and "copycat" in only_dep["unknown"]
      and "curve_speed" in only_dep["unknown"]
      and "entry_speed" in only_dep["unknown"])

liq_zero = B.score_features({"liquidity_usd": 0})
check("unknown: liquidity 0 is unknown (the no-quote marker), not thin",
      "liquidity" in liq_zero["unknown"]
      and "liquidity" not in liq_zero["contributions"])
liq_neg = B.score_features({"liquidity_usd": -5})
check("unknown: negative liquidity is unknown",
      "liquidity" in liq_neg["unknown"])
for liq, pts in ((3000.0, 10), (10000.0, 5), (50000.0, 0)):
    got = B.score_features({"liquidity_usd": liq})
    check("score: liquidity $%.0f contributes +%d" % (liq, pts),
          got["contributions"].get("liquidity") == pts)
for top, pts in ((10.0, 0), (30.0, 3), (50.0, 5)):
    got = B.score_features({"top10_pct": top})
    check("score: top10 %.0f%% contributes +%d" % (top, pts),
          got["contributions"].get("top10") == pts)
junk = B.score_features({"deployer_launches_24h": "abc",
                         "ttg_s": True, "curve_trades": None})
check("unknown: junk string / boolean / None are unknown, never 0",
      junk["bear_score"] == 0 and junk["known"] == 0
      and "deployer" in junk["unknown"]
      and "curve_speed" in junk["unknown"])

# ================= score_close: full vs eval-only =================
close_rec = {
    "mint": "x", "pnl_usd": 10.0,
    "entry_features": {
        "liquidity": 1000.0,
        "select": {"deployer_launches_24h": 0,
                   "ticker_others_24h": 2, "ttg_s": 700.0,
                   "curve_trades": 200, "grad_age_s": 120.0}}}
full, eval_only = B.score_close(close_rec)
check("score_close: eval-only excludes entry liquidity (70)",
      eval_only["bear_score"] == 70)
check("score_close: full adds thin liquidity (+10 = 80)",
      full["bear_score"] == 80
      and full["contributions"]["liquidity"] == 10)
check("score_close: top10 unknown in both (never in stored closes)",
      "top10" in full["unknown"] and "top10" in eval_only["unknown"])

# ================= evaluation wiring (journal-only) =================
NOW = time.time()
MINT_OK = "BearMintOK1111111111111111111111111111111pump"
MINT_HI = "BearMintHi1111111111111111111111111111111pump"
MINT_SER = "BearMintSer11111111111111111111111111111pump"
TRADER_OK = "BearTraderOK11111111111111111111111111111111"
TRADER_HI = "BearTraderHi11111111111111111111111111111111"
TRADER_SER = "BearTraderSer111111111111111111111111111111"

eng = GS.make_engine()
# Isolate the engine: the conviction gate enqueues WEAK candidates
# into the review queue under the engine's ledger dir — a temp dir
# keeps test fixtures out of the LIVE review queue (2026-11-21).
_eng_home = tempfile.mkdtemp(prefix="bear-eng-")
eng.LEDGER_PATH = os.path.join(_eng_home, "grad_select_ledger.json")
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}

GS._index_refresh = lambda now: None
GS._INDEX["available"] = True
GS._INDEX["coverage_start"] = NOW - 90000
GS._INDEX["by_mint"][MINT_OK] = {
    "trader": TRADER_OK, "ts": NOW - 60, "bonding_curve": "Curve111"}
GS._INDEX["by_trader"][TRADER_OK] = [NOW - 600]
GS._INDEX["by_mint"][MINT_HI] = {
    "trader": TRADER_HI, "ts": NOW - 700, "bonding_curve": "Curve111"}
# TRADER_HI absent from by_trader: zero launches in 24h (bearish).
GS._INDEX["by_mint"][MINT_SER] = {
    "trader": TRADER_SER, "ts": NOW - 300, "bonding_curve": "Curve111"}
GS._INDEX["by_trader"][TRADER_SER] = [NOW - 3600, NOW - 3500, NOW - 300]

_CURVE = {
    MINT_OK: {"trades": 80, "created_ts": NOW - 60,
              "oldest_sig": "sig-ok", "capped": False,
              "exhausted": True},
    MINT_HI: {"trades": 200, "created_ts": NOW - 700,
              "oldest_sig": "sig-hi", "capped": False,
              "exhausted": True},
    MINT_SER: {"trades": 90, "created_ts": NOW - 300,
               "oldest_sig": "sig-ser", "capped": False,
               "exhausted": True},
}


def _fake_curve_stats(addr):
    return _CURVE[_fake_curve_stats.mint]


GS._curve_stats = _fake_curve_stats


class _FakeSat:
    def __init__(self, launches):
        self.launches = launches
        self.updated = NOW

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": dict(self.launches)}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


GS._saturation = _FakeSat({})


def _stream_cand(mint, symbol):
    return {"mint": mint, "symbol": symbol, "created_ts": NOW,
            "dex": "pumpswap", "pool_address": "PoolX",
            "migration_sig": "", "reserve_usd": 0.0, "fdv_usd": 0.0,
            "source": "pumpportal-migration"}


def _eval(mint, symbol):
    _fake_curve_stats.mint = mint
    return GS.evaluate_candidate(eng, _stream_cand(mint, symbol), NOW)


v = _eval(MINT_OK, "BEAROK")
check("journal: clean pass carries a bear_score field",
      isinstance(v["values"].get("bear_score"), dict)
      and v["values"]["bear_score"]["bear_score"] == 0
      and v["values"]["bear_score"]["version"] == 1)
check("journal: pass bear_score lists liquidity+top10 unknown",
      v["values"]["bear_score"]["unknown"] == ["liquidity", "top10"])

v_ser = _eval(MINT_SER, "BEARSER")
check("journal: filter reject ALSO carries a bear_score field",
      v_ser["verdict"] == "reject"
      and v_ser["reason"] == "serial_deployer"
      and isinstance(v_ser["values"].get("bear_score"), dict)
      and v_ser["values"]["bear_score"]["contributions"].get(
          "deployer") == 25)

# High-bear but filter-passing: deployer 0 (+25), ttg 700 (+15),
# curve 200 trades (+5), fast age (+0), copycat 0 (+0) = 45.
# Conviction gate (2026-11-21, piece 1): this fixture is WEAK on
# its OTHER components too (new deployer + slow curve), so the
# conviction gate reviews it — the bear score itself still never
# rejects alone (BEAR_GATE_THRESHOLD stays None; there is no
# "bear_score" reject reason). The bear score is journaled, and
# bear is one of eight conviction components, never a solo gate.
v_hi = _eval(MINT_HI, "BEARHI")
check("gate: high-bear candidate (score 45) reviews — conviction, "
      "not bear",
      v_hi["verdict"] == "review"
      and v_hi["reason"] == "conviction_review"
      and v_hi["values"]["bear_score"]["bear_score"] == 45
      and v_hi["values"]["bear_score"]["contributions"] == {
          "deployer": 25, "copycat": 0, "curve_speed": 15,
          "curve_trades": 5, "entry_speed": 0})
check("gate: standalone bear gate is still off (journal-only)",
      GS.BEAR_GATE_THRESHOLD is None)

# Fail-soft: a raising scorer must not change the verdict.
_orig = GS._bear.score_features


def _boom(features):
    raise RuntimeError("scorer exploded")


GS._bear.score_features = _boom
v_boom = _eval(MINT_OK, "BEAROK")
check("fail-soft: raising scorer leaves verdict byte-identical",
      v_boom["verdict"] == "pass" and v_boom["reason"] == ""
      and "bear_score" not in v_boom["values"])
GS._bear.score_features = _orig

# ================= calibration on a fixture ledger =================
from importlib.machinery import SourceFileLoader
_spec = importlib.util.spec_from_loader(
    "bear_calibrate",
    SourceFileLoader("bear_calibrate",
                     os.path.join(REPO, "scripts", "bear-calibrate")))
CAL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CAL)

_home = tempfile.mkdtemp(prefix="bear-cal-")


def _close(mint, pnl, sel, liq=None):
    ef = {"select": dict(sel)}
    if liq is not None:
        ef["liquidity"] = liq
    return {"mint": mint, "symbol": "T", "pnl_usd": pnl,
            "entry_ts": 1000.0, "close_ts": 2000.0,
            "closed_reason": "test",
            "entry_features": ef}


_fixture = {"closes": [
    _close("A", 20.0,
           {"deployer_launches_24h": 1, "ticker_others_24h": 0,
            "ttg_s": 35.0, "curve_trades": 80, "grad_age_s": 0.2},
           liq=50000.0),
    _close("B", -10.0,
           {"deployer_launches_24h": 0, "ticker_others_24h": 0,
            "ttg_s": 35.0, "curve_trades": 80, "grad_age_s": 120.0}),
    _close("C", -15.0,
           {"deployer_launches_24h": 0, "ticker_others_24h": 2,
            "ttg_s": 700.0, "curve_trades": 200, "grad_age_s": 120.0},
           liq=1000.0),
    _close("D", 5.0,
           {"deployer_launches_24h": 1, "ticker_others_24h": 1,
            "ttg_s": 300.0, "curve_trades": 150, "grad_age_s": 10.0},
           liq=10000.0),
]}
with open(os.path.join(_home, "grad_select_ledger.json"), "w") as f:
    json.dump(_fixture, f)

res = CAL.calibrate_lane("grad_select", home=_home)
check("calibration: overall 4 closes / 2 wins / net 0.00",
      res["closes"] == 4 and res["overall"] == (4, 2, 0.0))
check("calibration: eval band 0-19 holds A only (1 close, 1 win, +20)",
      res["eval_bands"]["0-19"] == (1, 1, 20.0))
check("calibration: eval band 20-39 holds D only (1 close, 1 win, +5)",
      res["eval_bands"]["20-39"] == (1, 1, 5.0))
check("calibration: eval band 40-59 holds B only (1 close, 0 wins, -10)",
      res["eval_bands"]["40-59"] == (1, 0, -10.0))
check("calibration: eval band 60-79 holds C only (1 close, 0 wins, -15)",
      res["eval_bands"]["60-79"] == (1, 0, -15.0))
check("calibration: eval band 80-100 empty",
      res["eval_bands"]["80-100"] == (0, 0, 0.0))
check("calibration: full band moves thin-liquidity C to 80-100",
      res["full_bands"]["80-100"] == (1, 0, -15.0)
      and res["full_bands"]["40-59"] == (1, 0, -10.0))
check("calibration: missing ledger file is empty, never raises",
      CAL.calibrate_lane("grad_raw", home=_home)["closes"] == 0)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print("  FAIL " + f)
    sys.exit(1)
print("ALL BEAR SCORE TESTS PASSED")
