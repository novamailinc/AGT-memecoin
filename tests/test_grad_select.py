#!/usr/bin/env python3
"""Graduation SELECT lane tests (2026-10-24).

Covers grad_select.py: killswitch registration (append-only),
engine isolation + policy, the pre-registered terms, and the four
evidence filters as unit evaluations with every data source
stubbed (recorder index, curve pagination, saturation tally):
serial deployer rejected, copycat rejected, non-canonical
rejected, slow curve rejected, many-trades curve rejected,
unknown inputs rejected (fail-closed), and a clean fast
graduation ACCEPTED end-to-end through scan_once with the safety
module rigged to explode if consulted. Plus the verdict report
(raw + ex-top-1 + same-window head-to-head vs grad_raw).
No network. Plain script — run directly, not via pytest.
"""
import json
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad as GATED
from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import saturation as S

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
MINT_OK = "SelMintOK111111111111111111111111111111pump"
MINT_SER = "SelMintSer11111111111111111111111111111pump"
MINT_COPY = "SelMintCopy1111111111111111111111111111pump"
MINT_SLOW = "SelMintSlow1111111111111111111111111111pump"
MINT_MANY = "SelMintMany1111111111111111111111111111pump"
MINT_NC = "SelMintNC111111111111111111111111111111pump"
MINT_UNK = "SelMintUnk11111111111111111111111111111pump"
MINT_FB = "SelMintFB111111111111111111111111111111pump"
TRADER_OK = "TraderOK1111111111111111111111111111111111"
TRADER_SER = "TraderSerial111111111111111111111111111111"

# ---------------- killswitch registration (append-only) ----------------
check("killswitch: grad_select registered (window/min 100, halt)",
      "grad_select" in KS.CFG
      and KS.CFG["grad_select"]["window"] == 100
      and KS.CFG["grad_select"]["min_trades"] == 100
      and KS.CFG["grad_select"]["halt"] is True)
check("killswitch: other lanes unchanged",
      KS.CFG["grad_raw"]["window"] == 100
      and KS.CFG["grad"]["min_trades"] == 100
      and KS.CFG["watcher"]["halt"] is True)

_ks_tmp = tempfile.mkdtemp(prefix="gradsel-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")

# ---------------- engine isolation + policy ----------------
eng = GS.make_engine()
check("engine: lane is grad_select", eng.LANE == "grad_select")
check("engine: ledger is grad_select_ledger.json",
      eng.LEDGER_PATH.endswith("grad_select_ledger.json"))
check("engine: MAX_OPEN 20 (task terms)", eng.MAX_OPEN == 20)
check("engine: no daily cap (bankroll-bound)",
      eng.DAILY_ENTRY_CAP is None)
check("engine: age window 1800s like grad_raw",
      eng.MAX_GRAD_AGE_S == 1800.0)
check("engine: safety override behaves as the no-gate policy",
      eng._safety_ok("anymint") == (True, "", None, False)
      and eng._veto_ok("anymint", "SYM", None) == (True, ""))
check("gated lane untouched", GATED.LANE == "grad"
      and GATED.LEDGER_PATH.endswith("grad_ledger.json"))
check("raw lane untouched", GR.LANE == "grad_raw"
      and GR.LEDGER_PATH.endswith("grad_raw_ledger.json"))

_tmp = tempfile.mkdtemp(prefix="gradsel-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_select_ledger.json")
led = eng.ensure_ledger()
pre = led["preregistered_verdict"]
check("preregistration: lane grad_select, sample 100",
      pre["lane"] == "grad_select" and pre["sample_closes"] == 100)
check("preregistration: terms declare NO safety gate / NO veto",
      "NONE" in pre["terms"]["safety_gate"]
      and pre["terms"]["veto"] == "NONE")
check("preregistration: terms declare all four filters",
      all(k in pre["terms"]["filters"] for k in (
          "serial_deployer", "fast_curve_few_trades",
          "canonical_only", "copycat_ticker")))
check("preregistration: rule promises raw + ex-top-1 + head-to-head",
      "EX-TOP-1" in pre["rule"] and "head-to-head" in pre["rule"])

# stub the price layer (no network); safety must NEVER be consulted
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}


class _ExplodingSafety:
    def check_token(self, *a, **k):
        raise AssertionError("safety consulted on the SELECT lane")


eng._safety = _ExplodingSafety()

