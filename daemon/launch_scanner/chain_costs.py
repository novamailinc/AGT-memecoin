#!/usr/bin/env python3
"""Per-chain transaction cost models for paper trading (2026-10-17).

The honest-cost layer the multichain paper mirror stands on. fills.py
models PRICE IMPACT (adverse fills, liquidity-scaled slippage) — this
module models what fills.py doesn't: the per-transaction costs that
dominate small-size trading on some chains.

The critical case is Ethereum: a $1 paper position paying ~$5 gas per
swap starts ~500% underwater before price moves. Modeling that honestly
— instead of pretending it away — is what makes the ETH paper lane
productive: it proves with math why $1 ETH strategies can't work, which
is exactly what the user needs to know before gambling real money.

Cost assumptions (documented, knob-tunable, visible in every ledger):
- solana:     $0.001/tx gas (negligible) PLUS, since the 2026-10-23
              P3 fee audit, the venue swap fee when a venue is named,
              and since the 2026-11-22/23 fee-program capture the
              venue's OWN dynamic schedule: pump.fun bonding curve /
              PumpSwap canonical first tier = 1.25% per swap, with the
              full market-cap tier schedule and quote-category flat
              fees decoded from the on-chain FeeConfig / GlobalConfig
              accounts (see the venue-fee section below). fills.py
              slippage is separate and still applies.
- base:       $0.03/tx  (L2 gas, ~$0.01-0.05 per swap in practice)
- ethereum:   $5.00/tx  (conservative static estimate; gas is volatile —
              150k gas @ ~20 gwei @ ~$3000 ETH ≈ $9. The estimate is
              deliberately conservative; the gate below uses it)
- hyperliquid: 0.035% taker fee per tx, no gas (exchange-settled spot)

2026-10-17 RECALIBRATION: the original $5.00 ETH estimate was stale
2024-era thinking (20 gwei). October 2026 reality: ~0.7 gwei average,
~$0.30-0.40 per swap. The model now uses $1.00 (~3x current, headroom
for spikes). The conclusion is unchanged — 4 txs x $1.00 = $4.00 =
400% cost drag on a $1 position, still far above the 25% gate — but
the math is now honest instead of 15x overstated.

COST_GATE_PCT: pre-registered entry gate. If the modeled worst-case
round-trip cost (entry + all exit legs) exceeds this fraction of the
$1 notional, the mirror does NOT open — it logs COST-BLOCKED with the
math. This is not refusing to measure; it IS the measurement for
chains where costs exceed any plausible edge room. 25% default.

Free-only: pure math, no network. Never raises.
"""

# Gas per transaction in USD (one swap = one tx).
# Calibrated 2026-10-17: Ethereum L1 gas collapsed in 2026 (block gas
# limit doubled in 2025, Fusaka Dec 2025). Oct 2026: avg ~0.7 gwei,
# base fee as low as 0.067 gwei; a 150k-gas swap at 1 gwei @ $2,500 ETH
# is ~$0.38. $1.00 below = ~3x headroom for gas spikes (spikes still
# happen when the network gets busy). Re-calibrate if regime changes.
GAS_USD = {
    "solana": 0.001,
    "base": 0.03,       # L2 gas, ~$0.01-0.05 per swap in practice
    "ethereum": 1.00,   # see calibration note above
    "hyperliquid": 0.0,
}

# Hyperliquid spot taker fee (percent).
HL_TAKER_FEE_PCT = 0.035

