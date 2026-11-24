"""Deterministic ladder exits with an unconditional-stop guarantee.

The ladder (fractions are of the ORIGINAL volume):

* Leg A — sell 25% at 1.5x entry. Banking leg A arms the breakeven stop.
* Leg B — sell 25% at 3x entry.
* Leg C — close the remainder on a 30% retrace from the peak (only armed
  once leg A has banked; before that, the hard stop and time stop are the
  protection).
* Hard stop — close everything at -50% from entry. Unconditional.
* Time stop — close everything 24h after entry. Unconditional.
* Breakeven stop — once leg A banks, the stop moves to entry: if the
  quote falls back to entry, close everything.
* Dip buyback — one per position, after a leg banks, on a >=30% retrace,
  price at or above entry, age < 24h. In practice the retrace is the
  trailing-stop trigger, so the buyback is emitted as a re-entry action
  alongside the trailing close.

DISCIPLINE IS THE EDGE: the check order is fixed — hard stop, breakeven,
time stop, legs, trailing/buyback — and a stop can never be overridden
or delayed by anything in this module. A stop fires even mid-crash and
even if every other signal says hold.

QUOTE-DEAD PATH: if there is no usable quote (``None`` or <= 0) and the
position is past its time stop, it is closed at ZERO value — an
administered write-off, never a market price — and journaled as a
write-off. A dead quote before the time stop means hold and wait: the
time stop, not hope, decides. This is the fix for positions rotting
open for 37+ days because their feed died.

Positions are plain dicts (see :func:`new_position`). :meth:`ExitManager.manage`
updates the position's bookkeeping in place (peak, legs, breakeven) and
returns a list of action dicts. Actions are instructions for the owner
to execute; this module never executes a trade itself.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import TraderConfig


def new_position(
    ticket: int,
    token: str,
    entry_price: float,
    entry_ts: float,
    volume: float,
    deployer: str = "",
    lane: str = "",
) -> Dict[str, Any]:
    """Create a position dict in the shape :class:`ExitManager` expects.

    ``entry_ts`` is seconds since the epoch. ``volume`` is the stake in
    account units. ``remaining`` starts at 1.0 (100% of original volume).
    """
    return {
        "ticket": int(ticket),
        "token": token,
        "deployer": deployer,
        "lane": lane,
        "entry_price": float(entry_price),
        "entry_ts": float(entry_ts),
        "peak_price": float(entry_price),
        "volume": float(volume),
        "remaining": 1.0,
        "legs_a": False,
        "legs_b": False,
        "breakeven_armed": False,
        "buyback_used": False,
        "closed": False,
    }


class ExitManager:
    """Pure ladder-exit decisions. No I/O, no network, no discretion."""

    def __init__(self, config: TraderConfig | None = None) -> None:
        self.config = config or TraderConfig()

    def manage(
        self,
        position: Dict[str, Any],
        quote: Optional[float],
        now_ts: float,
    ) -> List[Dict[str, Any]]:
        """Decide what to do with a position at this quote and time.

        Returns a list of action dicts, each with ``action`` (``"sell"``,
        ``"close_all"``, ``"write_off"`` or ``"buyback"``), ``reason``,
        ``fraction`` of original volume, and ``close_price`` (0.0 for a
        write-off). An empty list means hold.
        """
        if position.get("closed"):
            return []
        cfg = self.config
        entry = float(position["entry_price"])
        age_s = float(now_ts) - float(position["entry_ts"])
        time_stop_s = cfg.time_stop_hours * 3600.0

        # -- quote-dead path: only the time stop can close a position
        #    with no usable quote, and it closes it at zero. ----------
        if quote is None or quote <= 0:
            if age_s >= time_stop_s:
                position["closed"] = True
                return [
                    {
                        "action": "write_off",
                        "reason": "quote_dead_time_stop",
                        "fraction": float(position.get("remaining", 1.0)),
                        "close_price": 0.0,
                    }
                ]
            return []

        quote = float(quote)
        if quote > float(position["peak_price"]):
            position["peak_price"] = quote
        peak = float(position["peak_price"])

        # -- hard stop: unconditional, checked first. ------------------
        if quote <= entry * cfg.hard_stop_mult:
            position["closed"] = True
            return [
                {
                    "action": "close_all",
                    "reason": "hard_stop",
                    "fraction": float(position.get("remaining", 1.0)),
                    "close_price": quote,
                }
            ]

        # -- breakeven stop: armed by leg A, stop sits at entry. ------
        if position.get("breakeven_armed") and quote <= entry:
            position["closed"] = True
            return [
                {
                    "action": "close_all",
                    "reason": "breakeven_stop",
                    "fraction": float(position.get("remaining", 1.0)),
                    "close_price": quote,
                }
            ]

        # -- time stop: unconditional, 24h after entry. ---------------
        if age_s >= time_stop_s:
            position["closed"] = True
            return [
                {
                    "action": "close_all",
                    "reason": "time_stop",
                    "fraction": float(position.get("remaining", 1.0)),
                    "close_price": quote,
                }
            ]

        actions: List[Dict[str, Any]] = []

        # -- leg A ------------------------------------------------------
        if not position.get("legs_a") and quote >= entry * cfg.leg_a_mult:
            position["legs_a"] = True
            position["breakeven_armed"] = True
            position["remaining"] = float(position["remaining"]) - cfg.leg_a_fraction
            actions.append(
                {
                    "action": "sell",
                    "reason": "leg_a",
                    "fraction": cfg.leg_a_fraction,
                    "close_price": quote,
                }
            )

        # -- leg B ------------------------------------------------------
        if (
            position.get("legs_a")
            and not position.get("legs_b")
            and quote >= entry * cfg.leg_b_mult
        ):
            position["legs_b"] = True
            position["remaining"] = float(position["remaining"]) - cfg.leg_b_fraction
            actions.append(
                {
                    "action": "sell",
                    "reason": "leg_b",
                    "fraction": cfg.leg_b_fraction,
                    "close_price": quote,
                }
            )

        # -- leg C: trailing stop on the remainder (armed by leg A). ---
        #    The dip buyback rides along as a re-entry after the close.
        if position.get("legs_a") and quote <= peak * (1.0 - cfg.trailing_pct):
            remaining = float(position.get("remaining", 1.0))
            position["remaining"] = 0.0
            position["closed"] = True
            actions.append(
                {
                    "action": "close_all",
                    "reason": "trailing_stop",
                    "fraction": remaining,
                    "close_price": quote,
                }
            )
            if (
                not position.get("buyback_used")
                and quote >= entry
                and age_s < time_stop_s
            ):
                position["buyback_used"] = True
                actions.append(
                    {
                        "action": "buyback",
                        "reason": "dip_buyback",
                        "fraction": cfg.dip_buyback_fraction,
                        "close_price": quote,
                    }
                )
            return actions

        return actions
