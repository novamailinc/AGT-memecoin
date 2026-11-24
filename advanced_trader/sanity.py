"""Price sanity guard.

A bad quote must never become a fill. This guard rejects any quote that
is non-positive or absurdly far above the entry price (default: more than
100x entry — a 5000x quote is a broken feed, never a real price).

A rejected quote is counted and recorded in a bounded in-memory log. It
produces NO fill and NO journal entry as if it were real. The guard does
no I/O: the owner wires the rejection log to their own logging.

``check`` returns ``(ok, reason)``. ``reason`` is ``"ok"`` on success,
otherwise one of ``"no_quote"``, ``"nonpositive_quote"``,
``"bad_entry_price"`` or ``"quote_spike"``.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .config import TraderConfig


class PriceGuard:
    """Deterministic quote validation with rejection accounting."""

    def __init__(self, config: TraderConfig | None = None) -> None:
        self.config = config or TraderConfig()
        self.rejected_count: int = 0
        self._log: Deque[Dict[str, float | str]] = deque(maxlen=100)

    def check(
        self, price: Optional[float], entry_price: float
    ) -> Tuple[bool, str]:
        """Validate a quote against the entry price."""
        if price is None:
            self._reject("no_quote", price, entry_price)
            return False, "no_quote"
        if price <= 0:
            self._reject("nonpositive_quote", price, entry_price)
            return False, "nonpositive_quote"
        if entry_price <= 0:
            self._reject("bad_entry_price", price, entry_price)
            return False, "bad_entry_price"
        if price > entry_price * self.config.max_quote_multiple:
            self._reject("quote_spike", price, entry_price)
            return False, "quote_spike"
        return True, "ok"

    def _reject(
        self, reason: str, price: Optional[float], entry_price: float
    ) -> None:
        self.rejected_count += 1
        self._log.append(
            {
                "reason": reason,
                "price": -1.0 if price is None else float(price),
                "entry_price": float(entry_price),
                "multiple": (
                    -1.0
                    if price is None or entry_price <= 0
                    else float(price) / float(entry_price)
                ),
            }
        )

    def rejections(self) -> List[Dict[str, float | str]]:
        """Most recent rejections, oldest first (bounded to the last 100)."""
        return list(self._log)
