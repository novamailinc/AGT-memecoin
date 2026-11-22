#!/usr/bin/env python3
"""SlotClock — on-chain age for Rule A with (almost) zero RPC.

Adapted from the pumpfun-bonkfun-bot reference
(``src/core/slot_clock.py``), re-shaped for THIS bot's constraints:

* The reference runs a background ``getSlot`` every 2s. We do NOT:
  a 2s background reader would spend ~43,200 getSlot calls/day
  against a 1M-credit/month free Helius tier — more than the whole
  tier, on top of a quota that is already spent. Instead this
  clock refreshes ON DEMAND, at most one getSlot per 2s, and only
  when a chain-age check is actually running (a handful of calls
  a day at our entry volume). There is no background thread.
* ``age(slot)`` is pure arithmetic at 0.4s/slot once a fresh
  reading exists; it never makes an RPC call itself beyond the
  throttled on-demand refresh above.
* Two sources feed it: an on-demand ``getSlot`` read, and
  ``observe(slot)`` for slots delivered by a stream (a stream can
  run slots ahead of the node, so the clock keeps whichever
  reading puts the newest slot furthest ahead). Failed reads keep
  the last reading; a reading older than STALE_SECONDS is
  discarded, and ``age()`` returns None — fail-closed, so the
  caller falls back to detection age.

Rule A (grad_age <= 30s) currently measures from detection time
— a slow feed delivery or a backed-up queue can pass an old coin
as fresh. Chain age measures from the migration slot instead:
strictly more correct, same 30s bar. The bar is unchanged.

Paper only. Free-only. Never raises.
"""

import logging
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.slot_clock")

#: Seconds per slot the cluster targets; real slots run a little longer.
SLOT_SECONDS = 0.4
#: Seconds after which a reading is too old to count from: real slots
#: run longer than SLOT_SECONDS, so the estimate drifts the longer
#: it runs.
STALE_SECONDS = 10.0
#: Minimum seconds between on-demand getSlot attempts (the clock
#: never spends more than one getSlot per interval, however many
#: chain-age checks run in the same cycle).
REFRESH_MIN_INTERVAL_S = 2.0


def _default_slot_fn():
    """Current slot via reserves.rpc (SharedGuard-aware: returns
    None immediately when the guard is hot, zero HTTP). Never
    raises, never carries credentials."""
    try:
        from daemon.launch_scanner import reserves as _res
    except Exception:
        try:
            import reserves as _res  # type: ignore
        except Exception:
            return None
    try:
        slot = _res.rpc("getSlot", [])
        return int(slot) if isinstance(slot, int) else None
    except Exception:
        return None


class SlotClock:
    """Tracks the cluster's newest slot without a background reader.

    ``observe(slot)`` counts a slot a listener just delivered;
    ``age(slot)`` returns seconds since ``slot`` at SLOT_SECONDS
    a slot, or None when unknown/stale (fail-closed). ``refresh()``
    performs the throttled on-demand getSlot read.
    """

    def __init__(self, slot_fn=None):
        self._slot_fn = slot_fn or _default_slot_fn
        # By source ("node", "stream"): the newest slot it gave and
        # the monotonic time it arrived.
        self._readings = {}
        self._last_attempt = 0.0

    def refresh(self):
        """One throttled on-demand getSlot read. Returns the slot
        or None. Never raises; a failed read keeps the last
        reading."""
        now = time.monotonic()
        if now - self._last_attempt < REFRESH_MIN_INTERVAL_S:
            node = self._readings.get("node")
            return node[0] if node else None
        self._last_attempt = now
        try:
            slot = self._slot_fn()
        except Exception:
            return None
        if isinstance(slot, int) and slot > 0:
            self._readings["node"] = (slot, now)
            return slot
        return None

    def observe(self, slot):
        """Count a slot a listener just delivered; None does
        nothing."""
        if slot is None:
            return
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return
        if slot <= 0:
            return
        now = time.monotonic()
        last = self._readings.get("stream")
        if last is None or slot >= last[0] or \
                now - last[1] > STALE_SECONDS:
            self._readings["stream"] = (slot, now)

    def current_slot(self):
        """Freshest known slot, or None. Refreshes on demand
        (throttled)."""
        self.refresh()
        now = time.monotonic()
        fresh = [reading for reading, read_at in
                 self._readings.values()
                 if now - read_at <= STALE_SECONDS]
        return max(fresh) if fresh else None

    def age(self, slot):
        """Seconds since ``slot``, at SLOT_SECONDS a slot.

        Never below 0. None when ``slot`` is None, no current slot
        has been read yet, or the last reading is older than
        STALE_SECONDS. May spend at most one throttled getSlot
        (see refresh); otherwise pure arithmetic.
        """
        if slot is None:
            return None
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return None
        if slot <= 0:
            return None
        self.refresh()
        now = time.monotonic()
        # Each fresh reading's estimate of the time since `slot`;
        # the largest comes from the reading that puts the newest
        # slot furthest ahead.
        ages = [
            now - read_at + (reading - slot) * SLOT_SECONDS
            for reading, read_at in self._readings.values()
            if now - read_at <= STALE_SECONDS
        ]
        if not ages:
            return None
        return max(0.0, *ages)
