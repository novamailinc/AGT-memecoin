"""Configuration for advanced_trader.

Every threshold the trader uses lives here, with a sane default and an
environment-variable override (prefix ``ADV_TRADER_``). Configuration is
read once at construction; the hot path never touches the environment.

All money values are in the account's native unit (SOL in the sibling bot,
USD in AGT-memecoin's $25 paper stakes). The package does not care which —
it only ever compares like with like.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


@dataclass
class TraderConfig:
    """All thresholds, with defaults chosen to be safe, not aggressive."""

    # -- account -------------------------------------------------------
    bankroll_start: float = 1000.0
    """Starting bankroll, in account units. Used for drawdown tracking."""

    # -- edge validation (edge.py) --------------------------------------
    edge_window: int = 100
    """Rolling closes kept per lane for expectancy stats."""
    edge_min_trades: int = 30
    """Minimum closes before a lane is trusted at all."""
    edge_min_expectancy: float = 0.0
    """A lane is tradable only if out-of-sample expectancy beats this."""

    # -- position sizing (sizing.py) ------------------------------------
    kelly_fraction: float = 0.25
    """Fractional Kelly. 0.25 = quarter-Kelly: most of the growth, a
    fraction of the volatility of full Kelly."""
    max_stake: float = 25.0
    """Hard cap on a single trade, in account units."""
    max_bankroll_pct_per_trade: float = 0.025
    """Hard cap on a single trade as a fraction of bankroll (2.5%)."""
    streak_halve_after: int = 3
    """Halve stake after this many consecutive losses on a lane."""
    streak_halt_after: int = 5
    """Halt a lane after this many consecutive losses (human re-enable)."""

    # -- exits (exits.py) -----------------------------------------------
    leg_a_mult: float = 1.5
    """Leg A: sell 25% at 1.5x entry."""
    leg_b_mult: float = 3.0
    """Leg B: sell 25% at 3x entry."""
    leg_a_fraction: float = 0.25
    leg_b_fraction: float = 0.25
    trailing_pct: float = 0.30
    """Leg C: close the remainder on a 30% retrace from the peak."""
    hard_stop_mult: float = 0.5
    """Hard stop: close everything at -50% from entry. Unconditional."""
    time_stop_hours: float = 24.0
    """Time stop: close everything 24h after entry. Unconditional —
    fires even with a dead quote (write-off at zero)."""
    dip_buyback_fraction: float = 0.25
    """One dip buyback per position: 25% of original size, after a leg
    banked, on a >=30% retrace, price at or above entry, age < 24h."""

    # -- risk governor (risk.py) ----------------------------------------
    daily_loss_limit_lane: float = 50.0
    """Halt a lane for the day once its net daily loss reaches this."""
    daily_loss_limit_global: float = 100.0
    """Halt all trading for the day once the net daily loss reaches this."""
    max_open_positions: int = 20
    """Maximum simultaneous open positions across all lanes."""
    max_exposure_per_token: float = 50.0
    """Maximum open stake in any single token."""
    max_exposure_per_deployer: float = 100.0
    """Maximum open stake across tokens from one deployer."""
    max_drawdown_pct: float = 0.20
    """Circuit breaker: halt all trading at a 20% drawdown from peak
    equity. Human re-enable only."""

    # -- price sanity (sanity.py) ---------------------------------------
    max_quote_multiple: float = 100.0
    """Reject any quote above this multiple of the entry price (a 5000x
    quote is a broken feed, never a real price)."""

    # -- tickets (tickets.py) -------------------------------------------
    ticket_state_file: str = "tickets.json"
    """Where the monotonic ticket counter persists (atomic JSON)."""
    ticket_start: int = 1
    """First ticket number issued."""

    # -- pump signals (signals.py) --------------------------------------
    corroboration_window_s: float = 5.0
    """Seconds a signal stays live for corroboration counting."""
    min_evidence_corroborated: float = 0.5
    """Minimum evidence score (0..1) for a signal confirmed by 2+
    providers."""
    min_evidence_single: float = 0.8
    """Minimum evidence score for a lone, uncorroborated signal — a
    higher bar, because one provider alone is weaker evidence."""

    # -- experience (experience.py) -------------------------------------
    experience_state_file: str = "experience.json"
    """Where closed-trade outcome stats persist (atomic JSON)."""
    experience_weight_max: float = 1.25
    """Largest size multiplier a winning pattern earns (still inside
    the Kelly caps)."""
    experience_min_trades: int = 5
    """Minimum recorded trades on a key before its expectancy can move
    the weight up or down."""
    experience_block_streak: int = 5
    """Consecutive losses on a key after which the pattern is blocked
    entirely (weight 0) until a human resets the store."""

    @classmethod
    def from_env(cls) -> "TraderConfig":
        """Build a config, applying ``ADV_TRADER_*`` overrides."""
        p = "ADV_TRADER_"
        return cls(
            bankroll_start=_env_float(p + "BANKROLL_START", 1000.0),
            edge_window=_env_int(p + "EDGE_WINDOW", 100),
            edge_min_trades=_env_int(p + "EDGE_MIN_TRADES", 30),
            edge_min_expectancy=_env_float(p + "EDGE_MIN_EXPECTANCY", 0.0),
            kelly_fraction=_env_float(p + "KELLY_FRACTION", 0.25),
            max_stake=_env_float(p + "MAX_STAKE", 25.0),
            max_bankroll_pct_per_trade=_env_float(
                p + "MAX_BANKROLL_PCT_PER_TRADE", 0.025
            ),
            streak_halve_after=_env_int(p + "STREAK_HALVE_AFTER", 3),
            streak_halt_after=_env_int(p + "STREAK_HALT_AFTER", 5),
            leg_a_mult=_env_float(p + "LEG_A_MULT", 1.5),
            leg_b_mult=_env_float(p + "LEG_B_MULT", 3.0),
            leg_a_fraction=_env_float(p + "LEG_A_FRACTION", 0.25),
            leg_b_fraction=_env_float(p + "LEG_B_FRACTION", 0.25),
            trailing_pct=_env_float(p + "TRAILING_PCT", 0.30),
            hard_stop_mult=_env_float(p + "HARD_STOP_MULT", 0.5),
            time_stop_hours=_env_float(p + "TIME_STOP_HOURS", 24.0),
            dip_buyback_fraction=_env_float(p + "DIP_BUYBACK_FRACTION", 0.25),
            daily_loss_limit_lane=_env_float(p + "DAILY_LOSS_LIMIT_LANE", 50.0),
            daily_loss_limit_global=_env_float(
                p + "DAILY_LOSS_LIMIT_GLOBAL", 100.0
            ),
            max_open_positions=_env_int(p + "MAX_OPEN_POSITIONS", 20),
            max_exposure_per_token=_env_float(p + "MAX_EXPOSURE_PER_TOKEN", 50.0),
            max_exposure_per_deployer=_env_float(
                p + "MAX_EXPOSURE_PER_DEPLOYER", 100.0
            ),
            max_drawdown_pct=_env_float(p + "MAX_DRAWDOWN_PCT", 0.20),
            max_quote_multiple=_env_float(p + "MAX_QUOTE_MULTIPLE", 100.0),
            ticket_state_file=_env_str(p + "TICKET_STATE_FILE", "tickets.json"),
            ticket_start=_env_int(p + "TICKET_START", 1),
            corroboration_window_s=_env_float(p + "CORROBORATION_WINDOW_S", 5.0),
            min_evidence_corroborated=_env_float(
                p + "MIN_EVIDENCE_CORROBORATED", 0.5
            ),
            min_evidence_single=_env_float(p + "MIN_EVIDENCE_SINGLE", 0.8),
            experience_state_file=_env_str(
                p + "EXPERIENCE_STATE_FILE", "experience.json"
            ),
            experience_weight_max=_env_float(p + "EXPERIENCE_WEIGHT_MAX", 1.25),
            experience_min_trades=_env_int(p + "EXPERIENCE_MIN_TRADES", 5),
            experience_block_streak=_env_int(p + "EXPERIENCE_BLOCK_STREAK", 5),
        )
