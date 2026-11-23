#!/usr/bin/env python3
"""Hermetic tests for reserves.curve_price_observed/_usd: pricing
math, observation-time provenance, and the plausibility band that
fails closed when the account layout does not describe a standard
pump.fun curve (live-verified 2026-11-22: len-166 accounts read
27x-inflated "reserves" under the documented offsets).

No network: get_account / mint_decimals / sol_usd_price are stubbed.
Run: python3 tests/test_reserves_curve_price.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("FOREX_AGENT_HOME",
                      tempfile.mkdtemp(prefix="curve-price-test-"))

from daemon.launch_scanner import reserves as R

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


SOL_TS = 1_000_000.0          # when the SOL/USD cache observed


def _curve_blob(v_token, v_sol, complete=0, supply=1_000_000_000_000_000):
    b = bytearray(81)
    b[:8] = bytes.fromhex("17b7f83760d8ac60")
    struct.pack_into("<Q", b, 8, v_token)
    struct.pack_into("<Q", b, 16, v_sol)
    struct.pack_into("<Q", b, 24, v_token // 2)   # real token (unused)
    struct.pack_into("<Q", b, 32, v_sol // 2)     # real sol (unused)
    struct.pack_into("<Q", b, 40, supply)
    b[48] = complete
    return bytes(b)


def _stub(v_token, v_sol, sol_usd=70.0, complete=0):
    R.get_account = lambda pubkey, exempt=False: (
        "pump", _curve_blob(v_token, v_sol, complete=complete))
    R.mint_decimals = lambda mint, exempt=False: 6
    R.sol_usd_price = lambda now=None: sol_usd
    R._SOL_CACHE.update(px=sol_usd, ts=SOL_TS)
    R.derive_curve_pda = lambda mint: "CurvePDA111"


# Standard mid-curve state: 30 + 40 SOL virtual over ~700M tokens
# => 70 / 700e6 = 1e-7 SOL, inside the band.
_stub(v_token=700_000_000_000_000, v_sol=70_000_000_000)
px, obs_ts, detail = R.curve_price_observed("MINTok")
check("standard curve: priced (1e-7 SOL x 70 USD = 7e-6)",
      abs(px - 7e-6) / 7e-6 < 1e-9)
check("observation ts = min(read, SOL cache ts)",
      obs_ts == SOL_TS)
check("detail names the curve", "bonding curve" in detail)
px2, detail2 = R.curve_price_usd("MINTok")
check("curve_price_usd wrapper keeps (price, detail) shape",
      abs(px2 - px) < 1e-18 and "bonding curve" in detail2)

# Graduated curve: complete -> no price.
_stub(v_token=280_000_000_000_000, v_sol=115_000_000_000, complete=1)
px, _ts, detail = R.curve_price_observed("MINTdone")
check("completed curve: no price", px == 0.0 and "complete" in detail)

# The len-166 failure mode: fields that parse "cleanly" but imply a
# spot price 27x above any real curve state (live read 2026-11-22,
# token HZ3v...pump: 4.9e-6 SOL vs DexScreener mcap evidence ~1.7e-7).
_stub(v_token=561_347_647_226_103, v_sol=2_725_603_221_766)
px, _ts, detail = R.curve_price_observed("MINTinflated")
check("layout-inflated price refused (fail-closed, no mark)",
      px == 0.0 and "implausible curve price" in detail)

# Band edges: creation ratio passes, above-graduation refuses.
_stub(v_token=1_073_000_000_000_000, v_sol=30_000_000_000)  # creation
px, _ts, _d = R.curve_price_observed("MINTnew")
check("creation-ratio price (2.8e-8 SOL) inside the band", px > 0)
_stub(v_token=100_000_000_000, v_sol=1_000_000_000)  # 1e-5 SOL: 25x grad
px, _ts, detail = R.curve_price_observed("MINThigh")
check("above-band price refused", px == 0.0
      and "implausible curve price" in detail)

if FAILS:
    print("\nFAILURES: %s" % FAILS)
    sys.exit(1)
print("\nALL CURVE-PRICE TESTS PASSED")
