#!/usr/bin/env python3
"""Smart-SELECT lane tests (2026-10-27).

Covers daemon/launch_scanner/smart_select.py, the measurement-only
test lane that fixes the smart-money design flaws (watcher v1/v2
were both KILLED following smart-watcher buys blindly):
  * killswitch registration (smart_select 100/100/halt);
  * engine isolation + policy ($1, $100 bankroll, MAX_OPEN 20,
    no daily cap, 1800s window, STANDARD ladder — 30% trail,
    24h time stop — NOT moon's) and the other lane modules
    untouched (grad_select still $25, grad still gated);
  * pre-registered verdict terms (100 closes, filters, ladder);
  * the canonical filter is HONEST for smart fires: a fire with
    no migration record rejects unknown_canonical (no
    migration-event bypass — build_candidate never uses the
    "pumpportal-migration" source);
  * the real four filters, through the real evaluate_candidate:
    pass, serial_deployer, slow_curve, copycat_ticker,
    unknown_curve, unknown_serial rejections;
  * scan-level behavior: fire+pass opens (stamped
    smart-watcher-select); fire+ANY filter fail does NOT enter
    and consider_candidate is never reached; unknown on any
    filter fails closed; dedupe-ever;
  * standard ladder exits: legs 50/25/25 at 2x/3x, 30% trail
    (25% drawdown survives, 35% closes), -50% hard stop,
    24h time stop (25h-old position closes);
  * cost gate blocks when modeled costs exceed 25%;
  * verdict reporting: raw + ex-top-1 + same-period head-to-
    head vs grad_select on per-$1 expectancy (closes without
    entry_notional_usd are skipped, never guessed).
No network: the price layer, the filter index, curve stats, and
saturation are all stubbed. Plain script — run directly, not
via pytest.
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

from daemon.launch_scanner import grad as GATED
from daemon.launch_scanner import grad_raw as GR_RAW
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import saturation as S
from daemon.launch_scanner import smart_select as SS
from daemon.launch_scanner import killswitch as KS

# CRITICAL: smart_select imports grad_select FLAT (sys.path has
# daemon/launch_scanner first), while this test imports it via the
# package path. Those are two DIFFERENT module objects. The filter
# fixtures MUST be installed on SS._sel — the module the lane
# actually calls — or every stub silently misses. Same for the
# killswitch: each isolated grad engine carries its own _ks
# reference (flat killswitch), so state isolation is set per
# engine below, not only on the package KS.
SEL = SS._sel

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
MINT_P = "SelMintPass1111111111111111111111111111pump"
MINT_S = "SelMintSerial11111111111111111111111111pump"
MINT_C = "SelMintCopy111111111111111111111111111pump"
MINT_U = "SelMintUnknown111111111111111111111111pump"


def _fire(mint, sym, ts):
    # Schema-valid smart-watcher publish (v=1, chain, mint, source).
    return {"v": 1, "chain": "solana", "mint": mint, "symbol": sym,
            "ts": ts, "source": "smart-watcher",
            "price": 0.001, "buys": 1, "sells": 0, "buys_usd": 10.0}


def _norm(symbol):
    return str(symbol or "").strip().upper().lstrip("$").strip()


class _FakeSaturation:
    """Ticker-saturation tally stub: per-ticker launch timestamps."""

    def __init__(self, launches):
        self._launches = launches

    def _refresh_if_stale(self):
        return {"updated": NOW, "launches": self._launches, "wm_ts": 0}

    def _norm(self, symbol):
        return _norm(symbol)

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


def _install_filter_fixture(index, curve, saturation):
    """Stub grad_select's filter inputs; returns a restore fn."""
    orig = (SEL._index_refresh, SEL._curve_stats, SEL._saturation,
            dict(SEL._INDEX))
    SEL._index_refresh = lambda now: None
    SEL._curve_stats = lambda addr: dict(curve)
    SEL._saturation = saturation
    SEL._INDEX.clear()
    SEL._INDEX.update(index)

    def restore():
        SEL._index_refresh, SEL._curve_stats, SEL._saturation = orig[:3]
        SEL._INDEX.clear()
        SEL._INDEX.update(orig[3])
    return restore


def _pass_index(mint, trader="Trader1", birth_ago=1800):
    return {"by_mig": {mint: {"pool": "PoolX", "ts": NOW}},
            "by_mint": {mint: {"trader": trader, "ts": NOW - birth_ago,
                               "bonding_curve": "Curve1"}},
            "by_trader": {trader: [NOW - birth_ago]},
            "coverage_start": NOW - 86400 * 3,
            "available": True}


_PASS_CURVE = {"trades": 100, "created_ts": NOW - 1800,
               "oldest_sig": None, "capped": False, "exhausted": True,
               "reason": None}


