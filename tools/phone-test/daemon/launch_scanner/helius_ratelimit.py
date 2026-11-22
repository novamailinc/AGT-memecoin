"""Shared Helius rate-limit guard for the launch scanner.

Both ``smartmoney`` and ``deployers`` hit the same Helius RPC endpoint with
the same API key, so they share one rate-limit bucket — and one cooldown.
Without coordination, each module retries blindly on HTTP 429 and the
combined hammering keeps the bucket empty (2026-10-07: 42k+ 429s in the
log, smart-money signal degraded to a limp).

Protocol (fail-soft, never raises):
- Before any Helius call: ``cooldown_remaining()`` — >0 means skip the
  call and return None, exactly like a failed request today.
- After a call: ``report_ratelimited()`` on HTTP 429, ``report_success()``
  on any successful response. Anything else (timeouts, 5xx, no key)
  leaves the cooldown untouched — only 429 engages the brake.

Backoff: 60s, doubling to a 600s cap, reset on first success. All state is
module-level (the daemons are single-process); stdlib only so tests can
import it freely.
"""

from __future__ import annotations

import time

_BASE_BACKOFF_S = 60.0
_MAX_BACKOFF_S = 600.0


class RateGuard:
    """One rate-limit bucket: one guard per API key.

    The original module-level functions all shared a single bucket
    because ``smartmoney`` and ``deployers`` used one key. A second
    (discovery) key gets its own guard so research traffic never
    contends with the live signals' budget.
    """

    def __init__(self) -> None:
        self._cooldown_until = 0.0
        self._backoff_s = _BASE_BACKOFF_S

    def cooldown_remaining(self, now: float | None = None) -> float:
        """Seconds left on this guard's cooldown, 0 when clear."""
        t = time.time() if now is None else now
        return max(0.0, self._cooldown_until - t)

    def report_ratelimited(self, now: float | None = None) -> float:
        """Engage/extend the cooldown. Returns the seconds set."""
        t = time.time() if now is None else now
        self._cooldown_until = t + self._backoff_s
        set_for = self._backoff_s
        self._backoff_s = min(_MAX_BACKOFF_S, self._backoff_s * 2.0)
        return set_for

    def report_success(self) -> None:
        """A call got through — reset the backoff ladder."""
        self._backoff_s = _BASE_BACKOFF_S

    def reset(self) -> None:
        """Clear cooldown and backoff state."""
        self._cooldown_until = 0.0
        self._backoff_s = _BASE_BACKOFF_S


# The original shared bucket: smartmoney + deployers on the main key.
# Kept as module-level functions for backward compatibility.
_SHARED = RateGuard()


def cooldown_remaining(now: Optional[float] = None) -> float:
    """Seconds left on the shared cooldown, 0 when clear."""
    return _SHARED.cooldown_remaining(now)


def report_ratelimited(now: Optional[float] = None) -> float:
    """Engage/extend the cooldown. Returns the cooldown seconds set."""
    return _SHARED.report_ratelimited(now)


def report_success() -> None:
    """A call got through — reset the backoff ladder."""
    _SHARED.report_success()


def reset_for_tests() -> None:
    """Test hook: clear cooldown and backoff state."""
    _SHARED.reset()


def new_guard() -> RateGuard:
    """A fresh, independent rate-limit bucket (e.g. a second API key)."""
    return RateGuard()
