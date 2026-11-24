#!/usr/bin/env python3
"""Graduation RAW lane tests (2026-10-23).

Covers grad_raw.py: killswitch registration, engine isolation (the
gated grad module is untouched by the raw engine's overrides), the
pre-registered no-gate terms, PumpPortal migration conversion,
NO-GATE entry (a candidate opens even though the safety module
would explode if consulted), stream-vs-backfill detection stamps,
dedupe-ever, and the absence of a daily entry cap. No network:
every feed is stubbed. Plain script — run directly, not via pytest.
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
from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import saturation as S

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
MINT1 = "RawMint11111111111111111111111111111111pump"
MINT2 = "RawMint22222222222222222222222222222222pump"
MINT2B = "RawMint2B2222222222222222222222222222222pump"

# ---------------- killswitch registration ----------------
check("killswitch: grad_raw registered (window/min 100, halt)",
      "grad_raw" in KS.CFG and KS.CFG["grad_raw"]["window"] == 100
      and KS.CFG["grad_raw"]["min_trades"] == 100
      and KS.CFG["grad_raw"]["halt"] is True)

# isolate killswitch state for the whole run
_ks_tmp = tempfile.mkdtemp(prefix="gradraw-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")

# ---------------- engine isolation + policy ----------------
eng = GR.make_engine()
check("engine: lane is grad_raw", eng.LANE == "grad_raw")
check("engine: ledger is grad_raw_ledger.json",
      eng.LEDGER_PATH.endswith("grad_raw_ledger.json"))
check("engine: age window widened to 1800s",
      eng.MAX_GRAD_AGE_S == 1800.0)
check("engine: no daily cap (bankroll-bound)",
      eng.DAILY_ENTRY_CAP >= 100000)
check("gated lane untouched: grad module still lane 'grad'",
      GATED.LANE == "grad"
      and GATED.LEDGER_PATH.endswith("grad_ledger.json")
      and GATED.MAX_GRAD_AGE_S == 600.0
      # gated daily cap REMOVED 2026-10-24 (user order, ODIE miss);
      # the raw engine's 100000 override must not leak either way.
      and GATED.DAILY_ENTRY_CAP is None)

_tmp = tempfile.mkdtemp(prefix="gradraw-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_raw_ledger.json")
led = eng.ensure_ledger()
pre = led["preregistered_verdict"]
check("preregistration: lane grad_raw, sample 100",
      pre["lane"] == "grad_raw" and pre["sample_closes"] == 100)
check("preregistration: terms declare NO safety gate / NO veto",
      "NONE" in pre["terms"]["safety_gate"]
      and pre["terms"]["veto"] == "NONE")
check("preregistration: terms declare the four evidence filters",
      isinstance(pre["terms"].get("filters"), dict)
      and "serial_deployer" in pre["terms"]["filters"]
      and "fast_curve_few_trades" in pre["terms"]["filters"]
      and "canonical_only" in pre["terms"]["filters"]
      and "copycat_ticker" in pre["terms"]["filters"])
check("preregistration: terms declare no daily cap",
      pre["terms"]["daily_entry_cap"] is None)

# stub the price layer (no network); safety must NEVER be consulted
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}


class _ExplodingSafety:
    def check_token(self, *a, **k):
        raise AssertionError("safety consulted on the RAW lane")


eng._safety = _ExplodingSafety()

# ---------------- filter data stubs (2026-11-10 filter port) ----------------
# The raw lane now gates entries on grad_select's four filters, via
# the SHARED module — seed its in-memory index through GR._select
# (the exact object the lane code calls, never a second copy).
# Refresh disabled; no network. Same stub shapes as
# test_grad_select.py.
SEL = GR._select
# 2026-11-24 conservative contract: recorder-only sub-threshold
# counts never admit; stub the authoritative-history hook with this
# fixture's INTENDED complete per-creator history (explicit table,
# never derived from the recorder index).
SEL._AUTHORITATIVE_LAUNCHES_24H = lambda c, n: SEL.authoritative_proof(
    c, _AUTH_HIST_RAW.get(c, 0), n)
_AUTH_HIST_RAW = {}
TRADER1 = "TraderRaw11111111111111111111111111111111"
_AUTH_HIST_RAW[TRADER1] = 1
TRADER2 = "TraderRaw21111111111111111111111111111111"
_AUTH_HIST_RAW[TRADER2] = 1
TRADER2B = "TraderRaw2B111111111111111111111111111111"
_AUTH_HIST_RAW[TRADER2B] = 1
TRADER3 = "TraderRaw31111111111111111111111111111111"
_AUTH_HIST_RAW[TRADER3] = 1
check("filter port: raw binds a grad_select module",
      SEL is not None and hasattr(SEL, "evaluate_candidate"))
SEL._index_refresh = lambda now: None
SEL._INDEX["available"] = True
SEL._INDEX["coverage_start"] = NOW - 90000
SEL._INDEX["event_ts"] = [NOW - 86400 + i * 300 for i in range(289)]  # continuity fixture (2026-11-24)


def _birth(mint, trader, ts, curve):
    SEL._INDEX["by_mint"][mint] = {
        "trader": trader, "ts": ts, "bonding_curve": curve}


_birth(MINT1, TRADER1, NOW - 600, "CurveM1")
SEL._INDEX["by_trader"][TRADER1] = [NOW - 600]
_birth(MINT2, TRADER2, NOW - 900, "CurveM2")
SEL._INDEX["by_trader"][TRADER2] = [NOW - 900]
SEL._INDEX["by_mig"][MINT2] = {"pool": "PoolB", "ts": NOW - 60}
_birth(MINT2B, TRADER2B, NOW - 900, "CurveM2B")
SEL._INDEX["by_trader"][TRADER2B] = [NOW - 900]
SEL._INDEX["by_mig"][MINT2B] = {"pool": "PoolB2", "ts": NOW - 60}

_CURVE = {
    "CurveM1": {"trades": 80, "created_ts": NOW - 600,
                "oldest_sig": "sig-m1", "capped": False,
                "exhausted": True},
    "CurveM2": {"trades": 70, "created_ts": NOW - 900,
                "oldest_sig": "sig-m2", "capped": False,
                "exhausted": True},
    "CurveM2B": {"trades": 70, "created_ts": NOW - 900,
                 "oldest_sig": "sig-m2b", "capped": False,
                 "exhausted": True},
    "CurveM3": {"trades": 60, "created_ts": NOW - 300,
                "oldest_sig": "sig-m3", "capped": False,
                "exhausted": True},
}
SEL._curve_stats = lambda addr: _CURVE.get(addr) or {
    "trades": None, "created_ts": None, "oldest_sig": None,
    "capped": False, "exhausted": False}


class _FakeSat:
    def __init__(self):
        self.updated = NOW

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": {}}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


SEL._saturation = _FakeSat()

# ---------------- migration conversion ----------------
mig = {"mint": MINT1, "symbol": "RAW1", "source": "pumpportal-migration",
       "created_ts": NOW,
       "extra": {"migration_pool": "pumpswap", "market_id": "PoolA"}}
cand = GR.migration_to_candidate(mig, NOW)
check("migration: pumpswap graduation converts",
      cand is not None and cand["mint"] == MINT1
      and cand["dex"] == "pumpswap"
      and cand["pool_address"] == "PoolA"
      and cand["source"] == "pumpportal-migration")
check("migration: new-token event rejected",
      GR.migration_to_candidate(
          {"mint": MINT1, "source": "pumpportal-new",
           "extra": {"migration_pool": "pumpswap"}}, NOW) is None)
check("migration: non-pump mint rejected",
      GR.migration_to_candidate(
          {"mint": "SomeMint111111111111111111111111111111",
           "source": "pumpportal-migration",
           "extra": {"migration_pool": "pumpswap"}}, NOW) is None)
check("migration: unknown venue rejected",
      GR.migration_to_candidate(
          {"mint": MINT1, "source": "pumpportal-migration",
           "extra": {"migration_pool": "meteora"}}, NOW) is None)
check("migration: converted row passes the graduation filter",
      eng.is_graduation_candidate(cand, NOW) is not None)

# ---------------- no-gate entry via the stream ----------------
class FakeFeed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        out, self._items = self._items, []
        return out


res = GR.scan_once(eng, feed=FakeFeed([mig]), now=NOW)
check("scan: stream candidate opened (filters passed; NO safety gate)",
      res["opened"] == 1 and res["stream"] == 1
      and res.get("select_rejected") == 0)
st = eng.account_state()
check("ledger: 1 open, bankroll $%.2f after the $%.2f shot"
      % (100.0 - GR.NOTIONAL_USD, GR.NOTIONAL_USD),
      st["n_open"] == 1
      and abs(st["bankroll"] - (100.0 - GR.NOTIONAL_USD)) < 1e-6)
d = eng._read()
pos = d["positions"][MINT1]
check("entry stamped: detection=pumpportal-migration, gate=select, "
      "measured filter values",
      pos["entry_features"].get("detection") == "pumpportal-migration"
      and pos["entry_features"].get("gate") == "select"
      and pos["entry_features"].get("select", {}).get("creator")
      == TRADER1
      and pos["entry_features"]["select"].get("curve_trades") == 80
      and pos["entry_features"]["select"].get("ticker_others_24h") == 0
      and pos["source"] == "graduation-raw")
check("entry snapshot honestly shows no safety data",
      pos["entry_snapshot"]["safety"]["score"] == 0
      and pos["entry_snapshot"]["safety"]["flags"] == [])

# dedupe-ever: the same graduation can never re-enter
res2 = GR.scan_once(eng, feed=FakeFeed([mig]), now=NOW + 5)
check("dedupe: same mint never opens twice", res2["opened"] == 0)

# ---------------- GT backfill path + no daily cap ----------------
# Pre-load today's entry count far beyond the gated lane's cap of
# 20 — the raw lane must still enter (bankroll is its only limit).
with eng._ledger_lock():
    d = eng._read()
    d["entries_day"] = {"date": eng._today(), "count": 5000}
    eng._write(d)
# Rule A (2026-11-21, exit-study Rule 1): the backfill exemption
# is gone — a 15-min-old GT graduation no longer enters; only a
# fresh backfill row (graduated within 30s) opens. The old row
# uses MINT2 (now dead: slow_entry) and the fresh row uses
# MINT2B, so the exits section below still has two positions.
gt_old = {"mint": MINT2, "symbol": "RAW2", "created_ts": NOW - 900,
          "dex": "raydium", "pool_address": "PoolB",
          "reserve_usd": 8000.0, "fdv_usd": 70000.0}
res3 = GR.scan_once(eng, gt_rows=[gt_old], now=NOW)
check("backfill: 15-min-old GT graduation REJECTED slow_entry (Rule A)",
      res3["opened"] == 0 and res3.get("select_rejected") == 1
      and res3.get("select_reasons") == {"slow_entry": 1})
gt_fresh = {"mint": MINT2B, "symbol": "RAW2B", "created_ts": NOW - 10,
            "dex": "raydium", "pool_address": "PoolB2",
            "reserve_usd": 8000.0, "fdv_usd": 70000.0}
res3b = GR.scan_once(eng, gt_rows=[gt_fresh], now=NOW)
check("backfill: fresh (10s) GT graduation still enters",
      res3b["opened"] == 1)
d = eng._read()
check("backfill: entry stamped geckoterminal-backfill",
      d["positions"][MINT2B]["entry_features"].get("detection")
      == "geckoterminal-backfill")
check("no daily cap: entries_day count kept counting past 5000",
      d["entries_day"]["count"] == 5001)

# ---------------- exits still the user's ladder ----------------
closes = eng.poll_exits(price_fn=lambda c, m, s: 0.0021)  # 2.1x
d = eng._read()
check("ladder: leg A (50% @2x) fired on both positions at 2.1x",
      all(d["positions"][m]["legs"]["a"]["done"] for m in (MINT1, MINT2B)))
closes = eng.poll_exits(price_fn=lambda c, m, s: 0.0004)  # -60%
d = eng._read()
check("ladder: hard stop closed both positions",
      d["positions"] == {} and len(d["closes"]) == 2
      and all(c["closed_reason"] == "hard_stop_-50pct"
              for c in d["closes"]))
check("closes recorded under lane grad_raw",
      all(c["lane"] == "grad_raw" for c in d["closes"]))

# ---------------- pool-address merge (phantom guard, 2026-10-23) ----------------
# A stream row that arrives without market_id must not be lost AND
# must not enter pool-less: it waits on the watchlist until the GT
# backfill row supplies the pool address.
MINT3 = "RawMint33333333333333333333333333333333pump"
_birth(MINT3, TRADER3, NOW - 300, "CurveM3")
SEL._INDEX["by_trader"][TRADER3] = [NOW - 300]
mig_nopool = {"mint": MINT3, "symbol": "RAW3",
              "source": "pumpportal-migration", "created_ts": NOW,
              "extra": {"migration_pool": "pumpswap"}}
res4 = GR.scan_once(eng, feed=FakeFeed([mig_nopool]), now=NOW)
d = eng._read()
check("phantom guard: stream row without pool address waits, never opens",
      res4["opened"] == 0 and MINT3 in d["watchlist"]
      and MINT3 not in d["positions"])
gt_row3 = {"mint": MINT3, "symbol": "RAW3", "created_ts": NOW - 300,
           "dex": "pumpswap", "pool_address": "PoolC",
           "reserve_usd": 9000.0, "fdv_usd": 80000.0}
res5 = GR.scan_once(eng, gt_rows=[gt_row3], now=NOW)
d = eng._read()
check("phantom guard: backfill pool address reached the position",
      d["positions"].get(MINT3, {}).get("entry_features", {})
      .get("pool") == "PoolC")
check("phantom guard: merged row opens once the pool is known",
      res5["opened"] == 1 and MINT3 in d["positions"])
check("phantom guard: first-detection stamp kept (stream source)",
      d["positions"][MINT3]["entry_features"].get("detection")
      == "pumpportal-migration")

# ---------------- parallel filter stage (Phase 1 B1+C1) --------
# Three fresh candidates in ONE scan cycle: two clean (must open)
# and one serial deployer (must reject). They run through the
# parallel evaluator + single batched ledger write, so this also
# proves the workers do not mix candidates up: each open must
# carry its OWN creator, and the reject must be the serial one.
# The curve fake is already keyed on the curve address (thread-
# safe). Top up the temp ledger first: the lane's $100 bankroll
# already spent $75 on MINT1/2/3, and two more $25 opens need
# the headroom — the top-up is test bookkeeping, not lane logic.
MINT4 = "RawMint44444444444444444444444444444444pump"
MINT5 = "RawMint55555555555555555555555555555555pump"
MINT6 = "RawMint66666666666666666666666666666666pump"
TRADER4 = "TraderRaw41111111111111111111111111111111"
_AUTH_HIST_RAW[TRADER4] = 1
TRADER5 = "TraderRaw51111111111111111111111111111111"
_AUTH_HIST_RAW[TRADER5] = 1
TRADER6 = "TraderRaw61111111111111111111111111111111"
_birth(MINT4, TRADER4, NOW - 600, "CurveM4")
SEL._INDEX["by_trader"][TRADER4] = [NOW - 600]
_birth(MINT5, TRADER5, NOW - 300, "CurveM5")
SEL._INDEX["by_trader"][TRADER5] = [NOW - 300]
_birth(MINT6, TRADER6, NOW - 400, "CurveM6")
SEL._INDEX["by_trader"][TRADER6] = [NOW - 3600, NOW - 3500,
                                     NOW - 400]
_CURVE.update({
    "CurveM4": {"trades": 80, "created_ts": NOW - 600,
                "oldest_sig": "sig-m4", "capped": False,
                "exhausted": True},
    "CurveM5": {"trades": 90, "created_ts": NOW - 300,
                "oldest_sig": "sig-m5", "capped": False,
                "exhausted": True},
    "CurveM6": {"trades": 70, "created_ts": NOW - 400,
                "oldest_sig": "sig-m6", "capped": False,
                "exhausted": True},
})
with eng._ledger_lock():
    d = eng._read()
    d["bankroll"] = 100.0
    eng._write(d)
_j_before = ((eng._read().get("select_journal") or {}).get(
    "counts", {}).get("serial_deployer") or 0)
res6 = GR.scan_once(
    eng, feed=FakeFeed([
        {"mint": MINT4, "symbol": "RAW4",
         "source": "pumpportal-migration", "created_ts": NOW,
         "extra": {"migration_pool": "pumpswap",
                   "market_id": "PoolD"}},
        {"mint": MINT5, "symbol": "RAW5",
         "source": "pumpportal-migration", "created_ts": NOW,
         "extra": {"migration_pool": "pumpswap",
                   "market_id": "PoolE"}},
        {"mint": MINT6, "symbol": "RAW6",
         "source": "pumpportal-migration", "created_ts": NOW,
         "extra": {"migration_pool": "pumpswap",
                   "market_id": "PoolF"}}]),
    now=NOW + 20)
check("scan parallel: 2 clean opened, 1 serial rejected in one cycle",
      res6["opened"] == 2 and res6["select_rejected"] == 1
      and res6["select_reasons"].get("serial_deployer") == 1)
d = eng._read()
check("scan parallel: each open carries its OWN creator (no mix-up)",
      (d["positions"][MINT4]["entry_features"].get("select") or {}
       ).get("creator") == TRADER4
      and (d["positions"][MINT5]["entry_features"].get("select")
           or {}).get("creator") == TRADER5)
check("scan parallel: serial mint dead, journal count +1",
      "select: serial_deployer" in str(
          d["dead_mints"].get(MINT6, {}).get("reason", ""))
      and (d.get("select_journal") or {}).get("counts", {}).get(
          "serial_deployer") == _j_before + 1)

print()
if fails:
    print("FAILURES (%d): %s" % (len(fails), fails))
    sys.exit(1)
print("ALL GRAD-RAW TESTS PASSED")