# ---------------- killswitch registration ----------------
check("killswitch: smart_select registered (window/min 100, halt)",
      "smart_select" in KS.CFG
      and KS.CFG["smart_select"]["window"] == 100
      and KS.CFG["smart_select"]["min_trades"] == 100
      and KS.CFG["smart_select"]["halt"] is True)

# isolate killswitch state for the whole run
_ks_tmp = tempfile.mkdtemp(prefix="smart-select-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")

# ---------------- engine identity + policy ----------------
eng = SS.make_engine()
try:
    eng._ks.STATE_DIR = _ks_tmp
    eng._ks.STATE_FILE = os.path.join(_ks_tmp, "state.json")
except Exception:
    pass
check("engine: lane is smart_select", eng.LANE == "smart_select")
check("engine: ledger is smart_select_ledger.json",
      eng.LEDGER_PATH.endswith("smart_select_ledger.json"))
check("engine: $1 stake (new-lane standard)", eng.NOTIONAL_USD == 1.00)
check("engine: $100 starting bankroll", SS.BANKROLL_START == 100.00)
check("engine: MAX_OPEN 20 (grad_select's value)",
      eng.MAX_OPEN == 20)
check("engine: NO daily cap (fires are rare)",
      eng.DAILY_ENTRY_CAP is None)
check("engine: age window 1800s (same as grad_select)",
      eng.MAX_GRAD_AGE_S == 1800.0)
check("engine: STANDARD ladder 50/25/25 at 2x/3x",
      eng.LEG_A_FRAC == 0.50 and eng.LEG_A_TARGET == 2.0
      and eng.LEG_B_FRAC == 0.25 and eng.LEG_B_TARGET == 3.0
      and eng.LEG_C_FRAC == 0.25)
check("engine: STANDARD trail 30% (NOT moon's 50%)",
      eng.LEG_C_TRAIL == 0.30)
check("engine: STANDARD 24h time stop (NOT moon's inf)",
      eng.TIME_STOP_H == 24)
check("engine: hard stop intact at -50%",
      eng.HARD_STOP_MULT == 0.50)
check("engine: no-gate safety/veto overrides installed",
      eng._safety_ok("x")[0] is True and eng._veto_ok("x", "y", None)[0])
check("grad_select module untouched: $25, own lane/ledger",
      GS.NOTIONAL_USD == 25.00 and GS.LANE == "grad_select"
      and GS.LEDGER_PATH.endswith("grad_select_ledger.json"))
check("lane's own grad_select reference (flat module) also "
      "untouched: $25, grad_select lane",
      SEL is not None and SEL.NOTIONAL_USD == 25.00
      and SEL.LANE == "grad_select")
check("gated grad module untouched: trail 0.30, time stop 24h",
      GATED.LEG_C_TRAIL == 0.30 and GATED.TIME_STOP_H == 24)
check("grad_raw module untouched: $25 stake, own lane",
      GR_RAW.NOTIONAL_USD == 25.00 and GR_RAW.LANE == "grad_raw")

_tmp = tempfile.mkdtemp(prefix="smart-select-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "smart_select_ledger.json")
led = eng.ensure_ledger()
pre = led["preregistered_verdict"]
check("preregistration: lane smart_select, sample 100",
      pre["lane"] == "smart_select" and pre["sample_closes"] == 100)
check("preregistration: killswitch_cfg 100/100/halt",
      pre["killswitch_cfg"]["window"] == 100
      and pre["killswitch_cfg"]["min_trades"] == 100
      and pre["killswitch_cfg"]["halt"] is True)
check("preregistration: terms declare $1, standard ladder, filters",
      pre["terms"]["notional_usd"] == 1.00
      and "30%" in pre["terms"]["ladder"]
      and "24h" in pre["terms"]["ladder"]
      and "serial_deployer" in pre["terms"]["filters"]
      and "canonical_only" in pre["terms"]["filters"]
      and "copycat_ticker" in pre["terms"]["filters"])
check("preregistration: terms declare NO safety gate / NO veto",
      "NONE" in pre["terms"]["safety_gate"]
      and pre["terms"]["veto"] == "NONE")
check("preregistration: rule is expectancy<=0 halts at 100 closes",
      "100" in pre["rule"] and "halts permanently" in pre["rule"])

# Backdate the ledger so fixture fires (ts NOW) are not "history".
with eng._ledger_lock():
    d = eng._read()
    d["created_ts"] = NOW - 3600
    eng._write(d)

# stub the price layer (no network); safety must NEVER be consulted
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}


class _ExplodingSafety:
    def check_token(self, *a, **k):
        raise AssertionError("safety consulted on the SMART_SELECT lane")


eng._safety = _ExplodingSafety()

