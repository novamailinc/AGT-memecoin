#!/usr/bin/env python3
"""Automatic switches for the paper-only Solana memecoin bot
(2026-11-21, advanced build).

DECISION + JOURNALING ONLY. This module never calls Helius, never
touches the quiet state file, never routes around a rate limit,
and never changes any endpoint. It answers one question — given
the 429 count the watch already measured and the quiet state the
caller already read, should quiet engage, release, or hold? — and
it journals the switch so the record shows why the fleet went
quiet. The caller (e.g. scripts/helius-429-watch) performs the
action: "engage" means quiet_set(hours=duration_s/3600, reason),
"release" means quiet_clear(reason); "hold" means do nothing.

Auto-quiet is deliberately SHORTER than the manual quiet: 1 hour,
not 12. A wrong auto-engage self-corrects at the next probe; the
manual 12h quiet (helius_ratelimit.quiet_set / quiet_report_probe
re-entry) is untouched and still the operator's lever. The public-
RPC fallback already engages automatically when Helius reads fail
(reserves.rpc); this module adds the decision + the journal, not
a new fallback. It never switches endpoints to evade a 429 — it
only REDUCES load (engage quiet) or journals a switch.

Contract:
  * Pure decision + jsonl journaling, stdlib only.
  * Every public function never raises.
  * Home resolution: explicit ``home`` wins, else
    ``FOREX_AGENT_HOME`` at call time, else ``~/.forex-agent``
    (same as lessons.py).
  * Journal writes are atomic (per-process tmp + os.replace)
    under a per-file flock, bounded to the newest 500 lines
    (same pattern as lessons.py).

Paper only. Nothing here touches real money, keys, or wallets.
"""

import fcntl
import json
import os
import time

# Engage at 5+ 429s in the last 10 minutes. NOTE: the alert watch
# (scripts/helius-429-watch / helius_ratelimit.pacer_429_spike)
# fires on MORE than 5; auto-quiet engages one 429 earlier, at
# >= 5, so the load shed starts before the alert threshold trips.
AUTO_QUIET_THRESHOLD = 5
# One hour, NOT the manual 12h: a wrong auto-engage self-corrects
# at the next probe instead of silencing the fleet for half a day.
AUTO_QUIET_DURATION_S = 3600
SWITCH_LOG_MAX = 500
SWITCH_LOG_FILE = "switch_log.jsonl"


def _home(home=None):
    if home:
        return str(home)
    return os.environ.get(
        "FOREX_AGENT_HOME",
        os.path.join(os.path.expanduser("~"), ".forex-agent"))


def switch_log_path(home=None):
    """<HOME>/switch_log.jsonl. Never raises."""
    try:
        return os.path.join(_home(home), SWITCH_LOG_FILE)
    except Exception:
        return SWITCH_LOG_FILE


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


def _quiet_active(quiet_state, now):
    """True while a quiet period is in force, mirroring
    helius_ratelimit.quiet_active: active flag AND until in the
    future. A state dict without an "until" key falls back to
    the flag alone (the caller may pass a partial snapshot).
    Never raises."""
    try:
        st = quiet_state if isinstance(quiet_state, dict) else {}
        if not st.get("active"):
            return False
        if "until" not in st:
            return True
        until = _num(st.get("until"))
        if until is None:
            return False
        return until > now
    except Exception:
        return False


def _probe_succeeded(quiet_state):
    """True when the quiet state's probe resolved OK.

    Accepts the quiet_status() shape (probe_result {"ts", "ok"})
    and the flat spellings probe_ok / probe_succeeded. Never
    raises."""
    try:
        st = quiet_state if isinstance(quiet_state, dict) else {}
        pr = st.get("probe_result")
        if isinstance(pr, dict) and pr.get("ok") is True:
            return True
        if st.get("probe_ok") is True:
            return True
        if st.get("probe_succeeded") is True:
            return True
        return False
    except Exception:
        return False