# ---------------------------------------------------------------------
# Venue (protocol/AMM) swap fees.
#
# 2026-10-23 P3 audit: verified from pump.fun's own Fees Page that the
# bonding curve and first PumpSwap canonical tier charge 1.25% per swap
# (0.95% protocol + 0.30% creator), collected IN THE QUOTE ASSET on top
# of price/slippage. Before that audit the model charged Solana trades
# $0.001 gas ONLY, omitting the ~2.5% round-trip venue drag entirely.
#
# 2026-11-23: the static table was superseded by the captured on-chain
# fee-program schedule below (the earlier "-0.05% per band" tail was an
# approximation that overstated the top tiers by up to 2.5 bps).
#
# 2026-11-23 FEE-PROGRAM CAPTURE: the venue fee schedule is read from
# the captured on-chain accounts (tests/fixtures/fee_program/, captured
# via the approved PublicNode read at 2026-11-23T04:21Z; derivation per
# the pump_fees / pump_amm IDLs and pump-fun/pump-public-docs
# FEE_PROGRAM_README) through pump_fee_schedule.py, which mirrors
# pump-fees::get_fees / pump-fees-math::calculate_fee_tier exactly:
#
# - pump.fun bonding curve: FeeConfig fee_tiers currently a SINGLE tier
#   0.95% protocol + 0.30% creator = 1.25% at every market cap (the
#   tiered mechanism is live on-chain; the admin can add tiers at any
#   time).
# - PumpSwap CANONICAL pools (pool.creator == pump pool authority):
#   market-cap tiered in QUOTE LAMPORTS. SOL-quote schedule totals:
#     >=0 SOL: 1.25% (0.02 LP / 0.93 protocol / 0.30 creator)
#     >=420: 1.20  >=1470: 1.15  >=2460: 1.10  >=3440: 1.05
#     >=4420: 1.00  >=9820: 0.95  >=14740: 0.90  >=19650: 0.85
#     >=24560: 0.80  >=29470: 0.75  >=34380: 0.70  >=39300: 0.65
#     >=44210: 0.60  >=49120: 0.55  >=54030: 0.53  >=58940: 0.50
#     >=63860: 0.48  >=68770: 0.45  >=73681: 0.43  >=78590: 0.40
#     >=83500: 0.38  >=88400: 0.35  >=93330: 0.33  >=98240: 0.30
#   (the LP share is 0.20% and protocol 0.05% in every tier above the
#   first; the creator share steps down). Stable-quote (USDC) pools use
#   the separate stable_fee_tiers (thresholds 0 / 59 / 300 / ... /
#   20000 quote units); other quote mints pay exotic_flat_fees 0.30%.
#   Non-canonical PumpSwap pools pay flat_fees = 0.30% total.
# - Fallback when a FeeConfig account is absent: pump program Global
#   fee_basis_points + creator_fee_basis_points (curve) or PumpSwap
#   GlobalConfig lp 0.20% + protocol 0.05% + coin creator 0.05%.
# - Per-coin creator overrides replace the tier creator share when the
#   config allows it (GlobalConfig creator_fee_configurable=true, max
#   3.00%); a coin with NO creator set is charged no creator fee.
# - Fees are taken IN THE QUOTE ASSET on every trade, linear in the
#   traded quote amount: on a buy the fee comes out of the SOL spent
#   (only (1 - fee) enters the curve); on a sell it is deducted from
#   the proceeds. The fee BPS does not depend on trade size.
# - The captured schedule is AS-OF the capture timestamp; the static
#   table below is the committed copy of that capture and is used only
#   when the generated artifact is unreadable. An unknown market cap
#   selects the HIGHEST-FEE tier (conservative - never an under-charge).
#
# Only trades that explicitly name a venue pay a venue fee; no fee is
# ever guessed for a trade that doesn't name one.
PUMP_FEE_PCT_BONDING_CURVE = 1.25
PUMP_FEE_PCT_CANONICAL_LOWEST_TIER = 1.25
PUMP_FEE_PCT_NONCANONICAL_FLAT = 0.30

# Static copy of the captured PumpSwap canonical SOL-quote tier table
# ((mcap_sol_upper_bound_exclusive, total_fee_pct), ascending) - the
# fallback when the generated artifact is missing. Regenerate with
# scripts/gen_pump_fee_schedule.py; tests keep this in lockstep with
# the capture.
PUMP_CANONICAL_TIERS_SOL = (
    (420, 1.25), (1470, 1.20), (2460, 1.15), (3440, 1.10),
    (4420, 1.05), (9820, 1.00), (14740, 0.95), (19650, 0.90),
    (24560, 0.85), (29470, 0.80), (34380, 0.75), (39300, 0.70),
    (44210, 0.65), (49120, 0.60), (54030, 0.55), (58940, 0.53),
    (63860, 0.50), (68770, 0.48), (73681, 0.45), (78590, 0.43),
    (83500, 0.40), (88400, 0.38), (93330, 0.35), (98240, 0.33),
    (float("inf"), 0.30),
)