# ---------------- candidate construction: canonical honesty ----------------
restore = _install_filter_fixture(
    {"by_mig": {}, "by_mint": {}, "by_trader": {},
     "coverage_start": NOW - 86400 * 3, "available": True},
    _PASS_CURVE, _FakeSaturation({}))
try:
    cand = SS.build_candidate(_fire(MINT_U, "UNK", NOW), NOW)
    check("candidate: source is smart-watcher (never "
          "pumpportal-migration)", cand["source"] == "smart-watcher")
    check("candidate: no migration record -> created_ts 0, empty pool",
          cand["created_ts"] == 0 and cand["pool_address"] == "")
    v = SEL.evaluate_candidate(eng, cand, NOW)
    check("REAL FILTER: no migration record -> unknown_canonical "
          "(fail-closed, no bypass)",
          v["verdict"] == "reject" and v["reason"] == "unknown_canonical")
finally:
    restore()

restore = _install_filter_fixture(_pass_index(MINT_P), _PASS_CURVE,
                                  _FakeSaturation({}))
try:
    cand = SS.build_candidate(_fire(MINT_P, "TEST", NOW), NOW)
    check("candidate: migration record -> pool + ts from recorder",
          cand["pool_address"] == "PoolX" and cand["created_ts"] == NOW)
    v = SEL.evaluate_candidate(eng, cand, NOW)
    check("REAL FILTER: matching migration pool passes canonical",
          v["verdict"] == "pass"
          and v["values"]["canonical_basis"]
          == "recorder_migration_match")
    check("REAL FILTER: pass values measured (serial 1, ttg 1800, "
          "curve 100, copycat 0)",
          v["values"]["deployer_launches_24h"] == 1
          and v["values"]["ttg_s"] == 1800.0
          and v["values"]["curve_trades"] == 100
          and v["values"]["ticker_others_24h"] == 0)
finally:
    restore()

# ---------------- real-filter rejections ----------------
idx = _pass_index(MINT_S, trader="SerialTrader")
idx["by_trader"]["SerialTrader"] = [NOW - 100, NOW - 200, NOW - 300]
restore = _install_filter_fixture(idx, _PASS_CURVE, _FakeSaturation({}))
try:
    v = SEL.evaluate_candidate(
        eng, SS.build_candidate(_fire(MINT_S, "SER", NOW), NOW), NOW)
    check("REAL FILTER: 3 launches/24h -> serial_deployer reject",
          v["verdict"] == "reject" and v["reason"] == "serial_deployer")
finally:
    restore()

idx = _pass_index(MINT_P, birth_ago=7200)
restore = _install_filter_fixture(
    idx, dict(_PASS_CURVE, created_ts=NOW - 7200), _FakeSaturation({}))
try:
    v = SEL.evaluate_candidate(
        eng, SS.build_candidate(_fire(MINT_P, "SLOW", NOW), NOW), NOW)
    check("REAL FILTER: TTG 7200s -> slow_curve reject",
          v["verdict"] == "reject" and v["reason"] == "slow_curve")
finally:
    restore()

restore = _install_filter_fixture(
    _pass_index(MINT_C), _PASS_CURVE,
    _FakeSaturation({"COPY": [int(NOW * 1000) - 100000,
                              int(NOW * 1000) - 200000,
                              int(NOW * 1000) - 300000,
                              int(NOW * 1000) - 400000]}))
try:
    v = SEL.evaluate_candidate(
        eng, SS.build_candidate(_fire(MINT_C, "COPY", NOW), NOW), NOW)
    check("REAL FILTER: 4 same-ticker launches -> copycat_ticker "
          "reject", v["verdict"] == "reject"
          and v["reason"] == "copycat_ticker")
finally:
    restore()

idx = _pass_index(MINT_U)
idx["by_mint"][MINT_U]["bonding_curve"] = ""
restore = _install_filter_fixture(
    idx, {"trades": None, "created_ts": None, "oldest_sig": None,
          "capped": False, "exhausted": False, "reason": "no curve"},
    _FakeSaturation({}))
try:
    v = SEL.evaluate_candidate(
        eng, SS.build_candidate(_fire(MINT_U, "NOC", NOW), NOW), NOW)
    check("REAL FILTER: no curve stats -> unknown_curve reject",
          v["verdict"] == "reject" and v["reason"] == "unknown_curve")
finally:
    restore()

idx = _pass_index(MINT_U)
idx["by_mint"][MINT_U]["trader"] = ""
restore = _install_filter_fixture(
    idx, dict(_PASS_CURVE, oldest_sig=None, exhausted=False),
    _FakeSaturation({}))