# ---------------- filter data stubs ----------------
# Recorder index: fully in-memory, refresh disabled.
GS._index_refresh = lambda now: None
GS._INDEX["available"] = True
GS._INDEX["coverage_start"] = NOW - 90000


def _birth(mint, trader, ts, curve="Curve111"):
    GS._INDEX["by_mint"][mint] = {
        "trader": trader, "ts": ts, "bonding_curve": curve}


# by_trader launch logs are populated explicitly per scenario:
# TRADER_OK has exactly ONE launch in the trailing 24h; the
# serial deployer has three. Other mints get their own traders
# (absent from by_trader = zero other launches counted).
_birth(MINT_OK, TRADER_OK, NOW - 600)
GS._INDEX["by_trader"][TRADER_OK] = [NOW - 600]
_birth(MINT_SER, TRADER_SER, NOW - 300)
GS._INDEX["by_trader"][TRADER_SER] = [NOW - 3600, NOW - 3500, NOW - 300]
_birth(MINT_COPY, "TraderCopy11111111111111111111111111111111",
       NOW - 900)
_birth(MINT_SLOW, "TraderSlow11111111111111111111111111111111",
       NOW - 7200)
_birth(MINT_MANY, "TraderMany11111111111111111111111111111111",
       NOW - 400)
_birth(MINT_NC, "TraderNC1111111111111111111111111111111111",
       NOW - 300)
GS._INDEX["by_mig"][MINT_NC] = {"pool": "RealPool", "ts": NOW - 60}

_CURVE = {
    MINT_OK: {"trades": 80, "created_ts": NOW - 600,
              "oldest_sig": "sig-ok", "capped": False,
              "exhausted": True},
    MINT_SER: {"trades": 90, "created_ts": NOW - 300,
               "oldest_sig": "sig-ser", "capped": False,
               "exhausted": True},
    MINT_COPY: {"trades": 70, "created_ts": NOW - 900,
                "oldest_sig": "sig-copy", "capped": False,
                "exhausted": True},
    MINT_SLOW: {"trades": 50, "created_ts": NOW - 7200,
                "oldest_sig": "sig-slow", "capped": False,
                "exhausted": True},
    MINT_MANY: {"trades": 900, "created_ts": None,
                "oldest_sig": None, "capped": True,
                "exhausted": False},
    MINT_NC: {"trades": 60, "created_ts": NOW - 300,
              "oldest_sig": "sig-nc", "capped": False,
              "exhausted": True},
    MINT_UNK: {"trades": None, "created_ts": None,
               "oldest_sig": None, "capped": False,
               "exhausted": False},
    MINT_FB: {"trades": 40, "created_ts": NOW - 500,
              "oldest_sig": "sig-fb", "capped": False,
              "exhausted": True},
}
def _fake_curve_stats(addr):
    # the stub keys on the curve address the evaluator derived;
    # tests give every mint the same curve string, so resolve via
    # the currently-evaluated mint recorded by the wrapper below.
    return _CURVE[_fake_curve_stats.mint]


GS._curve_stats = _fake_curve_stats
GS._creator_from_oldest_tx = lambda sig: {"sig-fb": TRADER_OK}.get(sig)
# Canonical chain fallback (2026-11-20): stubbed to fail closed so
# this suite stays offline — the fallback's live behavior is covered
# by tests/test_canonical_fallback.py with a fake reserves module.
GS._canonical_pool_from_chain = \
    lambda curve_addr, pool, created_ts: None


class _FakeSat:
    """Saturation stand-in: per-ticker launch timestamps (ms), with
    the rolling-count helper delegated to the real saturation.py."""

    def __init__(self, launches):
        self.launches = launches  # norm symbol -> [created_ts_ms, ...]
        self.updated = NOW

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": dict(self.launches)}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


_SAT = _FakeSat({})
GS._saturation = _SAT


def _stream_cand(mint, symbol):
    return {"mint": mint, "symbol": symbol, "created_ts": NOW,
            "dex": "pumpswap", "pool_address": "PoolX",
            "migration_sig": "", "reserve_usd": 0.0, "fdv_usd": 0.0,
            "source": "pumpportal-migration"}


def _eval(mint, symbol):
    _fake_curve_stats.mint = mint
    return GS.evaluate_candidate(eng, _stream_cand(mint, symbol), NOW)


