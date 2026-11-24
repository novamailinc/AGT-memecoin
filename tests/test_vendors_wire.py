#!/usr/bin/env python3
"""Vendor fallback-chain wiring tests (advanced build, piece 4).

Covers the vendors.py reconciliation + wiring (2026-11-21):
the declared chains are the LIVE order, and each check stamps
which vendor answered into ctx.values["vendor_chain"] (keyed by
need) so the select journal records the fallback path; the exit
poll stamps which vendor priced each eval into
pos["vendor_chain"], carried into the close record by _finalize.
Journal-only — nothing here gates an entry or an exit.
No network: every feed is stubbed. Temp ledgers only.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys
import tempfile
import time
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad as G
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import saturation as S
from daemon.launch_scanner import vendors as V

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
MINT_OK = "VendMintOK1111111111111111111111111111111pump"
TRADER_OK = "TraderVendOK111111111111111111111111111111"

# ---------------- select-side harness (from test_grad_select) ----
_tmp = tempfile.mkdtemp(prefix="vendors-wire-")
eng = GS.make_engine()
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
GS._INDEX["by_mint"][MINT_OK] = {
    "trader": TRADER_OK, "ts": NOW - 600, "bonding_curve": "Curve111"}
GS._INDEX["by_trader"][TRADER_OK] = [NOW - 600]
# 2026-11-24 conservative contract: stub the authoritative-history
# hook with this fixture's INTENDED complete history (TRADER_OK: 1),
# explicit, never derived from the recorder index.
GS._AUTHORITATIVE_LAUNCHES_24H = lambda c, n: \
    GS.authoritative_proof(c, {TRADER_OK: 1}.get(c, 0), n)

_CURVE = {
    MINT_OK: {"trades": 80, "created_ts": NOW - 600,
              "oldest_sig": "sig-ok", "capped": False,
              "exhausted": True, "rpc_source": "helius"},
}


def _fake_curve_stats(addr):
    return _CURVE[_fake_curve_stats.mint]


GS._curve_stats = _fake_curve_stats
GS._creator_from_oldest_tx = lambda sig: None
GS._canonical_pool_from_chain = \
    lambda curve_addr, pool, created_ts: None


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


def _eval(mint, symbol):
    _fake_curve_stats.mint = mint
    return GS.evaluate_candidate(
        eng, {"mint": mint, "symbol": symbol, "created_ts": NOW,
              "dex": "pumpswap", "pool_address": "PoolX",
              "migration_sig": "", "reserve_usd": 0.0, "fdv_usd": 0.0,
              "source": "pumpportal-migration"}, NOW)


# ---------------- select: all four needs stamped ----------------
check("wire: vendors module imported in grad_select",
      GS._vendors is not None)
v = _eval(MINT_OK, "OKONE")
check("wire: clean fast graduation still PASSES",
      v["verdict"] == "pass")
vc = v["values"].get("vendor_chain") or {}
check("wire: curve_reads stamped helius_rpc",
      (vc.get("curve_reads") or {}).get("used") == "helius_rpc")
check("wire: copycat_tally stamped local_recorder",
      (vc.get("copycat_tally") or {}).get("used") == "local_recorder")
check("wire: canonical_pool stamped migration_event",
      (vc.get("canonical_pool") or {}).get("used") == "migration_event")
check("wire: deployer_launches stamped local_recorder",
      (vc.get("deployer_launches") or {}).get("used")
      == "local_recorder")
check("wire: stamped chains are the declared live order",
      (vc.get("curve_reads") or {}).get("chain")
      == ["curve_cache", "helius_rpc", "public_rpc"]
      and (vc.get("copycat_tally") or {}).get("chain")
      == ["local_recorder", "dex_screener"])

# rpc_source variants map to the right chain step.
_CURVE[MINT_OK]["rpc_source"] = "cache"
v = _eval(MINT_OK, "OKONE")
check("wire: curve cache hit stamps curve_cache",
      ((v["values"].get("vendor_chain") or {}).get("curve_reads")
       or {}).get("used") == "curve_cache")
_CURVE[MINT_OK]["rpc_source"] = "public-rpc"
v = _eval(MINT_OK, "OKONE")
check("wire: public-RPC fallback stamps public_rpc",
      ((v["values"].get("vendor_chain") or {}).get("curve_reads")
       or {}).get("used") == "public_rpc")
_CURVE[MINT_OK]["rpc_source"] = "helius"

# ---------------- exit-side harness (from test_grad) -------------
TMPD = tempfile.mkdtemp(prefix="vendors-wire-grad-")
G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")


class FakeKS:
    def __init__(self):
        self.halted = set()
        self.recorded = []

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        self.halted.add(lane)
        return {"lane": lane, "action": "HALTED"}


FAKE_KS = FakeKS()
SAFETY_OK = {"passed": True, "unknown": False, "score": 7,
             "flags": [], "fails": [], "source": "rugcheck"}
TAPE_OK = {"usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
           "chg_m5": 3.0, "price_usd": 0.00007,
           "tape_source": "dexscreener"}


def _patch_all(price=0.00007):
    p1 = patch.object(G, "_veto")
    p2 = patch.object(G, "_safety")
    p3 = patch.object(G, "_ks", FAKE_KS)
    p4 = patch.object(G, "quote_price", return_value=price)
    p5 = patch.object(G, "_fresh_tape", return_value=dict(TAPE_OK))
    p6 = patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)
    m_veto, m_safety = p1.start(), p2.start()
    for p in (p3, p4, p5, p6):
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = dict(SAFETY_OK)
    return (p1, p2, p3, p4, p5, p6)


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.00007):
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps = _patch_all(price=price)
    G.ensure_ledger()
    return ps


def _cand(mint="AAA111pump", age_s=60.0, dex="pumpswap", **kw):
    c = {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
         "chain": "solana", "dex": dex,
         "created_ts": time.time() - age_s,
         "pool_address": "POOL1", "reserve_usd": 6_000.0,
         "fdv_usd": 69_000.0, "price_usd": 0.00007,
         "source": "gecko-new-pool"}
    c.update(kw)
    return c


check("wire: vendors module imported in grad", G._vendors is not None)

# price_fn prices -> caller_price_fn stamps the position.
ps = _fresh()
G.consider_candidate(_cand(mint="VND111pump"))
entry = G._load()["positions"]["VND111pump"]["entry_price"]
G.poll_exits(lambda c, m, s: entry * 1.10)
pos = G._load()["positions"].get("VND111pump") or {}
vc = pos.get("vendor_chain") or {}
check("wire: price_fn eval stamps caller_price_fn",
      (vc.get("exit_prices") or {}).get("used") == "caller_price_fn")
# price_fn dark is final (0 = dark this cycle, no fall-through
# to quote_price — the elif chain); the ds/gt path needs a poll
# with NO price_fn, so quote_price() prices the eval.
G.consider_candidate(_cand(mint="VND222pump"))
entry2 = G._load()["positions"]["VND222pump"]["entry_price"]
G.poll_exits()
pos2 = G._load()["positions"].get("VND222pump") or {}
vc2 = pos2.get("vendor_chain") or {}
check("wire: ds/gt eval stamps ds_gt on a fresh position",
      (vc2.get("exit_prices") or {}).get("used") == "ds_gt")
# First stamp wins: the VND111 position keeps caller_price_fn
# even after a ds/gt eval.
pos = G._load()["positions"].get("VND111pump") or {}
vc = pos.get("vendor_chain") or {}
check("wire: same-need stamp is not overwritten",
      (vc.get("exit_prices") or {}).get("used") == "caller_price_fn")
# hard stop closes VND111 only (mint-aware price_fn); VND222
# stays open at 1.0x.
closes = G.poll_exits(
    lambda c, m, s: entry * 0.49 if m == "VND111pump" else entry * 1.0)
rec = (G._load()["closes"] or [{}])[-1]
check("wire: hard stop still closes live",
      len(closes) == 1 and rec.get("closed_reason") == "hard_stop_-50pct")
check("wire: close carries vendor_chain exit_prices",
      ((rec.get("vendor_chain") or {}).get("exit_prices") or {})
      .get("used") == "caller_price_fn")
_unpatch(ps)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("ALL VENDOR-WIRE TESTS PASSED")