try:
    v = SEL.evaluate_candidate(
        eng, SS.build_candidate(_fire(MINT_U, "NOS", NOW), NOW), NOW)
    check("REAL FILTER: no creator resolvable -> unknown_serial "
          "reject", v["verdict"] == "reject"
          and v["reason"] == "unknown_serial")
finally:
    restore()

# ---------------- scan: fire+pass opens; fire+fail never reaches consider --
restore = _install_filter_fixture(_pass_index(MINT_P), _PASS_CURVE,
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(eng, signals=[_fire(MINT_P, "TEST", NOW)], now=NOW)
    check("scan: fire+pass opens", res["opened"] == 1)
    st = eng.account_state()
    check("ledger: 1 open, bankroll $99.00 after the $1 shot",
          st["n_open"] == 1 and abs(st["bankroll"] - 99.00) < 1e-6)
    d = eng._read()
    pos = d["positions"][MINT_P]
    check("entry stamped: source smart-watcher-select, detection "
          "smart-watcher, gate smart_select",
          pos["source"] == "smart-watcher-select"
          and pos["entry_features"].get("detection") == "smart-watcher"
          and pos["entry_features"].get("gate") == "smart_select")
    check("entry stamped: full filter values ride along",
          pos["entry_features"].get("select", {}).get("ttg_s") == 1800.0
          and pos["entry_features"]["select"].get("curve_trades") == 100)
    check("entry legs carry the STANDARD trail (0.30)",
          pos["legs"]["c"]["trail"] == 0.30)
finally:
    restore()

# dedupe-ever: same fire again, and a fresh fire for the same mint
restore = _install_filter_fixture(_pass_index(MINT_P), _PASS_CURVE,
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(eng, signals=[_fire(MINT_P, "TEST", NOW)], now=NOW)
    check("dedupe: same fire twice -> no second entry",
          res["opened"] == 0)
    res = SS.scan_once(eng, signals=[_fire(MINT_P, "TEST", NOW + 5)],
                       now=NOW + 5)
    check("dedupe: fresh fire for an open mint -> no second entry",
          res["opened"] == 0 and eng.account_state()["n_open"] == 1)
finally:
    restore()

# filter-fail scan: consider must NEVER be called, for ANY reason.
# NOTE (2026-10-28 deferred-evaluation fix): unknown_canonical is
# NO LONGER a rejection — a pre-graduation fire parks (see the
# dedicated block below). The five genuine filter failures below
# still go straight to dead_mints.
orig_eval = SEL.evaluate_candidate
orig_consider = eng.consider_candidate
calls = {"n": 0}


def _counting_consider(cand, now):
    calls["n"] += 1
    return orig_consider(cand, now)


eng.consider_candidate = _counting_consider
try:
    for i, reason in enumerate(["serial_deployer", "slow_curve",
                                "copycat_ticker", "unknown_serial",
                                "unknown_curve"]):
        mint = "SelMintFail%d1111111111111111111111111pump" % i
        SEL.evaluate_candidate = lambda e, c, n, r=reason: {
            "verdict": "reject", "reason": r, "values": {}}
        before = calls["n"]
        res = SS.scan_once(eng, signals=[_fire(mint, "FL", NOW + 10 + i)],
                           now=NOW + 10 + i)
        d = eng._read()
        check("scan: %s reject -> no entry, consider never called, "
              "dead_mint stamped" % reason,
              res["opened"] == 0 and calls["n"] == before
              and mint in d["dead_mints"]
              and d["dead_mints"][mint]["reason"]
              == "smart_select: %s" % reason
              and d["select_journal"]["counts"].get(reason) == 1)
    # unknown_canonical -> PARKED, not rejected (deferred
    # evaluation): the coin has not graduated yet, so the filter
    # cannot be evaluated. No entry, consider never called, NO
    # dead_mint, NOT journaled as a rejection.
    mint = "SelMintFail51111111111111111111111111pump"
    SEL.evaluate_candidate = lambda e, c, n: {
        "verdict": "reject", "reason": "unknown_canonical",
        "values": {}}
    before = calls["n"]
    res = SS.scan_once(eng, signals=[_fire(mint, "FL", NOW + 15)],
                       now=NOW + 15)
    d = eng._read()
    check("scan: unknown_canonical -> PARKED (not rejected), no "
          "entry, consider never called, no dead_mint, no "
          "rejection journal",
          res["opened"] == 0 and calls["n"] == before
          and res["parked"] == 1
          and mint in d["watchlist"]
          and d["watchlist"][mint].get("parked") is True
          and mint not in d["dead_mints"]
          and d["select_journal"]["counts"].get(
              "unknown_canonical") is None
          and any(a["mint"] == mint and a["outcome"] == "parked"
                  for a in d["fire_audit"]))
finally:
    SEL.evaluate_candidate = orig_eval
    eng.consider_candidate = orig_consider

# ---------------- deferred evaluation (2026-10-28 fix) ----------------
# A pre-graduation fire PARKS; the coin is evaluated at graduation.
# Fresh engine (isolated ledger) so journal counts are exact.
engD = SS.make_engine()
try:
    engD._ks.STATE_DIR = _ks_tmp
    engD._ks.STATE_FILE = os.path.join(_ks_tmp, "state.json")
except Exception:
    pass
_tmpD = tempfile.mkdtemp(prefix="smart-select-deferred-")
engD.LEDGER_PATH = os.path.join(_tmpD, "smart_select_ledger.json")
engD.ensure_ledger()
with engD._ledger_lock():
    d = engD._read()
    d["created_ts"] = NOW - 3600
    engD._write(d)
engD.quote_price = lambda chain, mint, spot_index=None: 0.001
engD._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                 "usd_market_cap": 60000.0,
                                 "tape_source": "test"}
engD._safety = _ExplodingSafety()


def _mig_index(mint, mig_ts, trader="TraderD", birth_ago=1800,
               launches=None):
    return {"by_mig": {mint: {"pool": "PoolD", "ts": mig_ts}},
            "by_mint": {mint: {"trader": trader,
                               "ts": mig_ts - birth_ago,
                               "bonding_curve": "CurveD"}},
            "by_trader": {trader: (launches if launches is not None
                                   else [mig_ts - birth_ago])},
            "coverage_start": NOW - 86400 * 3,
            "available": True}


_EMPTY_INDEX = {"by_mig": {}, "by_mint": {}, "by_trader": {},
                "coverage_start": NOW - 86400 * 3, "available": True}


def _mig_curve(mig_ts, birth_ago=1800):
    return {"trades": 100, "created_ts": mig_ts - birth_ago,
            "oldest_sig": None, "capped": False, "exhausted": True,
            "reason": None}


# (a) fire pre-graduation -> PARKED, not entered (real filter,
# empty migration index -> unknown_canonical -> park).
_MINT_DEF = "SelMintDef1111111111111111111111111111pump"
_T0 = NOW - 1000
restore = _install_filter_fixture(_EMPTY_INDEX, _mig_curve(_T0),
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[_fire(_MINT_DEF, "DEF", _T0)],
                       now=_T0)
    d = engD._read()
    check("deferred (a): pre-graduation fire -> parked, NOT entered",
          res["parked"] == 1 and res["opened"] == 0
          and _MINT_DEF in d["watchlist"]
          and d["watchlist"][_MINT_DEF].get("parked") is True
          and _MINT_DEF not in d["positions"]
          and _MINT_DEF not in d["dead_mints"])
finally:
    restore()

# (b) graduation + all four filters pass -> ENTERS, stamped at
# evaluation (graduation) time, not fire time.
_T1 = _T0 + 60
restore = _install_filter_fixture(_mig_index(_MINT_DEF, _T1),
                                  _mig_curve(_T1),
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[], now=_T1)
    d = engD._read()
    pos = d["positions"].get(_MINT_DEF)
    check("deferred (b): graduation + all filters pass -> enters",
          res["opened"] == 1 and pos is not None
          and pos["source"] == "smart-watcher-select"
          and pos["entry_features"].get("detection") == "smart-watcher"
          and pos["entry_features"].get("gate") == "smart_select")
    check("deferred (b): entry stamped at evaluation time "
          "(migration ts), fire ts preserved for journaling",
          pos is not None
          and pos.get("born_ts") == _T1
          and pos["entry_features"].get("fire_ts") == _T0
          and pos["entry_features"].get("select", {}).get("ttg_s")
          == 1800.0)
    check("deferred (b): audit trail shows park then open",
          [a["outcome"] for a in d["fire_audit"]
           if a["mint"] == _MINT_DEF] == ["parked", "pending",
                                          "opened"])
finally:
    restore()

# (f, entered case) dedupe-ever: a fresh fire for an entered mint
# never opens a second position.
restore = _install_filter_fixture(_mig_index(_MINT_DEF, _T1),
                                  _mig_curve(_T1),
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[_fire(_MINT_DEF, "DEF", _T1)],
                       now=_T1)
    check("deferred (f): fresh fire for an entered mint -> no "
          "second entry",
          res["opened"] == 0
          and engD.account_state()["n_open"] == 1)
finally:
    restore()

# (c) graduation + a filter fails -> does NOT enter (serial).
_MINT_SER = "SelMintDefSer111111111111111111111111pump"
_T2 = NOW - 800
restore = _install_filter_fixture(_EMPTY_INDEX, _mig_curve(_T2),
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[_fire(_MINT_SER, "SER", _T2)],
                       now=_T2)
    check("deferred (c) setup: serial candidate parks pre-graduation",
          res["parked"] == 1)
finally:
    restore()
_T3 = _T2 + 60
restore = _install_filter_fixture(
    _mig_index(_MINT_SER, _T3, trader="SerialD",
               launches=[_T3 - 100, _T3 - 200, _T3 - 300]),
    _mig_curve(_T3), _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[], now=_T3)
    d = engD._read()
    check("deferred (c): graduation + serial_deployer -> NO entry, "
          "dead_mint, rejection journaled",
          res["opened"] == 0
          and _MINT_SER not in d["positions"]
          and _MINT_SER in d["dead_mints"]
          and d["dead_mints"][_MINT_SER]["reason"]
          == "smart_select: serial_deployer"
          and d["select_journal"]["counts"].get(
              "serial_deployer") == 1
          and any(a["mint"] == _MINT_SER
                  and a["outcome"] == "blocked"
                  and "deferred reject" in a["reason"]
                  for a in d["fire_audit"]))
finally:
    restore()

# (d) unknown at evaluation -> does NOT enter (fail-closed).
_MINT_UNK = "SelMintDefUnk111111111111111111111111pump"
_T4 = NOW - 600
restore = _install_filter_fixture(_EMPTY_INDEX, _mig_curve(_T4),
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[_fire(_MINT_UNK, "UNK", _T4)],
                       now=_T4)
    check("deferred (d) setup: unknown-curve candidate parks",
          res["parked"] == 1)
finally:
    restore()
_T5 = _T4 + 60
_idx_unk = _mig_index(_MINT_UNK, _T5)
_idx_unk["by_mint"][_MINT_UNK]["bonding_curve"] = ""
restore = _install_filter_fixture(
    _idx_unk,
    {"trades": None, "created_ts": None, "oldest_sig": None,
     "capped": False, "exhausted": False, "reason": "no curve"},
    _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[], now=_T5)
    d = engD._read()
    check("deferred (d): graduation + unknown_curve at evaluation "
          "-> NO entry, fail-closed reject",
          res["opened"] == 0
          and _MINT_UNK not in d["positions"]
          and _MINT_UNK in d["dead_mints"]
          and d["dead_mints"][_MINT_UNK]["reason"]
          == "smart_select: unknown_curve"
          and d["select_journal"]["counts"].get("unknown_curve") == 1)
finally:
    restore()

# (e) window expiry -> dropped with reason; (f, dropped case)
# dedupe-ever: a later fire never re-enters.
_MINT_EXP = "SelMintDefExp111111111111111111111111pump"
_T6 = NOW - 400
restore = _install_filter_fixture(_EMPTY_INDEX, _mig_curve(_T6),
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[_fire(_MINT_EXP, "EXP", _T6)],
                       now=_T6)
    check("deferred (e) setup: expiry candidate parks",
          res["parked"] == 1)
finally:
    restore()
restore = _install_filter_fixture(_EMPTY_INDEX, _mig_curve(_T6),
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[], now=_T6 + 1801)
    d = engD._read()
    check("deferred (e): no migration within 1800s of fire -> "
          "dropped with window-expiry reason",
          res["expired"] == 1
          and _MINT_EXP not in d["watchlist"]
          and _MINT_EXP in d["dead_mints"]
          and "graduation window expired"
          in d["dead_mints"][_MINT_EXP]["reason"]
          and any(a["mint"] == _MINT_EXP
                  and a["outcome"] == "dropped"
                  for a in d["fire_audit"]))
finally:
    restore()
_T7 = _T6 + 1900
restore = _install_filter_fixture(_mig_index(_MINT_EXP, _T7),
                                  _mig_curve(_T7),
                                  _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[_fire(_MINT_EXP, "EXP", _T7)],
                       now=_T7)
    d = engD._read()
    check("deferred (f): fresh fire for an expired mint -> "
          "skipped, never re-enters",
          res["opened"] == 0
          and _MINT_EXP not in d["positions"]
          and _MINT_EXP in d["dead_mints"])
finally:
    restore()

# (g) already-graduated fire still evaluates IMMEDIATELY (no park):
# a graduated fire failing a filter goes straight to dead_mints.
_MINT_IMM = "SelMintDefImm111111111111111111111111pump"
_T8 = NOW - 200
restore = _install_filter_fixture(
    _mig_index(_MINT_IMM, _T8, trader="SerialD",
               launches=[_T8 - 100, _T8 - 200, _T8 - 300]),
    _mig_curve(_T8), _FakeSaturation({}))
try:
    res = SS.scan_once(engD, signals=[_fire(_MINT_IMM, "IMM", _T8)],
                       now=_T8)
    d = engD._read()
    check("deferred (g): graduated fire + serial fail -> immediate "
          "dead_mint, never parked",
          res["parked"] == 0 and res["opened"] == 0
          and _MINT_IMM in d["dead_mints"]
          and _MINT_IMM not in d["watchlist"]
          and not any(a["mint"] == _MINT_IMM
                      and a["outcome"] == "parked"
                      for a in d["fire_audit"]))
finally:
    restore()

# ---------------- standard ladder exits ----------------
# Drive MINT_P to 4x: legs A (2x) and B (3x) fire, leg C peaks 4x.
px = {"v": 0.004}
eng.poll_exits(price_fn=lambda c, m, s: px["v"])
d = eng._read()
pos = d["positions"][MINT_P]
check("ladder: legs A and B fired at 4x",
      pos["legs"]["a"]["done"] and pos["legs"]["b"]["done"])
check("leg C peak tracked to 4x",
      abs(pos["legs"]["c"]["peak"] - 0.004) < 1e-9)
px["v"] = 0.003  # 25% below the 0.004 peak
eng.poll_exits(price_fn=lambda c, m, s: px["v"])
d = eng._read()
check("TRAIL 30%: 25% drawdown does NOT close leg C",
      MINT_P in d["positions"]
      and not d["positions"][MINT_P]["legs"]["c"]["done"])
px["v"] = 0.0026  # 35% below the 0.004 peak
eng.poll_exits(price_fn=lambda c, m, s: px["v"])
d = eng._read()
check("TRAIL 30%: 35% drawdown closes leg C -> ladder_complete",
      MINT_P not in d["positions"] and len(d["closes"]) == 1
      and d["closes"][0]["closed_reason"] == "ladder_complete"
      and d["closes"][0]["lane"] == "smart_select")

# hard stop: fresh position, -60% closes at hard_stop_-50pct
restore = _install_filter_fixture(
    _pass_index("SelMintHard1111111111111111111111111pump"),
    _PASS_CURVE, _FakeSaturation({}))
try:
    res = SS.scan_once(
        eng,
        signals=[_fire("SelMintHard1111111111111111111111111pump",
                       "HRD", NOW + 20)], now=NOW + 20)
    check("hard-stop setup: position opened", res["opened"] == 1)
finally:
    restore()
eng.poll_exits(price_fn=lambda c, m, s: 0.0004)  # -60%
d = eng._read()
check("hard stop intact: -60% closes at hard_stop_-50pct",
      "SelMintHard1111111111111111111111111pump" not in d["positions"]
      and any(c["mint"] == "SelMintHard1111111111111111111111111pump"
              and c["closed_reason"] == "hard_stop_-50pct"
              for c in d["closes"]))

# time stop: 25h-old position closes at 24h rule
restore = _install_filter_fixture(
    _pass_index("SelMintTime1111111111111111111111111pump"),
    _PASS_CURVE, _FakeSaturation({}))
try:
    res = SS.scan_once(
        eng,
        signals=[_fire("SelMintTime1111111111111111111111111pump",
                       "TME", NOW + 30)], now=NOW + 30)
    check("time-stop setup: position opened", res["opened"] == 1)
finally:
    restore()
with eng._ledger_lock():
    d = eng._read()
    d["positions"]["SelMintTime1111111111111111111111111pump"][
        "entry_ts"] = NOW - 25 * 3600
    eng._write(d)
eng.poll_exits(price_fn=lambda c, m, s: 0.001)  # flat 1.0x
d = eng._read()
check("TIME STOP 24h: 25h-old position closes (unlike moon)",
      "SelMintTime1111111111111111111111111pump" not in d["positions"]
      and any(c["mint"] == "SelMintTime1111111111111111111111111pump"
              and c["closed_reason"] == "time_stop_24h"
              for c in d["closes"]))

# ---------------- cost gate blocks ----------------
eng2 = SS.make_engine()
try:
    eng2._ks.STATE_DIR = _ks_tmp
    eng2._ks.STATE_FILE = os.path.join(_ks_tmp, "state.json")
except Exception:
    pass
_tmp2 = tempfile.mkdtemp(prefix="smart-select-cost-")
eng2.LEDGER_PATH = os.path.join(_tmp2, "smart_select_ledger.json")
eng2.ensure_ledger()
with eng2._ledger_lock():
    d = eng2._read()
    d["created_ts"] = NOW - 3600
    eng2._write(d)
eng2.quote_price = lambda chain, mint, spot_index=None: 0.001
eng2._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                 "usd_market_cap": 60000.0,
                                 "tape_source": "test"}
