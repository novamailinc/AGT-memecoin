#!/usr/bin/env python3
"""Dynamic pump.fun / PumpSwap fee schedule from captured on-chain accounts.

The fee-model repair: chain_costs.py previously hard-coded pump fees from
a 2026-10-23 reading of pump.fun's Fees Page. The venue's own docs (the
FEE_PROGRAM_README in pump-fun/pump-public-docs) state the fee schedule
is DYNAMIC, read at trade time from on-chain accounts the fee-program
admin can change at any time:

- pump_fees program (pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ)
  FeeConfig PDA per OWNING PROGRAM (seeds ["fee_config",
  config_program_id]): one for the pump bonding-curve program, one for
  the PumpSwap AMM program. Holds flat_fees, fee_tiers (SOL-like quote),
  stable_fee_tiers (stable quote), exotic_flat_fees.
- PumpSwap GlobalConfig PDA: fallback bps used when the AMM's FeeConfig
  account is absent, plus creator_fee_configurable /
  max_configurable_creator_fee_bps.

This module decodes a captured set of those accounts (see
tests/fixtures/fee_program/capture_meta.json for capture provenance) and
reproduces the published fee-selection logic exactly, mirroring
pump-fees::get_fees / pump-fees-math::calculate_fee_tier as implemented
in the official SDKs (@pump-fun/pump-sdk 4.3.1 swap/index.js,
@pump-fun/pump-swap-sdk 2.2.0 sdk/fees.js):

- Tier selection (calculateFeeTier): fees below the first tier's
  threshold are the FIRST tier's fees; otherwise the HIGHEST tier whose
  threshold <= market cap. Tiers are consulted in quote lamports.
- Quote categories: SOL-like quote mints (system default / native mint,
  incl. token-2022 native) use fee_tiers; stable quotes (USDC) use
  stable_fee_tiers, falling back to fee_tiers when the stable list is
  empty; anything else ("exotic") uses exotic_flat_fees, falling back to
  flat_fees when the exotic fees are all zero.
- Canonical pump pools (pool.creator == pump pool-authority PDA) use the
  tiered schedule; every other PumpSwap pool pays flat_fees.
- Bonding curves always use the pump program's FeeConfig tiers (there is
  no non-canonical curve); when a FeeConfig is absent the curve falls
  back to the pump program Global account's fee_basis_points /
  creator_fee_basis_points and PumpSwap falls back to its GlobalConfig's
  lp/protocol/coin_creator bps.
- Creator overrides: when the relevant config has creator fee
  configurability enabled and the coin carries a nonzero creator fee
  bps, that value REPLACES the tier's creator_fee_bps. When the coin's
  creator is the default pubkey the creator fee is not charged (except
  on a brand-new bonding curve).
- Market cap in quote lamports: bonding curve =
  virtual_quote_reserves * mint_supply / virtual_token_reserves
  (mayhem-mode curves use the real mint supply; normal curves use the
  1e9-token supply); PumpSwap pool =
  effective_quote_reserve * base_mint_supply / base_reserve, where
  effective_quote_reserve = vault quote reserve + signed
  virtual_quote_reserves (mayhem pools use PUMP_AMM total supply).

Amounts: fee bps never depends on trade size; every component is linear
in the quote amount, so a fee in quote units is
quote_amount * total_bps / 10_000. (The on-chain get_fees CPI takes a
trade_size_lamports argument, but the published fee-selection code does
not use it for bps selection; it is not modeled here.)

Everything here is AS-OF the capture timestamp recorded in the fixture
metadata: the fee admin can update these accounts at any time, so a
schedule derived from a capture is evidence about that capture, not a
perpetual truth. Callers that need current values must re-capture
through an approved read route. Parsing and selection REFUSE
(PumpFeesError) on missing accounts, empty tier lists, zero-reserve
market caps, or malformed metadata - they never guess a fee.
"""

import json
import os
from datetime import datetime, timezone

from daemon.launch_scanner import pump_fees as _pf

PumpFeesError = _pf.PumpFeesError

