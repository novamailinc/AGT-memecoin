"""Continuous pool-reserve monitoring: durable snapshot history with
migration and abnormal-movement detection (6.2 depth).

MEASUREMENT / ADVISORY ONLY. Nothing here gates entries, scores
coins, or touches exits; no caller polls providers from this module.
Snapshots are ingested from callers that already read pool reserves
(reserves.pool_price_usd / scan_pool_vaults lanes), so the monitor
works identically on live data and on offline fixtures.

What this adds over the point-in-time reads in reserves.py:

- DURABLE HISTORY: append-only reserve_snapshots.jsonl under an
  exclusive flock, torn tail repaired under lock before append and
  counted, per-pool sample cap with labeled drops, byte-cap
  compaction, and a sidecar meta file whose counters surface through
  monitor_status(). A missing history file is unknown_missing, never
  a clean empty.
- ABNORMAL MOVEMENT: drain/spike detection against the trailing
  median of the pool's OWN recorded samples. Too few samples is an
  explicit insufficient_history label, never a fabricated baseline
  and never a verdict from thin air.
- MIGRATION DETECTION: canonical-pool transitions per mint with
  three-valued provenance: "explicit" (a snapshot carried a
  migration marker), "inferred_from_reserves" (old pool drained to
  near zero while a new pool appeared), "unrecorded" (the pool set
  changed with no supporting evidence). Both pools active after a
  transition is a labeled conflict, never silently resolved.

Provenance vocabulary matches the rest of the system: missing is not
zero, absent keys are unrecorded, invalid numbers (bool, NaN, inf,
negative reserves) are rejected at the boundary and counted, never
clamped into plausible-looking data. Out-of-order snapshots are
retained and labeled; detection always works in ts order because
append time is not the logical version.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import statistics
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("reserve_monitor")

_SCHEMA_V = 1
_MAX_SAMPLES_PER_POOL = 200      # newest kept; drops labeled in meta
_MAX_FILE_BYTES = 512 * 1024     # compaction trigger
_MIN_BASELINE_SAMPLES = 5        # below this: insufficient_history
_MAX_TS_FUTURE_SKEW_S = 300.0    # absurd future ts rejected

_DRAIN_RATIO = 0.5               # latest <= 50% of trailing median
_SPIKE_RATIO = 2.0               # latest >= 200% of trailing median
_MIGRATION_DRAIN_RATIO = 0.05    # old pool at <=5% of its peak = drained


def _dir(home: Optional[str] = None) -> Path:
    base = Path(home) if home else Path(
        os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
    return base / "launch_scanner"


def _file(home: Optional[str] = None) -> Path:
    return _dir(home) / "reserve_snapshots.jsonl"


def _lock_file(home: Optional[str] = None) -> Path:
    return _dir(home) / "reserve_snapshots.lock"


def _meta_file(home: Optional[str] = None) -> Path:
    return _dir(home) / "reserve_snapshots_meta.json"


def _is_num(v: Any) -> bool:
    """True only for a real finite number: bool is not a count and
    NaN/inf can never be ordered, median-ed, or merged safely."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v not in (float("inf"), float("-inf")))