eng2._safety = _ExplodingSafety()


class _BlockingCosts:
    def tx_cost_usd(self, *a, **k):
        return 0.0

    def cost_gate(self, *a, **k):
        return True, ("COST-BLOCKED: modeled round-trip $0.60 "
                      "(60.0%) exceeds 25% of $1.00 notional")

    def cost_summary(self, *a, **k):
        return {}


eng2._costs = _BlockingCosts()
restore = _install_filter_fixture(
    _pass_index("SelMintCost1111111111111111111111111pump"),
    _PASS_CURVE, _FakeSaturation({}))
try:
    res = SS.scan_once(
        eng2,
        signals=[_fire("SelMintCost1111111111111111111111111pump",
                       "CST", NOW + 40)], now=NOW + 40)
    d = eng2._read()
    check("cost gate: 60% modeled cost -> NO entry, watchlist "
          "dropped as blocked",
          res["opened"] == 0
          and "SelMintCost1111111111111111111111111pump"
          not in d["positions"])
    check("cost gate: fire audit records the COST-BLOCKED reason",
          any(a["outcome"] == "blocked" and "COST-BLOCKED" in a["reason"]
              for a in d["fire_audit"]))
finally:
    restore()

# ---------------- verdict reporting: raw + ex-top-1 + head-to-head -------
eng3 = SS.make_engine()
try:
    eng3._ks.STATE_DIR = _ks_tmp
    eng3._ks.STATE_FILE = os.path.join(_ks_tmp, "state.json")
