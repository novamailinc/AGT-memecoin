#!/usr/bin/env python3
"""Scan scheduling for the paper-only Solana memecoin bot
(2026-11-21, advanced build).

DECISION + JOURNALING ONLY. This module never scans, never calls
any endpoint, and never touches a lane. It answers one question
per trader-loop tick — given the regime, the 429 pressure, the
fresh-graduation flow, and the open positions, should this tick
scan, at what cadence, and in what mode? — so the loop can skip
the scan portion of a tick while exits, the GT backfill cadence,
and the armed watcher keep running. Scanning rests; trading does
not die.

Modes:
  * FULL (cadence 5s) — the trader loop's normal POLL_SECS tick.
  * REDUCED (cadence 30s) — 429 pressure or a COLD regime: scan
    at most once per 30s, so the scan path stops hammering a
    throttled or dead market.
  * REST (no scan) — ONLY when the regime is COLD and there are
    no fresh graduations in the last 10 minutes and no positions
    open. A REST window ends the moment a fresh graduation
    arrives or a position opens: should_scan() is stateless and
    re-evaluates every call, so the next tick leaves REST.

The ``scan`` flag also honours the cadence: a tick is due only
when ``now - last_scan_ts >= cadence_s`` (``last_scan_ts`` None
means due now; a future/clock-skewed timestamp means due now).
The caller owns ``last_scan_ts`` and updates it after each scan
it actually runs. The mode is still reported on a not-due tick,
so the caller can tell "waiting for cadence" (mode FULL/REDUCED,
scan False) from "resting" (mode REST, scan False).

The regime string is ACCEPTED, never computed here: it comes
from the parent's regime module. Recognised values are COLD,
NORMAL, HOT (case-insensitive). Any other regime value is bad
input and fails open to FULL — a typo in the regime feed must
never rest the scanner.

Contract:
  * Pure decision + jsonl journaling, stdlib only.
  * Every public function never raises.
  * Fail-open: any bad input -> {"scan": True, "cadence_s": 5,
    "mode": "FULL", "reason": "fail_open"}. Scanning is the
    safe default — a broken scheduler must never silence the
    entry path; the killswitch (not this module) is what halts
    a lane.
  * Home resolution: explicit ``home`` wins, else
    ``FOREX_AGENT_HOME`` at call time, else ``~/.forex-agent``
    (same as lessons.py).
  * Window journal writes are atomic (per-process tmp +
    os.replace) under a per-file flock, bounded to the newest
    500 lines (same pattern as lessons.py). The caller journals
    a window when the mode CHANGES (start_ts = when the mode
    began, end_ts = when it ended); this module only provides
    the append/read helpers, it does not track windows itself.

Paper only. Nothing here touches real money, keys, or wallets.
"""

import fcntl
import json
import os
import time

REGIMES = ("COLD", "NORMAL", "HOT")
MODES = ("FULL", "REDUCED", "REST")
FULL_CADENCE_S = 5
REDUCED_CADENCE_S = 30
# REST re-check interval: the loop still calls should_scan() on
# its normal tick, but a caller that sleeps on cadence_s between
# schedule checks re-evaluates at most 30s after a REST begins —
# so a fresh graduation ends a REST window within 30s at most
# even if the caller paces on cadence. The scan itself stays off
# (scan False) for the whole REST window.
REST_CADENCE_S = 30
SCAN_WINDOW_MAX = 500
SCAN_WINDOW_FILE = "scan_windows.jsonl"

_FAIL_OPEN = {"scan": True, "cadence_s": FULL_CADENCE_S,
              "mode": "FULL", "reason": "fail_open"}


def _home(home=None):
    if home:
        return str(home)
    return os.environ.get(
        "FOREX_AGENT_HOME",
        os.path.join(os.path.expanduser("~"), ".forex-agent"))


def scan_windows_path(home=None):
    """<HOME>/scan_windows.jsonl. Never raises."""
    try:
        return os.path.join(_home(home), SCAN_WINDOW_FILE)
    except Exception:
        return SCAN_WINDOW_FILE