# ---------------------------------------------------------------------
# Documentary constants (program ids / mints from the published IDLs
# and SDKs - not secrets).

PUMP_FEES_PROGRAM = _pf.PUMP_FEES_PROGRAM
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

SOL_LIKE_QUOTE_MINTS = frozenset({
    "11111111111111111111111111111111",              # default pubkey
    "So11111111111111111111111111111111111111112",  # native mint (both)
})

STABLE_QUOTE_MINTS = frozenset({
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
})

QUOTE_SOL = "sol"
QUOTE_STABLE = "stable"
QUOTE_EXOTIC = "exotic"

BPS_DENOMINATOR = 10_000

# Bonding-curve market cap uses the fixed 1e9-token supply (in base
# units) for normal curves; mayhem-mode curves use their real supply.
NORMAL_CURVE_SUPPLY_BASE_UNITS = 1_000_000_000_000_000  # 1e9 * 1e6


class FeeScheduleError(PumpFeesError):
    """Refusal: the schedule inputs cannot determine an honest fee."""


# ---------------------------------------------------------------------
# Quote-category and tier selection (exact SDK mirrors).

def quote_category(quote_mint):
    """"sol" | "stable" | "exotic" for a quote mint address.

    None / empty defaults to "sol" (the SDKs' default quote mint is the
    native mint; pump bonding curves are SOL-quoted unless explicitly
    otherwise). Raises FeeScheduleError on a non-string."""
    if quote_mint is None or quote_mint == "":
        return QUOTE_SOL
    if not isinstance(quote_mint, str):
        raise FeeScheduleError("quote mint must be a string address")
    if quote_mint in SOL_LIKE_QUOTE_MINTS:
        return QUOTE_SOL
    if quote_mint in STABLE_QUOTE_MINTS:
        return QUOTE_STABLE
    return QUOTE_EXOTIC


def _fees_total(fees):
    return int(fees["lp_fee_bps"]) + int(fees["protocol_fee_bps"]) + \
        int(fees["creator_fee_bps"])


def _is_zero_fees(fees):
    return _fees_total(fees) == 0


def calculate_fee_tier(tiers, market_cap_lamports):
    """Mirror of pump-fees-math::calculate_fee_tier().

    tiers: parsed FeeConfig fee_tiers / stable_fee_tiers list.
    market_cap_lamports: int >= 0 in quote lamports; None means the
    market cap is unknown and the HIGHEST-FEE tier is returned (the
    honest conservative default for a cost model - never an
    under-charge). Empty tier lists refuse."""
    if not tiers:
        raise FeeScheduleError("fee tier list is empty")
    if market_cap_lamports is None:
        return dict(max(tiers, key=lambda t: _fees_total(t["fees"]))
                    ["fees"])
    mcap = int(market_cap_lamports)
    if mcap < 0:
        raise FeeScheduleError("negative market cap")
    first = tiers[0]
    if mcap < int(first["market_cap_lamports_threshold"]):
        return dict(first["fees"])
    for tier in reversed(tiers):
        if mcap >= int(tier["market_cap_lamports_threshold"]):
            return dict(tier["fees"])
    return dict(first["fees"])


def market_cap_lamports_curve(virtual_quote_reserves, mint_supply,
                              virtual_token_reserves):
    """Bonding-curve market cap in quote lamports (floor division).
    Refuses on zero token reserves (division by zero on-chain)."""
    vtr = int(virtual_token_reserves)
    if vtr <= 0:
        raise FeeScheduleError(
            "bonding-curve virtual token reserves cannot be zero")
    return (int(virtual_quote_reserves) * int(mint_supply)) // vtr


