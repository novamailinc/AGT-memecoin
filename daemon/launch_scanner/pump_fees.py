#!/usr/bin/env python3
"""pump_fees / PumpSwap fee-config account parsers (2026-11-22).

OFFLINE parser preparation for the fee-model repair: decode the two
on-chain accounts the dynamic fee schedule lives in, from raw account
bytes. No network here — callers supply bytes captured through an
approved route.

Layouts verified against the published IDLs:
- pump_fees program (pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ),
  FeeConfig account — PDA seeds ["fee_config", config_program_id], one
  per owning program (pump and pump_amm):
    https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump_fees.json
- PumpSwap (pump_amm) GlobalConfig account:
    https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump_amm.json

FeeConfig (Anchor/borsh, after the 8-byte discriminator):
    bump u8, admin Pubkey,
    flat_fees Fees, fee_tiers Vec<FeeTier>,
    stable_fee_tiers Vec<FeeTier>, exotic_flat_fees Fees
FeeTier = { market_cap_lamports_threshold u128, fees Fees }
Fees    = { lp_fee_bps u64, protocol_fee_bps u64, creator_fee_bps u64 }

GlobalConfig (after the 8-byte discriminator):
    admin, lp_fee_basis_points u64, protocol_fee_basis_points u64,
    disable_flags u8, protocol_fee_recipients [Pubkey; 8],
    coin_creator_fee_basis_points u64, admin_set_coin_creator_authority,
    whitelist_pda, reserved_fee_recipient, mayhem_mode_enabled bool,
    reserved_fee_recipients [Pubkey; 7], is_cashback_enabled bool,
    buyback_fee_recipients [Pubkey; 8], buyback_basis_points u64,
    boost_authority, boost_enabled bool, creator_fee_configurable bool,
    max_configurable_creator_fee_bps u64

Parsed values are AS-OF the capture timestamp only: both accounts are
admin-mutable. Parsing refuses (PumpFeesError) on a wrong
discriminator, truncation, an impossible vec length, or a fee above
100% — never coerced, never guessed.
"""

import struct

FEE_CONFIG_DISC = bytes([143, 52, 146, 187, 219, 123, 76, 155])
GLOBAL_CONFIG_DISC = bytes([149, 8, 156, 202, 160, 252, 176, 217])

# Published PDAs/program ids (documentary constants, not secrets).
PUMP_FEES_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
FEE_CONFIG_SEED = b"fee_config"

_BPS_MAX = 10_000          # 100%; anything above is nonsense
_MAX_TIERS = 64            # sanity bound for vec length prefixes

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class PumpFeesError(Exception):
    """Refusal: the bytes are not a parseable, sane fee account."""