_SCHEDULE = None
_SCHEDULE_TRIED = False


def _pump_schedule():
    """The captured on-chain fee schedule, or None when the generated
    artifact is unavailable (the static tables then apply). Never
    raises."""
    global _SCHEDULE, _SCHEDULE_TRIED
    if _SCHEDULE_TRIED:
        return _SCHEDULE
    _SCHEDULE_TRIED = True
    try:
        import os
        from daemon.launch_scanner import pump_fee_schedule as _pfs
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "pump_fee_schedule_captured.json")
        _SCHEDULE = _pfs.load_schedule_file(path)
    except Exception:
        _SCHEDULE = None
    return _SCHEDULE


def _schedule_fee_pct(section, quote_mint=None, mcap_sol=None,
                      mcap_lamports=None):
    """Total venue fee % from the captured schedule for one pump
    surface ("bonding_curve" | "pumpswap"). Unknown market cap selects
    the highest-fee tier. Never raises."""
    try:
        from daemon.launch_scanner import pump_fee_schedule as _pfs
        sched = _pump_schedule()
        if sched is None:
            return None
        cat = _pfs.quote_category(quote_mint)
        lamports = mcap_lamports
        if lamports is None and mcap_sol is not None and \
                cat == _pfs.QUOTE_SOL:
            lamports = int(float(mcap_sol) * 1e9)
        cfg = _pfs.fee_config_from_schedule(sched, section)
        fees = _pfs.fee_config_fees(cfg, quote_category_=cat,
                                    market_cap_lamports=lamports,
                                    is_pump_pool=True)
        return _pfs.total_fee_pct(fees)
    except Exception:
        return None


def pump_canonical_fee_pct(mcap_sol=None, mcap_lamports=None,
                           quote_mint=None):
    """PumpSwap canonical-pool total swap fee %.

    Market cap selects the tier in QUOTE LAMPORTS (mcap_lamports, or
    mcap_sol x 1e9 for SOL quotes - pump.fun's own definition: token
    price in quote x supply). Unknown/None mcap -> the first-tier rate
    (1.25%): graduation trades happen at that tier, and defaulting UP
    is the honest direction for a cost model. Never raises."""
    try:
        pct = _schedule_fee_pct("pumpswap", quote_mint, mcap_sol,
                                mcap_lamports)
        if pct is not None:
            return pct
        if mcap_sol is None and mcap_lamports is None:
            return PUMP_FEE_PCT_CANONICAL_LOWEST_TIER
        m = float(mcap_lamports) / 1e9 if mcap_lamports is not None \
            else float(mcap_sol)
        if m < 0:
            return PUMP_FEE_PCT_CANONICAL_LOWEST_TIER
        for upper, pct in PUMP_CANONICAL_TIERS_SOL:
            if m < upper:
                return pct
        return PUMP_CANONICAL_TIERS_SOL[-1][1]
    except Exception:
        return PUMP_FEE_PCT_CANONICAL_LOWEST_TIER


def pump_bonding_curve_fee_pct(quote_mint=None, mcap_lamports=None):
    """pump.fun bonding-curve total swap fee % from the captured
    FeeConfig (currently a flat 1.25% at every market cap). Never
    raises."""
    try:
        pct = _schedule_fee_pct("bonding_curve", quote_mint,
                                None, mcap_lamports)
        if pct is not None:
            return pct
        return PUMP_FEE_PCT_BONDING_CURVE
    except Exception:
        return PUMP_FEE_PCT_BONDING_CURVE


