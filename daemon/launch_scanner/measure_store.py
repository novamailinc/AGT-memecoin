"""Durable persistence for the measurement ledgers (2026-10-09, phase 1a).

Problem: ``proof.measure()`` and ``footprints.measure()`` record into
``state["proof_checks"]`` / ``state["footprint_signals"]``, which ride in
the ``daemon_state`` journal blob. On a reboot the journal read can come
back empty (``get_daemon_state`` returns {} on any read error); the
throttled save then persists the empty blob and the keep=10 prune ages
the populated rows out — the measurement ledger is wiped (observed:
28 records -> 2).

This module is the backstop: the same two ledgers are persisted as JSON
under the runtime home (``<home>/launch_scanner/measurement.json``) and
merged back into the in-memory state whenever the blob's ledgers are
missing or empty.

Contract:
  - Write-through from the measure call sites, throttled internally to
    at most one disk write per ~60s (crash staleness < ~5 min).
  - Restore-on-load in ``run_once()``: per-mint newest-ts-wins merge,
    only when the blob's ledger is missing/empty.
  - Anti-clobber guard before every ``save_daemon_state``: NEVER persist
    an empty measurement ledger over a populated one.
  - 0600 file permissions (consistent with smart_wallets.json and other
    private runtime files); atomic writes (temp file + os.replace), so
    a crash can never leave corrupt/partial data.
  - Measurement behavior is UNCHANGED: same keys, same 500-entry bound,
    same 7-day TTL, same call sites. ``proof.measure()`` /
    ``footprints.measure()`` keep writing the in-memory ledgers exactly
    as today, so the verdict analyzer keeps reading the journal blob.

Never raises: every public function catches its own exceptions, because
measurement plumbing must never break a scanner cycle.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("measure_store")

LEDGER_KEYS = ("proof_checks", "footprint_signals")
_SUBDIR = "launch_scanner"
_FILENAME = "measurement.json"

# Mirrors proof._LEDGER_TTL_S / footprints._LEDGER_TTL_S (7 days) and
# proof._MAX_ENTRIES / footprints._MAX_ENTRIES (500). Duplicated here so
# this module never imports the measurement modules (import order).
_LEDGER_TTL_S = 7 * 24 * 3600
_MAX_ENTRIES = 500

# Write-through throttle: at most one disk write per path per gap.
_WRITE_GAP_S = 60.0
_last_write: Dict[str, float] = {}


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _ts(rec: Any) -> float:
    return _f(rec.get("ts")) if isinstance(rec, dict) else 0.0


def _testing() -> bool:
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def default_path() -> str:
    """Durable store path under the runtime home."""
    from daemon.common import get_home  # noqa: PLC0415 (lazy: init order)
    return os.path.join(get_home(), _SUBDIR, _FILENAME)


def _resolve_path(path: Optional[str]) -> Optional[str]:
    """Explicit paths always win (even under tests); the default path is
    never resolved under the unit-test suite, so tests can't touch the
    real runtime tree unless they pass an explicit tmp path."""
    if path is not None:
        return str(path)
    if _testing():
        return None
    try:
        return default_path()
    except Exception:
        logger.exception("measure_store: could not resolve home")
        return None


def load(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Read the durable ledgers, TTL-pruned. Returns {} on any failure —
    a missing/corrupt file is not an error, it just means no backstop."""
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
        logger.warning("measure_store: unreadable store at %s", p)
        return out
    except Exception:
        logger.exception("measure_store: load failed")
        return out
    if not isinstance(data, dict):
        return out
    cutoff = time.time() - _LEDGER_TTL_S
    for key in LEDGER_KEYS:
        ledger = data.get(key)
        if not isinstance(ledger, dict):
            continue
        fresh = {str(m): r for m, r in ledger.items()
                 if isinstance(r, dict) and _ts(r) >= cutoff}
        if fresh:
            out[key] = fresh
    return out