def _b58encode(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + (s or "")


class _Cursor:
    def __init__(self, data, what):
        self.d = data
        self.o = 0
        self.what = what

    def take(self, n, field):
        if self.o + n > len(self.d):
            raise PumpFeesError(
                "%s: truncated at %s (need %d bytes at offset %d,"
                " have %d)" % (self.what, field, n, self.o,
                               len(self.d)))
        b = self.d[self.o:self.o + n]
        self.o += n
        return b

    def u8(self, field):
        return self.take(1, field)[0]

    def bool_(self, field):
        v = self.u8(field)
        if v not in (0, 1):
            raise PumpFeesError("%s: non-bool %s=%d" %
                                (self.what, field, v))
        return bool(v)

    def u64(self, field):
        return struct.unpack("<Q", self.take(8, field))[0]

    def u128(self, field):
        return int.from_bytes(self.take(16, field), "little")

    def u32(self, field):
        return struct.unpack("<I", self.take(4, field))[0]

    def pubkey(self, field):
        return _b58encode(self.take(32, field))


def _parse_fees(c, field):
    fees = {
        "lp_fee_bps": c.u64(field + ".lp_fee_bps"),
        "protocol_fee_bps": c.u64(field + ".protocol_fee_bps"),
        "creator_fee_bps": c.u64(field + ".creator_fee_bps"),
    }
    for k, v in fees.items():
        if v > _BPS_MAX:
            raise PumpFeesError("%s: %s=%d bps exceeds 100%%"
                                % (c.what, k, v))
    return fees


def _parse_tiers(c, field):
    n = c.u32(field + ".len")
    if n > _MAX_TIERS:
        raise PumpFeesError("%s: %s length %d implausible"
                            % (c.what, field, n))
    tiers = []
    for i in range(n):
        tiers.append({
            "market_cap_lamports_threshold":
                c.u128("%s[%d].threshold" % (field, i)),
            "fees": _parse_fees(c, "%s[%d].fees" % (field, i)),
        })
    return tiers


def parse_fee_config(data):
    """Decode a pump_fees FeeConfig account. Raises PumpFeesError."""
    if not isinstance(data, (bytes, bytearray)):
        raise PumpFeesError("FeeConfig: bytes required")
    if len(data) < 8 or bytes(data[:8]) != FEE_CONFIG_DISC:
        raise PumpFeesError("FeeConfig: wrong discriminator")
    c = _Cursor(bytes(data), "FeeConfig")
    c.take(8, "discriminator")
    return {
        "bump": c.u8("bump"),
        "admin": c.pubkey("admin"),
        "flat_fees": _parse_fees(c, "flat_fees"),
        "fee_tiers": _parse_tiers(c, "fee_tiers"),
        "stable_fee_tiers": _parse_tiers(c, "stable_fee_tiers"),
        "exotic_flat_fees": _parse_fees(c, "exotic_flat_fees"),
        "consumed_bytes": c.o,
        "account_bytes": len(data),
    }


def parse_global_config(data):
    """Decode a PumpSwap GlobalConfig account. Raises PumpFeesError."""
    if not isinstance(data, (bytes, bytearray)):
        raise PumpFeesError("GlobalConfig: bytes required")
    if len(data) < 8 or bytes(data[:8]) != GLOBAL_CONFIG_DISC:
        raise PumpFeesError("GlobalConfig: wrong discriminator")
    c = _Cursor(bytes(data), "GlobalConfig")
    c.take(8, "discriminator")
    return {
        "admin": c.pubkey("admin"),
        "lp_fee_basis_points": c.u64("lp_fee_basis_points"),
        "protocol_fee_basis_points": c.u64("protocol_fee_basis_points"),
        "disable_flags": c.u8("disable_flags"),
        "protocol_fee_recipients":
            [c.pubkey("protocol_fee_recipients[%d]" % i)
             for i in range(8)],
        "coin_creator_fee_basis_points":
            c.u64("coin_creator_fee_basis_points"),
        "admin_set_coin_creator_authority":
            c.pubkey("admin_set_coin_creator_authority"),
        "whitelist_pda": c.pubkey("whitelist_pda"),
        "reserved_fee_recipient": c.pubkey("reserved_fee_recipient"),
        "mayhem_mode_enabled": c.bool_("mayhem_mode_enabled"),
        "reserved_fee_recipients":
            [c.pubkey("reserved_fee_recipients[%d]" % i)
             for i in range(7)],
        "is_cashback_enabled": c.bool_("is_cashback_enabled"),
        "buyback_fee_recipients":
            [c.pubkey("buyback_fee_recipients[%d]" % i)
             for i in range(8)],
        "buyback_basis_points": c.u64("buyback_basis_points"),
        "boost_authority": c.pubkey("boost_authority"),
        "boost_enabled": c.bool_("boost_enabled"),
        "creator_fee_configurable": c.bool_("creator_fee_configurable"),
        "max_configurable_creator_fee_bps":
            c.u64("max_configurable_creator_fee_bps"),
        "consumed_bytes": c.o,
        "account_bytes": len(data),
    }
