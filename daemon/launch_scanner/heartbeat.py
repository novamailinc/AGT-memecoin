#!/usr/bin/env python3
"""Shared realtime heartbeat (2026-10-23, user order: "add heartbeat
every two seconds").

The watchdog has always written ~/.forex-agent/run/heartbeat.json on
its ~5-minute cycle (checked_at + daemon states + actions). The
trading arm (scripts/watcher-trader) and the core exit machinery
(scripts/realtime-sync) now ALSO beat into the same file every
BEAT_INTERVAL_S seconds, under a "beats" section:

    {"checked_at": ..., "daemons": [...], "actions": ...,
     "beats": {"realtime-sync": {"ts": ..., "pid": ...,
                                 "iso": ...},
               "watcher-trader": {...}}}

Writers merge, never clobber: every write is a flock-guarded
read-modify-write with an atomic tmp+rename, preserving sections the
writer does not own (the watchdog preserves "beats"; the beaters
preserve the watchdog's checked_at/daemons/actions). Consumers read
the cadence with beats_age()/beats_fresh(): a beat older than
FRESH_S seconds means that emitter is not actually ticking at the
2-second cadence (dead, wedged, or never started).

Cheap by design: one small JSON file, writers at 2s cadence are two
processes; the lock is held for a read+write of a few hundred bytes.

Free-only: stdlib only. Never raises — a failed beat must never take
a trading loop down with it.
"""
import fcntl
import json
import os
import time

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
HEARTBEAT_PATH = os.path.join(HOME, "run", "heartbeat.json")

BEAT_INTERVAL_S = 2.0    # the user's ordered cadence
FRESH_S = 30.0           # consumers: a beat older than this is stale
                         # (15 missed beats — dead/wedged, not jitter)


def _lock_path():
    return HEARTBEAT_PATH + ".lock"


def _read_doc():
    try:
        with open(HEARTBEAT_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_doc(d):
    """Atomic write. Caller holds the flock."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
        tmp = HEARTBEAT_PATH + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, HEARTBEAT_PATH)
        return True
    except Exception:
        return False


def beat(source, extra=None):
    """Record one heartbeat tick for `source`, merging into the
    shared doc (every other section preserved). Never raises;
    returns True when the beat landed."""
    try:
        lf = open(_lock_path(), "w")
        try:
            fcntl.flock(lf, fcntl.LOCK_EX)
            d = _read_doc()
            beats = d.get("beats")
            if not isinstance(beats, dict):
                beats = {}
            rec = {"ts": time.time(), "pid": os.getpid(),
                   "iso": time.strftime(
                       "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if isinstance(extra, dict):
                rec.update(extra)
            beats[str(source)] = rec
            d["beats"] = beats
            return _write_doc(d)
        finally:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            finally:
                lf.close()
    except Exception:
        return False


def read_beats():
    """{source: beat-record} from the shared doc. Never raises."""
    try:
        beats = _read_doc().get("beats")
        return beats if isinstance(beats, dict) else {}
    except Exception:
        return {}


def beats_age(source, now=None):
    """Seconds since `source` last beat, or None when it never has.
    Never raises."""
    try:
        rec = read_beats().get(str(source)) or {}
        ts = float(rec.get("ts") or 0)
        if ts <= 0:
            return None
        return (time.time() if now is None else now) - ts
    except Exception:
        return None


def beats_fresh(source, now=None, fresh_s=FRESH_S):
    """True when `source` beat within fresh_s. Never raises."""
    age = beats_age(source, now=now)
    return age is not None and age <= fresh_s


class Beater:
    """Cadence helper for loops that tick faster than the beat
    interval: call .due() each iteration; beat when it returns True.
    The first call is always due (a fresh process proves liveness
    immediately, not 2s after boot)."""

    def __init__(self, source, interval_s=BEAT_INTERVAL_S):
        self.source = source
        self.interval_s = float(interval_s)
        self._last = 0.0

    def due(self, now=None):
        now = time.time() if now is None else now
        if now - self._last >= self.interval_s:
            self._last = now
            return True
        return False

    def tick(self, extra=None, now=None):
        """Beat if due. Returns True when a beat was emitted."""
        if self.due(now):
            return beat(self.source, extra)
        return False