# ---------------- filter unit evaluations ----------------
v = _eval(MINT_OK, "OKONE")
check("filter: clean fast graduation PASSES",
      v["verdict"] == "pass")
check("filter: pass journals measured values",
      v["values"].get("creator") == TRADER_OK
      and v["values"].get("deployer_launches_24h") == 1
      and v["values"].get("curve_trades") == 80
      and abs(v["values"].get("ttg_s", -1) - 600) < 2
      and v["values"].get("canonical_basis") == "migration_event")

v = _eval(MINT_SER, "SERONE")
check("filter: serial deployer (>=3 launches/24h) REJECTED",
      v["verdict"] == "reject" and v["reason"] == "serial_deployer"
      and v["values"].get("deployer_launches_24h") >= 3)

# tally incl. self (4 launches inside 24h -> 3 others)
_SAT.launches = {"COPYCAT": [int(NOW * 1000) - 100000,
                             int(NOW * 1000) - 200000,
                             int(NOW * 1000) - 300000,
                             int(NOW * 1000) - 400000]}
v = _eval(MINT_COPY, "CopyCat")
check("filter: copycat ticker (>=3 others/24h) REJECTED",
      v["verdict"] == "reject" and v["reason"] == "copycat_ticker"
      and v["values"].get("ticker_others_24h") == 3)
_SAT.launches = {}

_SAT.launches = {"?": [int(NOW * 1000) - i * 100000
                       for i in range(1, 7)]}  # literal '?' census
v = _eval(MINT_COPY, "?")
check("filter: unknown-symbol '?' placeholder SKIPS copycat lookup",
      v.get("reason") != "copycat_ticker"
      and v["values"].get("ticker_tally_24h") == 0
      and v["values"].get("ticker_others_24h") == 0)
_SAT.launches = {}

# -- regression: rolling 24h window (2026-11-08 window bug) --------
# Mates 25-47h old must NOT count (the DOPA/SHANE case: the old
# two-bucket sum rejected them on 98/65 "others" when the true
# rolling-24h count was 0/1).
_SAT.launches = {"STALE": sorted([
    int(NOW * 1000) - 900000,            # self (MINT_COPY, NOW-900)
    int(NOW * 1000) - 25 * 3600 * 1000,
    int(NOW * 1000) - 30 * 3600 * 1000,
    int(NOW * 1000) - 47 * 3600 * 1000])}
v = _eval(MINT_COPY, "Stale")
# Conviction gate (2026-11-21, piece 1): this fixture is a WEAK
# candidate (new deployer + slow curve + bear 50, mcap unknown),
# so the gate reviews it instead of passing it — the rolling-24h
# window itself is still the 0 counted mates, which is what this
# test proves.
check("filter: copycat mates 25-47h old PASS (rolling 24h window)",
      v["verdict"] == "review"
      and v["reason"] == "conviction_review"
      and v["values"].get("ticker_others_24h") == 0)
_SAT.launches = {}

# >=3 mates inside 24h must still REJECT.
_SAT.launches = {"BUSY": sorted([
    int(NOW * 1000) - 900000,            # self
    int(NOW * 1000) - 3600 * 1000,
    int(NOW * 1000) - 2 * 3600 * 1000,
    int(NOW * 1000) - 3 * 3600 * 1000])}
v = _eval(MINT_COPY, "Busy")
check("filter: copycat >=3 mates inside 24h REJECTED",
      v["verdict"] == "reject" and v["reason"] == "copycat_ticker"
      and v["values"].get("ticker_others_24h") == 3)
_SAT.launches = {}

# Boundary: a mate at exactly now-24h is OUTSIDE the window
# (window is (now-24h, now]); a mate 1s inside counts.
_SAT.launches = {"EDGE": sorted([
    int(NOW * 1000) - 900000,            # self
    int(NOW * 1000) - 86400 * 1000,      # exactly 24h: excluded
    int(NOW * 1000) - 86399 * 1000])}    # 1s inside: counted
v = _eval(MINT_COPY, "Edge")
# Conviction gate (2026-11-21, piece 1): this fixture is a WEAK
# candidate (new deployer + slow curve + bear 50), so the gate
# reviews it instead of passing it — the copycat boundary itself
# is still the 1 counted mate, which is what this test proves.
check("filter: copycat mate at exactly 24h does NOT count",
      v["verdict"] == "review"
      and v["reason"] == "conviction_review"
      and v["values"].get("ticker_others_24h") == 1)
