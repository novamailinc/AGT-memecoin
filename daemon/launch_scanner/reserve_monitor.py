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
  and never a verdict from thin air. Every result carries the last
  observed timestamp and its age at query time; the monitor imposes
  no freshness policy of its own.
- MIGRATION DETECTION: canonical-pool transitions per mint, causal
  and three-valued. A transition old->new is anchored at the new
  pool's first observed ts. The old pool's baseline uses ONLY
  pre-transition samples (no look-ahead); its post-transition state
  is the LATEST observation, so a drained-then-recovered pool is a
  labeled conflict, not a silent pass. Explicit recorded event evidence
  requires both origin and destination plus event ID and event time.
  No runtime event producer exists yet. Bare and destination-only
  markers are unverified associations, never proof. Pool appearance
  is not canonical identity, and reserve-based inference remains an
  unvalidated advisory heuristic.

Units: amounts are stored exactly as the caller observed them (raw
vault amounts for the armed_watch lane); quote_mint / decimals /
slot / per-side observation times are recorded only when the caller
supplies them - absent means unrecorded, never assumed. The
drain/spike/migration ratios and the baseline minimum are
UNVALIDATED ADVISORY HEURISTICS (labeled as such in every result);
no trading threshold approval exists or is claimed.

Retention honesty: the byte cap is a compaction TRIGGER, not a hard
bound - compaction keeps the per-pool newest samples, which can
still exceed the byte cap across many pools. Dropped samples and
dropped unreadable lines are counted in the sidecar meta and
propagated into EVERY history/detector result (retention block);
corruption never vanishes silently into a separate status. Meta
writes are best-effort without fsync; a meta write failure loses
counters, never data.

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
_MIGRATION_DRAIN_RATIO = 0.05    # old pool latest <=5% of causal peak
_THRESHOLDS_PROVENANCE = "unvalidated_advisory_heuristic"


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


def _meta_int(meta: Dict[str, Any], key: str) -> Any:
    """Clean non-bool int from the sidecar, else None (corrupt value:
    isolated by the caller, never coerced into a plausible count and
    never allowed to crash a read)."""
    v = meta.get(key)
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    if v is None:
        return 0
    return None