def venue_fee_pct(chain, venue=None, mcap_sol=None, mcap_lamports=None,
                  quote_mint=None):
    """Per-swap venue (protocol/AMM) fee % for one trade. 0.0 when no
    venue is named - never guessed. Never raises.

    venue: "pump" (legacy name: pump venues at the canonical schedule,
    first tier when the market cap is unknown), "pumpfun_curve"
    (bonding curve), "pumpswap"/"pumpswap_canonical" (canonical pool
    tiers), "pumpswap_flat" (non-canonical pool flat fee)."""
    try:
        if (chain or "").lower() != "solana":
            return 0.0
        v = (venue or "").lower()
        if v == "pump":
            return pump_canonical_fee_pct(mcap_sol, mcap_lamports,
                                          quote_mint)
        if v == "pumpfun_curve":
            return pump_bonding_curve_fee_pct(quote_mint,
                                              mcap_lamports)
        if v in ("pumpswap", "pumpswap_canonical"):
            return pump_canonical_fee_pct(mcap_sol, mcap_lamports,
                                          quote_mint)
        if v == "pumpswap_flat":
            try:
                from daemon.launch_scanner import pump_fee_schedule \
                    as _pfs
                sched = _pump_schedule()
                if sched is not None:
                    return _pfs.total_fee_pct(
                        sched["pumpswap"]["flat_fees"])
            except Exception:
                pass
            return PUMP_FEE_PCT_NONCANONICAL_FLAT
        return 0.0
    except Exception:
        return 0.0


# DexScreener dex labels that map to modeled pump venues (exact match;
# graduation.py documents "pump" as the bonding curve and "pumpswap" as
# the AMM - "pumpswap" merely contains the substring "pump"). Any other
# label is an unmodeled venue.
DEX_VENUE_SOLANA = {
    "pump": "pumpfun_curve",
    "pump.fun": "pumpfun_curve",
    "pumpfun": "pumpfun_curve",
    "pumpswap": "pumpswap",
}


def lane_dex_venue(chain, dex):
    """(venue, reason) for a scanner lane's fee model.

    venue: the venue string to pass to tx_cost_usd / cost_gate; "" when
    the chain models its own trading fee without a venue kwarg
    (hyperliquid's taker fee); None when the context is UNKNOWN and the
    caller must REFUSE the entry rather than invent a fee.

    Grounding: the dex label comes from the lane's own fresh tape
    (DexScreener dexId). Lanes do not pass market cap or quote mint:
    they carry USD mcap, not quote-lamport mcap, and no in-lane SOL/USD
    conversion exists, so the fee module's unknown-mcap path selects the
    highest tier and its unknown-quote path defaults to the SOL category
    - each the maximum fee, the conservative direction for a cost model.
    Never raises."""
    try:
        c = (chain or "").lower()
        if c == "hyperliquid":
            return "", ""
        if c != "solana":
            return None, ("venue fee unmodeled for chain '%s' (only "
                          "solana pump venues and the hyperliquid taker "
                          "fee are modeled)" % (c or "?"))
        v = DEX_VENUE_SOLANA.get(str(dex or "").strip().lower())
        if v is None:
            return None, ("venue fee unmodeled for dex '%s' (only the "
                          "pump.fun curve and PumpSwap are modeled)"
                          % (str(dex or "?")[:32]))
        return v, ""
    except Exception:
        return None, "venue classification failed (refusing)"


def venue_fee_usd(chain, traded_usd=1.0, venue=None, mcap_sol=None,
                  mcap_lamports=None, quote_mint=None):
    """Venue fee in USD for ONE swap of traded_usd. Never raises."""
    try:
        return float(traded_usd or 0) * \
            venue_fee_pct(chain, venue, mcap_sol, mcap_lamports,
                          quote_mint) / 100.0
    except Exception:
        return 0.0

# Worst-case tx count for the user ladder: entry + 3 exit legs
# (50% @2x, 25% @3x, 25% trailing). Hard stop / time stop close the
# remainder in a single tx, so they never add beyond this.
WORST_CASE_TXS = 4

# Pre-registered cost gate: block entry when modeled round-trip costs
# exceed this % of notional. Knob-tunable; 25% default.
COST_GATE_PCT = 25.0

SUPPORTED = ("solana", "base", "ethereum", "hyperliquid")