_SAT.launches = {}

v = _eval(MINT_SLOW, "SLOWONE")
check("filter: slow curve (TTG 2h) REJECTED",
      v["verdict"] == "reject" and v["reason"] == "slow_curve")

v = _eval(MINT_MANY, "MANYONE")
check("filter: many-trades curve (900 sigs) REJECTED",
      v["verdict"] == "reject" and v["reason"] == "many_trades_curve")

gt_nc = {"mint": MINT_NC, "symbol": "NCONE", "created_ts": NOW - 60,
         "dex": "pumpswap", "pool_address": "FakePool",
         "reserve_usd": 8000.0, "fdv_usd": 70000.0,
         "source": "geckoterminal-backfill"}
_fake_curve_stats.mint = MINT_NC
v = GS.evaluate_candidate(eng, gt_nc, NOW)
check("filter: backfill pool != migration pool REJECTED noncanonical",
      v["verdict"] == "reject" and v["reason"] == "noncanonical_pool")

gt_unk = dict(gt_nc)
gt_unk["mint"] = MINT_UNK
gt_unk["pool_address"] = "AnyPool"
_fake_curve_stats.mint = MINT_UNK
v = GS.evaluate_candidate(eng, gt_unk, NOW)
check("filter: backfill with no migration record REJECTED (fail-closed)",
      v["verdict"] == "reject" and v["reason"] == "unknown_canonical")

v = _eval(MINT_UNK, "UNKONE")
check("filter: unmeasurable curve REJECTED (fail-closed)",
      v["verdict"] == "reject" and v["reason"] in (
          "unknown_serial", "unknown_curve"))

# No birth record at all: creator must come from the curve's
# create transaction (exhausted pagination), then pass serial.
v = _eval(MINT_FB, "FBONE")
check("filter: creator via curve create-tx fallback PASSES",
      v["verdict"] == "pass"
      and v["values"].get("creator_source") == "curve_oldest_tx"
      and v["values"].get("creator") == TRADER_OK)

# ---------------- Rule A: entry-speed bar (2026-11-16) --------------
# Entries >30s after graduation lose money on the stored closes;
# the bar rejects them (slow_entry) and fails closed on unknown age.
def _aged_cand(mint, symbol, age_s):
    c = _stream_cand(mint, symbol)
    c["created_ts"] = NOW - age_s
    return c

_fake_curve_stats.mint = MINT_OK
v = GS.evaluate_candidate(eng, _aged_cand(MINT_OK, "OKONE", 0), NOW)
check("rule A: entry at age 0s PASSES and stamps grad_age_s",
      v["verdict"] == "pass"
      and abs(v["values"].get("grad_age_s", -1) - 0) < 1)

_fake_curve_stats.mint = MINT_OK
v = GS.evaluate_candidate(eng, _aged_cand(MINT_OK, "OKONE", 30), NOW)
check("rule A: entry at exactly 30s PASSES (boundary inclusive)",
      v["verdict"] == "pass")

_fake_curve_stats.mint = MINT_OK
v = GS.evaluate_candidate(eng, _aged_cand(MINT_OK, "OKONE", 31), NOW)
check("rule A: entry at 31s REJECTED slow_entry",
      v["verdict"] == "reject" and v["reason"] == "slow_entry"
      and abs(v["values"].get("grad_age_s", -1) - 31) < 1)

_fake_curve_stats.mint = MINT_OK
v = GS.evaluate_candidate(eng, _aged_cand(MINT_OK, "OKONE", 120), NOW)
check("rule A: stream entry at 120s REJECTED slow_entry",
      v["verdict"] == "reject" and v["reason"] == "slow_entry")

# Rule A on GT-backfill (2026-11-21, exit-study Rule 1): the
# backfill exemption is gone — a backfill row is gated on the
# same 30s bar (age = now - created_ts, the pool's creation
# second). Backfill rows are minutes old by construction, so
# in practice backfill is detection-only.
MINT_BF = "SelMintBF1111111111111111111111111111111pump"
_birth(MINT_BF, TRADER_OK, NOW - 600)
GS._INDEX["by_mig"][MINT_BF] = {"pool": "PoolBF", "ts": NOW - 120}
_CURVE[MINT_BF] = {"trades": 80, "created_ts": NOW - 600,
                   "oldest_sig": "sig-bf", "capped": False,
                   "exhausted": True}