def market_cap_lamports_pool(quote_reserve, base_mint_supply,
                             base_reserve, virtual_quote_reserves=0,
                             is_mayhem_mode=False,
                             mayhem_supply=None):
    """PumpSwap pool market cap in quote lamports: the EFFECTIVE quote
    reserve (vault reserve plus the pool's signed virtual_quote_reserves)
    times circulating supply over base reserve. Mayhem pools price off
    the fixed pump_amm total supply; pass it as mayhem_supply.
    Refuses on zero base reserve or missing mayhem supply."""
    br = int(base_reserve)
    if br <= 0:
        raise FeeScheduleError("pool base reserve cannot be zero")
    if is_mayhem_mode:
        if mayhem_supply is None:
            raise FeeScheduleError(
                "mayhem pool market cap needs the pump_amm total supply")
        supply = int(mayhem_supply)
    else:
        supply = int(base_mint_supply)
    effective_quote = int(quote_reserve) + int(virtual_quote_reserves)
    return (effective_quote * supply) // br


def _apply_creator_override(fees, creator_fee_configurable,
                            creator_fee_bps, creator_unset,
                            charge_when_unset=False):
    """Apply the published creator-fee rules to a tier's fees.

    - A nonzero per-coin creator_fee_bps replaces the tier's creator
      component when the owning config allows creator fee
      configurability.
    - An unset creator (default pubkey) zeroes the creator component,
      unless charge_when_unset (a brand-new bonding curve still charges
      its creator fee)."""
    out = {
        "lp_fee_bps": int(fees["lp_fee_bps"]),
        "protocol_fee_bps": int(fees["protocol_fee_bps"]),
        "creator_fee_bps": int(fees["creator_fee_bps"]),
    }
    if creator_fee_configurable and creator_fee_bps:
        out["creator_fee_bps"] = int(creator_fee_bps)
    if creator_unset and not charge_when_unset:
        out["creator_fee_bps"] = 0
    return out


def fee_config_fees(cfg, *, quote_category_, market_cap_lamports=None,
                    is_pump_pool=True):
    """Select fees from a parsed FeeConfig.

    is_pump_pool=False means a NON-canonical PumpSwap pool: flat_fees
    regardless of quote or market cap (pump-fees::get_fees checks
    is_pump_pool first). Bonding curves are always their program's own,
    so pass the default True for them."""
    if cfg is None:
        raise FeeScheduleError("FeeConfig account is required")
    if not is_pump_pool:
        return dict(cfg["flat_fees"])
    if quote_category_ == QUOTE_SOL:
        return calculate_fee_tier(cfg["fee_tiers"], market_cap_lamports)
    if quote_category_ == QUOTE_STABLE:
        tiers = cfg["stable_fee_tiers"] or cfg["fee_tiers"]
        return calculate_fee_tier(tiers, market_cap_lamports)
    # exotic quote
    exotic = cfg["exotic_flat_fees"]
    if _is_zero_fees(exotic):
        return dict(cfg["flat_fees"])
    return dict(exotic)


def bonding_curve_fees(cfg, *, quote_mint=None, market_cap_lamports=None,
                       global_fee_bps=None, global_creator_bps=None,
                       creator_fee_configurable=False,
                       creator_fee_bps=None, creator_unset=False,
                       is_new_curve=False):
    """Total fee components for one pump bonding-curve trade.

    cfg: parsed pump-program FeeConfig or None. When cfg is None the
    pump program Global account's fee_basis_points /
    creator_fee_basis_points are the fallback and BOTH must be supplied
    (never guessed)."""
    cat = quote_category(quote_mint)
    if cfg is None:
        if global_fee_bps is None or global_creator_bps is None:
            raise FeeScheduleError(
                "no pump FeeConfig and no Global fallback values "
                "supplied - refusing to guess fees")
        fees = {"lp_fee_bps": 0,
                "protocol_fee_bps": int(global_fee_bps),
                "creator_fee_bps": int(global_creator_bps)}
    else:
        fees = fee_config_fees(cfg, quote_category_=cat,
                               market_cap_lamports=market_cap_lamports,
                               is_pump_pool=True)
    return _apply_creator_override(
        fees, creator_fee_configurable, creator_fee_bps,
        creator_unset, charge_when_unset=is_new_curve)