except Exception:
    pass
_tmp3 = tempfile.mkdtemp(prefix="smart-select-verdict-")
eng3.LEDGER_PATH = os.path.join(_tmp3, "smart_select_ledger.json")
SS.GRAD_SELECT_LEDGER_PATH = os.path.join(_tmp3, "grad_select.json")
eng3.ensure_ledger()
with eng3._ledger_lock():
    d = eng3._read()
    d["created_ts"] = NOW - 7200
    d["closes"] = [
        {"mint": "a", "pnl_usd": 10.0, "close_ts": NOW - 3600},
        {"mint": "b", "pnl_usd": -1.0, "close_ts": NOW - 1800},
        {"mint": "c", "pnl_usd": -1.0, "close_ts": NOW - 900},
    ]
    eng3._write(d)
with open(SS.GRAD_SELECT_LEDGER_PATH, "w") as f:
    json.dump({"closes": [
        # in-window, with notional -> counts (per-$1: +2.0, -0.04)
        {"mint": "g1", "pnl_usd": 50.0, "entry_notional_usd": 25.0,
         "close_ts": NOW - 3600},
        {"mint": "g2", "pnl_usd": -1.0, "entry_notional_usd": 25.0,
         "close_ts": NOW - 1800},
        # in-window but NO notional field -> skipped, never guessed
        {"mint": "g3", "pnl_usd": 999.0, "close_ts": NOW - 900},
        # out-of-window -> skipped
        {"mint": "g4", "pnl_usd": 999.0, "entry_notional_usd": 25.0,
         "close_ts": NOW - 99999},
    ]}, f)
