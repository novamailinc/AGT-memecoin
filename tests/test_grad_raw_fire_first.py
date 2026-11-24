#!/usr/bin/env python3
"""Graduation RAW lane FIRE-FIRST tests (2026-10-23).

Covers grad_raw.py's migration-transaction instant entry: payload
pricing correctness (migration-tx reserves -> price, then the same
honest adverse fill + venue fee the classic path applies), pool
address filled from the tx, fallback to the live quote path when
the payload cannot price (missing signature / tx unavailable),
post-entry deviation flagging (dq_deviation / dq_unverifiable /
ok), dedupe + killswitch behavior on the instant path, fresh_only
stream-wake scans, and gated-lane isolation. No network: the tx
fetch and both quote layers are stubbed. Plain script — run
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

from daemon.launch_scanner import grad as GATED
from daemon.launch_scanner import grad_raw as GR
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import saturation as _S


def _scan_cycle(eng, **kw):
    """One scan as its own cycle: the 2026-11-21 cadence gate
    (scan_schedule, piece 9) rate-limits back-to-back scans, but this
    test drives each scan deliberately, so reset the lane's clock."""
    if GR._select is not None:
        GR._select._SCAN_SCHEDULE_STATE["last_scan_ts"].pop(
            GR.LANE, None)
    _orig = GR.scan_once
    return _orig(eng, **kw)

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
MINT = "FireMint1111111111111111111111111111111pump"
MINT2 = "FireMint2222222222222222222222222222222pump"
MINT3 = "FireMint3333333333333333333333333333333pump"
MINT4 = "FireMint4444444444444444444444444444444pump"
MINT5 = "FireMint5555555555555555555555555555555pump"
MINT6 = "FireMint6666666666666666666666666666666pump"
MINT7 = "FireMint7777777777777777777777777777777pump"
MINT8 = "FireMint8888888888888888888888888888888pump"

# ---------------- isolation ----------------
_ks_tmp = tempfile.mkdtemp(prefix="gradraw-ff-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")

eng = GR.make_engine()
check("fire-first installed on the raw engine only",
      getattr(eng, "_ff_installed", False) is True
      and not hasattr(GATED, "_ff_installed")
      and GATED.LANE == "grad")

_tmp = tempfile.mkdtemp(prefix="gradraw-ff-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_raw_ledger.json")
eng.ensure_ledger()
# Fund the fixture for every planned entry at the lane's notional
# (7 entries), whatever the current stake is — the test asserts
# entry mechanics, not the $1-era bankroll size.
with eng._ledger_lock():
    _d = eng._read()
    _d["bankroll"] = 10 * GR.NOTIONAL_USD
    eng._write(_d)

# Classic-path stubs at the ORIGINAL layer: the fire-first wrappers
# delegate here whenever no entry context is active. A mutable
# holder lets each phase set the "live market" price.
LIVE = {"px": 0.001}
eng._ff_orig_quote_price = \
    lambda chain, mint, spot_index=None: LIVE["px"]
eng._ff_orig_fresh_tape = lambda mint: {
    "usd_liquidity": 5000.0, "usd_market_cap": 60000.0,
    "tape_source": "test"}


class _ExplodingSafety:
    def check_token(self, *a, **k):
        raise AssertionError("safety consulted on the RAW lane")


eng._safety = _ExplodingSafety()

# ---------------- filter data stubs (2026-11-10 filter port) ----------------
# The raw lane now gates entries on grad_select's four filters, via
# the SHARED module — seed its in-memory index through GR._select.
# Refresh disabled; no network. Every fixture mint is a clean,
# fast, single-launch graduation so the fire-first mechanics under
# test are exercised, not the filters.
SEL = GR._select
# 2026-11-24 conservative contract: recorder-only sub-threshold
# counts never admit; stub the authoritative-history hook with this
# fixture's INTENDED complete per-creator history (explicit table,
# never derived from the recorder index).
SEL._AUTHORITATIVE_LAUNCHES_24H = lambda c, n: SEL.authoritative_proof(
    c, 1 if str(c).startswith("TraderFF") else 0, n)