def _bf_cand(age_s):
    return {"mint": MINT_BF, "symbol": "BFONE",
            "created_ts": NOW - age_s,
            "dex": "pumpswap", "pool_address": "PoolBF",
            "reserve_usd": 8000.0, "fdv_usd": 70000.0,
            "source": "geckoterminal-backfill"}


_fake_curve_stats.mint = MINT_BF
v = GS.evaluate_candidate(eng, _bf_cand(120), NOW)
check("rule A: backfill entry at 120s REJECTED slow_entry",
      v["verdict"] == "reject" and v["reason"] == "slow_entry"
      and abs(v["values"].get("grad_age_s", -1) - 120) < 1
      and v["values"].get("canonical_basis")
      == "recorder_migration_match")

_fake_curve_stats.mint = MINT_BF
v = GS.evaluate_candidate(eng, _bf_cand(10), NOW)
check("rule A: backfill entry at 10s PASSES (fresh backfill still enters)",
      v["verdict"] == "pass"
      and abs(v["values"].get("grad_age_s", -1) - 10) < 1)

c_noage = _stream_cand(MINT_OK, "OKONE")
del c_noage["created_ts"]
_fake_curve_stats.mint = MINT_OK
v = GS.evaluate_candidate(eng, c_noage, NOW)
check("rule A: missing created_ts REJECTED unknown_grad_age (fail-closed)",
      v["verdict"] == "reject" and v["reason"] == "unknown_grad_age")

# ---------------- scan_once integration ----------------
class FakeFeed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        out, self._items = self._items, []
        return out


def _mig_item(mint, symbol):
    return {"mint": mint, "symbol": symbol,
            "source": "pumpportal-migration", "created_ts": NOW,
            "extra": {"migration_pool": "pumpswap",
                      "market_id": "PoolX", "signature": ""}}


_fake_curve_stats.mint = MINT_OK
res = GS.scan_once(eng, feed=FakeFeed([_mig_item(MINT_OK, "OKONE")]),
                   now=NOW)
check("scan: clean stream graduation OPENED (safety never consulted)",
      res["opened"] == 1 and res["stream"] == 1)
d = eng._read()
pos = d["positions"][MINT_OK]
check("entry stamped: source graduation-select, gate=select",
      pos["source"] == "graduation-select"
      and pos["entry_features"].get("gate") == "select")
check("entry journals the select measurements",
      (pos["entry_features"].get("select") or {}).get(
          "curve_trades") == 80
      and (pos["entry_features"].get("select") or {}).get(
          "creator") == TRADER_OK)

_fake_curve_stats.mint = MINT_SER
res = GS.scan_once(eng, feed=FakeFeed([_mig_item(MINT_SER, "SERONE")]),
                   now=NOW + 5)
check("scan: serial deployer NOT opened, counted as filter-rejected",
      res["opened"] == 0 and res["select_rejected"] == 1
      and res["select_reasons"].get("serial_deployer") == 1)
d = eng._read()
check("rejection journaled in ledger select_journal",
      (d.get("select_journal") or {}).get("counts", {}).get(
          "serial_deployer") == 1)
check("rejected mint is dead with the select reason",
      "select: serial_deployer" in str(
          d["dead_mints"].get(MINT_SER, {}).get("reason", "")))

res = GS.scan_once(eng, feed=FakeFeed([_mig_item(MINT_OK, "OKONE")]),
                   now=NOW + 10)
check("dedupe: accepted mint never opens twice", res["opened"] == 0)