def save(path: Optional[str], ledgers: Dict[str, Any]) -> bool:
    """Atomic, 0600 write of the ledgers. True on success. Never raises.

    Serializes fully BEFORE touching the filesystem, writes to a temp
    file in the same directory, then os.replace()s it over the target —
    a crash can never leave a partial/corrupt measurement.json."""
    p = _resolve_path(path)
    if not p:
        return False
    try:
        payload = {}
        for key in LEDGER_KEYS:
            ledger = (ledgers or {}).get(key)
            if isinstance(ledger, dict):
                payload[key] = {str(m): r for m, r in ledger.items()
                                if isinstance(r, dict)}
        blob = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    except Exception:
        logger.exception("measure_store: serialize failed")
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
        logger.exception("measure_store: write failed")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def merge_newest(blob: Dict[str, Any],
                 disk: Dict[str, Any]) -> Dict[str, Any]:
    """Per-mint newest-ts-wins merge of two ledgers."""
    out = dict(blob) if isinstance(blob, dict) else {}
    for mint, rec in (disk or {}).items():
        if not isinstance(rec, dict):
            continue
        cur = out.get(mint)
        if not isinstance(cur, dict) or _ts(rec) > _ts(cur):
            out[mint] = rec
    return out


def restore(state: Dict[str, Any],
            path: Optional[str] = None) -> bool:
    """Merge durable records into an empty/missing in-memory ledger.

    Only acts when the blob's ledger is missing or empty — live
    in-memory data is never overwritten. Used both at load time
    (run_once, after the journal read) and as the anti-clobber guard
    right before every save_daemon_state: NEVER persist an empty
    measurement ledger over a populated one.

    Returns True when any ledger gained records. Never raises.
    """
    try:
        if not isinstance(state, dict):
            return False
        disk = load(path)
        if not disk:
            return False
        changed = False
        for key in LEDGER_KEYS:
            blob = state.get(key)
            if (not isinstance(blob, dict) or not blob) and disk.get(key):
                state[key] = merge_newest(
                    blob if isinstance(blob, dict) else {}, disk[key])
                changed = True
        return changed
    except Exception:
        logger.exception("measure_store: restore failed")
        return False


def maybe_persist(state: Dict[str, Any],
                  path: Optional[str] = None,
                  now: Optional[float] = None) -> bool:
    """Throttled write-through of the in-memory ledgers to the durable
    store. At most one disk write per ~60s per path (crash staleness
    stays under ~5 minutes).

    Anti-clobber: the incoming ledgers are merged (per-mint
    newest-ts-wins) against the existing file instead of overwriting
    it, so an empty in-memory ledger can never wipe a populated store.
    Stale (>7d) records are pruned and each ledger is re-bounded to
    500, mirroring proof/footprints measure(). Unchanged content is
    not rewritten. Never raises.
    """
    try:
        p = _resolve_path(path)
        if p is None or not isinstance(state, dict):
            return False
        now = time.time() if now is None else float(now)
        if now - _last_write.get(p, 0.0) < _WRITE_GAP_S:
            return False
        existing = load(p)
        # Normalize: load() drops empty ledgers, but merged always
        # carries both keys — normalize before the equality check so
        # unchanged content is not rewritten.
        existing = {k: existing.get(k, {}) for k in LEDGER_KEYS}
        cutoff = now - _LEDGER_TTL_S
        merged: Dict[str, Dict[str, Any]] = {}
        for key in LEDGER_KEYS:
            incoming = state.get(key)
            incoming = dict(incoming) if isinstance(incoming, dict) else {}
            m = merge_newest(existing.get(key, {}), incoming)
            m = {mint: r for mint, r in m.items()
                 if isinstance(r, dict) and _ts(r) >= cutoff}
            if len(m) > _MAX_ENTRIES:
                keep = sorted(m, key=lambda k: _ts(m[k]))[-_MAX_ENTRIES:]
                m = {k: m[k] for k in keep}
            merged[key] = m
        if merged == existing:
            _last_write[p] = now
            return False
        if save(p, merged):
            _last_write[p] = now
            return True
        return False
    except Exception:
        logger.exception("measure_store: persist failed")
        return False
