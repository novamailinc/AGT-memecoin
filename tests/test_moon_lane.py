#!/usr/bin/env python3
"""Moon-bag lane tests (2026-10-26).

Covers moon_lane.py: killswitch registration, engine isolation
(the gated grad module AND grad_raw are untouched by the moon
engine's overrides), the pre-registered moon terms, and — the
experiment itself, behaviorally, not just by constant:
  * final-leg trail is 0.50, not 0.30 (a 35% drawdown from peak
    does NOT close leg C; a 55% drawdown DOES);
  * NO time stop (a position backdated 25h survives at 1.0x,
    while the same backdate on a grad_raw engine closes it);
  * legs are 50/25/25, hard stop intact, entries ungated like
    grad_raw (no safety gate/veto), dedupe-ever, no daily cap.
No network: every feed is stubbed, migration rows carry no
signature (so the FIRE-FIRST path falls back to the stubbed live
quote, exactly as in test_grad_raw.py). Plain script — run
directly, not via pytest.
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

# DISCLOSED TEST EDIT (stale vs the mandatory entry guard): this file was RED at
# baseline 28499f5 because every entry was refused with "shared paper account
# not enabled". It now runs entries through the shared account with
# TEST-FIXTURE limits and a large fixture balance (not owner values).
import importlib as _il, tempfile as _tf
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
import tests._guard_fixture as _GF


def _fresh_shared_account():
    os.environ["FOREX_AGENT_HOME"] = _tf.mkdtemp(prefix="shared-acct-home-")
    os.environ["PAPER_ACCOUNT_START"] = "1000"
    os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None)
    os.environ.pop("PAPER_ACCOUNT_TRADE_SIZE_USD", None)
    import daemon.launch_scanner.paper_account as _pa
    _pa = _il.reload(_pa)
    _GF.install(_pa)


_fresh_shared_account()

from daemon.launch_scanner import grad as GATED
from daemon.launch_scanner import grad_select as GS
from tests.fixture_guarantee import admit_in_script_process, fixture_proof  # noqa: E402
admit_in_script_process(GS)
from daemon.launch_scanner import grad_raw as GR_RAW
from daemon.launch_scanner import moon_lane as ML
from daemon.launch_scanner import killswitch as KS

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
MINT1 = "MoonMint1111111111111111111111111111111pump"
MINT2 = "MoonMint2222222222222222222222222222222pump"
MINT3 = "MoonMint3333333333333333333333333333333pump"
MINT4 = "MoonMint4444444444444444444444444444444pump"

# ---------------- killswitch registration ----------------
check("killswitch: moon registered (window/min 100, halt)",
      "moon" in KS.CFG and KS.CFG["moon"]["window"] == 100
      and KS.CFG["moon"]["min_trades"] == 100
      and KS.CFG["moon"]["halt"] is True)

# isolate killswitch state for the whole run
_ks_tmp = tempfile.mkdtemp(prefix="moon-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")
KS.SHADOW_FILE = os.path.join(_ks_tmp, "shadow.json")
KS.LOCK_FILE = os.path.join(_ks_tmp, "state.lock")

# ---------------- engine identity + exit params ----------------
eng = ML.make_engine()
check("engine: lane is moon", eng.LANE == "moon")
check("engine: ledger is moon_ledger.json",
      eng.LEDGER_PATH.endswith("moon_ledger.json"))
check("engine: $1 stake (NOT grad_raw's $25)",
      eng.NOTIONAL_USD == 1.00)
check("engine: $100 starting bankroll",
      ML.BANKROLL_START == 100.00)
check("engine: MAX_OPEN mirrors grad_raw (150)",
      eng.MAX_OPEN == 150)
check("engine: no daily cap (bankroll-bound)",
      eng.DAILY_ENTRY_CAP >= 100000)
check("engine: age window 1800s (mirrors grad_raw)",
      eng.MAX_GRAD_AGE_S == 1800.0)
check("EXPERIMENT: final-leg trail is 0.50, not 0.30",
      eng.LEG_C_TRAIL == 0.50)
check("EXPERIMENT: time stop disabled (inf horizon)",
      eng.TIME_STOP_H == float("inf"))
check("legs unchanged: 50/25/25 at 2x/3x",
      eng.LEG_A_FRAC == 0.50 and eng.LEG_A_TARGET == 2.0
      and eng.LEG_B_FRAC == 0.25 and eng.LEG_B_TARGET == 3.0
      and eng.LEG_C_FRAC == 0.25)
check("hard stop intact at -50%",
      eng.HARD_STOP_MULT == 0.50)
check("gated lane untouched: trail 0.30, time stop 24h",
      GATED.LEG_C_TRAIL == 0.30 and GATED.TIME_STOP_H == 24)
check("grad_raw module untouched: $25 stake, own lane/ledger",
      GR_RAW.NOTIONAL_USD == 25.00 and GR_RAW.LANE == "grad_raw"
      and GR_RAW.LEDGER_PATH.endswith("grad_raw_ledger.json"))

_tmp = tempfile.mkdtemp(prefix="moon-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "moon_ledger.json")
led = eng.ensure_ledger()
pre = led["preregistered_verdict"]
check("preregistration: lane moon, sample 100",
      pre["lane"] == "moon" and pre["sample_closes"] == 100)
check("preregistration: killswitch_cfg 100/100/halt",
      pre["killswitch_cfg"]["window"] == 100
      and pre["killswitch_cfg"]["min_trades"] == 100
      and pre["killswitch_cfg"]["halt"] is True)
check("preregistration: terms declare trail 50%, NO time stop, $1",
      "50%" in pre["terms"]["ladder"]
      and "NO time stop" in pre["terms"]["ladder"]
      and pre["terms"]["notional_usd"] == 1.00)
check("preregistration: terms declare NO safety gate / NO veto",
      "NONE" in pre["terms"]["safety_gate"]
      and pre["terms"]["veto"] == "NONE")

# stub the price layer (no network); safety must NEVER be consulted
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(), "usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}


class _ExplodingSafety:
    def check_token(self, *a, **k):
        raise AssertionError("safety consulted on the MOON lane")


eng._safety = _ExplodingSafety()


class FakeFeed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        out, self._items = self._items, []
        return out


def _mig(mint, sym):
    return {"mint": mint, "symbol": sym, "source": "pumpportal-migration",
            "created_ts": NOW,
            "extra": {"migration_pool": "pumpswap",
                      "market_id": "Pool" + sym}}


def _birth(mint, sym):
    # Birth 60s before the migration: the select filter's serial
    # + fast-curve checks read the creator and the creation time
    # from this record (the live feed always births before it
    # migrates). Trader is a fresh fake deployer (0 launches).
    return {"mint": mint, "symbol": sym, "source": "pumpportal-new",
            "created_ts": NOW - 60,
            "extra": {"trader": "DeplM33333333333333333333333333333333333",
                      "bonding_curve": "CurveM333333333333333333333333333333333",
                      "state_from_event": True}}


# ---------------- ungated entry + dedupe ----------------
res = ML.scan_once(eng, feed=FakeFeed([_mig(MINT1, "M1")]), now=NOW)
check("scan: stream candidate opened with NO safety gate",
      res["opened"] == 1 and res["stream"] == 1)
st = eng.account_state()
check("ledger: 1 open, bankroll $99.00 after the $1 shot",
      st["n_open"] == 1
      and abs(st["bankroll"] - 99.00) < 1e-6)
d = eng._read()
pos = d["positions"][MINT1]
check("entry stamped: detection=pumpportal-migration, gate=none",
      pos["entry_features"].get("detection") == "pumpportal-migration"
      and pos["entry_features"].get("gate") == "none"
      and pos["source"] == "graduation-moon")
check("entry legs carry the moon trail (0.50 stored at entry)",
      pos["legs"]["c"]["trail"] == 0.50)
res2 = ML.scan_once(eng, feed=FakeFeed([_mig(MINT1, "M1")]),
                    now=NOW + 5)
check("dedupe: same mint never opens twice", res2["opened"] == 0)

# ---------------- trail behavior: 50%, not 30% ----------------
# Entry ~0.001. Drive to 4x: legs A (2x) and B (3x) fire, leg C
# peak becomes 0.004. A 35% drawdown (2.6x) must NOT close leg C
# under a 50% trail (it WOULD under grad_raw's 30% trail); a 55%
# drawdown (1.8x) MUST close it.
px = {"v": 0.004}
eng.poll_exits(price_fn=lambda c, m, s: px["v"])
d = eng._read()
pos = d["positions"][MINT1]
check("ladder: legs A and B fired at 4x",
      pos["legs"]["a"]["done"] and pos["legs"]["b"]["done"])
check("leg C peak tracked to 4x",
      abs(pos["legs"]["c"]["peak"] - 0.004) < 1e-9)
px["v"] = 0.0026  # 35% below the 0.004 peak
eng.poll_exits(price_fn=lambda c, m, s: px["v"])
d = eng._read()
check("TRAIL 50%: 35% drawdown does NOT close leg C",
      MINT1 in d["positions"]
      and not d["positions"][MINT1]["legs"]["c"]["done"])
px["v"] = 0.0018  # 55% below the 0.004 peak
eng.poll_exits(price_fn=lambda c, m, s: px["v"])
d = eng._read()
check("TRAIL 50%: 55% drawdown closes leg C -> ladder_complete",
      MINT1 not in d["positions"] and len(d["closes"]) == 1
      and d["closes"][0]["closed_reason"] == "ladder_complete"
      and d["closes"][0]["lane"] == "moon")

# ---------------- no time stop: 25h-old position survives ----------------
res = ML.scan_once(eng, feed=FakeFeed([_mig(MINT2, "M2")]), now=NOW)
check("time-stop setup: second position opened", res["opened"] == 1)
with eng._ledger_lock():
    d = eng._read()
    d["positions"][MINT2]["entry_ts"] = NOW - 25 * 3600
    eng._write(d)
eng.poll_exits(price_fn=lambda c, m, s: 0.001)  # flat 1.0x
d = eng._read()
check("NO TIME STOP: 25h-old position survives at 1.0x",
      MINT2 in d["positions"])

# control: the SAME backdate on a grad_raw engine MUST close it
reng = GR_RAW.make_engine()
_rtmp = tempfile.mkdtemp(prefix="moon-control-")
reng.LEDGER_PATH = os.path.join(_rtmp, "grad_raw_ledger.json")
reng.ensure_ledger()
reng.quote_price = lambda chain, mint, spot_index=None: 0.001
reng._fresh_tape = lambda mint: {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(), "usd_liquidity": 5000.0,
                                 "usd_market_cap": 60000.0,
                                 "tape_source": "test"}
reng._safety = _ExplodingSafety()
# The raw lane runs grad_select's evidence filters (since the
# 2026-11-10 filter port), and every filter's data source is
# dark in this test process: the saturation tally is stale (the
# pump.fun frontend API 403s), DexScreener has no pairs for the
# fake "M3" ticker, and Helius is quota-spent. The filters fail
# closed, so the control stubs the DATA layer the way it already
# stubs the price layer — the exit behavior under test is
# unchanged. Birth first (creator + creation time for the serial
# and fast-curve checks), then the migration.
_orig_curve_stats = GS._curve_stats
_orig_count_24h = GS._ds_ticker.count_24h if GS._ds_ticker else None
_orig_saturation = GS._saturation
GS._curve_stats = lambda curve_addr: {
    "trades": 10, "created_ts": NOW - 60,
    "oldest_sig": "FakeCreateSig333333333333333333333333333",
    "creation_slot": 300000100, "capped": False, "exhausted": True}
GS._saturation = None  # force the DexScreener path, deterministically
# 2026-11-24 conservative contract (serial coverage): recorder-only counts
# never admit, so this control supplies a typed authoritative proof for its
# fixture creator (0 launches in 24h), scoped to this call and restored.
_orig_auth = GS._AUTHORITATIVE_LAUNCHES_24H
GS._AUTHORITATIVE_LAUNCHES_24H = lambda creator, now: \
    fixture_proof(GS, creator, 0, now)
if GS._ds_ticker is not None:
    GS._ds_ticker.count_24h = lambda symbol, now=None, self_mint=None: {
        "tally": 1, "others": 0, "top_mint": MINT3,
        "self_seen": True, "pairs_seen": 1}
try:
    res = GR_RAW.scan_once(
        reng, feed=FakeFeed([_birth(MINT3, "M3"), _mig(MINT3, "M3")]),
        now=NOW)
finally:
    GS._curve_stats = _orig_curve_stats
    GS._saturation = _orig_saturation
    GS._AUTHORITATIVE_LAUNCHES_24H = _orig_auth
    if GS._ds_ticker is not None and _orig_count_24h is not None:
        GS._ds_ticker.count_24h = _orig_count_24h
check("control setup: grad_raw position opened", res["opened"] == 1)
with reng._ledger_lock():
    d = reng._read()
    d["positions"][MINT3]["entry_ts"] = NOW - 25 * 3600
    reng._write(d)
reng.poll_exits(price_fn=lambda c, m, s: 0.001)
d = reng._read()
check("control: grad_raw DOES time-stop the 25h-old position",
      MINT3 not in d["positions"] and len(d["closes"]) == 1
      and d["closes"][0]["closed_reason"] == "time_stop_24h")

# ---------------- hard stop intact ----------------
res = ML.scan_once(eng, feed=FakeFeed([_mig(MINT4, "M4")]), now=NOW)
check("hard-stop setup: third moon position opened",
      res["opened"] == 1)
eng.poll_exits(price_fn=lambda c, m, s: 0.0004)  # -60%
d = eng._read()
check("hard stop intact: -60% closes at hard_stop_-50pct",
      MINT4 not in d["positions"]
      and any(c["mint"] == MINT4
              and c["closed_reason"] == "hard_stop_-50pct"
              for c in d["closes"]))

# ---------------- no daily cap ----------------
with eng._ledger_lock():
    d = eng._read()
    d["entries_day"] = {"date": eng._today(), "count": 5000}
    eng._write(d)
MINT5 = "MoonMint5555555555555555555555555555555pump"
gt_row = {"mint": MINT5, "symbol": "M5", "created_ts": NOW - 900,
          "dex": "raydium", "pool_address": "PoolM5",
          "reserve_usd": 8000.0, "fdv_usd": 70000.0}
res = ML.scan_once(eng, gt_rows=[gt_row], now=NOW)
check("no daily cap: backfill entry opens past count 5000",
      res["opened"] == 1)

print()
if fails:
    print("FAILURES (%d): %s" % (len(fails), fails))
    sys.exit(1)
print("ALL MOON-LANE TESTS PASSED")