def pumpswap_fees(cfg, gc, *, is_pump_pool, quote_mint=None,
                  market_cap_lamports=None,
                  creator_fee_bps=None, coin_creator_unset=False):
    """Total fee components for one PumpSwap trade.

    cfg: parsed pump_amm FeeConfig or None; gc: parsed PumpSwap
    GlobalConfig or None. When cfg is None the GlobalConfig's
    lp/protocol/coin_creator bps are the fallback, so gc is then
    required. Creator override applies when
    gc["creator_fee_configurable"] and a nonzero per-coin
    creator_fee_bps is supplied; a default-pubkey coin creator zeroes
    the creator component."""
    if cfg is None:
        if gc is None:
            raise FeeScheduleError(
                "no pump_amm FeeConfig and no GlobalConfig fallback - "
                "refusing to guess fees")
        fees = {"lp_fee_bps": int(gc["lp_fee_basis_points"]),
                "protocol_fee_bps": int(gc["protocol_fee_basis_points"]),
                "creator_fee_bps":
                    int(gc["coin_creator_fee_basis_points"])}
    else:
        fees = fee_config_fees(
            cfg, quote_category_=quote_category(quote_mint),
            market_cap_lamports=market_cap_lamports,
            is_pump_pool=is_pump_pool)
    configurable = bool(gc and gc.get("creator_fee_configurable"))
    return _apply_creator_override(
        fees, configurable, creator_fee_bps, coin_creator_unset)


def total_fee_bps(fees):
    """Total per-swap fee in bps (lp + protocol + creator)."""
    return _fees_total(fees)


def total_fee_pct(fees):
    return _fees_total(fees) / 100.0


# ---------------------------------------------------------------------
# Fixture capture loading and schedule artifacts.

_EXPECTED_OWNER = {
    "FC_PUMP": PUMP_FEES_PROGRAM,
    "FC_PAMM": PUMP_FEES_PROGRAM,
    "GC_PAMM": PUMP_AMM_PROGRAM,
}
_EXPECTED_DISC = {
    "FC_PUMP": _pf.FEE_CONFIG_DISC,
    "FC_PAMM": _pf.FEE_CONFIG_DISC,
    "GC_PAMM": _pf.GLOBAL_CONFIG_DISC,
}


