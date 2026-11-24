"""Shared exit-poll cadence helper (2026-11-20, faster-exits item 1b).

Why this exists: the five live trader loops all sleep POLL_SECS = 5
nominally, but the loop body (scan_once + GT backfill + entry RPC +
cards) stretches the real exit-eval gap to a measured median ~33s
(p90 ~60-70s) for young positions — read off the per-eval
quote_journal timestamps in the last 500 closes of each grad-family
ledger. In that gap a memecoin can fall from above the -50% stop to
~0.06x (median trigger-eval multiple on stop closes, prev eval
>0.5x in 100% of cases), so the stop never sees 0.5x. The fix is
latency-only: evaluate exits on a dedicated thread at a fixed
~10s cadence while any position is young (first 15 min after
entry, when dumps are fastest), then fall back to the main loop's
own poll as backstop. No exit decision, threshold, price source,
or fill changes — the thread calls the lane's existing poll_exits
with the lane's existing price_fn, inside the pacer (exit pricing
is unchanged: Jupiter v3 / DexScreener / paced-Helius order).

Quote-volume bound (see quote_cost_estimate): a fast poll costs
one quote per open position (the per-position DexScreener call in
grad._quote_detail has no cache; Jupiter v3 is batched with a 12s
cache and is only the fallback). Fast polls run ONLY while a young
position exists, so the extra volume is n_open * 6/min per lane
at most, and zero when no position is young. No Helius attempts
are added: this module makes no HTTP call of any kind.

Pure functions (is_fast_age / has_young_position / poll_interval_s
/ quote_cost_estimate) are unit-testable with no network and no
ledger. ExitPoller is the thread wrapper the trader scripts start.
"""

import threading
import time

# Poll every ~10s while a position is young ...
FAST_INTERVAL_S = 10.0
# ... where "young" is the first 15 minutes after entry.
FAST_WINDOW_S = 900.0
# When no position is young, the thread only re-checks the ledger
# this often (a ledger read, no quotes) — the main loop's own poll
# remains the backstop for older positions.
IDLE_CHECK_S = 30.0


def position_age_s(entry_ts, now=None):
    """Seconds since entry. Negative (future-dated entry_ts, clock
    skew) is returned as-is; callers treat it as young (fail-fast
    direction: more polling, never less)."""
    now = time.time() if now is None else now
    try:
        return float(now) - float(entry_ts)
    except Exception:
        return 0.0


def is_fast_age(age_s, window_s=FAST_WINDOW_S):
    """True while a position of this age gets fast exit polls."""
    try:
        return float(age_s) < float(window_s)
    except Exception:
        return True


def has_young_position(positions, now=None, window_s=FAST_WINDOW_S):
    """True if any open position is younger than window_s.

    positions is a mapping mint -> position dict (as returned by
    engine.open_positions()), or any iterable of position dicts.
    A position with a missing/unparseable entry_ts counts as young
    (fail-fast: poll it rather than assume it is old). Never raises.
    """
    now = time.time() if now is None else now
    if positions is None:
        return True  # unknown open set: poll fast, never assume old
    try:
        items = positions.values() if hasattr(positions, "values") \
            else positions
        for pos in items or []:
            try:
                entry_ts = (pos or {}).get("entry_ts")
                if entry_ts is None:
                    return True
                if is_fast_age(position_age_s(entry_ts, now), window_s):
                    return True
            except Exception:
                return True
    except Exception:
        return True
    return False


def poll_interval_s(positions, now=None, fast_s=FAST_INTERVAL_S,
                    idle_s=IDLE_CHECK_S, window_s=FAST_WINDOW_S):
    """Seconds until the next exit poll: fast while any position is
    young, idle-check otherwise."""
    if has_young_position(positions, now, window_s):
        return float(fast_s)
    return float(idle_s)


def quote_cost_estimate(n_open, interval_s):
    """Upper-bound extra quote calls per minute for one lane polling
    every interval_s with n_open positions open. One poll costs at
    most one quote per open position (grad._quote_detail: one
    DexScreener call per position, no cache; momentum's Jupiter
    batch is cheaper — one batched call per 12s cache window — so
    this is a bound, not an exact count for momentum)."""
    try:
        n = max(0, int(n_open))
        iv = float(interval_s)
        if iv <= 0:
            return 0.0
        return n * 60.0 / iv
    except Exception:
        return 0.0


class ExitPoller:
    """Dedicated exit-poll thread for one trader loop.

    poll_fn: () -> closes, the lane's existing poll_exits call
        (same price_fn the main loop uses — same pricing, same
        decisions, inside the pacer).
    positions_fn: () -> open positions mapping, used only to decide
        fast vs idle (a ledger read, no quotes).
    Never raises out of the thread: a failing poll is logged via
    on_error (or swallowed) and retried next interval. Polls never
    overlap: a slow poll simply delays the next one (no pile-up).
    The main loop keeps its own poll_exits call as backstop, so if
    this thread dies the lane degrades to its previous cadence.
    """

    def __init__(self, poll_fn, positions_fn, fast_s=FAST_INTERVAL_S,
                 idle_s=IDLE_CHECK_S, window_s=FAST_WINDOW_S,
                 on_error=None, time_fn=None, sleep_fn=None):
        self._poll_fn = poll_fn
        self._positions_fn = positions_fn
        self._fast_s = float(fast_s)
        self._idle_s = float(idle_s)
        self._window_s = float(window_s)
        self._on_error = on_error
        self._time_fn = time_fn or time.time
        self._sleep_fn = sleep_fn or time.sleep
        self._stop = threading.Event()
        self._thread = None
        self.polls = 0
        self.errors = 0

    def start(self):
        """Start the daemon thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="exit-poller", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout=None):
        """Ask the thread to stop and wait for it (best effort)."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        return t is None or not t.is_alive()

    def _run(self):
        while not self._stop.is_set():
            try:
                try:
                    positions = self._positions_fn()
                except Exception:
                    positions = None
                interval = poll_interval_s(
                    positions, self._time_fn(), self._fast_s,
                    self._idle_s, self._window_s)
                if interval <= self._fast_s:
                    try:
                        self._poll_fn()
                        self.polls += 1
                    except Exception as e:
                        self.errors += 1
                        if self._on_error is not None:
                            try:
                                self._on_error(e)
                            except Exception:
                                pass
                    # Sleep the fast interval AFTER the poll, so a
                    # slow poll delays (never overlaps) the next one.
                    self._sleep_fn(self._fast_s)
                else:
                    self._sleep_fn(self._idle_s)
            except Exception as e:
                self.errors += 1
                if self._on_error is not None:
                    try:
                        self._on_error(e)
                    except Exception:
                        pass
                self._sleep_fn(self._fast_s)