rep = SS.verdict_report(eng3, end=NOW)
check("verdict: 3 closes, net +8.00, expectancy +2.666667",
      rep["n_closes"] == 3 and rep["net_usd"] == 8.0
      and abs(rep["expectancy_raw"] - 8.0 / 3) < 1e-6)
check("verdict: ex-top-1 drops the +10 -> -1.0",
      rep["expectancy_ex_top1"] == -1.0)
check("verdict: win rate 1/3", rep["win_rate"] == round(1 / 3, 4))
h2h = rep["head_to_head_grad_select_same_period"]
check("head-to-head: only 2 grad_select closes count (notional + "
      "in-window)", h2h["n_closes"] == 2)
check("head-to-head: per-$1 expectancy (2.0 + -0.04)/2 = 0.98",
      abs(h2h["expectancy_per_usd"] - 0.98) < 1e-6)
check("head-to-head: win rate 1/2", h2h["win_rate"] == 0.5)

# ---------------- bus-fire validation ----------------
check("fire validation: valid smart-watcher fire accepted",
      SS._is_smart_fire(_fire(MINT_P, "T", NOW)))
check("fire validation: wrong source rejected",
      not SS._is_smart_fire(dict(_fire(MINT_P, "T", NOW),
                                 source="degen-feed")))
check("fire validation: non-solana rejected",
      not SS._is_smart_fire(dict(_fire(MINT_P, "T", NOW),
                                 chain="base")))
check("fire validation: missing mint rejected",
      not SS._is_smart_fire(dict(_fire(MINT_P, "T", NOW), mint="")))
check("fire validation: missing version rejected (schema)",
      not SS._is_smart_fire({k: v for k, v in
                             _fire(MINT_P, "T", NOW).items()
                             if k != "v"}))

print()
if fails:
    print("FAILURES (%d): %s" % (len(fails), fails))
    sys.exit(1)
print("ALL SMART-SELECT TESTS PASSED")
