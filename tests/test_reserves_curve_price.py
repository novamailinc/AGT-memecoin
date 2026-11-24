#!/usr/bin/env python3
"""Hermetic tests for reserves.curve_price_observed/_usd under the
quote-aware pump.fun schema (authoritative pump-public-docs IDL:
quote_mint pubkey @83, so >= 115-byte accounts; quote asset is NOT
always SOL — live-verified 2026-11-22: PUMP-quoted curves exist and
were mispriced ~27,778x by the SOL-assuming parser).

The schema gate (owner + discriminator + >=115-byte documented
layout) is primary; the USD plausibility band is a secondary garbage
filter and is NOT trusted to catch a wrong-layout decode.

No network: get_account / mint_decimals / sol_usd_price /
quote_usd_price are stubbed. The captured-live PUMP-quoted fixture
(tests/fixtures/pump_curve_pump_quoted.json) is exercised with its
real account bytes.

Run: python3 tests/test_reserves_curve_price.py
"""
import base64
import json
import os
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("FOREX_AGENT_HOME",
                      tempfile.mkdtemp(prefix="curve-price-test-"))

from daemon import solana_ids
from daemon.launch_scanner import reserves as R

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


SOL_TS = 1_000_000.0          # when the quote/USD cache observed
PUMP_MINT = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
def _wsol_bytes():
    return _b58decode(solana_ids.WSOL_MINT)


def _b58decode(s):
    alpha = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for ch in s:
        n = n * 58 + alpha.index(ch)
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + n.to_bytes((n.bit_length() + 7) // 8, "big")


def _curve_blob(v_token, v_quote, quote_mint_b=None, complete=0,
                supply=1_000_000_000_000_000, length=166):
    if quote_mint_b is None:
        quote_mint_b = _wsol_bytes()
    b = bytearray(length)
    b[:8] = bytes.fromhex("17b7f83760d8ac60")
    struct.pack_into("<Q", b, 8, v_token)
    struct.pack_into("<Q", b, 16, v_quote)
    struct.pack_into("<Q", b, 24, v_token // 2)   # real token (unused)
    struct.pack_into("<Q", b, 32, v_quote // 2)   # real quote (unused)
    struct.pack_into("<Q", b, 40, supply)
    b[48] = complete
    b[83:115] = quote_mint_b
    return bytes(b)


def _reset_quote_caches():
    R._QUOTE_USD_CACHE.clear()
    R._QUOTE_DEC_CACHE.clear()


# ---- 1. WSOL-quoted current-layout curve: SOL path -----------------
# 70 SOL virtual over 700M tokens(dec 6) => 1e-7 SOL => 7e-6 USD
R.derive_curve_pda = lambda mint: "CurvePDA111"   # no hashing in tests
_reset_quote_caches()
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    _curve_blob(700_000_000_000_000, 70_000_000_000))
R.mint_decimals = lambda mint, exempt=False: 6
R.sol_usd_price = lambda now=None: 70.0
R._SOL_CACHE.update(px=70.0, ts=SOL_TS)
px, obs_ts, detail = R.curve_price_observed("MINTok")
check("WSOL-quoted curve priced (1e-7 SOL x 70 USD = 7e-6)",
      abs(px - 7e-6) / 7e-6 < 1e-6)
check("WSOL: obs_ts = stale SOL cache ts, not wrap time",
      obs_ts == SOL_TS)
check("WSOL: detail names the quote mint", "quote=So11111" in detail)

# ---- 2. PUMP-quoted LIVE fixture: the 27,778x defect's subject ----
fx = json.load(open(os.path.join(
    os.path.dirname(__file__), "fixtures",
    "pump_curve_pump_quoted.json")))
blob = base64.b64decode(fx["curve_pregrad"]["b64"])
R.get_account = lambda pubkey, exempt=False: (
    fx["curve_pregrad"]["owner"], blob)
R.mint_decimals = lambda mint, exempt=False: 6   # token AND PUMP dec 6
R.sol_usd_price = lambda now=None: 70.0
R._SOL_CACHE.update(px=70.0, ts=SOL_TS)
R.quote_usd_price = lambda mint, now=None: (0.00253, SOL_TS)
R.quote_decimals = lambda mint, exempt=False: 6
px, obs_ts, detail = R.curve_price_observed(
    fx["curve_pregrad"]["mint"])
# The assertion is FORMULA consistency on the fixture's own parsed
# reserves (quote denomination + decimals), not a stale cross-check:
# DexScreener showed 1.237e-5 for this pair at capture, but reserves
# moved between reads (parsed now: v_quote=2.5524e12, v_token=5.994e14)
# -> 4.258e-3 PUMP x $0.00253 = 1.077e-5 USD.
_c = R.parse_bonding_curve(blob)
_expected = ((_c["v_sol"] / 1e6) / (_c["v_token"] / 1e6)) * 0.00253
check("PUMP-quoted live fixture priced in PUMP terms x quote USD "
      "(was 27.6x off as SOL)",
      _expected > 0 and abs(px - _expected) / _expected < 1e-9)
check("PUMP fixture lands in the plausible USD range (~1.08e-5)",
      0.9e-5 < px < 1.2e-5)
check("PUMP: obs_ts = quote cache ts", obs_ts == SOL_TS)
check("PUMP: detail names the quote mint", "quote=pumpCmXq" in detail)

# ---- 3. legacy short account (81 bytes): refused -------------------
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    _curve_blob(700_000_000_000_000, 70_000_000_000, length=81))
px, _ts, detail = R.curve_price_observed("MINTok")
check("81-byte account refused (no supported documented layout)",
      px == 0.0 and "unsupported" in detail)

# ---- 4. 49-byte account: refused -----------------------------------
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    _curve_blob(700_000_000_000_000, 70_000_000_000, length=49))
px, _ts, detail = R.curve_price_observed("MINTok")
check("49-byte account refused", px == 0.0 and "unsupported" in detail)