SEL._index_refresh = lambda now: None
SEL._INDEX["available"] = True
SEL._INDEX["coverage_start"] = NOW - 90000
SEL._INDEX["event_ts"] = [NOW - 86400 + i * 300 for i in range(289)]  # continuity fixture (2026-11-24)

_ALL_MINTS = [MINT, MINT2, MINT3, MINT4, MINT5, MINT6, MINT7, MINT8]


def _birth_ff(mint, i):
    trader = "TraderFF%d111111111111111111111111111111" % i
    SEL._INDEX["by_mint"][mint] = {
        "trader": trader, "ts": NOW - 600,
        "bonding_curve": "CurveFF%d" % i}
    SEL._INDEX["by_trader"][trader] = [NOW - 600]


for _i, _m in enumerate(_ALL_MINTS):
    _birth_ff(_m, _i)
SEL._INDEX["by_mig"][MINT7] = {
    "pool": "PoolBackfill111111111111111111111111", "ts": NOW - 60}

_FF_CURVE = {
    "CurveFF%d" % i: {"trades": 80, "created_ts": NOW - 600,
                      "oldest_sig": "sig-ff%d" % i, "capped": False,
                      "exhausted": True}
    for i in range(len(_ALL_MINTS))
}
SEL._curve_stats = lambda addr: _FF_CURVE.get(addr) or {
    "trades": None, "created_ts": None, "oldest_sig": None,
    "capped": False, "exhausted": False}


class _FakeSatFF:
    def __init__(self):
        self.updated = NOW

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": {}}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return _S.rolling_count(state, symbol, window_s, now_s)


SEL._saturation = _FakeSatFF()

PAYLOAD_INFO = {"price_usd": 8.0e-5, "liquidity_usd": 32300.0,
                "mcap_usd": 80000.0,
                "pool": "TxPool111111111111111111111111111111111",
                "quote_sol": 85.0, "sol_usd": 190.0}
eng._ff_tx_price_override = lambda sig, mint: (dict(PAYLOAD_INFO), "")


class FakeFeed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        out, self._items = self._items, []
        return out


def mig_item(mint, sig="SigFireFirst1111111111111111111111111111",
             market_id=""):
    """A migration item in the REAL recorded payload shape: venue
    alias pump-amm, market_id empty, market_cap_sol 0 — plus the
    signature, the one field that prices the entry."""
    return {"mint": mint, "symbol": "FIRE",
            "source": "pumpportal-migration",
            "created_ts": time.time(),
            "extra": {"migration_pool": "pump-amm",
                      "market_id": market_id,
                      "market_cap_sol": 0.0,
                      "signature": sig}}


# ---------------- pure tx parse ----------------
WSOL = "So11111111111111111111111111111111111111112"
fake_tx = {"meta": {"err": None, "postTokenBalances": [
    {"owner": "DecoyWallet111111111111111111111111111111",
     "mint": MINT,
     "uiTokenAmount": {"amount": "5000000000", "decimals": 6}},
    {"owner": "PoolOwner1111111111111111111111111111111111",
     "mint": MINT,
     "uiTokenAmount": {"amount": "206900000000000", "decimals": 6}},
    {"owner": "PoolOwner1111111111111111111111111111111111",
     "mint": WSOL,
     "uiTokenAmount": {"amount": "85000000000", "decimals": 9}},
]}}
info, why = GR._ff_parse_tx_price(fake_tx, MINT, sol_usd=200.0)
exp_px = (85.0 / 206900000.0) * 200.0
check("tx parse: price from the pool's own initial reserves",
      info is not None and abs(info["price_usd"] - exp_px) < 1e-10
      and info["pool"] == "PoolOwner1111111111111111111111111111111111")
check("tx parse: mcap = price x fixed supply, inside bounds",
      info is not None
      and abs(info["mcap_usd"] - exp_px * 1e9) < 1e-3)
unknown_tx = dict(fake_tx, meta=dict(fake_tx['meta']))
del unknown_tx['meta']['err']
unknown_info, unknown_reason = GR._ff_parse_tx_price(unknown_tx,MINT,sol_usd=100.0)
check("tx parse: missing status stays unknown", unknown_info is None and 'tx_unknown' in unknown_reason)

