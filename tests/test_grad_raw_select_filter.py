#!/usr/bin/env python3
"""Graduation RAW lane — select-filter port tests (2026-11-10).

Covers the filter port in grad_raw.py: entries are gated on
grad_select's four evidence filters via the SHARED module (no
copied logic), rejects are journaled per reason, passed entries
are stamped gate=select with the measured values, exits / stakes /
dedupe / killswitch are untouched, and a missing select module
fails closed. No network: every feed and filter input is stubbed.
Plain script — run directly, not via pytest.
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

# grad_select FIRST: grad_raw must bind this same module object
# (shared recorder index), not load a second copy.
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import saturation as S

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
MINT_OK = "RawFiltOK11111111111111111111111111111111pump"
MINT_SER = "RawFiltSER1111111111111111111111111111111pump"
MINT_COPY = "RawFiltCOPY111111111111111111111111111111pump"
MINT_SLOW = "RawFiltSLOW111111111111111111111111111111pump"
MINT_MANY = "RawFiltMANY111111111111111111111111111111pump"
MINT_NC = "RawFiltNC11111111111111111111111111111111pump"
MINT_FB = "RawFiltFB11111111111111111111111111111111pump"
MINT_FB2 = "RawFiltFB2111111111111111111111111111111pump"
MINT_UNK = "RawFiltUNK1111111111111111111111111111111pump"

TRADER_OK = "TraderFiltOK111111111111111111111111111111"
TRADER_SER = "TraderFiltSER11111111111111111111111111111"
TRADER_COPY = "TraderFiltCOPY1111111111111111111111111111"
TRADER_SLOW = "TraderFiltSLOW1111111111111111111111111111"
TRADER_MANY = "TraderFiltMANY1111111111111111111111111111"
TRADER_NC = "TraderFiltNC111111111111111111111111111111"
TRADER_FB = "TraderFiltFB111111111111111111111111111111"
TRADER_FB2 = "TraderFiltFB21111111111111111111111111111"

# ---------------- shared module binding ----------------
check("filter port: raw binds the SAME grad_select module object "
      "(shared index, no drift)", GR._select is GS)

# isolate killswitch state for the whole run
_ks_tmp = tempfile.mkdtemp(prefix="rawfilt-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")

# ---------------- engine ----------------
eng = GR.make_engine()
check("engine: lane is grad_raw", eng.LANE == "grad_raw")
check("engine: $25 stakes, MAX_OPEN and killswitch lane unchanged",
      eng.NOTIONAL_USD == 25.00 and eng.MAX_OPEN == 150
      and GR.LANE == "grad_raw")
_tmp = tempfile.mkdtemp(prefix="rawfilt-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_raw_ledger.json")
eng.ensure_ledger()

# stub the price layer (no network); safety must NEVER be consulted
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}


class _ExplodingSafety:
    def check_token(self, *a, **k):
        raise AssertionError("safety consulted on the RAW lane")


eng._safety = _ExplodingSafety()

# ---------------- filter data stubs ----------------
# Recorder index: fully in-memory, refresh disabled.
GS._index_refresh = lambda now: None
GS._INDEX["available"] = True
GS._INDEX["coverage_start"] = NOW - 90000
GS._INDEX["event_ts"] = [NOW - 86400 + i * 300 for i in range(289)]  # continuity fixture (2026-11-24)


def _birth(mint, trader, ts, curve):
    GS._INDEX["by_mint"][mint] = {
        "trader": trader, "ts": ts, "bonding_curve": curve}


_birth(MINT_OK, TRADER_OK, NOW - 600, "CurveOK")
GS._INDEX["by_trader"][TRADER_OK] = [NOW - 600]
_birth(MINT_SER, TRADER_SER, NOW - 300, "CurveSER")
GS._INDEX["by_trader"][TRADER_SER] = [NOW - 3600, NOW - 3500, NOW - 300]
_birth(MINT_COPY, TRADER_COPY, NOW - 900, "CurveCOPY")
GS._INDEX["by_trader"][TRADER_COPY] = [NOW - 900]
_birth(MINT_SLOW, TRADER_SLOW, NOW - 7200, "CurveSLOW")
GS._INDEX["by_trader"][TRADER_SLOW] = [NOW - 7200]
_birth(MINT_MANY, TRADER_MANY, NOW - 400, "CurveMANY")
GS._INDEX["by_trader"][TRADER_MANY] = [NOW - 400]
_birth(MINT_NC, TRADER_NC, NOW - 300, "CurveNC")
GS._INDEX["by_trader"][TRADER_NC] = [NOW - 300]
GS._INDEX["by_mig"][MINT_NC] = {"pool": "RealPool", "ts": NOW - 60}
_birth(MINT_FB, TRADER_FB, NOW - 500, "CurveFB")
GS._INDEX["by_trader"][TRADER_FB] = [NOW - 500]
GS._INDEX["by_mig"][MINT_FB] = {"pool": "PoolFB", "ts": NOW - 60}
_birth(MINT_FB2, TRADER_FB2, NOW - 500, "CurveFB2")
GS._INDEX["by_trader"][TRADER_FB2] = [NOW - 500]
# 2026-11-24 conservative contract: recorder-only sub-threshold
# counts never admit; stub the authoritative-history hook with this
# fixture's INTENDED complete per-creator history (explicit table,
# never derived from the recorder index).
GS._AUTHORITATIVE_LAUNCHES_24H = lambda c, n: GS.authoritative_proof(
    c, {TRADER_OK: 1, TRADER_COPY: 1, TRADER_SLOW: 1, TRADER_MANY: 1,
        TRADER_NC: 1, TRADER_FB: 1, TRADER_FB2: 1}.get(c, 0), n)
GS._INDEX["by_mig"][MINT_FB2] = {"pool": "PoolFB2", "ts": NOW - 60}
# MINT_UNK deliberately has NO birth record (unknown everything).

_CURVE = {
    "CurveOK": {"trades": 80, "created_ts": NOW - 600,
                "oldest_sig": "sig-ok", "capped": False,
                "exhausted": True},
    "CurveSER": {"trades": 90, "created_ts": NOW - 300,
                 "oldest_sig": "sig-ser", "capped": False,
                 "exhausted": True},
    "CurveCOPY": {"trades": 70, "created_ts": NOW - 900,
                  "oldest_sig": "sig-copy", "capped": False,
                  "exhausted": True},
    "CurveSLOW": {"trades": 50, "created_ts": NOW - 7200,
                  "oldest_sig": "sig-slow", "capped": False,
                  "exhausted": True},
    "CurveMANY": {"trades": 900, "created_ts": None,
                  "oldest_sig": None, "capped": True,
                  "exhausted": False},
    "CurveNC": {"trades": 60, "created_ts": NOW - 300,
                "oldest_sig": "sig-nc", "capped": False,
                "exhausted": True},
    "CurveFB": {"trades": 40, "created_ts": NOW - 500,
                "oldest_sig": "sig-fb", "capped": False,
                "exhausted": True},
    "CurveFB2": {"trades": 40, "created_ts": NOW - 500,
                 "oldest_sig": "sig-fb2", "capped": False,
                 "exhausted": True},
}
GS._curve_stats = lambda addr: _CURVE.get(addr) or {
    "trades": None, "created_ts": None, "oldest_sig": None,
    "capped": False, "exhausted": False}


class _FakeSat:
    """Saturation stand-in: ticker COPY launched 4x in 24h (the
    candidate's own birth is one of them, so others = 3 -> reject);
    every other ticker is unlaunched."""

    def __init__(self):
        self.updated = NOW
        self.launches = {"COPY": [
            int((NOW - 7200) * 1000), int((NOW - 3600) * 1000),
            int((NOW - 1800) * 1000), int((NOW - 900) * 1000)]}

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": dict(self.launches)}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


GS._saturation = _FakeSat()


class FakeFeed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        out, self._items = self._items, []
        return out


def _mig(mint, symbol):
    return {"mint": mint, "symbol": symbol,
            "source": "pumpportal-migration", "created_ts": NOW,
            "extra": {"migration_pool": "pumpswap",
                      "market_id": "PoolX"}}


def _journal():
    d = eng._read()
    return (d.get("select_journal") or {}).get("counts") or {}


# ---------------- clean entry passes and is stamped ----------------
res = GR.scan_once(eng, feed=FakeFeed([_mig(MINT_OK, "OKONE")]),
                   now=NOW)
check("filter: clean fast graduation OPENS",
      res["opened"] == 1 and res.get("select_rejected") == 0)
d = eng._read()
pos = d["positions"][MINT_OK]
check("filter: passed entry stamped gate=select + measured values",
      pos["entry_features"].get("gate") == "select"
      and pos["entry_features"].get("select", {}).get("creator")
      == TRADER_OK
      and pos["entry_features"]["select"].get("curve_trades") == 80
      and pos["entry_features"]["select"].get("ticker_others_24h") == 0)
check("filter: no rejections journaled for the pass",
      _journal() == {})

# dedupe-ever still holds for a passed entry
res = GR.scan_once(eng, feed=FakeFeed([_mig(MINT_OK, "OKONE")]),
                   now=NOW + 5)
check("dedupe: same mint never opens twice", res["opened"] == 0)

# ---------------- each filter rejects with its own reason ----------------
res = GR.scan_once(eng, feed=FakeFeed([_mig(MINT_SER, "SERONE")]),
                   now=NOW + 10)
check("filter: serial deployer REJECTED (no entry)",
      res["opened"] == 0 and res.get("select_rejected") == 1
      and res.get("select_reasons") == {"serial_deployer": 1})
d = eng._read()
check("filter: serial reject lands in dead_mints with the reason",
      d["dead_mints"].get(MINT_SER, {}).get("reason")
      == "select: serial_deployer")
check("filter: serial reject journaled",
      _journal().get("serial_deployer") == 1)

res = GR.scan_once(eng, feed=FakeFeed([_mig(MINT_COPY, "COPY")]),
                   now=NOW + 20)
check("filter: copycat ticker REJECTED",
      res["opened"] == 0 and res.get("select_rejected") == 1
      and res.get("select_reasons") == {"copycat_ticker": 1})
check("filter: copycat reject journaled",
      _journal().get("copycat_ticker") == 1)

res = GR.scan_once(eng, feed=FakeFeed([_mig(MINT_SLOW, "SLOWONE")]),
                   now=NOW + 30)
check("filter: slow curve REJECTED",
      res["opened"] == 0 and res.get("select_rejected") == 1
      and res.get("select_reasons") == {"slow_curve": 1})

res = GR.scan_once(eng, feed=FakeFeed([_mig(MINT_MANY, "MANYONE")]),
                   now=NOW + 40)
check("filter: many-trades curve REJECTED",
      res["opened"] == 0 and res.get("select_rejected") == 1
      and res.get("select_reasons") == {"many_trades_curve": 1})

# rejected mints are never re-evaluated (dead_mints dedupes them)
res = GR.scan_once(eng, feed=FakeFeed([_mig(MINT_SER, "SERONE")]),
                   now=NOW + 50)
check("filter: rejected mint is not re-evaluated on re-arrival",
      res["opened"] == 0 and res.get("select_rejected") == 0
      and _journal().get("serial_deployer") == 1)

# ---------------- GT backfill: canonical match passes, mismatch rejects ----
# Rule A (2026-11-21, exit-study Rule 1): the backfill exemption is
# gone — a backfill row older than 30s is rejected slow_entry, and
# only a fresh backfill row (graduated within 30s) opens. The old
# row uses MINT_FB (now dead: slow_entry) and the fresh row uses
# MINT_FB2, so the exits section below still has two open positions.
# NOTE: the fresh scan runs at wall-clock time, not the suite's
# simulated NOW+60 — recheck_entry_age recomputes the age at entry
# time with time.time(), so a created_ts set relative to the
# simulated clock would look future-dated to the recheck.
gt_old = {"mint": MINT_FB, "symbol": "FBONE", "created_ts": NOW - 500,
          "dex": "pumpswap", "pool_address": "PoolFB",
          "reserve_usd": 9000.0, "fdv_usd": 80000.0}
res = GR.scan_once(eng, gt_rows=[gt_old], now=NOW + 60)
check("filter: backfill at 560s REJECTED slow_entry (Rule A)",
      res["opened"] == 0 and res.get("select_rejected") == 1
      and res.get("select_reasons") == {"slow_entry": 1})

_fresh_now = time.time()
gt_fresh = {"mint": MINT_FB2, "symbol": "FB2ONE",
            "created_ts": _fresh_now - 10,
            "dex": "pumpswap", "pool_address": "PoolFB2",
            "reserve_usd": 9000.0, "fdv_usd": 80000.0}
res = GR.scan_once(eng, gt_rows=[gt_fresh], now=_fresh_now)
check("filter: fresh backfill (10s) with pool match OPENS",
      res["opened"] == 1)

gt_nc = {"mint": MINT_NC, "symbol": "NCONE", "created_ts": NOW - 300,
         "dex": "pumpswap", "pool_address": "OtherPool",
         "reserve_usd": 9000.0, "fdv_usd": 80000.0}
res = GR.scan_once(eng, gt_rows=[gt_nc], now=NOW + 70)
check("filter: backfill with pool mismatch REJECTED (noncanonical)",
      res["opened"] == 0 and res.get("select_rejected") == 1
      and res.get("select_reasons") == {"noncanonical_pool": 1})

# ---------------- fail-closed on unmeasurable candidates ----------------
res = GR.scan_once(eng, feed=FakeFeed([_mig(MINT_UNK, "UNKONE")]),
                   now=NOW + 80)
check("filter: unknown candidate REJECTED fail-closed (no entry)",
      res["opened"] == 0 and res.get("select_rejected") == 1
      and res.get("select_reasons") == {"unknown_curve": 1})

# ---------------- fresh_only: stream-wake evaluates fresh only ----------
# Manually plant an unevaluated watchlist row; a fresh_only scan
# must leave it alone (main loop's business), then a normal scan
# evaluates it.
with eng._ledger_lock():
    d = eng._read()
    d["watchlist"][MINT_SLOW] = {
        "mint": MINT_SLOW, "symbol": "SLOWONE", "created_ts": NOW,
        "dex": "pumpswap", "pool_address": "PoolX",
        "source": "pumpportal-migration", "first_seen_ts": NOW}
    eng._write(d)
res = GR.scan_once(eng, feed=FakeFeed([]), now=NOW + 90,
                   fresh_only=True)
d = eng._read()
check("fresh_only: unevaluated non-fresh watchlist row left alone",
      MINT_SLOW in d["watchlist"]
      and "select" not in d["watchlist"][MINT_SLOW]
      and res.get("select_rejected") == 0)
res = GR.scan_once(eng, feed=FakeFeed([]), now=NOW + 100)
check("main loop: planted row evaluated (slow curve rejects)",
      res.get("select_rejected") == 1
      and res.get("select_reasons") == {"slow_curve": 1})

# ---------------- exits unchanged (the user's ladder) ----------------
closes = eng.poll_exits(price_fn=lambda c, m, s: 0.0021)  # 2.1x
d = eng._read()
check("exits: leg A (50% @2x) fired on both filtered entries",
      all(d["positions"][m]["legs"]["a"]["done"]
          for m in (MINT_OK, MINT_FB2)))
closes = eng.poll_exits(price_fn=lambda c, m, s: 0.0004)  # -60%
d = eng._read()
check("exits: hard stop closed both filtered positions",
      d["positions"] == {} and len(d["closes"]) == 2
      and all(c["closed_reason"] == "hard_stop_-50pct"
              for c in d["closes"]))
check("exits: closes recorded under lane grad_raw",
      all(c["lane"] == "grad_raw" for c in d["closes"]))

# ---------------- fail-closed when the select module is missing ----------
# A mint never seen before: with no select module the verdict must
# come from the guard itself, not from any filter data.
MINT_FRESH = "RawFiltFRESH11111111111111111111111111111pump"
_saved = GR._select
GR._select = None
try:
    res = GR.scan_once(
        eng, feed=FakeFeed([_mig(MINT_FRESH, "FRESHONE")]),
        now=NOW + 110)
    check("fail-closed: missing select module rejects every entry",
          res["opened"] == 0 and res.get("select_rejected") == 1
          and res.get("select_reasons") == {"unknown_select": 1})
finally:
    GR._select = _saved

print()
if fails:
    print("FAILURES (%d): %s" % (len(fails), fails))
    sys.exit(1)
print("ALL GRAD-RAW SELECT-FILTER TESTS PASSED")
