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
              P3 fee audit, the venue swap fee when a venue is named:
              pump.fun bonding curve / PumpSwap canonical first tier
              = 1.25% per swap, verified from pump.fun's own Fees
              Page (see the venue-fee section below). fills.py
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
# Venue (protocol/AMM) swap fees — P3 fee-model audit, 2026-10-23.
#
# VERIFIED from a PRIMARY source: pump.fun's own Fees Page, which
# forms part of its Terms of Use (https://pump.fun/docs/fees, page
# "Last Updated: 17 October 2026", read 2026-10-23):
#
# - Bonding curve (SOL- and USDC-paired coins), EVERY trade (buy and
#   sell): total 1.25% = 0.300% creator + 0.95% protocol + 0% LP.
# - PumpSwap CANONICAL pools (pools created by graduation) are tiered
#   by market cap, where market cap = token price in SOL (or USDC)
#   x 1 billion tokens. SOL schedule (total fee):
#     0-420 SOL: 1.25% (0.300 creator / 0.930 protocol / 0.020 LP)
#     420-1470: 1.20%   1470-2460: 1.15%   2460-3440: 1.10%
#     3440-4420: 1.05%  4420-9820: 1.00%   9820-14740: 0.95%
#     then -0.05% per band down to 0.30% at >= 98240 SOL
#     (creator share falls, protocol pinned at 0.05%, LP at 0.20%).
#   Non-canonical PumpSwap pools: flat 0.30% total.
# - Fees are collected IN THE QUOTE ASSET on every trade: on a buy
#   the fee is taken out of the SOL spent (only (1 - fee) of the
#   spend actually buys tokens / enters the curve); on a sell it is
#   deducted from the proceeds. It is therefore a per-swap % charge
#   ON TOP OF price/slippage — exactly what tx_cost_usd must add when
#   a venue is specified.
#
# A fresh graduation sits in the FIRST canonical tier (mcap in SOL
# at graduation is at/below the 420 SOL boundary), so graduation
# trades pay 1.25% per swap on entry and, for the short holds these
# lanes take, ~1.25% per exit swap too (a 2-3x run only reaches the
# 1.20% band — using the flat first-tier rate is conservative by
# <= 0.05pp on winners and exact for everything that falls). Before
# this audit the model charged Solana trades $0.001 gas ONLY, i.e.
# it omitted this ~2.5% round-trip venue drag entirely.
#
# Scope: ONLY trades that explicitly name venue="pump" pay this.
# Other Solana venues charge differently (e.g. Raydium AMM 0.25%),
# so no venue fee is ever guessed for a trade that doesn't name one.
PUMP_FEE_PCT_BONDING_CURVE = 1.25
PUMP_FEE_PCT_CANONICAL_LOWEST_TIER = 1.25

# PumpSwap canonical-pool total-fee tiers for SOL-paired coins:
# (mcap_sol_upper_bound_exclusive, total_fee_pct), ascending.
PUMP_CANONICAL_TIERS_SOL = (
    (420, 1.25), (1470, 1.20), (2460, 1.15), (3440, 1.10),
    (4420, 1.05), (9820, 1.00), (14740, 0.95), (19650, 0.90),
    (24560, 0.85), (29470, 0.80), (34380, 0.75), (39300, 0.70),
    (44210, 0.65), (49120, 0.60), (54030, 0.55), (58940, 0.525),
    (63860, 0.50), (68770, 0.475), (73681, 0.45), (78590, 0.425),
    (83500, 0.40), (88400, 0.375), (93330, 0.35), (98240, 0.325),
    (float("inf"), 0.30),
)


def pump_canonical_fee_pct(mcap_sol=None):
    """PumpSwap canonical-pool total swap fee % for a SOL-paired coin.

    mcap_sol = token price in SOL x 1e9 (pump.fun's own definition).
    Unknown/None mcap -> the first-tier rate (1.25%): graduation
    trades happen at that tier, and defaulting UP is the honest
    direction for a cost model. Never raises."""
    try:
        if mcap_sol is None:
            return PUMP_FEE_PCT_CANONICAL_LOWEST_TIER
        m = float(mcap_sol)
        if m < 0:
            return PUMP_FEE_PCT_CANONICAL_LOWEST_TIER
        for upper, pct in PUMP_CANONICAL_TIERS_SOL:
            if m < upper:
                return pct
        return PUMP_CANONICAL_TIERS_SOL[-1][1]
    except Exception:
        return PUMP_FEE_PCT_CANONICAL_LOWEST_TIER


