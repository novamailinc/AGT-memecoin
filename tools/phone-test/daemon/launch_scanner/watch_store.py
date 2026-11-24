"""Durable backstop for the milestone watch (2026-10-09, miss-catch rebuild).

Problem: ``run_once()`` reloads the daemon state blob from the journal on
every cycle while the full save is throttled to ``state_save_interval_seconds``
(300s). A milestone record added between saves is dropped on the next cycle's
reload — verified 2026-10-09: 3 mints tracked at 21:42–21:48 were absent from
the watch saved at 22:41. The slow-cooker (late-runner catcher) reads this
watch, so it silently missed coins like QUBIT (tracked 22:13, record lost
~22:14, slow-cooker window 22:58–01:13 never saw it).

Contract (mirrors measure_store.py):
  - persist(state): immediate write of ``state["milestone_watch"]`` to
    ``<home>/launch_scanner/watch.json``. Called from run_once's finally
    block only when watch membership changed — same-cycle durability
    without the 6MB blob write amplification.
  - restore(state): per-mint newest-card_ts-wins merge of the durable
    records into the in-memory watch. Called at the top of run_once after
    the journal read. Expired (>24h) records are never resurrected.
  - 0600 permissions, atomic writes (temp + os.replace), never raises:
    watch plumbing must never break a scanner cycle.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("watch_store")

_WATCH_KEY = "milestone_watch"
_SUBDIR = "launch_scanner"
_FILENAME = "watch.json"

# Mirrors milestones._EXPIRY_S (24h). Duplicated so this module never
# imports milestones (import order).
_EXPIRY_S = 24 * 3600


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _card_ts(rec: Any) -> float:
    return _f(rec.get("card_ts")) if isinstance(rec, dict) else 0.0


def _testing() -> bool:
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def default_path() -> str:
    """Durable store path under the runtime home."""
    from daemon.common import get_home  # noqa: PLC0415 (lazy: init order)
    return os.path.join(get_home(), _SUBDIR, _FILENAME)


def _resolve_path(path: Optional[str]) -> Optional[str]:
    """Explicit paths always win (even under tests); the default path is
    never resolved under the unit-test suite."""
    if path is not None:
        return str(path)
    if _testing():
        return None
    try:
        return default_path()
    except Exception:
        logger.exception("watch_store: could not resolve home")
        return None


def membership(state: Dict[str, Any]) -> Set[str]:
    """The set of tracked mints — cheap change detector for run_once."""
    try:
        w = (state or {}).get(_WATCH_KEY)
        if isinstance(w, dict):
            return set(w.keys())
    except Exception:
        pass
    return set()


def load(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Read the durable watch, expiry-pruned. {} on any failure."""
    out: Dict[str, Dict[str, Any]] = {}
    p = _resolve_path(path)
    if not p:
        return out
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return out
    except (OSError, ValueError):
        logger.warning("watch_store: unreadable store at %s", p)
        return out
    except Exception:
        logger.exception("watch_store: load failed")
        return out
    if not isinstance(data, dict):
        return out
    watch = data.get(_WATCH_KEY)
    if not isinstance(watch, dict):
        return out
    now = time.time()
    for mint, rec in watch.items():
        if not isinstance(rec, dict):
            continue
        # Never resurrect expired records: a 24h-expired coin stays dead.
        if now - _card_ts(rec) > _EXPIRY_S:
            continue
        out[str(mint)] = rec
    return out


def persist(state: Dict[str, Any],
            path: Optional[str] = None) -> bool:
    """Atomic, 0600 write of the current in-memory watch. True on
    success. Never raises. Serializes fully before touching the
    filesystem (temp file + os.replace)."""
    p = _resolve_path(path)
    if p is None or not isinstance(state, dict):
        return False
    try:
        watch = state.get(_WATCH_KEY)
        payload = {_WATCH_KEY: {str(m): r for m, r in watch.items()
                                if isinstance(r, dict)}} \
            if isinstance(watch, dict) else {_WATCH_KEY: {}}
        blob = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except Exception:
        logger.exception("watch_store: serialize failed")
        return False
    tmp = "%s.tmp.%d" % (p, os.getpid())
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        return True
    except Exception:
        logger.exception("watch_store: write failed")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def restore(state: Dict[str, Any],
            path: Optional[str] = None) -> bool:
    """Merge durable watch records into the in-memory watch.

    Per-mint newest-card_ts-wins: a durable record fills in for a mint
    the journal blob lost (the reload-every-cycle gap), but never
    overwrites a live in-memory record that is newer. Returns True when
    the watch gained records. Never raises.
    """
    try:
        if not isinstance(state, dict):
            return False
        disk = load(path)
        if not disk:
            return False
        watch = state.get(_WATCH_KEY)
        if not isinstance(watch, dict):
            watch = {}
            state[_WATCH_KEY] = watch
        changed = False
        for mint, rec in disk.items():
            cur = watch.get(mint)
            if not isinstance(cur, dict) or _card_ts(rec) > _card_ts(cur):
                watch[mint] = rec
                changed = True
        return changed
    except Exception:
        logger.exception("watch_store: restore failed")
        return False
