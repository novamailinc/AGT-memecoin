#!/usr/bin/env python3
"""Jupiter Price v3 exit-pricing tests (2026-11-17).

Covers daemon/launch_scanner/jup_price.py and its wiring into the
grad engine (grad.py — the shared engine behind grad_select,
grad_raw, tranche, tranche_protect) and ceo.py:
  * v3 response parsing (usdPrice per mint; bad rows dropped)
  * batching (one GET per MAX_BATCH mints) + short TTL cache
    (hit inside TTL, refetch after expiry)
  * HTTP error / timeout / malformed-body fallback to {} (never
    raises, never a fabricated price)
  * graduated-vs-curve routing: a mint v3 has no entry for falls
    through to the existing DexScreener / Helius-reserves path
    (missing-mint fallback); a curve token in ceo.py is never
    offered to v3 at all
  * exit journaling: the close record carries price_source
    "jup-v3" when Jupiter priced the exit, "ds/gt" when the
    fallback did
  * entry quotes are untouched: quote_price() never consults
    Jupiter (even a exploding Jupiter module changes nothing)
No network. Plain script — run directly, not via pytest.
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
from unittest.mock import patch

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import ceo as C
from daemon.launch_scanner import grad as G
from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import jup_price as J

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


MINT_A = "MintA111111111111111111111111111111111111pump"
MINT_B = "MintB111111111111111111111111111111111111pump"
MINT_CURVE = "MintCurve11111111111111111111111111111111pump"

# ---------------- v3 response parsing ----------------
check("parse: valid usdPrice rows kept",
      J.parse_prices({MINT_A: {"usdPrice": 0.0123, "decimals": 6},
                      MINT_B: {"usdPrice": "0.5"}})
      == {MINT_A: 0.0123, MINT_B: 0.5})
check("parse: missing/zero/negative/non-numeric dropped, never priced",
      J.parse_prices({MINT_A: {"liquidity": 1},
                      MINT_B: {"usdPrice": 0},
                      "M3": {"usdPrice": -1},
                      "M4": {"usdPrice": "abc"},
                      "M5": "not-a-dict",
                      "M6": {"usdPrice": 0.02}})
      == {"M6": 0.02})
check("parse: non-dict body -> {}",
      J.parse_prices(None) == {} and J.parse_prices([1, 2]) == {})
check("parse: v3 URL only (v2 is dead, never used)",
      J.PRICE_V3_URL == "https://api.jup.ag/price/v3"
      and "v2" not in J.PRICE_V3_URL)
check("parse: cache TTL is short (10-15s)",
      10.0 <= J.CACHE_TTL_S <= 15.0)

# ---------------- batching + cache ----------------
J.clear_cache()
_calls = []


def _recording_fetch(mints):
    _calls.append(list(mints))
    return {m: 0.01 for m in mints}


with patch.object(J, "_fetch_batch", side_effect=_recording_fetch):
    mints55 = ["BatchMint%02d" % i for i in range(55)]
    out = J.get_prices(mints55, now=1000.0)
check("batch: 55 mints fetched in 2 calls (50 + 5)",
      len(_calls) == 2 and len(_calls[0]) == 50 and len(_calls[1]) == 5
      and len(out) == 55 and out["BatchMint00"] == 0.01)
with patch.object(J, "_fetch_batch", side_effect=_recording_fetch):
    J.get_prices(["BatchMint00"], now=1005.0)  # inside TTL: cache hit
check("cache: hit inside TTL makes zero HTTP calls",
      len(_calls) == 2)
with patch.object(J, "_fetch_batch", side_effect=_recording_fetch):
    J.get_prices(["BatchMint00"], now=1013.0)  # past 12s TTL: refetch
check("cache: expired entry refetched after TTL",
      len(_calls) == 3)
J.clear_cache()

# ---------------- HTTP error / timeout fallback ----------------
J.clear_cache()


class _BadResp:
    status = 500

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


with patch("urllib.request.urlopen", return_value=_BadResp()):
    check("http: non-200 falls back to {} (never raises)",
          J.get_prices([MINT_A], now=2000.0) == {})
with patch("urllib.request.urlopen",
           side_effect=urllib.error.HTTPError(
               "https://api.jup.ag/price/v3", 429, "Too Many Requests",
               {}, None)):
    check("http: HTTPError falls back to {} (never raises)",
          J.get_prices([MINT_A], now=2001.0) == {})
with patch("urllib.request.urlopen", side_effect=TimeoutError()):
    check("http: timeout falls back to {} (never raises)",
          J.get_prices([MINT_A], now=2002.0) == {})
with patch.object(J, "_fetch_batch", side_effect=RuntimeError("boom")):
    check("http: exploding fetcher still returns {} (never raises)",
          J.get_prices([MINT_A], now=2003.0) == {})
check("http: get_price unknown mint is 0.0, never fabricated",
      J.get_price(MINT_A, now=2004.0) == 0.0)
J.clear_cache()

# ---------------- harness: temp ledgers, fake killswitch ----------------
TMPD = tempfile.mkdtemp(prefix="jup-price-test-")
G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")
C.LEDGER_PATH = os.path.join(TMPD, "ceo_ledger.json")
C.BUS_PATH = os.path.join(TMPD, "signal_bus.jsonl")


class FakeKS:
    def is_halted(self, lane):
        return False

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        pass

    def check_lane(self, lane):
        return {"lane": lane}


_FAKE_KS = FakeKS()


def _grad_pos(mint, entry=0.01, tokens=2500.0):
    return {
        "mint": mint, "symbol": "TKN", "chain": "solana",
        "source": "graduation", "lane": "grad",
        "entry_ts": time.time(), "quoted_price": entry,
        "entry_price": entry, "notional_usd": 25.0,
        "peak_price": entry, "peak_ts": time.time(),
        "tokens": tokens, "gas_in_usd": 0.0,
        "legs": {
            "a": {"frac": 0.5, "target": 2.0, "done": False,
                  "exit_price": 0, "realized_usd": 0},
            "b": {"frac": 0.25, "target": 3.0, "done": False,
                  "exit_price": 0, "realized_usd": 0},
            "c": {"frac": 0.25, "trail": 0.3, "peak": entry,
                  "done": False, "exit_price": 0, "realized_usd": 0},
        },
        "realized_usd": 0.0, "status": "open",
        "spot_index": None, "pair": "POOL1",
        "entry_snapshot": {"liquidity": 100000.0},
        "entry_features": {},
    }


def _seed_grad(mint, entry=0.01):
    G.ensure_ledger()
    with G._ledger_lock():
        d = G._read()
        d["positions"] = {mint: _grad_pos(mint, entry)}
        d["closes"] = []
        G._write(d)


def _ceo_pos(mint, entry=0.01, graduated=True):
    pos = _grad_pos(mint, entry, tokens=100.0)
    pos["lane"] = "ceo"
    pos["notional_usd"] = 1.0
    pos["legs"] = {
        "a": {"frac": 0.5, "target": 2.0, "done": False,
              "exit_price": 0, "realized_usd": 0},
        "b": {"frac": 0.25, "target": 3.0, "done": False,
              "exit_price": 0, "realized_usd": 0},
        "c": {"frac": 0.25, "trail": 0.3, "peak": entry,
              "done": False, "exit_price": 0, "realized_usd": 0},
    }
    if graduated:
        pos["source"] = "graduation"
        pos["entry_snapshot"] = {"liquidity": 100000.0,
                                 "graduation": {"pool_address": "POOL1"}}
    else:
        pos["source"] = "test-signal"
        pos["entry_snapshot"] = {"liquidity": 100000.0}
    return pos


def _seed_ceo(mint, entry=0.01, graduated=True):
    C.ensure_ledger()
    with C._ledger_lock():
        d = C._read()
        d["positions"] = {mint: _ceo_pos(mint, entry, graduated)}
        d["closes"] = []
        C._write(d)


class _FakeJup:
    """Stand-in for jup_price with a fixed price map."""
    SOURCE = "jup-v3"

    def __init__(self, prices):
        self.prices = dict(prices)
        self.calls = []

    def get_prices(self, mints, now=None):
        self.calls.append(list(mints or []))
        return {m: self.prices[m] for m in (mints or [])
                if m in self.prices}

    def get_price(self, mint, now=None):
        return self.prices.get(mint, 0.0)


# ---------------- grad engine: Jupiter prices the exit ----------------
_fake = _FakeJup({MINT_A: 0.02})  # 2x entry: leg A must fire
_seed_grad(MINT_A)
with patch.object(G, "_ks", _FAKE_KS), \
        patch.object(G, "_jup", _fake), \
        patch.object(G, "_reserves", None):
    closes = G.poll_exits()
pos = G.open_positions()[MINT_A]
check("grad: Jupiter 2x fires leg A (ladder unchanged)",
      pos["legs"]["a"]["done"] and not pos["legs"]["b"]["done"])
check("grad: quote journal stamps the Jupiter source",
      (pos.get("quote_journal") or [])[-1].get("src") == "jup-v3")
check("grad: position stamps last_price_source jup-v3",
      pos.get("last_price_source") == "jup-v3")
check("grad: batch call covered the open set in one call",
      _fake.calls and _fake.calls[0] == [MINT_A])

# hard stop via Jupiter -> close record carries price_source jup-v3
_fake = _FakeJup({MINT_A: 0.004})  # 0.4x: hard stop
_seed_grad(MINT_A)
with patch.object(G, "_ks", _FAKE_KS), \
        patch.object(G, "_jup", _fake), \
        patch.object(G, "_reserves", None):
    closes = G.poll_exits()
check("grad: Jupiter hard-stop close carries price_source jup-v3",
      len(closes) == 1 and closes[0].get("price_source") == "jup-v3"
      and closes[0].get("closed_reason") == "hard_stop_-50pct")

# ---------------- grad: missing mint falls back (no Helius burn) -------
# v3 knows nothing about this mint: the DS path prices it instead.
_fake = _FakeJup({})
_seed_grad(MINT_B)


def _ds_quote(chain, mint, spot_index=None):
    G._LAST_QUOTE_PAIR = ""
    G._LAST_QUOTE_LIQ = 50000.0
    return 0.004  # 0.4x: hard stop via the fallback


with patch.object(G, "_ks", _FAKE_KS), \
        patch.object(G, "_jup", _fake), \
        patch.object(G, "_reserves", None), \
        patch.object(G, "quote_price", side_effect=_ds_quote):
    closes = G.poll_exits()
check("grad: mint missing from v3 falls back to DS (close happens)",
      len(closes) == 1
      and closes[0].get("closed_reason") == "hard_stop_-50pct")
check("grad: fallback close is journaled ds/gt, not jup-v3",
      closes[0].get("price_source") == "ds/gt")

# ---------------- grad: armed position pre-empts Helius reserves -------
# peak >= 1.5x entry = ARMED: the old path would call
# reserves.price_for_position (Helius, exempt). Jupiter must win
# and the reserves leg must never be consulted.
_seed_grad(MINT_A)
with G._ledger_lock():
    d = G._read()
    d["positions"][MINT_A]["peak_price"] = 0.02  # armed at 2x
    G._write(d)


class _ExplodingReserves:
    def price_for_position(self, pos):
        raise AssertionError("Helius reserves must not be called "
                             "when Jupiter priced the mint")


_fake = _FakeJup({MINT_A: 0.02})
with patch.object(G, "_ks", _FAKE_KS), \
        patch.object(G, "_jup", _fake), \
        patch.object(G, "_reserves", _ExplodingReserves()):
    closes = G.poll_exits()
pos = G.open_positions().get(MINT_A)
check("grad: armed position priced by Jupiter, reserves untouched",
      pos is not None and pos.get("last_price_source") == "jup-v3")

# price_fn still wins when it prices; Jupiter is its fallback only.
_seed_grad(MINT_A)
_fake = _FakeJup({MINT_A: 0.004})
with patch.object(G, "_ks", _FAKE_KS), \
        patch.object(G, "_jup", _fake), \
        patch.object(G, "_reserves", None):
    closes = G.poll_exits(lambda c, m, s=None: 0.02)  # external 2x
pos = G.open_positions()[MINT_A]
check("grad: live price_fn wins over Jupiter when it prices",
      pos["legs"]["a"]["done"]
      and pos.get("last_price_source") == "external")
_seed_grad(MINT_A)
with patch.object(G, "_ks", _FAKE_KS), \
        patch.object(G, "_jup", _fake), \
        patch.object(G, "_reserves", None):
    closes = G.poll_exits(lambda c, m, s=None: 0.0)  # external dark
check("grad: dark price_fn falls back to Jupiter (hard-stop close)",
      len(closes) == 1
      and closes[0].get("price_source") == "jup-v3")

# ---------------- ceo: graduated vs curve routing ----------------------
check("ceo: graduated marker detected",
      C._is_graduated_solana(_ceo_pos(MINT_A, graduated=True)) is True)
check("ceo: curve token (no graduation marker) is NOT graduated",
      C._is_graduated_solana(_ceo_pos(MINT_CURVE, graduated=False))
      is False)
check("ceo: non-Solana chain is NOT graduated",
      C._is_graduated_solana(
          dict(_ceo_pos(MINT_A), chain="base")) is False)

_fake = _FakeJup({MINT_A: 0.004})
_seed_ceo(MINT_A, graduated=True)
with patch.object(C, "_ks", _FAKE_KS), \
        patch.object(C, "_jup", _fake), \
        patch.object(C, "quote_price",
                     side_effect=AssertionError(
                         "DS must not be called when Jupiter priced it")):
    closes = C.poll_exits()
check("ceo: graduated exit priced by Jupiter, close journaled jup-v3",
      len(closes) == 1 and closes[0].get("price_source") == "jup-v3"
      and closes[0].get("closed_reason") == "hard_stop_-50pct")

# curve token: Jupiter HAS a price for the mint in the stub, but the
# position is not graduated, so v3 must never be consulted for it —
# the DS path prices it and the close is journaled ds/gt.
_fake = _FakeJup({MINT_CURVE: 0.004})
_seed_ceo(MINT_CURVE, graduated=False)
with patch.object(C, "_ks", _FAKE_KS), \
        patch.object(C, "_jup", _fake), \
        patch.object(C, "quote_price", return_value=0.004):
    closes = C.poll_exits()
check("ceo: curve token never offered to Jupiter (batch excludes it)",
      _fake.calls == [[]] or all(
          MINT_CURVE not in call for call in _fake.calls))
check("ceo: curve close journaled ds/gt, Helius path preserved",
      len(closes) == 1 and closes[0].get("price_source") == "ds/gt")

# ---------------- entry quotes untouched by Jupiter ---------------------
class _ExplodingJup:
    SOURCE = "jup-v3"

    def get_prices(self, mints, now=None):
        raise AssertionError("entry quote must never call Jupiter")

    def get_price(self, mint, now=None):
        raise AssertionError("entry quote must never call Jupiter")


def _ds_pairs(url, timeout=12, payload=None):
    return [{"priceUsd": "0.02", "liquidity": {"usd": 50000.0}}]


with patch.object(G, "_jup", _ExplodingJup()), \
        patch.object(G, "_http_json", side_effect=_ds_pairs), \
        patch.object(G, "_fresh_tape", return_value=None), \
        patch.object(G, "QUOTE_RETRY_GAP_S", 0.0):
    check("entry: quote_price still DexScreener with Jupiter exploding",
          G.quote_price("solana", MINT_A) == 0.02)

# ---------------- select engine inherits the wiring --------------------
eng = GS.make_engine()
check("select: isolated engine imports jup_price",
      getattr(eng, "_jup", None) is not None)
check("select: engine poll_exits is the wired grad implementation",
      eng._jup.SOURCE == "jup-v3")

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all jup-price tests passed")
