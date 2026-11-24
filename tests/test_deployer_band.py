#!/usr/bin/env python3
"""Deployer-band forward paper test (2026-11-20).

Covers grad_select.py + grad_raw.py + tranche.py +
tranche_protect.py _deployer_band banding ("0" / "1-2" / "3+" /
"unknown", including the fail-closed unknown case) and the
_stamp_detection band stamping in all four lanes (reusing the count
Filter 1 already computed — no recompute, no RPC — and never gating
an entry), plus the deployer-band-report math on fixture closes —
window enforcement, no backfill, per-band win rate + PnL.

No network, no live ledgers (report math runs on in-memory fixtures;
the evaluate test stubs every data source in-process, same pattern as
tests/test_ab_forward.py). Plain script — run directly, not via pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import contextlib
import importlib.util
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad_select as GS  # noqa: E402
from tests.fixture_guarantee import admit_in_script_process, fixture_proof  # noqa: E402
admit_in_script_process(GS)
from daemon.launch_scanner import grad_raw as GR  # noqa: E402
from daemon.launch_scanner import tranche as TR  # noqa: E402
from daemon.launch_scanner import tranche_protect as TP  # noqa: E402
from daemon.launch_scanner import saturation as S  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- fake engine for _stamp_detection ----------------
class _FakeEngine:
    def __init__(self):
        self.d = {"positions": {}}

    def _ledger_lock(self):
        return contextlib.nullcontext()

    def _read(self):
        return self.d

    def _write(self, d):
        self.d = d


MINT = "BandMint1111111111111111111111111111111111pump"


def _stamp(module, values, raw=False):
    """Stamp once via module._stamp_detection; return entry_features."""
    eng = _FakeEngine()
    eng.d["positions"][MINT] = {"mint": MINT}
    if module is GR:
        module._stamp_detection(eng, MINT, "pumpportal-migration",
                                values=values)
    else:
        module._stamp_detection(eng, MINT, "pumpportal-migration",
                                values)
    return eng.d["positions"][MINT].get("entry_features") or {}


# ---------------- banding boundaries (pure function) --------------
for module, name in ((GS, "grad_select"), (GR, "grad_raw"),
                     (TR, "tranche"), (TP, "tranche_protect")):
    band = module._deployer_band
    check("%s: band 0 -> '0'" % name, band(0) == "0")
    check("%s: band 1 -> '1-2'" % name, band(1) == "1-2")
    check("%s: band 2 -> '1-2'" % name, band(2) == "1-2")
    check("%s: band 3 -> '3+'" % name, band(3) == "3+")
    check("%s: band 10 -> '3+'" % name, band(10) == "3+")
    check("%s: band None -> 'unknown' (fail-closed)" % name,
          band(None) == "unknown")
    check("%s: band 'x' -> 'unknown' (fail-closed)" % name,
          band("x") == "unknown")
    check("%s: band -1 -> 'unknown' (fail-closed)" % name,
          band(-1) == "unknown")
    check("%s: band missing key -> 'unknown'" % name,
          band({}.get("deployer_launches_24h")) == "unknown")

# ---------------- stamping in all four lanes ----------------------
for module, name in ((GS, "grad_select"), (GR, "grad_raw"),
                     (TR, "tranche"), (TP, "tranche_protect")):
    ef0 = _stamp(module, {"deployer_launches_24h": 0})
    check("%s: band '0' stamped at 0 launches" % name,
          ef0.get("deployer_band") == "0")
    ef1 = _stamp(module, {"deployer_launches_24h": 1})
    check("%s: band '1-2' stamped at 1 launch" % name,
          ef1.get("deployer_band") == "1-2")
    ef2 = _stamp(module, {"deployer_launches_24h": 2})
    check("%s: band '1-2' stamped at 2 launches" % name,
          ef2.get("deployer_band") == "1-2")
    ef3 = _stamp(module, {"deployer_launches_24h": 3})
    check("%s: band '3+' stamped at 3 launches" % name,
          ef3.get("deployer_band") == "3+")
    efu = _stamp(module, {})
    check("%s: band 'unknown' stamped when count missing" % name,
          efu.get("deployer_band") == "unknown")
    check("%s: filter values preserved alongside band" % name,
          ((ef2.get("select") or ef2.get("tranche")
            or ef2.get("tranche_protect") or {})
           .get("deployer_launches_24h") == 2))

# The A+B arm stamp is untouched by the band stamp (grad_select,
# grad_raw): same count, both stamps, no interference.
ef_ab = _stamp(GS, {"deployer_launches_24h": 2})
check("grad_select: ab_arm still 'A+B' alongside band '1-2'",
      ef_ab.get("ab_arm") == "A+B"
      and ef_ab.get("deployer_band") == "1-2")
ef_ab0 = _stamp(GS, {"deployer_launches_24h": 0})
check("grad_select: ab_arm still 'A' alongside band '0'",
      ef_ab0.get("ab_arm") == "A"
      and ef_ab0.get("deployer_band") == "0")
ef_ab_r = _stamp(GR, {"deployer_launches_24h": 1})
check("grad_raw: ab_arm still 'A+B' alongside band '1-2'",
      ef_ab_r.get("ab_arm") == "A+B"
      and ef_ab_r.get("deployer_band") == "1-2")

# grad_raw with values=None (pre-filter entry): no select measurement,
# so no band stamp — the report counts it as unstamped, never as "0".
ef_none = _stamp(GR, None)
check("grad_raw: no band stamped when values is None "
      "(unstamped, not '0')",
      "deployer_band" not in ef_none and ef_none.get("gate") == "none")


# ---------------- HARD RULE: band never gates an entry ------------
# A 1-launch candidate (band "1-2") still PASSES evaluation when
# Rule A passes — the band is journal-only, the filters + Rule A
# decide alone. Same stub pattern as tests/test_ab_forward.py.
NOW = time.time()
MINT_B = "BandMintB111111111111111111111111111111111pump"
TRADER_B = "TraderBandB1111111111111111111111111111111"

eng = GS.make_engine()
_tmp = tempfile.mkdtemp(prefix="bandfwd-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_select_ledger.json")
eng.ensure_ledger()
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}

GS._index_refresh = lambda now: None
GS._INDEX["available"] = True
GS._INDEX["coverage_start"] = NOW - 90000
GS._INDEX["event_ts"] = [NOW - 86400 + i * 300 for i in range(289)]  # continuity fixture (2026-11-24)
GS._INDEX["by_mint"][MINT_B] = {"trader": TRADER_B, "ts": NOW - 10,
                                "bonding_curve": "Curve111"}
# by_trader has ONE launch for TRADER_B in the trailing 24h.
GS._INDEX["by_trader"][TRADER_B] = [NOW - 100]
GS._curve_stats = lambda addr: {"trades": 80,
                                "created_ts": NOW - 10,
                                "oldest_sig": "sig-b", "capped": False,
                                "exhausted": True}
GS._creator_from_oldest_tx = lambda sig: None
# 2026-11-24 conservative contract: recorder-only sub-threshold counts
# never admit. Stub the authoritative-history hook with this fixture's
# intended complete history (TRADER_B truly launched 1 in 24h).
GS._AUTHORITATIVE_LAUNCHES_24H = lambda creator, now: \
    fixture_proof(GS, creator, 1, now)


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


GS._saturation = _FakeSat()

cand_b = {"mint": MINT_B, "symbol": "BANDB", "created_ts": NOW - 10,
          "dex": "pumpswap", "pool_address": "PoolX",
          "migration_sig": "", "reserve_usd": 0.0, "fdv_usd": 0.0,
          "source": "pumpportal-migration"}
v = GS.evaluate_candidate(eng, cand_b, NOW)
check("hard rule: 1-launch candidate (band '1-2') PASSES evaluation",
      v["verdict"] == "pass")
check("hard rule: evaluate reports deployer_launches_24h == 1",
      v["values"].get("deployer_launches_24h") == 1)
check("hard rule: band off evaluate values is '1-2'",
      GS._deployer_band(
          v["values"].get("deployer_launches_24h")) == "1-2")


# ---------------- report math on fixture closes -------------------
def _load_report():
    import importlib.machinery
    path = os.path.join(REPO, "scripts", "deployer-band-report")
    loader = importlib.machinery.SourceFileLoader(
        "deployer_band_report", path)
    spec = importlib.util.spec_from_loader("deployer_band_report",
                                           loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


RPT = _load_report()
START = 1794900000.0


def _close(entry_ts, band, pnl, stamped=True):
    ef = {} if not stamped else {"deployer_band": band}
    return {"mint": "M%d" % int(entry_ts), "entry_ts": entry_ts,
            "pnl_usd": pnl, "entry_features": ef}


fixture = [
    _close(START + 10, "0", 12.5),
    _close(START + 20, "0", -25.0),
    _close(START + 30, "0", 30.0),
    _close(START + 40, "1-2", 5.0),
    _close(START + 50, "1-2", -25.0),
    _close(START + 60, "3+", 50.0),
    _close(START + 70, "unknown", -10.0),
    _close(START - 100, "0", 999.0),   # before window: excluded
    _close(START + 80, "0", 7.5, stamped=False),  # unstamped
    _close(START + 90, "bogus", 100.0),  # bad stamp: unstamped
]
s = RPT.summarize(fixture, START)
check("report: band '0' counts 3 closes / 2 wins / net +$17.50",
      s["0"]["closes"] == 3 and s["0"]["wins"] == 2
      and abs(s["0"]["net_pnl"] - 17.50) < 0.01)
check("report: band '0' win rate 66.67% / expectancy +$5.83",
      abs(s["0"]["win_rate"] - 0.6667) < 0.001
      and abs(s["0"]["expectancy"] - 5.83) < 0.01)
check("report: band '1-2' counts 2 closes / 1 win / net -$20.00",
      s["1-2"]["closes"] == 2 and s["1-2"]["wins"] == 1
      and abs(s["1-2"]["net_pnl"] - (-20.00)) < 0.01)
check("report: band '1-2' win rate 50% / expectancy -$10.00",
      abs(s["1-2"]["win_rate"] - 0.5) < 0.001
      and abs(s["1-2"]["expectancy"] - (-10.00)) < 0.01)
check("report: band '3+' counts 1 close / 1 win / net +$50.00",
      s["3+"]["closes"] == 1 and s["3+"]["wins"] == 1
      and abs(s["3+"]["net_pnl"] - 50.00) < 0.01)
check("report: band 'unknown' counted in its own bucket, not '0'",
      s["unknown"]["closes"] == 1 and s["unknown"]["wins"] == 0
      and abs(s["unknown"]["net_pnl"] - (-10.00)) < 0.01
      and s["0"]["closes"] == 3)
check("report: pre-window close excluded, unstamped + bad stamp "
      "counted separately",
      s["total"] == 7 and s["unstamped"] == 2)
check("report: band_of returns None for unstamped close",
      RPT.band_of(_close(START + 1, "0", 1.0, stamped=False)) is None)
check("report: band_of returns None for bogus stamp",
      RPT.band_of(_close(START + 1, "bogus", 1.0)) is None)
check("report: band_of returns 'unknown' for unknown stamp",
      RPT.band_of(_close(START + 1, "unknown", 1.0)) == "unknown")

# Verdict refusal below 100 closes: summarize() carries no verdict
# logic at all — the gate lives in main(), which prints NO VERDICT
# while total < required.
check("report: verdict threshold is 100 forward closes",
      RPT.VERDICT_CLOSES_REQUIRED == 100)
check("report: 7-close fixture is below threshold (no verdict possible)",
      s["total"] < RPT.VERDICT_CLOSES_REQUIRED)

big = [_close(START + i, ("0", "1-2", "3+")[i % 3],
              10.0 if i % 3 else -25.0) for i in range(100)]
s100 = RPT.summarize(big, START)
check("report: 100-close fixture sums to 100 forward closes",
      s100["total"] == 100)
check("report: summarize returns stats only, no verdict key",
      "verdict" not in s100 and "verdict" not in s100["0"]
      and "verdict" not in s100["1-2"])

# No backfill: load_state creates the window at first use (started_ts
# = now) and a second load returns the SAME window; a pre-existing
# state file is preserved, never rewritten to an earlier start.
_tmp_home = tempfile.mkdtemp(prefix="bandfwd-home-")
os.environ["FOREX_AGENT_HOME"] = _tmp_home
RPT2 = _load_report()
st1 = RPT2.load_state()
check("report: first load_state creates state with started_ts ~= now",
      abs(st1.get("started_ts", 0) - time.time()) < 60)
check("report: state file written to FOREX_AGENT_HOME",
      os.path.exists(os.path.join(_tmp_home,
                                  "deployer_band_forward.json")))
st2 = RPT2.load_state()
check("report: second load_state returns the same window "
      "(no backfill, no reset)",
      st2.get("started_ts") == st1.get("started_ts"))
# Pre-existing older state is preserved as-is.
old_ts = time.time() - 86400
with open(os.path.join(_tmp_home, "deployer_band_forward.json"),
          "w") as f:
    import json as _json
    _json.dump({"started_ts": old_ts, "started_wat": "old",
                "verdict_closes_required": 100}, f)
st3 = RPT2.load_state()
check("report: pre-existing state preserved, never backfilled",
      st3.get("started_ts") == old_ts)

print("")
if fails:
    print("FAILED: %d check(s): %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
