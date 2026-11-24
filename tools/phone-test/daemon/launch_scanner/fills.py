"""Honest paper-fill simulator.

From the 2026-10-16 deep research: paper fills are fantasy unless they model
what a real taker actually pays. The documented failure mode: a builder
assumed 1.2% round-trip costs, measured ~4% in practice, and concluded
"the edge on new coins is speed."

honest_fill() models:
  1. Next-trade price, not quoted price — you never get the price you saw.
     Entry fills at quoted * (1 + slip), exits at quoted * (1 - slip).
  2. Slippage vs pool liquidity: impact ≈ 2 * size_usd / liquidity_usd,
     floored at a minimum (thin pools always cost you).
  3. Flat per-attempt cost (0.02 SOL ≈ $1.25 at $62.50/SOL — matches the
     existing COST_PER_SHOT convention).
  4. Priority fee assumption baked into the flat cost.

This is still a model, not a fill. It is honest in the specific sense that
it can only make paper results WORSE than the fantasy fills, never better.
If a strategy can't survive honest fills, it's not a strategy.

Usage:
    from fills import honest_entry, honest_exit
    entry_price = honest_entry(quoted_price, size_usd, liquidity_usd)
    exit_price  = honest_exit(quoted_price, size_usd, liquidity_usd)
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("forex_agent.daemon.launch_scanner.fills")

# Minimum slippage even on deep pools (priority fee + next-trade drift)
MIN_SLIPPAGE_PCT = 0.5
# Slippage model: impact_pct = 200 * size_usd / liquidity_usd (both legs)
SLIPPAGE_FACTOR = 200.0
# Cap: beyond this the model refuses (pool too thin to trade honestly)
MAX_SLIPPAGE_PCT = 25.0


def slippage_pct(size_usd: float, liquidity_usd: Optional[float]) -> float:
    """Estimate one-leg slippage % for a taker of size_usd.

    Unknown liquidity -> fail-open at the minimum (never blocks on missing
    data; the veto engine handles unknown-liquidity separately if needed).
    """
    if not liquidity_usd or liquidity_usd <= 0:
        return MIN_SLIPPAGE_PCT
    impact = SLIPPAGE_FACTOR * size_usd / liquidity_usd
    return min(max(impact, MIN_SLIPPAGE_PCT), MAX_SLIPPAGE_PCT)


def honest_entry(quoted_price: float, size_usd: float = 1.0,
                 liquidity_usd: Optional[float] = None) -> float:
    """What you actually pay: quoted price + slippage (adverse)."""
    slip = slippage_pct(size_usd, liquidity_usd)
    return quoted_price * (1.0 + slip / 100.0)


def honest_exit(quoted_price: float, size_usd: float = 1.0,
                liquidity_usd: Optional[float] = None) -> float:
    """What you actually receive: quoted price - slippage (adverse)."""
    slip = slippage_pct(size_usd, liquidity_usd)
    return quoted_price * (1.0 - slip / 100.0)


def round_trip_cost_pct(size_usd: float = 1.0,
                        liquidity_usd: Optional[float] = None) -> float:
    """Total friction of a round trip in %, excluding flat per-attempt fees."""
    # (1+s)/(1-s) - 1 ≈ 2s for small s
    s = slippage_pct(size_usd, liquidity_usd) / 100.0
    return 100.0 * ((1 + s) / (1 - s) - 1.0)


def too_thin_to_trade(size_usd: float = 1.0,
                      liquidity_usd: Optional[float] = None) -> bool:
    """True when slippage hits the cap — the pool can't honestly fill us."""
    return slippage_pct(size_usd, liquidity_usd) >= MAX_SLIPPAGE_PCT