class _file_lock:
    """Exclusive flock on <path>.lock. Never raises."""

    def __init__(self, path):
        self._path = path + ".lock"
        self._fh = None

    def __enter__(self):
        try:
            self._fh = open(self._path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._fh = None
        return self

    def __exit__(self, *a):
        try:
            if self._fh is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        except Exception:
            pass
        return False


def _num(v):
    """float(v) or None — never raises, never coerces junk."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _count(v):
    """Non-negative int count, or None when unparseable. Never
    raises. Bool is not a count."""
    n = _num(v)
    if n is None:
        return None
    return max(0, int(n))


def should_scan(now, state):
    """Scan decision for one trader-loop tick.

    ``now`` is epoch seconds. ``state`` is:
      {"regime": "COLD"|"NORMAL"|"HOT",
       "rate429_active": bool,
       "fresh_graduations_last_10m": int,
       "positions_open": int,
       "last_scan_ts": float|None}

    Returns {"scan", "cadence_s", "mode", "reason"}. Never
    raises; any bad input fails open to FULL (see _FAIL_OPEN).
    """
    try:
        t = _num(now)
        if t is None:
            return dict(_FAIL_OPEN)
        if not isinstance(state, dict):
            return dict(_FAIL_OPEN)
        regime = state.get("regime")
        if not isinstance(regime, str):
            return dict(_FAIL_OPEN)
        regime = regime.strip().upper()
        if regime not in REGIMES:
            return dict(_FAIL_OPEN)
        rate429 = bool(state.get("rate429_active"))
        fresh = _count(state.get("fresh_graduations_last_10m"))
        positions = _count(state.get("positions_open"))
        if fresh is None or positions is None:
            return dict(_FAIL_OPEN)
        last = state.get("last_scan_ts")
        last_ts = None if last is None else _num(last)
        if last is not None and last_ts is None:
            return dict(_FAIL_OPEN)

        # REST: only the fully-quiet case. Exits still run —
        # only the scan portion of the tick is skipped.
        if regime == "COLD" and fresh == 0 and positions == 0:
            return {"scan": False, "cadence_s": REST_CADENCE_S,
                    "mode": "REST",
                    "reason": ("rest: cold regime, no fresh "
                               "graduations in 10m, no open "
                               "positions — exits still run")}
        if rate429:
            mode, cadence = "REDUCED", REDUCED_CADENCE_S
            why = "429 active"
        elif regime == "COLD":
            mode, cadence = "REDUCED", REDUCED_CADENCE_S
            why = "cold regime"
        else:
            mode, cadence = "FULL", FULL_CADENCE_S
            why = "%s regime, no 429 pressure" % regime.lower()
        due = last_ts is None or last_ts > t \
            or (t - last_ts) >= cadence
        if due:
            return {"scan": True, "cadence_s": cadence,
                    "mode": mode,
                    "reason": "%s: %s" % (mode.lower(), why)}
        return {"scan": False, "cadence_s": cadence, "mode": mode,
                "reason": ("%s: %s (cadence not elapsed, next "
                           "scan in %.0fs)"
                           % (mode.lower(), why,
                              cadence - (t - last_ts)))}
    except Exception:
        return dict(_FAIL_OPEN)


def _read_all(home=None):
    """All scan-window records, in file order. [] if none."""
    try:
        p = scan_windows_path(home)
        if not os.path.exists(p):
            return []
        out = []
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        return out
    except Exception:
        return []


def _write_all(records, home=None):
    """Rewrite the scan-window log atomically (per-process tmp +
    os.replace, fsync). Never raises."""
    try:
        p = scan_windows_path(home)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = "%s.tmp-%d" % (p, os.getpid())
        with open(tmp, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def journal_window(home, window):
    """Append one scan window to <HOME>/scan_windows.jsonl.

    ``window`` is {"start_ts", "end_ts", "mode", "reason"}.
    ``mode`` must be FULL, REDUCED, or REST; ``start_ts`` must
    be numeric; ``end_ts`` may be None (window still open).
    Bounded to the newest SCAN_WINDOW_MAX (500) lines. Returns
    True when written, False otherwise. Never raises."""
    try:
        w = window if isinstance(window, dict) else {}
        mode = str(w.get("mode") or "").strip().upper()
        if mode not in MODES:
            return False
        start = _num(w.get("start_ts"))
        if start is None:
            return False
        end = w.get("end_ts")
        end_ts = None if end is None else _num(end)
        if end is not None and end_ts is None:
            return False
        rec = {"start_ts": start, "end_ts": end_ts, "mode": mode,
               "reason": str(w.get("reason") or "")}
        p = scan_windows_path(home)
        with _file_lock(p):
            recs = _read_all(home)
            recs.append(rec)
            recs = recs[-SCAN_WINDOW_MAX:]
            return _write_all(recs, home)
    except Exception:
        return False


def read_scan_windows(home=None, n=50):
    """Newest-first scan-window records, at most ``n``. [] when
    none or unreadable. Never raises."""
    try:
        try:
            n = int(n)
        except Exception:
            n = 50
        if n <= 0:
            return []
        recs = _read_all(home)
        return list(reversed(recs[-n:]))
    except Exception:
        return []
