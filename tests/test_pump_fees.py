#!/usr/bin/env python3
"""pump_fees account parser tests (2026-11-22).

Fixtures are SYNTHETIC, built by a local encoder from the published IDL
layouts (no live capture exists yet: the on-chain read route is still
under review). They prove the decoder matches the documented borsh
layout and that every malformed input is refused, never coerced.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import struct
import sys

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import pump_fees as PF

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def pk(n):
    """Deterministic 32-byte stand-in pubkey."""
    return bytes([n] * 32)


def enc_fees(lp, protocol, creator):
    return struct.pack("<QQQ", lp, protocol, creator)


def enc_tier(threshold, lp, protocol, creator):
    return threshold.to_bytes(16, "little") + enc_fees(lp, protocol,
                                                       creator)


def enc_fee_config(tiers, stable_tiers, flat=(25, 5, 0),
                   exotic=(30, 10, 10), admin_n=7):
    b = PF.FEE_CONFIG_DISC
    b += b"\xfa"                              # bump
    b += pk(admin_n)                          # admin
    b += enc_fees(*flat)                      # flat_fees
    b += struct.pack("<I", len(tiers))        # fee_tiers vec
    for t in tiers:
        b += enc_tier(*t)
    b += struct.pack("<I", len(stable_tiers))  # stable_fee_tiers
    for t in stable_tiers:
        b += enc_tier(*t)
    b += enc_fees(*exotic)                    # exotic_flat_fees
    return b


def enc_global_config():
    b = PF.GLOBAL_CONFIG_DISC
    b += pk(1)                                # admin
    b += struct.pack("<Q", 25)                # lp_fee_basis_points
    b += struct.pack("<Q", 5)                 # protocol_fee_basis_points
    b += b"\x00"                              # disable_flags
    b += b"".join(pk(10 + i) for i in range(8))   # protocol recipients
    b += struct.pack("<Q", 100)               # coin_creator_fee_bps
    b += pk(20)                               # admin_set_coin_creator_auth
    b += pk(21)                               # whitelist_pda
    b += pk(22)                               # reserved_fee_recipient
    b += b"\x01"                              # mayhem_mode_enabled
    b += b"".join(pk(30 + i) for i in range(7))   # reserved recipients
    b += b"\x00"                              # is_cashback_enabled
    b += b"".join(pk(40 + i) for i in range(8))   # buyback recipients
    b += struct.pack("<Q", 50)                # buyback_basis_points
    b += pk(50)                               # boost_authority
    b += b"\x01"                              # boost_enabled
    b += b"\x01"                              # creator_fee_configurable
    b += struct.pack("<Q", 500)               # max_configurable_creator
    return b


# ---------- FeeConfig happy path ----------
data = enc_fee_config(
    tiers=[(0, 95, 30, 0), (50_000_000_000_000, 80, 25, 5)],
    stable_tiers=[(0, 10, 2, 0)])
cfg = PF.parse_fee_config(data)
check("fee_config: bump + admin",
      cfg["bump"] == 0xfa and cfg["admin"] == PF._b58encode(pk(7)))
check("fee_config: flat fees",
      cfg["flat_fees"] == {"lp_fee_bps": 25, "protocol_fee_bps": 5,
                           "creator_fee_bps": 0})
check("fee_config: two tiers parsed in order",
      len(cfg["fee_tiers"]) == 2
      and cfg["fee_tiers"][0]["market_cap_lamports_threshold"] == 0
      and cfg["fee_tiers"][1]["market_cap_lamports_threshold"]
      == 50_000_000_000_000
      and cfg["fee_tiers"][1]["fees"]["creator_fee_bps"] == 5)
check("fee_config: stable tiers parsed",
      len(cfg["stable_fee_tiers"]) == 1
      and cfg["stable_fee_tiers"][0]["fees"]["lp_fee_bps"] == 10)
check("fee_config: exotic flat fees",
      cfg["exotic_flat_fees"]["creator_fee_bps"] == 10)
check("fee_config: consumed bytes match encoder length",
      cfg["consumed_bytes"] == len(data))
check("fee_config: trailing extension bytes tolerated",
      PF.parse_fee_config(data + b"\x00" * 64)["consumed_bytes"]
      == len(data))

# ---------- GlobalConfig happy path ----------
gc = PF.parse_global_config(enc_global_config())
check("global_config: scalar fees",
      gc["lp_fee_basis_points"] == 25
      and gc["protocol_fee_basis_points"] == 5
      and gc["coin_creator_fee_basis_points"] == 100
      and gc["buyback_basis_points"] == 50
      and gc["max_configurable_creator_fee_bps"] == 500)
check("global_config: flags",
      gc["mayhem_mode_enabled"] is True
      and gc["is_cashback_enabled"] is False
      and gc["boost_enabled"] is True
      and gc["creator_fee_configurable"] is True)
check("global_config: recipient arrays",
      len(gc["protocol_fee_recipients"]) == 8
      and gc["protocol_fee_recipients"][0] == PF._b58encode(pk(10))
      and len(gc["reserved_fee_recipients"]) == 7
      and len(gc["buyback_fee_recipients"]) == 8)
check("global_config: consumed bytes match encoder length",
      gc["consumed_bytes"] == gc["account_bytes"])


# ---------- refusals ----------
def refuses(name, fn, data):
    try:
        fn(data)
        check(name, False)
    except PF.PumpFeesError:
        check(name, True)
    except Exception:
        check(name + " (PumpFeesError, not %s)"
              % type(sys.exc_info()[1]).__name__, False)


refuses("refuse: wrong FeeConfig discriminator",
        PF.parse_fee_config, b"\x00" * 8 + data[8:])
refuses("refuse: short account",
        PF.parse_fee_config, data[:40])
refuses("refuse: absurd tier count",
        PF.parse_fee_config,
        PF.FEE_CONFIG_DISC + b"\xfa" + pk(7) + enc_fees(1, 1, 1)
        + struct.pack("<I", 1000))
refuses("refuse: fee above 100%",
        PF.parse_fee_config,
        enc_fee_config([], [], flat=(10_001, 0, 0)))
refuses("refuse: wrong GlobalConfig discriminator",
        PF.parse_global_config, data)
refuses("refuse: GlobalConfig truncation",
        PF.parse_global_config, enc_global_config()[:400])
_refuse_buf = bytearray(enc_global_config())
_refuse_buf[8 + 32 + 8 + 8 + 1 + 256 + 8 + 32 + 32 + 32] = 7  # mayhem bool
refuses("refuse: non-bool flag byte",
        PF.parse_global_config, bytes(_refuse_buf))

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL PUMP-FEES PARSER TESTS PASSED")