def auto_quiet_decision(rate429_last_10m, quiet_state):
    """Engage / release / hold the Helius quiet period.

    ``rate429_last_10m`` is the count scripts/helius-429-watch
    already measured (429s in the last 10 minutes, all buckets).
    ``quiet_state`` is the helius_ratelimit.quiet_status() dict
    (or any dict with "active", "until", "probe_result").

      * engage — 429s >= 5 and quiet not active. duration 3600s.
      * release — a probe succeeded and 429s < 5 (and quiet was
        active: there is nothing to release otherwise).
      * hold — everything else: below threshold, already quiet,
        probe still failing, or a probe success while 429s are
        still >= 5.

    Returns {"action", "reason", "duration_s"}; duration_s is 0
    except on engage. Never raises; garbage input fails soft
    (unparseable count reads as 0, unparseable state as not
    quiet with no probe outcome)."""
    try:
        now = time.time()
        n = _num(rate429_last_10m)
        if n is None or n < 0:
            n = 0.0
        count = int(n)
        active = _quiet_active(quiet_state, now)
        probe_ok = _probe_succeeded(quiet_state)
        if active and probe_ok and count < AUTO_QUIET_THRESHOLD:
            return {"action": "release",
                    "reason": ("probe succeeded and %d 429s in the "
                               "last 10m (< %d) — release quiet"
                               % (count, AUTO_QUIET_THRESHOLD)),
                    "duration_s": 0}
        if count >= AUTO_QUIET_THRESHOLD and not active:
            return {"action": "engage",
                    "reason": ("%d 429s in the last 10m (>= %d), "
                               "quiet not active — auto-quiet 1h"
                               % (count, AUTO_QUIET_THRESHOLD)),
                    "duration_s": AUTO_QUIET_DURATION_S}
        if active:
            if probe_ok:
                reason = ("probe succeeded but %d 429s in the last "
                          "10m still >= %d — hold quiet"
                          % (count, AUTO_QUIET_THRESHOLD))
            else:
                reason = ("quiet already active, %d 429s in the "
                          "last 10m — hold" % count)
        else:
            reason = ("%d 429s in the last 10m (< %d), quiet not "
                      "active — hold"
                      % (count, AUTO_QUIET_THRESHOLD))
        return {"action": "hold", "reason": reason, "duration_s": 0}
    except Exception:
        return {"action": "hold", "reason": "fail_open",
                "duration_s": 0}


def _read_all(home=None):
    """All switch-log records, in file order. [] if none."""
    try:
        p = switch_log_path(home)
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
    """Rewrite the switch log atomically (per-process tmp +
    os.replace, fsync). Never raises."""
    try:
        p = switch_log_path(home)
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


def log_switch(home, event):
    """Append one switch record to <HOME>/switch_log.jsonl.

    ``event`` is {"switch", "action", "reason"} (+ optional
    "ts", epoch seconds; defaults to now). The stored record is
    {"ts", "switch", "action", "reason"}. Bounded to the newest
    SWITCH_LOG_MAX (500) lines. Returns True when written, False
    otherwise. Never raises. ``home`` may be None (env/default
    resolution); a missing/unwritable home returns False."""
    try:
        ev = event if isinstance(event, dict) else {}
        switch = str(ev.get("switch") or "").strip()
        action = str(ev.get("action") or "").strip()
        if not switch or not action:
            return False
        ts = _num(ev.get("ts"))
        if ts is None:
            ts = time.time()
        rec = {"ts": ts, "switch": switch, "action": action,
               "reason": str(ev.get("reason") or "")}
        p = switch_log_path(home)
        with _file_lock(p):
            recs = _read_all(home)
            recs.append(rec)
            recs = recs[-SWITCH_LOG_MAX:]
            return _write_all(recs, home)
    except Exception:
        return False


def read_switch_log(home=None, n=50):
    """Newest-first switch records, at most ``n``. [] when none
    or unreadable. Never raises."""
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