# ---- 5. wrong owner: refused before parsing -------------------------
R.get_account = lambda pubkey, exempt=False: (
    "SomeOtherProgram1111111111111111111111111111",
    _curve_blob(700_000_000_000_000, 70_000_000_000))
px, _ts, detail = R.curve_price_observed("MINTok")
check("non-pump-program owner refused before pricing",
      px == 0.0 and "schema unverified" in detail)

# ---- 6. unknown quote asset: unpriceable -> explicit refusal -------
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    _curve_blob(700_000_000_000_000, 70_000_000_000,
                quote_mint_b=bytes(range(32))))
R.quote_usd_price = lambda mint, now=None: (0.0, SOL_TS)
px, _ts, detail = R.curve_price_observed("MINTok")
check("unpriceable quote asset refused explicitly",
      px == 0.0 and "unpriceable" in detail)

# ---- 7. quote decimals unknown: refuse, never assume ----------------
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    _curve_blob(700_000_000_000_000, 70_000_000_000,
                quote_mint_b=_b58decode(PUMP_MINT)))
R.quote_usd_price = lambda mint, now=None: (0.00253, SOL_TS)
R.quote_decimals = lambda mint, exempt=False: None
px, _ts, detail = R.curve_price_observed("MINTok")
check("unknown quote decimals refused (never assumed)",
      px == 0.0 and "decimals unknown" in detail)

# ---- 8. quote decimals are NOT hardcoded 6 --------------------------
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    # 9-dec quote: 70e9 raw units = 70 tokens at dec 9
    _curve_blob(700_000_000_000_000, 70_000_000_000,
                quote_mint_b=_b58decode(PUMP_MINT)))
R.quote_usd_price = lambda mint, now=None: (70.0, SOL_TS)
R.quote_decimals = lambda mint, exempt=False: 9
px, _ts, detail = R.curve_price_observed("MINTok")
check("9-decimal quote priced like SOL (7e-6 USD)",
      abs(px - 7e-6) / 7e-6 < 1e-6)

# ---- 9. complete curve: graduated refusal ----------------------------
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    _curve_blob(700_000_000_000_000, 70_000_000_000, complete=1))
R.quote_decimals = lambda mint, exempt=False: 9
px, _ts, detail = R.curve_price_observed("MINTok")
check("complete curve refused as graduated",
      px == 0.0 and "graduated" in detail)

# ---- 10. garbage decode: USD band catches it -------------------------
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    # 1e10 quote units over 1e6 tokens x $70 -> 7e5 USD
    _curve_blob(1_000_000_000_000, 10_000_000_000_000_000_000))
px, _ts, detail = R.curve_price_observed("MINTok")
check("garbage reserves refused by the USD band",
      px == 0.0 and "implausible" in detail)

# ---- 11. curve account absent (post-graduation closure) -------------
R.get_account = lambda pubkey, exempt=False: (None, None)
px, _ts, detail = R.curve_price_observed("MINTok")
check("missing curve account fails closed", px == 0.0)

# ---- 12. obs_ts reflects the quote observation, never wrap time ----
R.get_account = lambda pubkey, exempt=False: (
    solana_ids.PUMP_PROGRAM_ID,
    _curve_blob(700_000_000_000_000, 70_000_000_000))
old_quote_ts = time.time() - 5000   # stale quote observation
R.quote_usd_price = lambda mint, now=None: (70.0, old_quote_ts)
R.quote_decimals = lambda mint, exempt=False: 9
px, obs_ts, _detail = R.curve_price_observed("MINTok")
check("stale quote observation surfaces in obs_ts, not wrap time",
      abs(obs_ts - old_quote_ts) < 1.0)

print()
if FAILS:
    print("FAILURES: %d" % len(FAILS))
    sys.exit(1)
print("all curve-price tests passed")
