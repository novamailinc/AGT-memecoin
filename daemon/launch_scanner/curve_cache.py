"""Shared cross-process curve cache (2026-11-18, resilience build).

The same coin is evaluated by grad_select, grad_raw, tranche, and
tranche_protect within minutes, each spending one bounded
getSignaturesForAddress call (limit 251) on the same bonding
curve. This cache lets the later lanes reuse the first lane's raw
curve data instead of calling Helius again — up to 4x fewer curve
calls, so the pacer budget covers entries first.

Keyed by the bonding-curve PDA address. Stores RAW DATA ONLY —
signature count + the oldest signature's blockTime/signature/slot
+ fetch timestamp — never a verdict: every reader re-derives
pass/reject from the raw data through _curve_stats, so the filters
themselves are untouched. TTL 300s.

Staleness bound: signature counts only grow. A cached EXACT count
(< 251, exhausted) can be overtaken within the TTL if the coin
trades fast — the reader then sees the older, smaller count. The
risk is bounded by the 300s TTL and by the fact that evaluation
happens within minutes of graduation; a capped count (251) stays
over the bar forever. Never cache a failure (trades=None) or a
non-list result — and never cache an EMPTY response (count 0):
a real bonding curve always carries at least its create
transaction, so an empty page is a null/incomplete read, not
confirmed data; caching it would block every later lane from
retrieving the real count for the whole TTL (Phase 2).

Cache keys: the bonding-curve PDA address ONLY. That is adequate
because every fetch of this data uses the same parameters — one
bounded getSignaturesForAddress with limit CURVE_SIG_LIMIT (251)
at the transport default commitment, in grad_select._curve_stats
and _canonical_pool_from_chain alike. No caller varies the limit
or the commitment, so no parameter is missing from the key.
(Any future caller that varies them must extend the key first.)

Fail-open: any cache error (corrupt file, lock failure, bad data)
returns a miss and the caller makes a live call — the cache can
never fail an evaluation closed. File:
``~/.forex-agent/curve_cache.json``, flock-protected, atomic
tmp+os.replace in the destination directory (same pattern as the
pacer/guard). Entries pruned on write (expired + cap 500, oldest
first). Stdlib only.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from typing import Any, Dict, Optional

TTL_S = 300.0
MAX_ENTRIES = 500
_CACHE_FILE = "curve_cache.json"
_LOCK_FILE = "curve_cache.lock"


def _home(home: Optional[str] = None) -> str:
    if home:
        return home
    return os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")


class CurveCache:
    """One shared curve-data cache, cross-process via a JSON file."""

    def __init__(self, home: Optional[str] = None) -> None:
        self._home = _home(home)
        self._state_file = os.path.join(self._home, _CACHE_FILE)
        self._lock_file = os.path.join(self._home, _LOCK_FILE)

    # -- validation ----------------------------------------------------
    @staticmethod
    def _valid_entry(entry: Any, now: float) -> Optional[Dict[str, Any]]:
        """A stored entry, or None when missing/expired/malformed."""
        try:
            if not isinstance(entry, dict):
                return None
            count = entry.get("count")
            fetched = entry.get("fetched_ts")
            if not isinstance(count, int) or isinstance(count, bool):
                return None
            if count <= 0 or not isinstance(fetched, (int, float)):
                # count 0 is not confirmed data (see module docstring):
                # a legacy entry written before the Phase 2 fix must
                # not serve. A negative count was never valid.
                return None
            if now - float(fetched) > TTL_S:
                return None
            oldest = entry.get("oldest")
            if oldest is not None and not isinstance(oldest, dict):
                return None
            return {"count": count,
                    "oldest": dict(oldest) if oldest else None,
                    "fetched_ts": float(fetched)}
        except Exception:
            return None

    # -- file layer (fail-soft) ----------------------------------------
    def _read_all(self) -> Dict[str, Any]:
        try:
            with open(self._state_file, "r") as fh:
                data = json.load(fh)
            entries = (data or {}).get("entries") or {}
            return entries if isinstance(entries, dict) else {}
        except Exception:
            return {}

    def _write_all(self, entries: Dict[str, Any],
                   now: float) -> bool:
        """Prune (expired + cap, oldest first) and atomically write."""
        try:
            os.makedirs(self._home, exist_ok=True)
            lock_fh = open(self._lock_file, "a+")
        except Exception:
            return False
        try:
            locked = False
            for _ in range(5):
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except Exception:
                    time.sleep(0.01)
            if not locked:
                return False
            try:
                kept = {}
                for addr, entry in (entries or {}).items():
                    valid = self._valid_entry(entry, now)
                    if valid is not None:
                        kept[addr] = valid
                if len(kept) > MAX_ENTRIES:
                    ordered = sorted(
                        kept.items(),
                        key=lambda kv: kv[1]["fetched_ts"])
                    kept = dict(ordered[-MAX_ENTRIES:])
                tmp = "%s.tmp.%d" % (self._state_file, os.getpid())
                with open(tmp, "w") as fh:
                    json.dump({"entries": kept}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._state_file)
                return True
            finally:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)
                except Exception:
                    pass
        except Exception:
            return False
        finally:
            try:
                lock_fh.close()
            except Exception:
                pass

    # -- public API ------------------------------------------------------
    def get(self, curve_addr: str,
            now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Cached raw curve data for a bonding-curve PDA, or None
        on miss / expiry / any cache error (fail-open: the caller
        makes a live call). Never raises."""
        try:
            if not curve_addr:
                return None
            t = time.time() if now is None else now
            return self._valid_entry(
                self._read_all().get(curve_addr), t)
        except Exception:
            return None

    def put(self, curve_addr: str, count: int,
            oldest: Optional[Dict[str, Any]],
            now: Optional[float] = None) -> bool:
        """Store raw curve data (count + oldest signature dict).
        Only successful live reads are stored — callers never put
        failures, and an EMPTY response (count 0) is refused: it is
        a null/incomplete read, not confirmed data, and caching it
        would block later retrieval of the real count for the TTL
        (Phase 2). Returns False on any error or refusal (harmless:
        the next lane just makes its own live call). Never raises."""
        try:
            if not curve_addr or not isinstance(count, int) \
                    or count <= 0:
                return False
            t = time.time() if now is None else now
            entries = self._read_all()
            entries[curve_addr] = {
                "count": count,
                "oldest": dict(oldest) if oldest else None,
                "fetched_ts": t}
            return self._write_all(entries, t)
        except Exception:
            return False


_CACHE: Optional["CurveCache"] = None


def curve_cache() -> "CurveCache":
    """The shared curve-cache singleton (lazy; home from env)."""
    global _CACHE
    if _CACHE is None:
        _CACHE = CurveCache()
    return _CACHE


def reset_for_tests() -> None:
    """Test hook: drop the singleton (tests build their own)."""
    global _CACHE
    _CACHE = None