def venue_fee_pct(chain, venue=None, mcap_sol=None):
    """Per-swap venue (protocol/AMM) fee % for one trade. 0.0 when no
    venue is named — never guessed. Never raises."""
    try:
        if (chain or "").lower() == "solana" and \
                (venue or "").lower() == "pump":
            return pump_canonical_fee_pct(mcap_sol)
        return 0.0
    except Exception:
        return 0.0


def venue_fee_usd(chain, traded_usd=1.0, venue=None, mcap_sol=None):
    """Venue fee in USD for ONE swap of traded_usd. Never raises."""
    try:
        return float(traded_usd or 0) * \
            venue_fee_pct(chain, venue, mcap_sol) / 100.0
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


def tx_cost_usd(chain, notional_usd=1.0, venue=None, mcap_sol=None):
    """Cost of ONE transaction on this chain, in USD: network gas
    plus, when a venue is named, that venue's per-swap fee on the
    traded amount. Default (no venue) is byte-identical to the
    pre-P3-audit model. Never raises."""
    try:
        chain = (chain or "").lower()
        if chain == "hyperliquid":
            return float(notional_usd or 0) * HL_TAKER_FEE_PCT / 100.0
        return float(GAS_USD.get(chain, 0.0)) + \
            venue_fee_usd(chain, notional_usd, venue, mcap_sol)
    except Exception:
        return 0.0


def round_trip_cost_usd(chain, notional_usd=1.0, n_txs=WORST_CASE_TXS,
                        venue=None, mcap_sol=None):
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
            2.0 * venue_fee_usd(chain, notional_usd, venue, mcap_sol)
    except Exception:
        return 0.0


def round_trip_cost_pct(chain, notional_usd=1.0, n_txs=WORST_CASE_TXS,
                        venue=None, mcap_sol=None):
    """Worst-case round-trip tx costs as % of notional. Never raises."""
    try:
        n = float(notional_usd or 0)
        if n <= 0:
            return 100.0
        return round_trip_cost_usd(chain, n, n_txs, venue, mcap_sol) / n * 100.0
    except Exception:
        return 100.0


def cost_gate(chain, notional_usd=1.0, gate_pct=COST_GATE_PCT,
              venue=None, mcap_sol=None):
    """(blocked: bool, reason: str). Pre-registered entry gate.

    Blocked when modeled worst-case round-trip tx costs exceed gate_pct
    of notional. The reason carries the math so the ledger — and the
    user — can see exactly why.
    """
    try:
        pct = round_trip_cost_pct(chain, notional_usd,
                                  venue=venue, mcap_sol=mcap_sol)
        usd = round_trip_cost_usd(chain, notional_usd,
                                  venue=venue, mcap_sol=mcap_sol)
        if pct > float(gate_pct):
            return True, (
                "COST-BLOCKED: modeled round-trip ~$%.2f on $%.2f "
                "notional (%.0f%%) exceeds the %.0f%% cost gate — "
                "no edge room survives these costs"
                % (usd, float(notional_usd or 0), pct, float(gate_pct)))
        return False, ""
    except Exception:
        return False, ""


def cost_summary(chain, notional_usd=1.0, venue=None, mcap_sol=None):
    """Human-readable one-liner for ledgers and reports."""
    try:
        return "%s: ~$%.2f round-trip on $%.2f (%.1f%%)" % (
            (chain or "?"),
            round_trip_cost_usd(chain, notional_usd,
                                venue=venue, mcap_sol=mcap_sol),
            float(notional_usd or 0),
            round_trip_cost_pct(chain, notional_usd,
                                venue=venue, mcap_sol=mcap_sol))
    except Exception:
        return "%s: cost unknown" % (chain or "?")