def _meta_bump(key: str, n: int = 1, home: Optional[str] = None) -> None:
    """Increment a persistent meta counter under the lock. Best-effort;
    never raises. A corrupt stored value is reset to the increment and
    counted, never propagated."""
    try:
        meta = _meta_read(home)
        # Any corrupt stored counter is reset and counted, never
        # propagated or coerced into a plausible count.
        resets = 0
        for k in list(meta):
            if k == "meta_values_reset":
                continue
            if _meta_int(meta, k) is None:
                meta[k] = 0
                resets += 1
        if resets:
            meta["meta_values_reset"] = (
                _meta_int(meta, "meta_values_reset") or 0) + resets
        meta[key] = (_meta_int(meta, key) or 0) + n
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
                    migration_to: Optional[str] = None,
                    migration_from: Optional[str] = None,
                    migration_event_id: Optional[str] = None,
                    migration_event_ts: Optional[float] = None,
                    quote_mint: Optional[str] = None,
                    decimals: Optional[int] = None,
                    slot: Optional[int] = None,
                    base_ts: Optional[float] = None,
                    quote_ts: Optional[float] = None,
                    home: Optional[str] = None) -> bool:
    """Append one reserve snapshot. Returns False (fail-closed,
    counted) on any invalid input; never clamps, never raises.

    Amounts are stored exactly as observed (raw vault amounts for the
    armed_watch lane). quote_mint / decimals / slot / per-side
    observation times (base_ts, quote_ts, which can legitimately
    differ when one side comes from a push and the other from an
    earlier read) are recorded only when supplied; absent means
    unrecorded. A supplied-but-invalid optional field rejects the
    whole record - half-grounded context is worse than none.

    migration_event: optional caller-supplied marker (e.g.
    "pumpswap_graduation"). Explicit recorded evidence requires
    migration_from, migration_to, migration_event_id and event time.
    Proximity and destination-only markers are unverified associations.
    No runtime producer or independent event verification is implied."""
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
    if quote_mint is not None and not isinstance(quote_mint, str):
        _meta_bump("snapshots_rejected_invalid", home=home)
        return False
    for identity in (migration_from, migration_to, migration_event_id):
        if identity is not None and (not isinstance(identity, str)
                                     or not identity.strip()):
            _meta_bump("snapshots_rejected_invalid", home=home)
            return False
    if migration_event_ts is not None and (
            not _is_num(migration_event_ts)
            or not 0 < migration_event_ts <= ts):
        _meta_bump("snapshots_rejected_invalid", home=home)
        return False
    for opt in (decimals, slot):
        if opt is not None and (
                not isinstance(opt, int) or isinstance(opt, bool)
                or opt < 0):
            _meta_bump("snapshots_rejected_invalid", home=home)
            return False
    for opt_ts in (base_ts, quote_ts):
        if opt_ts is not None and (not _is_num(opt_ts) or opt_ts <= 0):
            _meta_bump("snapshots_rejected_invalid", home=home)
            return False
    rec = {"v": _SCHEMA_V, "record_type": "reserve_snapshot",
           "pool": pool, "mint": mint, "ts": float(ts),
           "base_amount": float(base_amount),
           "quote_amount": float(quote_amount), "source": source}
    if migration_event:
        rec["migration_event"] = str(migration_event)[:64]
    if migration_to:
        rec["migration_to"] = migration_to
    if migration_from:
        rec["migration_from"] = migration_from
    if migration_event_id:
        rec["migration_event_id"] = migration_event_id
    if migration_event_ts is not None:
        rec["migration_event_ts"] = migration_event_ts
    if quote_mint:
        rec["quote_mint"] = quote_mint
    if decimals is not None:
        rec["decimals"] = decimals
    if slot is not None:
        rec["slot"] = slot
    if base_ts is not None:
        rec["base_ts"] = float(base_ts)
    if quote_ts is not None:
        rec["quote_ts"] = float(quote_ts)
    if base_ts is not None and quote_ts is not None:
        # Skew exposed, never freshness-qualified: the consumer
        # judges whether the pair is fresh enough for its purpose.
        rec["pair_skew_s"] = abs(float(base_ts) - float(quote_ts))
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
                        and rec.get("pool")
                        and isinstance(rec.get("mint"), str)
                        and rec.get("mint")
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
    """Bound growth: per-pool newest _MAX_SAMPLES_PER_POOL samples,
    applied when the byte-cap trigger fires compaction. Neither is a
    hard bound - many pools can keep the file past the byte cap, and
    a pool's sample count is bounded only at compaction time.
    Dropped samples are counted, never silent.
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


def _retention(home: Optional[str] = None,
               unreadable_this_read: int = 0) -> Dict[str, Any]:
    """Retention/corruption provenance propagated into EVERY result:
    cumulative sidecar counters plus this read's unreadable lines.
    partial=True whenever any history was ever dropped, unreadable,
    torn, or the sidecar itself held corrupt values - no consumer can
    read a damaged history as clean. Never raises on a corrupt
    sidecar: corrupt values are isolated and labeled, not coerced."""
    try:
        meta = _meta_read(home)
    except Exception:
        meta = {}
    dropped_samples = _meta_int(meta, "samples_dropped_by_cap")
    dropped_unreadable = _meta_int(meta, "compaction_dropped_unreadable")
    torn = _meta_int(meta, "torn_tail_repairs")
    resets = _meta_int(meta, "meta_values_reset")
    corrupt = any(v is None for v in (
        dropped_samples, dropped_unreadable, torn, resets))
    dropped_samples = dropped_samples or 0
    dropped_unreadable = dropped_unreadable or 0
    torn = torn or 0
    resets = resets or 0
    # A past reset is PERSISTENT uncertainty: the counter history was
    # damaged and repaired, so partial never flips back to clean.
    return {"unreadable_this_read": unreadable_this_read,
            "samples_dropped_by_cap_total": dropped_samples,
            "compaction_dropped_unreadable_total": dropped_unreadable,
            "torn_tail_repairs_total": torn,
            "meta_values_reset_total": resets,
            "meta_corrupt": corrupt or resets > 0,
            "partial": bool(dropped_samples or dropped_unreadable
                            or torn or unreadable_this_read
                            or corrupt or resets)}


def pool_history(pool: str, home: Optional[str] = None) -> Dict[str, Any]:
    """All valid samples for one pool in ts order, with honest
    availability: unknown_missing when no history file exists (absence
    cannot prove zero history)."""
    records, unreadable, missing = _read_records(home)
    if missing:
        return {"status": "unknown_missing", "samples": [],
                "unreadable": 0,
                "retention": _retention(home)}
    samples = sorted((r for r in records if r["pool"] == pool),
                     key=lambda r: r["ts"])
    out_of_order = _count_out_of_order(records, pool)
    return {"status": "ok", "samples": samples,
            "unreadable": unreadable,
            "out_of_order_appends": out_of_order,
            "last_observed_ts": samples[-1]["ts"] if samples else None,
            "retention": _retention(home, unreadable)}


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
                    min_baseline: int = _MIN_BASELINE_SAMPLES,
                    now: Optional[float] = None
                    ) -> Dict[str, Any]:
    """Latest sample vs the trailing median of the pool's own recorded
    quote reserves. insufficient_history when the baseline is too
    thin - never a fabricated reference. Carries last observed ts and
    its age at query time; the monitor imposes no freshness policy.
    The ratios are unvalidated advisory heuristics. Measurement only."""
    now = time.time() if now is None else now
    hist = pool_history(pool, home=home)
    retention = hist["retention"]
    if hist["status"] != "ok":
        return {"status": hist["status"], "movement": None,
                "provenance": "no_history_file",
                "retention": retention}
    samples = hist["samples"]
    last_ts = hist["last_observed_ts"]
    age = (now - last_ts) if last_ts is not None else None
    if len(samples) < min_baseline + 1:
        return {"status": "insufficient_history", "movement": None,
                "samples": len(samples),
                "provenance": "recorded_snapshots_too_thin_for_baseline",
                "last_observed_ts": last_ts, "observed_age_s": age,
                "thresholds_provenance": _THRESHOLDS_PROVENANCE,
                "retention": retention}
    baseline_vals = [s["quote_amount"] for s in samples[:-1]]
    median = statistics.median(baseline_vals)
    latest = samples[-1]
    if median <= 0:
        # A zero/empty baseline says nothing about the latest value.
        return {"status": "baseline_unusable", "movement": None,
                "provenance": "zero_median_baseline_not_evidence",
                "last_observed_ts": last_ts, "observed_age_s": age,
                "thresholds_provenance": _THRESHOLDS_PROVENANCE,
                "retention": retention}
    ratio = latest["quote_amount"] / median
    movement = None
    if ratio <= drain_ratio:
        movement = {"kind": "drain", "ratio": ratio,
                    "latest_ts": latest["ts"],
                    "baseline_median": median,
                    "baseline_samples": len(baseline_vals),
                    "baseline_span_s": samples[-2]["ts"] - samples[0]["ts"]}
    elif ratio >= spike_ratio:
        movement = {"kind": "spike", "ratio": ratio,
                    "latest_ts": latest["ts"],
                    "baseline_median": median,
                    "baseline_samples": len(baseline_vals),
                    "baseline_span_s": samples[-2]["ts"] - samples[0]["ts"]}
    return {"status": "ok", "movement": movement, "ratio": ratio,
            "provenance": "recorded_snapshots_trailing_median",
            "last_observed_ts": last_ts, "observed_age_s": age,
            "thresholds_provenance": _THRESHOLDS_PROVENANCE,
            "retention": retention}


def _explicit_transitions(pairs, by_pool, first_ts):
    """Recorded bound events only, never proximity as proof.

    No runtime marker producer exists yet. A caller must supply event
    identity, event time, and BOTH origin and destination. This is
    recorded caller evidence, not independent on-chain verification.
    Bare/destination-only markers remain unverified associations.
    """
    explicit, association = set(), set()
    for i, (old, new) in enumerate(pairs):
        for r in by_pool[old] + by_pool[new]:
            if not r.get("migration_event"):
                continue
            event_ts = r.get("migration_event_ts")
            if (r.get("migration_from") == old
                    and r.get("migration_to") == new
                    and isinstance(r.get("migration_event_id"), str)
                    and r["migration_event_id"].strip()
                    and _is_num(event_ts) and 0 < event_ts <= r["ts"]):
                explicit.add(i)
            else:
                association.add(i)
    return explicit, association


def detect_migration(mint: str, home: Optional[str] = None,
                     now: Optional[float] = None) -> Dict[str, Any]:
    """Canonical-pool transitions for one mint, causal and
    three-valued. Baselines use only pre-transition samples (no
    look-ahead); post-transition state is the LATEST observation, so
    a drained-then-recovered old pool is a labeled conflict. Pool
    appearance alone is never canonical proof: absent causal or
    explicit evidence the transition is unknown/unrecorded, not an
    inference presented as authority."""
    now = time.time() if now is None else now
    records, unreadable, missing = _read_records(home)
    retention = _retention(home, unreadable)
    if missing:
        return {"status": "unknown_missing", "transitions": [],
                "retention": retention}
    by_pool: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        if rec["mint"] == mint:
            by_pool.setdefault(rec["pool"], []).append(rec)
    if not by_pool:
        return {"status": "no_history", "transitions": [],
                "retention": retention}
    for recs in by_pool.values():
        recs.sort(key=lambda r: r["ts"])
    first_ts = {p: recs[0]["ts"] for p, recs in by_pool.items()}
    pools_by_appearance = sorted(by_pool, key=lambda p: first_ts[p])
    pairs = list(zip(pools_by_appearance, pools_by_appearance[1:]))
    explicit_idx, assoc_idx = _explicit_transitions(
        pairs, by_pool, first_ts)
    transitions = []
    for idx, (old, new) in enumerate(pairs):
        old_recs, new_recs = by_pool[old], by_pool[new]
        anchor = first_ts[new]
        explicit = idx in explicit_idx
        associated = idx in assoc_idx and not explicit
        # Causal baseline: pre-transition samples only. No baseline
        # means no inference at all - unknown, not guessed.
        baseline = [r for r in old_recs if r["ts"] < anchor]
        old_after = [r for r in old_recs if r["ts"] >= anchor]
        latest_old = old_recs[-1]
        baseline_peak = (max(r["quote_amount"] for r in baseline)
                         if baseline else None)
        if baseline_peak and baseline_peak > 0 and old_after:
            old_drained = (latest_old["quote_amount"]
                           <= _MIGRATION_DRAIN_RATIO * baseline_peak)
            # Latest observation still near baseline = both pools
            # live after the transition: conflict, never resolved.
            conflict = not old_drained
            prov = ("explicit" if explicit
                    else "inferred_from_reserves" if old_drained
                    else "marker_association_unverified" if associated
                    else "unrecorded")
        elif explicit:
            old_drained = None
            conflict = bool(old_after)
            prov = "explicit"
        elif associated:
            old_drained = None
            conflict = bool(old_after)
            prov = "marker_association_unverified"
        else:
            old_drained = None
            conflict = bool(old_after)
            prov = "unknown"
        transitions.append({
            "from_pool": old, "to_pool": new,
            "to_pool_first_seen": anchor,
            "provenance": prov,
            "old_pool_drained": old_drained,
            "conflict_both_active": conflict,
            "baseline_peak": baseline_peak,
            "baseline_samples": len(baseline),
            "latest_old_quote": latest_old["quote_amount"],
            "latest_old_ts": latest_old["ts"],
            "thresholds_provenance": _THRESHOLDS_PROVENANCE})
    last_ts = max(recs[-1]["ts"] for recs in by_pool.values())
    return {"status": "ok", "transitions": transitions,
            "pools_seen": len(by_pool), "unreadable": unreadable,
            "last_observed_ts": last_ts,
            "observed_age_s": now - last_ts,
            "retention": retention}


def monitor_status(home: Optional[str] = None) -> Dict[str, Any]:
    """Availability + counters. missing meta = unknown_missing for the
    counter history, not zero."""
    meta_exists = _meta_file(home).exists()
    file_exists = _file(home).exists()
    meta = _meta_read(home)
    return {"history_file": "present" if file_exists else "missing",
            "meta": "present" if meta_exists else "unknown_missing",
            "counters": meta}