info2, why2 = GR._ff_parse_tx_price({"meta": {"err": None, "postTokenBalances": [
    {"owner": "PoolOwner1111111111111111111111111111111111",
     "mint": MINT,
     "uiTokenAmount": {"amount": "206900000000000", "decimals": 6}},
]}}, MINT, sol_usd=200.0)
check("tx parse: no paired WSOL vault -> no payload price",
      info2 is None and "no paired" in why2)
info3, why3 = GR._ff_parse_tx_price({"meta": {"err": None, "postTokenBalances": [
    {"owner": "PoolOwner1111111111111111111111111111111111",
     "mint": MINT,
     "uiTokenAmount": {"amount": "206900000000000", "decimals": 6}},
    {"owner": "PoolOwner1111111111111111111111111111111111",
     "mint": WSOL,
     "uiTokenAmount": {"amount": "850000000", "decimals": 9}},
]}}, MINT, sol_usd=200.0)
check("tx parse: garbage mcap (0.85 SOL pool) refused by bounds",
      info3 is None and "outside graduation bounds" in why3)

# ---------------- instant entry ----------------
res = _scan_cycle(eng, feed=FakeFeed([mig_item(MINT)]),
                   now=time.time())
check("instant: stream candidate opened", res["opened"] == 1)
d = eng._read()
pos = d["positions"][MINT]
ef = pos["entry_features"]
check("instant: entry_price_source=migration_payload journaled",
      ef.get("entry_price_source") == "migration_payload"
      and abs(ef.get("payload_price_usd") - 8.0e-5) < 1e-15
      and ef.get("payload_pool") == PAYLOAD_INFO["pool"])
check("instant: quoted price IS the payload price",
      pos["quoted_price"] == 8.0e-5)
gas = eng._costs.tx_cost_usd("solana", GR.NOTIONAL_USD,
                             venue=eng.VENUE)
exp_filled = eng._fills.honest_entry(8.0e-5,
                                     GR.NOTIONAL_USD - gas,
                                     PAYLOAD_INFO["liquidity_usd"])
check("instant: same honest adverse fill + venue fee as classic",
      abs(pos["entry_price"] - exp_filled) < 1e-12
      and abs(pos["tokens"]
              - (GR.NOTIONAL_USD - gas) / exp_filled) < 1e-3)
check("instant: pool address filled from the migration tx",
      pos["pair"] == PAYLOAD_INFO["pool"]
      and ef.get("pool") == PAYLOAD_INFO["pool"])
check("instant: entered at the migration instant (no quote chain)",
      pos["entry_ts"] - pos["born_ts"] < 2.0)
check("instant: notional debited, verification pending",
      abs(d["bankroll"] - 9 * GR.NOTIONAL_USD) < 1e-6
      and MINT in getattr(eng, "_ff_pending", {}))

# ---------------- post-entry verification ----------------
LIVE["px"] = 4.0e-5  # live market says the payload was 2x too high
GR._ff_verify_pending(eng, time.time() + 30)
d = eng._read()
ef = d["positions"][MINT]["entry_features"]
check("verify: 50% deviation -> dq_deviation + entry_dq flag",
      (ef.get("payload_verify") or {}).get("status") == "dq_deviation"
      and d["positions"][MINT].get("entry_dq") is True
      and MINT not in eng._ff_pending)

res = _scan_cycle(eng, feed=FakeFeed([mig_item(MINT2)]),
                   now=time.time())
check("instant: second payload entry opened", res["opened"] == 1)
LIVE["px"] = 8.3e-5  # 3.75% off the payload — inside tolerance
GR._ff_verify_pending(eng, time.time() + 30)
d = eng._read()
ef2 = d["positions"][MINT2]["entry_features"]
check("verify: small deviation -> ok, no dq flag",
      (ef2.get("payload_verify") or {}).get("status") == "ok"
      and "entry_dq" not in d["positions"][MINT2])

res = _scan_cycle(eng, feed=FakeFeed([mig_item(MINT3)]),
                   now=time.time())
check("instant: third payload entry opened", res["opened"] == 1)
LIVE["px"] = 0.0  # pool unquotable on every source
for i in range(3):
    GR._ff_verify_pending(eng, time.time() + 30 + i)
