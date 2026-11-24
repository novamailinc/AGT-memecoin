"""advanced_trader — a deterministic, paper-only trading decision layer.

This package is the *brain* for a Solana memecoin paper-trading bot. It
decides what to enter, how much to stake, and when to exit. It is NOT a
broker, NOT a price feed, and NOT live trading: it moves no real money,
holds no keys, and makes no network calls.

Design rules (non-negotiable):

* NO agent behaviour. Every decision is a pure deterministic function of
  market data + state: same inputs -> same outputs, every time. No LLM,
  no natural-language reasoning, no deliberation, no discretion.
* FAST. The hot path (``evaluate`` / ``manage``) is in-memory only, no I/O,
  no network, no sleeps, no retries. Decisions take milliseconds.
* DISCIPLINE IS THE EDGE. Hard stops are unconditional: the -50% stop and
  the 24h time stop fire even with a dead quote (write-off at zero), even
  mid-crash, even if every other signal says hold. Nothing overrides a stop.
* RISK LIMITS CANNOT BE OVERRIDDEN BY THE TRADER. Daily loss limits, the
  drawdown breaker and exposure caps live in ``risk.RiskGovernor``, a
  separate layer the decision code must consult and cannot bypass.
* TINY LOCAL STATE. The only persisted state is the ticket counter and
  per-lane statistics, as atomic JSON files. No database.

Public surface:

* :class:`advanced_trader.trader.AdvancedTrader` — the orchestrator.
* :class:`advanced_trader.config.TraderConfig` — every threshold, with
  environment-variable overrides.
"""

from .config import TraderConfig
from .edge import EdgeValidator
from .exits import ExitManager, new_position
from .experience import ExperienceStore, hold_bucket
from .risk import RiskGovernor
from .sanity import PriceGuard
from .signals import (
    ADAPTERS,
    AlchemySignalAdapter,
    ChainstackSignalAdapter,
    HeliusSignalAdapter,
    Signal,
    SignalAggregator,
    SignalSource,
)
from .sizing import KellySizer
from .tickets import TicketAllocator
from .trader import AdvancedTrader

__version__ = "0.1.0"

__all__ = [
    "ADAPTERS",
    "AdvancedTrader",
    "AlchemySignalAdapter",
    "ChainstackSignalAdapter",
    "EdgeValidator",
    "ExitManager",
    "ExperienceStore",
    "HeliusSignalAdapter",
    "KellySizer",
    "PriceGuard",
    "RiskGovernor",
    "Signal",
    "SignalAggregator",
    "SignalSource",
    "TicketAllocator",
    "TraderConfig",
    "hold_bucket",
    "new_position",
]
