#!/usr/bin/env python3
"""DexScreener ticker->mint tests (2026-11-17, item 2 of 5).

Covers daemon/launch_scanner/ds_ticker.py (no network: _fetch_search
is monkeypatched) and its grad_select Filter 4 integration:
solana-only filtering, exact-symbol match (case-insensitive),
FDV ordering / highest-FDV resolution, ambiguous-top and missing
ticker -> unknown (fail-closed), trailing-24h window incl. the
exactly-24h boundary, self-mint exclusion, dedupe cache, rate
guard (min interval + hourly cap), HTTP-error fallback, and the
Filter 4 fallback path: stale saturation -> dexscreener serves the
count (journalling copycat_source), fresh saturation still wins,
both sources dark -> unknown_copycat.
No network. Plain script — run directly, not via pytest.
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

import ds_ticker as DST
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import saturation as S

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
NOW_MS = int(NOW * 1000)
H = 3600 * 1000


def _pair(mint, symbol, fdv, created_ms, chain="solana"):
    return {"chainId": chain,
            "pairAddress": "Pair" + mint[:8],
            "baseToken": {"address": mint, "symbol": symbol},
            "fdv": fdv, "pairCreatedAt": created_ms}


def _payload(*pairs):
    return {"schemaVersion": "1.0.0", "pairs": list(pairs)}


# ---- parse: solana-only, exact symbol, FDV order -------------------
p = _payload(
    _pair("MintSOL1", "BONK", 5000.0, NOW_MS - 3600 * 1000),
    _pair("MintETH1", "BONK", 999999.0, NOW_MS - 1000, chain="ethereum"),
    _pair("MintSOL2", "bonk", 9000.0, NOW_MS - 2000),
    _pair("MintSOL3", "BONK2", 80000.0, NOW_MS - 3000),
    {"chainId": "solana", "pairAddress": "PairNoBase",
     "fdv": 1.0, "pairCreatedAt": NOW_MS - 4000},
    _pair("MintSOL4", "BONK", 100.0, NOW_MS - 25 * 3600 * 1000),
)
parsed = DST.parse_search(p)
check("parse: only solana pairs with a mint survive",
      [x["mint"] for x in parsed] == ["MintSOL1", "MintSOL2",
                                      "MintSOL3", "MintSOL4"])
check("parse: non-dict / non-list payload -> []",
      DST.parse_search(None) == [] and DST.parse_search({"pairs": {}}) == []
      and DST.parse_search({"pairs": "junk"}) == [])
check("parse: missing fdv -> 0.0, missing pairCreatedAt -> None",
      DST.parse_search(_payload({"chainId": "solana", "pairAddress": "P",
                                 "baseToken": {"address": "M",
                                               "symbol": "X"}}))
      == [{"mint": "M", "symbol": "X", "fdv": 0.0, "created_ms": None}])

m = DST.matching_pairs(p, "bonk")
check("match: exact symbol, case-insensitive, BONK2 excluded",
      [x["mint"] for x in m] == ["MintSOL2", "MintSOL1", "MintSOL4"])
check("match: FDV desc order", m[0]["fdv"] == 9000.0
      and m[1]["fdv"] == 5000.0 and m[2]["fdv"] == 100.0)
check("match: blank symbol -> []", DST.matching_pairs(p, "  ") == [])
check("match: no match -> []",
      DST.matching_pairs(_payload(_pair("M1", "PEPE", 1.0, NOW_MS)),
                         "BONK") == [])

# ---- fetch stubbing: cache, rate guard, errors ---------------------
_fetch_calls = []


def _stub_fetch(payload):
    def _f(symbol):
        _fetch_calls.append(symbol)
        return payload
    return _f


_orig_fetch = DST._fetch_search
DST.clear_cache()

DST._fetch_search = _stub_fetch(p)
_fetch_calls.clear()
r1 = DST.count_24h("BONK", now=NOW, self_mint="MintSOL2")
r2 = DST.count_24h("BONK", now=NOW, self_mint="MintSOL2")
check("count: cache hit — second call makes no fetch",
      len(_fetch_calls) == 1 and r1 == r2)
check("count: tally counts in-window only (25h-old excluded)",
      r1 is not None and r1["tally"] == 2 and r1["pairs_seen"] == 3)
check("count: self mint excluded from others",
      r1 is not None and r1["others"] == 1 and r1["self_seen"] is True)
check("count: top mint is highest-FDV match (all ages)",
      r1 is not None and r1["top_mint"] == "MintSOL2")

DST.clear_cache()
_fetch_calls.clear()
r = DST.count_24h("BONK", now=NOW, self_mint="MintNobody")
check("count: candidate not indexed yet — every match is an other",
      r is not None and r["tally"] == 2 and r["others"] == 2
      and r["self_seen"] is False)

# boundary: a mate at exactly now-24h is OUTSIDE (now-24h, now];
# a mate 1s inside counts.
DST.clear_cache()
DST._fetch_search = _stub_fetch(_payload(
    _pair("SelfMint", "EDGE", 100.0, NOW_MS - 900 * 1000),
    _pair("EdgeOld", "EDGE", 100.0, NOW_MS - 86400 * 1000),
    _pair("EdgeIn", "EDGE", 100.0, NOW_MS - 86399 * 1000)))
r = DST.count_24h("EDGE", now=NOW, self_mint="SelfMint")
check("count: mate at exactly 24h does NOT count, 1s inside does",
      r is not None and r["tally"] == 2 and r["others"] == 1)

# missing ticker -> unknown (fail-closed), never 0
DST.clear_cache()
DST._fetch_search = _stub_fetch(_payload(
    _pair("M1", "PEPE", 1.0, NOW_MS - 1000)))
check("count: zero matching pairs -> None (unknown, not 0)",
      DST.count_24h("BONK", now=NOW) is None)
check("resolve: zero matching pairs -> None",
      DST.resolve_mint("BONK", now=NOW + 200) is None)

# ambiguous top: two mints tied at max FDV -> None; count still works
DST.clear_cache()
DST._fetch_search = _stub_fetch(_payload(
    _pair("TieA", "TIE", 500.0, NOW_MS - 1000),
    _pair("TieB", "TIE", 500.0, NOW_MS - 2000)))
check("resolve: FDV tie at top between mints -> None (ambiguous)",
      DST.resolve_mint("TIE", now=NOW) is None)
r = DST.count_24h("TIE", now=NOW)
check("count: ambiguous top still counts, top_mint None",
      r is not None and r["tally"] == 2 and r["others"] == 2
      and r["top_mint"] is None)

# distinct top FDV resolves
DST.clear_cache()
DST._fetch_search = _stub_fetch(_payload(
    _pair("TopA", "TOP", 900.0, NOW_MS - 1000),
    _pair("TopB", "TOP", 100.0, NOW_MS - 2000)))
check("resolve: highest-FDV mint returned",
      DST.resolve_mint("TOP", now=NOW) == "TopA")

# HTTP error -> unknown; negative result cached briefly, then retries
DST.clear_cache()
DST._fetch_search = lambda symbol: None
_fetch_calls.clear()
check("count: fetch failure -> None (fail-closed)",
      DST.count_24h("BONK", now=NOW) is None)
DST._fetch_search = _stub_fetch(p)
_fetch_calls.clear()
check("count: failed lookup cached — no refetch inside FAIL_TTL_S",
      DST.count_24h("BONK", now=NOW + 10) is None
      and _fetch_calls == [])
check("count: negative cache expires — refetch after FAIL_TTL_S",
      DST.count_24h("BONK", now=NOW + 61) is not None
      and _fetch_calls == ["BONK"])

# rate guard: min interval
DST.clear_cache()
DST._fetch_search = _stub_fetch(p)
DST._last_fetch_ts = NOW  # a fetch just happened
_fetch_calls.clear()
check("guard: second distinct fetch inside 1s throttled -> None",
      DST.count_24h("BONK", now=NOW + 0.5) is None
      and _fetch_calls == [])
check("guard: fetch allowed after the interval",
      DST.count_24h("BONK", now=NOW + 1.5) is not None
      and _fetch_calls == ["BONK"])

# rate guard: hourly cap
DST.clear_cache()
DST._fetch_search = _stub_fetch(p)
DST._hour_stamps[:] = [NOW - 10] * DST.HOURLY_CAP
_fetch_calls.clear()
check("guard: hourly cap reached -> None, no fetch",
      DST.count_24h("BONK", now=NOW) is None and _fetch_calls == [])
DST.clear_cache()
check("guard: cap window rolls off after an hour",
      DST.count_24h("BONK", now=NOW + 3601) is not None)

# blank symbol -> None everywhere
DST.clear_cache()
check("guard: blank symbol -> None (count + resolve)",
      DST.count_24h("   ", now=NOW) is None
      and DST.resolve_mint("", now=NOW) is None)

DST._fetch_search = _orig_fetch
DST.clear_cache()

# ---- grad_select Filter 4 integration ------------------------------
# Mirror test_grad_select.py's stubs: clean fast graduation, recorder
# index in-memory, curve stats scripted, saturation stand-in.
eng = GS.make_engine()
_tmp = tempfile.mkdtemp(prefix="dsticker-ledger-")
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

MINT_DS = "DsMintOK11111111111111111111111111111111pump"
TRADER_DS = "TraderDS11111111111111111111111111111111"
GS._INDEX["by_mint"][MINT_DS] = {
    "trader": TRADER_DS, "ts": NOW - 600, "bonding_curve": "Curve111"}
GS._INDEX["by_trader"][TRADER_DS] = [NOW - 600]
# 2026-11-24 conservative contract: recorder-only sub-threshold
# counts never admit; stub the authoritative-history hook with this
# fixture's INTENDED complete per-creator history (explicit table,
# never derived from the recorder index).
GS._AUTHORITATIVE_LAUNCHES_24H = lambda c, n: \
    GS.authoritative_proof(c, {TRADER_DS: 1}.get(c, 0), n)
GS._curve_stats = lambda addr: {"trades": 80, "created_ts": NOW - 600,
                                "oldest_sig": "sig-ds", "capped": False,
                                "exhausted": True}
GS._creator_from_oldest_tx = lambda sig: None


class _FakeSat:
    """Saturation stand-in with a controllable freshness stamp."""

    def __init__(self, launches, updated):
        self.launches = launches
        self.updated = updated

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": dict(self.launches)}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


def _cand(mint, symbol):
    return {"mint": mint, "symbol": symbol, "created_ts": NOW,
            "dex": "pumpswap", "pool_address": "PoolX",
            "migration_sig": "", "reserve_usd": 0.0, "fdv_usd": 0.0,
            "source": "pumpportal-migration"}


_orig_sat = GS._saturation
_orig_ds = GS._ds_ticker
GS._ds_ticker = DST  # real module, fetch stubbed below

# Case A: stale saturation + DS shows self only -> PASS via dexscreener
_SAT_STALE = _FakeSat({}, NOW - 10000)
GS._saturation = _SAT_STALE
DST.clear_cache()
DST._fetch_search = _stub_fetch(_payload(
    _pair(MINT_DS, "DSTOK", 5000.0, NOW_MS - 600 * 1000)))
v = GS.evaluate_candidate(eng, _cand(MINT_DS, "DstOk"), NOW)
check("filter4: stale saturation + DS self-only -> PASS, source=dexscreener",
      v["verdict"] == "pass"
      and v["values"].get("copycat_source") == "dexscreener"
      and v["values"].get("ticker_others_24h") == 0)

# Case B: stale saturation + DS shows 3 others -> copycat REJECT
DST.clear_cache()
DST._fetch_search = _stub_fetch(_payload(
    _pair(MINT_DS, "DSTOK", 5000.0, NOW_MS - 600 * 1000),
    _pair("Mate1", "DSTOK", 4000.0, NOW_MS - 3600 * 1000),
    _pair("Mate2", "DSTOK", 3000.0, NOW_MS - 7200 * 1000),
    _pair("Mate3", "DSTOK", 2000.0, NOW_MS - 10800 * 1000)))
v = GS.evaluate_candidate(eng, _cand(MINT_DS, "DstOk"), NOW)
check("filter4: stale saturation + DS 3 others -> copycat REJECT",
      v["verdict"] == "reject" and v["reason"] == "copycat_ticker"
      and v["values"].get("ticker_others_24h") == 3
      and v["values"].get("copycat_source") == "dexscreener")

# Case C: stale saturation + DS dark -> unknown_copycat (fail-closed)
DST.clear_cache()
DST._fetch_search = lambda symbol: None
v = GS.evaluate_candidate(eng, _cand(MINT_DS, "DstOk"), NOW)
check("filter4: stale saturation + DS dark -> unknown_copycat",
      v["verdict"] == "reject" and v["reason"] == "unknown_copycat")

# Case D: fresh saturation still wins (DS never consulted)
_SAT_FRESH = _FakeSat(
    {"DSTOK": [NOW_MS - 600 * 1000, NOW_MS - 3600 * 1000,
               NOW_MS - 7200 * 1000, NOW_MS - 10800 * 1000]}, NOW)
GS._saturation = _SAT_FRESH
DST.clear_cache()
_fetch_calls.clear()
DST._fetch_search = _stub_fetch(_payload(
    _pair(MINT_DS, "DSTOK", 5000.0, NOW_MS - 600 * 1000)))
v = GS.evaluate_candidate(eng, _cand(MINT_DS, "DstOk"), NOW)
check("filter4: fresh saturation wins, DS not fetched, source=saturation",
      v["verdict"] == "reject" and v["reason"] == "copycat_ticker"
      and v["values"].get("copycat_source") == "saturation"
      and _fetch_calls == [])

# Case E: '?' placeholder skips lookup entirely (stale tally + DS dark)
GS._saturation = _SAT_STALE
DST.clear_cache()
DST._fetch_search = lambda symbol: None
v = GS.evaluate_candidate(eng, _cand(MINT_DS, "?"), NOW)
check("filter4: '?' skips copycat lookup, source=skipped",
      v["verdict"] == "pass"
      and v["values"].get("copycat_source") == "skipped"
      and v["values"].get("ticker_others_24h") == 0)

# Case F: both modules missing -> unknown_copycat
GS._saturation = None
GS._ds_ticker = None
v = GS.evaluate_candidate(eng, _cand(MINT_DS, "DstOk"), NOW)
check("filter4: no saturation + no ds_ticker -> unknown_copycat",
      v["verdict"] == "reject" and v["reason"] == "unknown_copycat")

# Case G: saturation module missing, DS alone serves the count
GS._saturation = None
GS._ds_ticker = DST
DST.clear_cache()
DST._fetch_search = _stub_fetch(_payload(
    _pair(MINT_DS, "DSTOK", 5000.0, NOW_MS - 600 * 1000)))
v = GS.evaluate_candidate(eng, _cand(MINT_DS, "DstOk"), NOW)
check("filter4: saturation missing + DS self-only -> PASS, source=dexscreener",
      v["verdict"] == "pass"
      and v["values"].get("copycat_source") == "dexscreener")

GS._saturation = _orig_sat
GS._ds_ticker = _orig_ds
DST._fetch_search = _orig_fetch
DST.clear_cache()

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
