#!/usr/bin/env python3
"""REVIEW sentinel queue for the paper-only grad lanes (advanced
build, piece 1, 2026-11-21).

Borderline (WEAK-conviction) candidates go here INSTEAD of
entering: the queue is the sentinel that watches what the
conviction bar refused, so the bar can be judged on what it
saved — not on silence.

Each lane gets ``<HOME>/review_queue/<lane>_review.jsonl`` (one
JSON record per line, bounded to the newest 200 per lane). A
record carries the mint, the full conviction score + components,
and a bounded snapshot of the measured values at decision time.

Contract:
  * Pure Python, stdlib only. Every public function never raises.
  * Writes are atomic (per-process tmp + os.replace) under a
    per-file flock, same pattern as lessons.py, so a crash or
    reboot never leaves a torn jsonl behind.
  * Idempotent per (mint, entry decision ts): enqueueing the same
    mint twice merges (never duplicates) — a candidate is
    evaluated once, its verdict cached on the watchlist row.
  * Home resolution: ``home`` passed explicitly wins; otherwise
    FOREX_AGENT_HOME at call time; otherwise ~/.forex-agent.
    Callers pass os.path.dirname(LEDGER_PATH) so temp ledgers in
    tests stay isolated from the live runtime.

Paper only. Nothing here touches real money, keys, or wallets.
"""

import fcntl
import json
import os
import time

LANES = ("grad_select", "grad_raw", "tranche", "tranche_protect")

MAX_RECORDS = 200

# Values snapshot: only the measurements that mattered, bounded.
_VALUE_KEYS = (
    "grad_age_s", "deployer_launches_24h", "curve_trades", "ttg_s",
    "ticker_others_24h", "mcap", "fdv_usd", "liquidity",
    "reserve_usd", "bear_score",
)


def _home(home=None):
    if home:
        return str(home)
    return os.environ.get(
        "FOREX_AGENT_HOME",
        os.path.join(os.path.expanduser("~"), ".forex-agent"))


def queue_dir(home=None):
    """<HOME>/review_queue, created on demand. Never raises."""
    try:
        d = os.path.join(_home(home), "review_queue")
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return os.path.join(_home(home), "review_queue")


def queue_path(lane, home=None):
    return os.path.join(queue_dir(home), "%s_review.jsonl" % lane)


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


def _read_all(lane, home=None):
    try:
        with open(queue_path(lane, home)) as fh:
            return [json.loads(line) for line in fh
                    if line.strip()]
    except Exception:
        return []


def _write_all(lane, recs, home=None):
    try:
        path = queue_path(lane, home)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as fh:
            for rec in recs[-MAX_RECORDS:]:
                fh.write(json.dumps(rec) + "\n")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _values_snapshot(values):
    snap = {}
    try:
        for key in _VALUE_KEYS:
            v = (values or {}).get(key)
            if v is None:
                continue
            if isinstance(v, dict):
                # bear_score arrives as a dict; keep the int.
                v = v.get("bear_score")
                if v is None:
                    continue
            snap[key] = v
    except Exception:
        pass
    return snap


def enqueue(lane, mint, conviction, values, symbol=None,
            source=None, now=None, home=None):
    """Append one WEAK candidate to the lane's REVIEW queue.

    Idempotent per mint: a second call for the same mint merges
    (never duplicates). Returns True when a record was written
    or merged, False otherwise. Never raises."""
    try:
        mint = str(mint or "")
        if not mint or lane not in LANES:
            return False
        now = time.time() if now is None else float(now)
        with _file_lock(queue_path(lane, home)):
            recs = _read_all(lane, home)
            for rec in recs:
                if str(rec.get("mint") or "") == mint:
                    # Merge: keep the first decision time, refresh
                    # the conviction (same candidate, same values).
                    rec["conviction"] = conviction
                    rec["values"] = _values_snapshot(values)
                    _write_all(lane, recs, home)
                    return True
            recs.append({
                "kind": "review", "lane": lane, "mint": mint,
                "symbol": str(symbol or "")[:32],
                "source": str(source or "")[:64],
                "ts": now,
                "conviction": conviction,
                "values": _values_snapshot(values),
            })
            _write_all(lane, recs, home)
            return True
    except Exception:
        return False


def list_queue(lane, home=None, n=50):
    """Newest-first review records for a lane (max n)."""
    try:
        if lane not in LANES:
            return []
        recs = _read_all(lane, home)
        recs.sort(key=lambda r: float(r.get("ts") or 0),
                  reverse=True)
        return recs[:n]
    except Exception:
        return []


def count(lane, home=None):
    """Number of records in the lane's review queue."""
    try:
        return len(_read_all(lane, home)) if lane in LANES else 0
    except Exception:
        return 0