def tx_cost_usd(chain, notional_usd=1.0, venue=None, mcap_sol=None,
                mcap_lamports=None, quote_mint=None):
    """Cost of ONE transaction on this chain, in USD: network gas
    plus, when a venue is named, that venue's per-swap fee on the
    traded amount. Default (no venue) is byte-identical to the
    pre-P3-audit model. Never raises."""
    try:
        chain = (chain or "").lower()
        if chain == "hyperliquid":
            return float(notional_usd or 0) * HL_TAKER_FEE_PCT / 100.0
        return float(GAS_USD.get(chain, 0.0)) + \
            venue_fee_usd(chain, notional_usd, venue, mcap_sol,
                          mcap_lamports, quote_mint)
    except Exception:
        return 0.0


def round_trip_cost_usd(chain, notional_usd=1.0, n_txs=WORST_CASE_TXS,
                        venue=None, mcap_sol=None, mcap_lamports=None,
                        quote_mint=None):
    """Worst-case modeled round-trip tx costs in USD (gas/fees only;
    slippage is modeled separately by fills.py). With a venue named,
    adds the venue fee on TWO full-notional swaps — the entry swap
    plus the exit swaps, whose leg fractions sum to the full
    position. Never raises."""
    try:
        chain = (chain or "").lower()
        if chain == "hyperliquid":
            # Fee scales with traded value; approximate on full notional
            # per tx (conservative — legs are fractions).
            return float(notional_usd or 0) * HL_TAKER_FEE_PCT / 100.0 * n_txs
        return float(GAS_USD.get(chain, 0.0)) * int(n_txs or 0) + \
            2.0 * venue_fee_usd(chain, notional_usd, venue, mcap_sol,
                                mcap_lamports, quote_mint)
    except Exception:
        return 0.0


def round_trip_cost_pct(chain, notional_usd=1.0, n_txs=WORST_CASE_TXS,
                        venue=None, mcap_sol=None, mcap_lamports=None,
                        quote_mint=None):
    """Worst-case round-trip tx costs as % of notional. Never raises."""
    try:
        n = float(notional_usd or 0)
        if n <= 0:
            return 100.0
        return round_trip_cost_usd(chain, n, n_txs, venue, mcap_sol,
                                   mcap_lamports, quote_mint) / n * 100.0
    except Exception:
        return 100.0


def cost_gate(chain, notional_usd=1.0, gate_pct=COST_GATE_PCT,
              venue=None, mcap_sol=None, mcap_lamports=None,
              quote_mint=None):
    """(blocked: bool, reason: str). Pre-registered entry gate.

    Blocked when modeled worst-case round-trip tx costs exceed gate_pct
    of notional. The reason carries the math so the ledger — and the
    user — can see exactly why.
    """
    try:
        pct = round_trip_cost_pct(chain, notional_usd, venue=venue,
                                  mcap_sol=mcap_sol,
                                  mcap_lamports=mcap_lamports,
                                  quote_mint=quote_mint)
        usd = round_trip_cost_usd(chain, notional_usd, venue=venue,
                                  mcap_sol=mcap_sol,
                                  mcap_lamports=mcap_lamports,
                                  quote_mint=quote_mint)
        if pct > float(gate_pct):
            return True, (
                "COST-BLOCKED: modeled round-trip ~$%.2f on $%.2f "
                "notional (%.0f%%) exceeds the %.0f%% cost gate — "
                "no edge room survives these costs"
                % (usd, float(notional_usd or 0), pct, float(gate_pct)))
        return False, ""
    except Exception:
        return False, ""


def cost_summary(chain, notional_usd=1.0, venue=None, mcap_sol=None,
                 mcap_lamports=None, quote_mint=None):
    """Human-readable one-liner for ledgers and reports."""
    try:
        return "%s: ~$%.2f round-trip on $%.2f (%.1f%%)" % (
            (chain or "?"),
            round_trip_cost_usd(chain, notional_usd, venue=venue,
                                mcap_sol=mcap_sol,
                                mcap_lamports=mcap_lamports,
                                quote_mint=quote_mint),
            float(notional_usd or 0),
            round_trip_cost_pct(chain, notional_usd, venue=venue,
                                mcap_sol=mcap_sol,
                                mcap_lamports=mcap_lamports,
                                quote_mint=quote_mint))
    except Exception:
        return "%s: cost unknown" % (chain or "?")