d = eng._read()
ef3 = d["positions"][MINT3]["entry_features"]
check("verify: persistently unquotable -> dq_unverifiable",
      (ef3.get("payload_verify") or {}).get("status")
      == "dq_unverifiable"
      and d["positions"][MINT3].get("entry_dq") is True)

# ---------------- fallback: tx cannot price ----------------
eng._ff_tx_price_override = \
    lambda sig, mint: (None, "migration tx not available yet")
LIVE["px"] = 0.001
# Pool address present (as the GT backfill merge would supply);
# the tx simply cannot price yet, so the classic quote path enters.
res = _scan_cycle(eng, feed=FakeFeed([mig_item(
    MINT4, market_id="PoolFallback111111111111111111111111")]),
    now=time.time())
d = eng._read()
ef4 = d["positions"][MINT4]["entry_features"]
check("fallback: entry still opens on the live quote path",
      res["opened"] == 1
      and d["positions"][MINT4]["quoted_price"] == 0.001)
check("fallback: price source + reason journaled",
      ef4.get("entry_price_source") == "live_quote"
      and "migration tx not available" in ef4.get("ff_fallback", ""))

# ---------------- fallback: no signature in payload ----------------
item5 = mig_item(MINT5, market_id="PoolClassic11111111111111111111111")
del item5["extra"]["signature"]
res = _scan_cycle(eng, feed=FakeFeed([item5]), now=time.time())
d = eng._read()
ef5 = d["positions"][MINT5]["entry_features"]
check("fallback: signature-less payload uses the classic path",
      res["opened"] == 1
      and ef5.get("entry_price_source") == "live_quote"
      and "no migration signature" in ef5.get("ff_fallback", "")
      and d["positions"][MINT5]["pair"]
      == "PoolClassic11111111111111111111111")

# ---------------- dedupe + killswitch on the instant path ----------------
eng._ff_tx_price_override = lambda sig, mint: (dict(PAYLOAD_INFO), "")
res = _scan_cycle(eng, feed=FakeFeed([mig_item(MINT)]),
                   now=time.time())
check("dedupe: instant path never re-enters a mint", res["opened"] == 0)
KS.halt_lane("grad_raw", "fire-first test halt")
res = _scan_cycle(eng, feed=FakeFeed([mig_item(MINT6)]),
                   now=time.time())
d = eng._read()
check("killswitch: halted lane blocks instant entries too",
      res["opened"] == 0 and MINT6 not in d["positions"])
if hasattr(KS, "reenable"):
    KS.reenable("grad_raw", "fire-first test cleanup")

# ---------------- fresh_only stream-wake scans ----------------
LIVE["px"] = 0.0  # classic path cannot price anything right now
gt_row = {"mint": MINT7, "symbol": "FIRE7",
          "created_ts": time.time() - 5, "dex": "pumpswap",
          "pool_address": "PoolBackfill111111111111111111111111",
          "reserve_usd": 9000.0, "fdv_usd": 80000.0}
_scan_cycle(eng, gt_rows=[gt_row], now=time.time())
d = eng._read()
check("fresh_only setup: unquotable backfill row waits on watchlist",
      MINT7 in d["watchlist"] and MINT7 not in d["positions"])
LIVE["px"] = 0.001
res = _scan_cycle(eng, feed=FakeFeed([mig_item(MINT8)]),
                   now=time.time(), fresh_only=True)
d = eng._read()
check("fresh_only: stream candidate enters, watchlist retry untouched",
      res["opened"] == 1 and MINT8 in d["positions"]
      and MINT7 in d["watchlist"] and MINT7 not in d["positions"])
res = _scan_cycle(eng, now=time.time())
d = eng._read()
ef7 = d["positions"][MINT7]["entry_features"]
check("main scan: backfill retry enters via the classic path",
      res["opened"] == 1
      and ef7.get("entry_price_source") == "live_quote"
      and ef7.get("detection") == "geckoterminal-backfill")

print()
if fails:
    print("FAILURES (%d): %s" % (len(fails), fails))
    sys.exit(1)
print("ALL GRAD-RAW FIRE-FIRST TESTS PASSED")