def _load_capture_meta(fixture_dir):
    path = os.path.join(fixture_dir, "capture_meta.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError) as exc:
        raise FeeScheduleError("capture metadata unreadable: %s" % exc)
    if not isinstance(meta, dict) or not isinstance(
            meta.get("accounts"), dict):
        raise FeeScheduleError("capture metadata malformed")
    return meta


def load_fixture_set(fixture_dir):
    """Load and cross-validate a captured fee-account set.

    Validates per account: file present, byte length matches the
    recorded data_len, discriminator matches the recorded bytes, the
    owning program matches the expected program for that account kind,
    and the capture timestamp parses. Returns
    {"fee_config_pump": ..., "fee_config_pump_amm": ...,
     "global_config_pump_amm": ..., "meta": {...}} with the parsed
    accounts and their capture timestamps. Refuses on any mismatch -
    a capture that does not match its own metadata is not evidence."""
    meta = _load_capture_meta(fixture_dir)
    out = {"meta": {}}
    parsers = {
        "FC_PUMP": ("fee_config_pump", _pf.parse_fee_config),
        "FC_PAMM": ("fee_config_pump_amm", _pf.parse_fee_config),
        "GC_PAMM": ("global_config_pump_amm", _pf.parse_global_config),
    }
    for key, (field, parser) in parsers.items():
        info = meta["accounts"].get(key)
        if not isinstance(info, dict):
            raise FeeScheduleError("capture metadata missing %s" % key)
        if info.get("owner") != _EXPECTED_OWNER[key]:
            raise FeeScheduleError(
                "%s: unexpected owner %r" % (key, info.get("owner")))
        if list(info.get("discriminator") or []) != list(
                _EXPECTED_DISC[key]):
            raise FeeScheduleError(
                "%s: metadata discriminator mismatch" % key)
        path = os.path.join(fixture_dir, info.get("file") or "")
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise FeeScheduleError("%s: unreadable: %s" % (key, exc))
        if len(raw) != int(info.get("data_len") or -1):
            raise FeeScheduleError(
                "%s: data_len %r != actual %d"
                % (key, info.get("data_len"), len(raw)))
        parsed = parser(raw)  # raises PumpFeesError on bad bytes
        ts = info.get("captured_utc")
        try:
            datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise FeeScheduleError(
                "%s: bad captured_utc %r" % (key, ts))
        out[field] = parsed
        out["meta"][field] = {
            "address": info.get("address"),
            "owner": info.get("owner"),
            "captured_utc": ts,
        }
    return out


def build_schedule(bundle):
    """Build the JSON-able fee schedule from a validated fixture set."""
    fc_pump = bundle["fee_config_pump"]
    fc_pamm = bundle["fee_config_pump_amm"]
    gc_pamm = bundle["global_config_pump_amm"]
    return {
        "schema": 1,
        "source": {
            "derivation": "pump_fees/pump_amm IDL layouts; selection "
                          "logic per pump-fun/pump-public-docs "
                          "FEE_PROGRAM_README and @pump-fun SDKs",
            "accounts": bundle["meta"],
        },
        "bonding_curve": {
            "flat_fees": fc_pump["flat_fees"],
            "fee_tiers": fc_pump["fee_tiers"],
            "stable_fee_tiers": fc_pump["stable_fee_tiers"],
            "exotic_flat_fees": fc_pump["exotic_flat_fees"],
        },
        "pumpswap": {
            "flat_fees": fc_pamm["flat_fees"],
            "fee_tiers": fc_pamm["fee_tiers"],
            "stable_fee_tiers": fc_pamm["stable_fee_tiers"],
            "exotic_flat_fees": fc_pamm["exotic_flat_fees"],
        },
        "pumpswap_global_config": {
            "lp_fee_basis_points": gc_pamm["lp_fee_basis_points"],
            "protocol_fee_basis_points":
                gc_pamm["protocol_fee_basis_points"],
            "coin_creator_fee_basis_points":
                gc_pamm["coin_creator_fee_basis_points"],
            "creator_fee_configurable":
                gc_pamm["creator_fee_configurable"],
            "max_configurable_creator_fee_bps":
                gc_pamm["max_configurable_creator_fee_bps"],
            "is_cashback_enabled": gc_pamm["is_cashback_enabled"],
        },
    }


def schedule_from_dict(d):
    """Validate a schedule artifact's shape. Refuses on mismatch."""
    try:
        if int(d["schema"]) != 1:
            raise ValueError("unsupported schema")
        for sect in ("bonding_curve", "pumpswap"):
            s = d[sect]
            for k in ("flat_fees", "fee_tiers", "stable_fee_tiers",
                      "exotic_flat_fees"):
                s[k]
            if not s["fee_tiers"]:
                raise ValueError("empty fee_tiers")
            for t in s["fee_tiers"] + s["stable_fee_tiers"]:
                int(t["market_cap_lamports_threshold"])
                _fees_total(t["fees"])
            for f in (s["flat_fees"], s["exotic_flat_fees"]):
                _fees_total(f)
        g = d["pumpswap_global_config"]
        int(g["lp_fee_basis_points"])
        int(g["protocol_fee_basis_points"])
        int(g["coin_creator_fee_basis_points"])
        d["source"]["accounts"]
    except (KeyError, TypeError, ValueError) as exc:
        raise FeeScheduleError("schedule artifact malformed: %s" % exc)
    return d


def load_schedule_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise FeeScheduleError("schedule artifact unreadable: %s" % exc)
    return schedule_from_dict(data)


def fee_config_from_schedule(schedule, section):
    """Reconstruct a parse_fee_config-shaped dict from a schedule
    section ("bonding_curve" | "pumpswap")."""
    s = schedule[section]
    return {
        "flat_fees": s["flat_fees"],
        "fee_tiers": s["fee_tiers"],
        "stable_fee_tiers": s["stable_fee_tiers"],
        "exotic_flat_fees": s["exotic_flat_fees"],
    }


def global_config_from_schedule(schedule):
    return dict(schedule["pumpswap_global_config"])