# ---------------- parallel filter stage (Phase 1 B1+C1) --------
# Three fresh candidates in ONE scan cycle: two clean (must open)
# and one serial deployer (must reject). They run through the
# parallel evaluator + single batched ledger write, so this also
# proves the workers do not mix candidates up: each open must
# carry its OWN creator, and the reject must be the serial one.
# The curve fake is keyed on the curve address (distinct per
# mint) so it is thread-safe — the old _fake_curve_stats resolves
# via a shared mutable attribute and would race here.
MINT_P1 = "ParMint11111111111111111111111111111111pump"
MINT_P2 = "ParMint22222222222222222222222222222222pump"
MINT_P3 = "ParMint33333333333333333333333333333333pump"
TRADER_P1 = "TraderP111111111111111111111111111111111"
TRADER_P2 = "TraderP211111111111111111111111111111111"
TRADER_P3 = "TraderP311111111111111111111111111111111"
_birth(MINT_P1, TRADER_P1, NOW - 600, curve="CurveP1")
GS._INDEX["by_trader"][TRADER_P1] = [NOW - 600]
_birth(MINT_P2, TRADER_P2, NOW - 300, curve="CurveP2")
GS._INDEX["by_trader"][TRADER_P2] = [NOW - 300]
_birth(MINT_P3, TRADER_P3, NOW - 400, curve="CurveP3")
GS._INDEX["by_trader"][TRADER_P3] = [NOW - 3600, NOW - 3500,
                                      NOW - 400]
_CURVE_PAR = {
    "CurveP1": {"trades": 80, "created_ts": NOW - 600,
                "oldest_sig": "sig-p1", "capped": False,
                "exhausted": True},
    "CurveP2": {"trades": 90, "created_ts": NOW - 300,
                "oldest_sig": "sig-p2", "capped": False,
                "exhausted": True},
    "CurveP3": {"trades": 70, "created_ts": NOW - 400,
                "oldest_sig": "sig-p3", "capped": False,
                "exhausted": True},
}
_prev_curve_stats = GS._curve_stats
GS._curve_stats = lambda addr: _CURVE_PAR[addr]
_SAT.launches = {}
res = GS.scan_once(
    eng, feed=FakeFeed([_mig_item(MINT_P1, "PARONE"),
                        _mig_item(MINT_P2, "PARTWO"),
                        _mig_item(MINT_P3, "PARTHR")]),
    now=NOW + 20)
check("scan parallel: 2 clean opened, 1 serial rejected in one cycle",
      res["opened"] == 2 and res["select_rejected"] == 1
      and res["select_reasons"].get("serial_deployer") == 1)
d = eng._read()
check("scan parallel: each open carries its OWN creator (no mix-up)",
      (d["positions"][MINT_P1]["entry_features"].get("select") or {}
       ).get("creator") == TRADER_P1
      and (d["positions"][MINT_P2]["entry_features"].get("select")
           or {}).get("creator") == TRADER_P2)
check("scan parallel: serial mint dead, journal count now 2",
      "select: serial_deployer" in str(
          d["dead_mints"].get(MINT_P3, {}).get("reason", ""))
      and (d.get("select_journal") or {}).get("counts", {}).get(
          "serial_deployer") == 2)
GS._curve_stats = _prev_curve_stats

# ---------------- verdict report ----------------
with eng._ledger_lock():
    d = eng._read()
    d["closes"] = [
        {"mint": "c1", "pnl_usd": 10.0, "close_ts": NOW + 100,
         "entry_ts": NOW},
        {"mint": "c2", "pnl_usd": -1.0, "close_ts": NOW + 200,
         "entry_ts": NOW},
        {"mint": "c3", "pnl_usd": -1.0, "close_ts": NOW + 300,
         "entry_ts": NOW},
    ]
    eng._write(d)
_raw_tmp = tempfile.mkdtemp(prefix="gradsel-raw-")
GS.GRAD_RAW_LEDGER_PATH = os.path.join(_raw_tmp, "grad_raw_ledger.json")
with open(GS.GRAD_RAW_LEDGER_PATH, "w") as f:
    json.dump({"closes": [
        {"mint": "r1", "pnl_usd": 2.0, "close_ts": NOW + 150},
        {"mint": "r2", "pnl_usd": -0.5, "close_ts": NOW + 250},
        {"mint": "r3", "pnl_usd": 99.0, "close_ts": NOW - 10 ** 7},
    ]}, f)
rep = GS.verdict_report(eng, end=NOW + 400)
check("verdict report: raw expectancy over the sample",
      rep["n_closes"] == 3
      and abs(rep["expectancy_raw"] - (8.0 / 3)) < 1e-6)
check("verdict report: ex-top-1 drops the single best close",
      abs(rep["expectancy_ex_top1"] - (-1.0)) < 1e-9)
h2h = rep["head_to_head_grad_raw_same_window"]
check("verdict report: head-to-head counts only same-window raw closes",
      h2h is not None and h2h["n_closes"] == 2
      and abs(h2h["expectancy"] - 0.75) < 1e-9)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
