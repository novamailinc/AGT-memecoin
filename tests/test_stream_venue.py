#!/usr/bin/env python3
"""Stream venue-name regression (2026-10-23 repair).

Live PumpPortal migration payloads name the PumpSwap venue
"pump-amm" — 82 of 84 real migration items recorded on 2026-10-23,
vs 2 "pumpswap" (one synthetic). pumpportal.parse_migration passed
the raw value through and grad_raw.migration_to_candidate whitelisted
only ("pumpswap", "raydium"), so stream detection folded ZERO
candidates all day while every grad/grad_raw entry arrived via the
GT backfill. The pre-existing suites were green because their
fixtures used the assumed "pumpswap" value — this suite uses the
REAL recorded payload shape. Plain script — run directly.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad as GATED
from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import pumpportal as PP
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import saturation as S

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
MINT = "VenueMint11111111111111111111111111111pump"

# ---------------- parser level: real payload shape ----------------
payload = {"txType": "migrate", "mint": MINT, "symbol": "VEN",
           "name": "Venue", "pool": "pump-amm",
           "marketId": "PoolReal", "signature": "sig",
           "marketCapSol": 85.0, "traderPublicKey": "Trader111"}
item = PP.parse_migration(payload)
check("parser: pump-amm payload parses", item is not None)
check("parser: venue normalized to pumpswap",
      item is not None
      and (item.get("extra") or {}).get("migration_pool") == "pumpswap")
item_ray = PP.parse_migration(dict(payload, pool="raydium"))
check("parser: raydium stays raydium",
      item_ray is not None
      and (item_ray.get("extra") or {}).get("migration_pool") == "raydium")
item_unk = PP.parse_migration(dict(payload, pool="meteora"))
check("parser: unknown venue passes through (consumers fail closed)",
      item_unk is not None
      and (item_unk.get("extra") or {}).get("migration_pool") == "meteora")

# ---------------- conversion level: raw item, alias value ----------
raw_item = {"mint": MINT, "symbol": "VEN",
            "source": "pumpportal-migration", "created_ts": NOW,
            "extra": {"migration_pool": "pump-amm",
                      "market_id": "PoolReal"}}
cand = GR.migration_to_candidate(raw_item, NOW)
check("conversion: pump-amm item converts", cand is not None)
check("conversion: candidate dex normalized to pumpswap",
      cand is not None and cand["dex"] == "pumpswap"
      and cand["source"] == "pumpportal-migration")
check("conversion: unknown venue still rejected (fail closed)",
      GR.migration_to_candidate(
          {"mint": MINT, "source": "pumpportal-migration",
           "extra": {"migration_pool": "meteora"}}, NOW) is None)
check("conversion: converted row passes the gated engine filter",
      cand is not None
      and GATED.is_graduation_candidate(cand, NOW) is not None)

# ---------------- filter data stubs (2026-11-22) ----------------
# The raw lane gates entries on grad_select's four filters since
# 2026-11-10: without deployer/curve data the shared filter
# fail-closes (unknown_serial) and the stream item never opens.
# Seed the shared select index through GR._select (the exact object
# the lane code calls), same shapes as test_grad_raw.py. No network.
SEL = GR._select
from tests.fixture_guarantee import admit_in_script_process, fixture_proof  # noqa: E402
admit_in_script_process(SEL)
SEL._index_refresh = lambda now: None
SEL._INDEX["available"] = True
SEL._INDEX["coverage_start"] = NOW - 90000
SEL._INDEX["event_ts"] = [NOW - 86400 + i * 300 for i in range(289)]  # continuity fixture (2026-11-24)
SEL._INDEX["by_mint"][MINT] = {
    "trader": "Trader111", "ts": NOW - 60,
    "bonding_curve": "CurveVenue"}
SEL._INDEX["by_trader"]["Trader111"] = [NOW - 60]
# 2026-11-24 conservative contract: recorder-only sub-threshold
# counts never admit; stub the authoritative-history hook with this
# fixture's INTENDED complete per-creator history (explicit table,
# never derived from the recorder index).
SEL._AUTHORITATIVE_LAUNCHES_24H = lambda c, n: \
    fixture_proof(SEL, c, {"Trader111": 1}.get(c, 0), n)
SEL._INDEX["by_mig"][MINT] = {"pool": "PoolReal", "ts": NOW}
SEL._curve_stats = lambda addr: {
    "trades": 80, "created_ts": NOW - 60, "oldest_sig": "sig-venue",
    "capped": False, "exhausted": True}


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

# ---------------- scan level: stream entry actually opens ----------
_ks_tmp = tempfile.mkdtemp(prefix="venue-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")
KS.SHADOW_FILE = os.path.join(_ks_tmp, "shadow.json")
KS.LOCK_FILE = os.path.join(_ks_tmp, "state.lock")
# DISCLOSED TEST EDIT (stale vs the mandatory entry guard, baseline red): entries are refused unless the shared account is
# enabled. TEST-FIXTURE limits (not owner values); no guard waived or patched.
import importlib as _il, tempfile as _tf
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
import tests._guard_fixture as _GF
os.environ["FOREX_AGENT_HOME"] = _tf.mkdtemp(prefix="shared-acct-home-")
os.environ["PAPER_ACCOUNT_START"] = "1000"
os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None)
os.environ.pop("PAPER_ACCOUNT_TRADE_SIZE_USD", None)
import daemon.launch_scanner.paper_account as _pa
_pa = _il.reload(_pa)
_GF.install(_pa)
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"
eng = GR.make_engine()
_tmp = tempfile.mkdtemp(prefix="venue-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_raw_ledger.json")
eng.ensure_ledger()
eng.quote_price = lambda chain, mint, spot_index=None: 0.001
eng._fresh_tape = lambda mint: {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(), "usd_liquidity": 5000.0,
                                "usd_market_cap": 60000.0,
                                "tape_source": "test"}


class FakeFeed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        out, self._items = self._items, []
        return out


res = GR.scan_once(eng, feed=FakeFeed([raw_item]), now=NOW)
check("scan: pump-amm stream item counted as stream",
      res["stream"] == 1)
check("scan: pump-amm stream item OPENED", res["opened"] == 1)
d = eng._read()
pos = d["positions"].get(MINT) or {}
check("scan: entry stamped detection=pumpportal-migration",
      (pos.get("entry_features") or {}).get("detection")
      == "pumpportal-migration")

print()
if fails:
    print("FAILURES:", len(fails))
    sys.exit(1)
print("ALL PASSED (test_stream_venue)")