@contextmanager
def _lock(home: Optional[str] = None):
    _dir(home).mkdir(parents=True, exist_ok=True)
    f = open(_lock_file(home), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield f
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _meta_read(home: Optional[str] = None) -> Dict[str, Any]:
    try:
        d = json.loads(_meta_file(home).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _meta_write(meta: Dict[str, Any], home: Optional[str] = None) -> None:
    try:
        p = _meta_file(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta))
        os.chmod(tmp, 0o600)
        tmp.replace(p)
    except OSError:
        pass


def _meta_bump(key: str, n: int = 1, home: Optional[str] = None) -> None:
    """Increment a persistent meta counter under the lock. Best-effort;
    never raises. Caller must already hold the lock OR accept the tiny
    race on a diagnostic counter."""
    try:
        meta = _meta_read(home)
        meta[key] = int(meta.get(key) or 0) + n
        _meta_write(meta, home)
    except Exception:
        pass


def _repair_torn_tail(home: Optional[str] = None) -> int:
    """Truncate an unterminated final line (crash mid-append must not
    glue the next record onto it). Returns bytes removed."""
    p = _file(home)
    try:
        data = p.read_bytes()
    except OSError:
        return 0
    if not data or data.endswith(b"\n"):
        return 0
    cut = data.rfind(b"\n")
    keep = data[:cut + 1] if cut >= 0 else b""
    try:
        p.write_bytes(keep)
    except OSError:
        return 0
    return len(data) - len(keep)


def _valid_amounts(base: Any, quote: Any) -> bool:
    return (_is_num(base) and _is_num(quote)
            and base >= 0 and quote >= 0)


def record_snapshot(pool: Any, mint: Any, ts: Any, base_amount: Any,
                    quote_amount: Any, source: str = "caller",
                    migration_event: Optional[str] = None,
                    home: Optional[str] = None) -> bool:
    """Append one reserve snapshot. Returns False (fail-closed,
    counted) on any invalid input; never clamps, never raises.

    migration_event: optional caller-supplied marker (e.g.
    "pumpswap_graduation") making a later pool transition explicit
    evidence rather than inference."""
    if not isinstance(pool, str) or not pool:
        _meta_bump("snapshots_rejected_invalid", home=home)
        return False
    if not isinstance(mint, str) or not mint:
        _meta_bump("snapshots_rejected_invalid", home=home)
        return False
    if not _is_num(ts) or ts <= 0 or ts > time.time() + _MAX_TS_FUTURE_SKEW_S:
        _meta_bump("snapshots_rejected_invalid", home=home)
        return False
    if not _valid_amounts(base_amount, quote_amount):
        _meta_bump("snapshots_rejected_invalid", home=home)
        return False
    rec = {"v": _SCHEMA_V, "record_type": "reserve_snapshot",
           "pool": pool, "mint": mint, "ts": float(ts),
           "base_amount": float(base_amount),
           "quote_amount": float(quote_amount), "source": source}
    if migration_event:
        rec["migration_event"] = str(migration_event)[:64]
    line = json.dumps(rec) + "\n"
    try:
        with _lock(home):
            removed = _repair_torn_tail(home)
            if removed:
                _meta_bump("torn_tail_repairs", home=home)
                _meta_bump("torn_tail_bytes_removed", removed, home=home)
            with open(_file(home), "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            _meta_bump("snapshots_recorded", home=home)
            _compact_if_needed(home)
        return True
    except OSError as exc:
        logger.warning("reserve snapshot append failed: %s", exc)
        _meta_bump("append_failures", home=home)
        return False


def _read_records(home: Optional[str] = None):
    """Returns (records, unreadable_count, missing)."""
    p = _file(home)
    if not p.exists():
        return [], 0, True
    records, unreadable = [], 0
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    unreadable += 1
                    continue
                if (isinstance(rec, dict)
                        and rec.get("record_type") == "reserve_snapshot"
                        and isinstance(rec.get("pool"), str)
                        and _is_num(rec.get("ts"))
                        and _valid_amounts(rec.get("base_amount"),
                                           rec.get("quote_amount"))):
                    records.append(rec)
                else:
                    unreadable += 1
    except OSError:
        return [], 0, True
    return records, unreadable, False


def _compact_if_needed(home: Optional[str] = None) -> None:
    """Bound the file: per-pool newest _MAX_SAMPLES_PER_POOL samples
    and a hard byte cap. Dropped samples are counted, never silent.
    Caller holds the lock."""
    p = _file(home)
    try:
        if p.stat().st_size <= _MAX_FILE_BYTES:
            return
    except OSError:
        return
    records, unreadable, missing = _read_records(home)
    if missing:
        return
    by_pool: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        by_pool.setdefault(rec["pool"], []).append(rec)
    kept, dropped = [], 0
    for pool, recs in by_pool.items():
        recs.sort(key=lambda r: r["ts"])
        overflow = len(recs) - _MAX_SAMPLES_PER_POOL
        if overflow > 0:
            dropped += overflow
            recs = recs[overflow:]
        kept.extend(recs)
    kept.sort(key=lambda r: r["ts"])
    tmp = p.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            for rec in kept:
                f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        tmp.replace(p)
    except OSError as exc:
        logger.warning("reserve snapshot compaction failed: %s", exc)
        _meta_bump("compaction_failures", home=home)
        try:
            tmp.unlink()
        except OSError:
            pass
        return
    _meta_bump("compactions", home=home)
    if dropped:
        _meta_bump("samples_dropped_by_cap", dropped, home=home)
    if unreadable:
        _meta_bump("compaction_dropped_unreadable", unreadable, home=home)


def pool_history(pool: str, home: Optional[str] = None) -> Dict[str, Any]:
    """All valid samples for one pool in ts order, with honest
    availability: unknown_missing when no history file exists (absence
    cannot prove zero history)."""
    records, unreadable, missing = _read_records(home)
    if missing:
        return {"status": "unknown_missing", "samples": [],
                "unreadable": 0}
    samples = sorted((r for r in records if r["pool"] == pool),
                     key=lambda r: r["ts"])
    out_of_order = _count_out_of_order(records, pool)
    return {"status": "ok", "samples": samples,
            "unreadable": unreadable,
            "out_of_order_appends": out_of_order}


def _count_out_of_order(records: List[Dict[str, Any]], pool: str) -> int:
    last = None
    n = 0
    for rec in records:  # append order
        if rec["pool"] != pool:
            continue
        if last is not None and rec["ts"] < last:
            n += 1
        last = max(last, rec["ts"]) if last is not None else rec["ts"]
    return n


def detect_abnormal(pool: str, home: Optional[str] = None,
                    drain_ratio: float = _DRAIN_RATIO,
                    spike_ratio: float = _SPIKE_RATIO,
                    min_baseline: int = _MIN_BASELINE_SAMPLES
                    ) -> Dict[str, Any]:
    """Latest sample vs the trailing median of the pool's own recorded
    quote reserves. insufficient_history when the baseline is too
    thin - never a fabricated reference. Measurement only."""
    hist = pool_history(pool, home=home)
    if hist["status"] != "ok":
        return {"status": hist["status"], "movement": None,
                "provenance": "no_history_file"}
    samples = hist["samples"]
    if len(samples) < min_baseline + 1:
        return {"status": "insufficient_history", "movement": None,
                "samples": len(samples),
                "provenance": "recorded_snapshots_too_thin_for_baseline"}
    baseline_vals = [s["quote_amount"] for s in samples[:-1]]
    median = statistics.median(baseline_vals)
    latest = samples[-1]
    if median <= 0:
        # A zero/empty baseline says nothing about the latest value.
        return {"status": "baseline_unusable", "movement": None,
                "provenance": "zero_median_baseline_not_evidence"}
    ratio = latest["quote_amount"] / median
    movement = None
    if ratio <= drain_ratio:
        movement = {"kind": "drain", "ratio": ratio,
                    "latest_ts": latest["ts"],
                    "baseline_median": median,
                    "baseline_samples": len(baseline_vals)}
    elif ratio >= spike_ratio:
        movement = {"kind": "spike", "ratio": ratio,
                    "latest_ts": latest["ts"],
                    "baseline_median": median,
                    "baseline_samples": len(baseline_vals)}
    return {"status": "ok", "movement": movement, "ratio": ratio,
            "provenance": "recorded_snapshots_trailing_median"}


def detect_migration(mint: str, home: Optional[str] = None
                     ) -> Dict[str, Any]:
    """Canonical-pool transitions for one mint, with per-transition
    provenance: explicit > inferred_from_reserves > unrecorded.
    Both pools active after a transition is a labeled conflict."""
    records, unreadable, missing = _read_records(home)
    if missing:
        return {"status": "unknown_missing", "transitions": []}
    by_pool: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        if rec["mint"] == mint:
            by_pool.setdefault(rec["pool"], []).append(rec)
    if not by_pool:
        return {"status": "no_history", "transitions": []}
    for recs in by_pool.values():
        recs.sort(key=lambda r: r["ts"])
    first_ts = {p: recs[0]["ts"] for p, recs in by_pool.items()}
    pools_by_appearance = sorted(by_pool, key=lambda p: first_ts[p])
    transitions = []
    for old, new in zip(pools_by_appearance, pools_by_appearance[1:]):
        old_recs, new_recs = by_pool[old], by_pool[new]
        new_first = first_ts[new]
        explicit = any(r.get("migration_event") for r in
                       old_recs + new_recs)
        old_peak = max(r["quote_amount"] for r in old_recs)
        old_after = [r for r in old_recs if r["ts"] >= new_first]
        old_drained = bool(old_after) and old_peak > 0 and (
            min(r["quote_amount"] for r in old_after)
            <= _MIGRATION_DRAIN_RATIO * old_peak)
        # Both pools carrying samples after the transition is a
        # contradiction: label it, never silently pick one.
        conflict = bool(old_after) and not old_drained
        if explicit:
            prov = "explicit"
        elif old_drained:
            prov = "inferred_from_reserves"
        else:
            prov = "unrecorded"
        transitions.append({
            "from_pool": old, "to_pool": new,
            "to_pool_first_seen": new_first,
            "provenance": prov,
            "old_pool_drained": old_drained,
            "conflict_both_active": conflict})
    return {"status": "ok", "transitions": transitions,
            "pools_seen": len(by_pool), "unreadable": unreadable}


def monitor_status(home: Optional[str] = None) -> Dict[str, Any]:
    """Availability + counters. missing meta = unknown_missing for the
    counter history, not zero."""
    meta_exists = _meta_file(home).exists()
    file_exists = _file(home).exists()
    meta = _meta_read(home)
    return {"history_file": "present" if file_exists else "missing",
            "meta": "present" if meta_exists else "unknown_missing",
            "counters": meta}
