"""Deployer tracking: which wallet launched which coin, and whether that
wallet's launches tend to run.

A "deployer" is the creator wallet behind a launch: for pump.fun mints it
is the launch transaction's creator; for direct pool launches
(pumpswap/meteora) it is the pool creator. Hot deployers — wallets with a
history of launches that ran — get their NEW coins fast-tracked to the
same instant-card path as the smart-buy fast-track.

Hard rule, stated once: HISTORY NEVER OVERRIDES HARD FAILS. The safety
gate (``_instant_verdict``, fail-closed, trial bar) runs FIRST regardless
of deployer heat — a hot deployer's honeypot is still blocked silently.
``evaluate_deployer_fasttrack`` takes ``safety_passed`` and returns None
when it is False, so the ordering is enforced in code, not just in docs.

Performance contract for the integrator (read this before wiring):

* Resolution is LAZY and CACHED. Never call ``resolve_deployer`` on the
  hot detection path. The intended call sites are:
  - quality-passed coins only (after ``_instant_verdict`` allowed them),
    resolved inline in the slow part of the cycle; or
  - a throttled background pass (one mint per cycle, e.g. the newest
    quality-passed coin without a cached deployer).
* Use ``cached_deployer(state, ...)`` — it reads
  ``state["deployer_cache"]`` first (mint -> {"deployer", "ts"}) and only
  resolves when the entry is missing or older than
  ``degen.deployer_resolve_ttl_s`` (default 6h). Negative results (None)
  are cached too, with the same TTL, so one bad mint never triggers
  repeated RPC calls.
* pump.fun mints resolve for FREE: ``sources.py`` already ships the
  creator in ``extra["creator"]`` — ``resolve_deployer`` reads it from
  ``ctx`` (the normalized launch source dict) and makes NO RPC call.
  Helius ``getSignaturesForAddress``/``getTransaction`` is only used for
  direct pool launches where no creator field exists.

Registry (shared file, 0600 — same convention as smartmoney.py)::

    ~/.forex-agent/launch_scanner/deployer_history.json

per deployer: ``{launches_seen, hits_2x, rugs, dead, first_seen, last_seen,
launch_ts (bounded), outcome_ts (bounded)}``

Persistence depth (2026-11-23, request section 4.2): the registry is
capped at ``_MAX_REGISTRY`` live entries and eviction used to erase
everything known about a cold deployer. An entry is now archived to an
append-only JSONL before eviction (``deployer_archive.jsonl``, same
disciplines as the launch archive): the deployer is REMEMBERED
(counters, first/last seen, bounded launch/outcome timestamps), even
though the live entry is gone. Memory semantics (code-defined):

- durability: an entry is evicted only after its archive append
  SUCCEEDS; a failed append keeps the entry (eviction retried on the
  next launch), never a silent drop. A blocked (unarchivable)
  eviction candidate is skipped, counted
  (``eviction_skips_blocked``), and the remaining eligible entries
  are tried within the same bounded pass; when every candidate is
  blocked the registry overflows the cap, recorded as
  ``eviction_overflow_events`` - an explicit unavailable eviction
  state, so the live-registry cap is best-effort, NOT a hard bound.
  A torn tail (crash mid-append) is repaired under the lock before
  the next append, and repairs are counted in the meta file;
- segments, not snapshots: each eviction archives one SEGMENT with a
  stable ``segment_id`` (deployer + its registry first_seen; an entry
  without a verifiable first_seen has no deterministic identity and
  archiving FAILS CLOSED, the live entry retained, rather than
  fabricating recency). A re-appearing deployer's later segment
  never erases an earlier one: reads and compaction keep a
  PER-SEGMENT entry per segment id (the dedup identity compaction
  cannot erase). Append time is NOT the logical version (an
  unlocked registry read-modify-write can archive a stale snapshot
  after a fresh one), so same-id snapshots merge by monotonic
  maximum per cumulative counter; a decrease is an explicit
  regression_conflict label, never an invented total and never a
  silent overwrite. Totals sum only over verifiable values (any
  corrupt/missing counter marks the row partial). Records with a
  corrupt archived_ts or no id are isolated and counted
  (``unusable_records`` / ``dropped_unreadable_total``), never
  poisoning the read. Segment detail grows with distinct evictions
  per deployer, bounded in practice by activity, NOT by an
  enforceable constant (no lifetime-dedup claim beyond the
  persisted ids). Live risk counters are NEVER rehydrated from
  the archive: a re-appearing deployer's live entry starts fresh;
- availability: a missing archive file is UNKNOWN (absence cannot
  prove zero history), reported as ``unknown_missing``; unreadable
  lines are counted in ``archive_status``; compaction-dropped
  corrupt lines, compaction failures and torn-tail repairs are
  persisted in a sidecar meta file and surfaced by
  ``deployer_archive()`` - a compacted file never looks complete
  when evidence was dropped;
- provenance: archived counters and timestamps are RECORDED values
  (``memory_provenance``), not verified skill;
- retention: compaction past ``_ARCHIVE_MAX_BYTES`` merges segments
  per deployer as above; total size is bounded in practice by
  deployer cardinality and activity, NOT by an enforceable constant.
  Retrieval is a full scan; a real index is a named future slice.

The archive is advisory memory only: no gate, entry or exit reads it
in this slice, so behavior is unchanged.

Outcomes are graded independently in this module (``record_outcome``);
a shared grading helper covering wallets AND deployers would be cleaner
for a later cleanup — noted for the integrator (see INTEGRATION below).

Knobs (integrator adds to config/defaults.yaml, getattr-with-default):

* ``deployer_tracking_enabled`` (default True)
* ``deployer_hot_hit_rate`` (default 0.30)
* ``deployer_min_launches`` (default 3)
* ``deployer_resolve_ttl_s`` (default 21600 = 6h)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import helius_ratelimit
from . import tx_guards as _txg

logger = logging.getLogger("forex_agent.daemon.launch_scanner.deployers")

_HOME = Path(os.environ.get("FOREX_AGENT_HOME", Path.home() / ".forex-agent"))
_REGISTRY_FILE = _HOME / "launch_scanner" / "deployer_history.json"
_ARCHIVE_FILE = _HOME / "launch_scanner" / "deployer_archive.jsonl"
_ARCHIVE_LOCK_FILE = _HOME / "launch_scanner" / "deployer_archive.lock"
_ARCHIVE_META_FILE = _HOME / "launch_scanner" / "deployer_archive_meta.json"
_ARCHIVE_SCHEMA_V = 1
_ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
_CACHE_KEY = "deployer_cache"
_SEEN_KEY = "deployer_fasttrack_seen"
_MAX_REGISTRY = 2000
_MAX_OUTCOME_TS = 30         # bounded per-deployer outcome history
_TIMEOUT = 20

_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={key}"
_KEY_FILE = _HOME / "helius" / "api_key"
# Fallback when the main key file is absent (2026-11-22 dead-key
# fix): the main key started returning HTTP 401 and was
# quarantined; deployer checks must keep working on the remaining
# live key instead of going dark.
_DISCOVERY_KEY_FILE = _HOME / "helius" / "api_key_discovery"
_key_cache: Optional[str] = None
_key_checked = False

# Outcomes accepted by record_outcome.
OUTCOMES = ("hit_2x", "rug", "dead")


# ----------------------------------------------------------------------
# Helius RPC (reused pattern from smartmoney.py; Solana only)
# ----------------------------------------------------------------------

def api_key() -> Optional[str]:
    """Helius key from the 0600 file or env. Cached; None when absent.

    2026-11-22 (dead-key fix): when the main key file is absent
    (quarantined after it started returning HTTP 401), fall back
    to the discovery key so deployer checks keep working on the
    remaining live key instead of going dark. The fallback shares
    the discovery key's pacer bucket with the reserves path — more
    contention than a dedicated key, but functional; a dedicated
    main key is restored by putting a valid key back in the main
    key file and restarting."""
    global _key_cache, _key_checked
    if _key_checked:
        return _key_cache
    _key_checked = True
    key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not key:
        try:
            key = _KEY_FILE.read_text().strip()
        except OSError:
            key = ""
    if not key:
        key = os.environ.get("HELIUS_API_KEY_DISCOVERY", "").strip()
        if not key:
            try:
                key = _DISCOVERY_KEY_FILE.read_text().strip()
            except OSError:
                key = ""
        if key:
            logger.warning(
                "deployers: main Helius key absent, using "
                "discovery key fallback")
    _key_cache = key or None
    return _key_cache


def _rpc(method: str, params: List[Any]) -> Any:
    """One Helius call through the fleet-wide pacer (2026-11-17
    pacing fix). The old in-process-only guard let every trader
    process hammer the same key with its own private cooldown;
    now every attempt first takes a pacer token for the main-key
    bucket, and a 429 engages progressive backoff + the single
    probe instead of a 600s lockout. Never raises."""
    key = api_key()
    if not key:
        return None
    bucket = helius_ratelimit.bucket_id_for_key(key)
    # Auto-shed (2026-11-18): deployer scans are background work —
    # when the bucket is in backoff they stop attempting entirely
    # (logged once per episode) instead of competing for the tokens
    # entry-critical calls need. Never raises.
    if not helius_ratelimit.background_call_allowed(bucket):
        helius_ratelimit.log_backoff_episode_once(
            bucket, "deployers", logger)
        return None
    # wait_s=0: take a token only if one is free right now — never
    # queue for a refilling token an entry call needs.
    if not helius_ratelimit.pacer_acquire(bucket, wait_s=0):
        return None  # pacer: no token — fail-soft like any miss
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method,
         "params": params}).encode()
    req = urllib.request.Request(
        _RPC_URL.format(key=key), data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0",
                 "Connection": "close"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(
                resp.read().decode("utf-8", "replace")).get("result")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            helius_ratelimit.pacer_report(bucket, False)
            logger.warning("deployer rpc %s rate-limited (429); "
                           "pacer backoff engaged", method)
        else:
            logger.warning("deployer rpc %s failed: HTTP %s",
                           method, exc.code)
        return None
    except Exception as exc:
        logger.warning("deployer rpc %s failed: %s", method, exc)
        return None
    helius_ratelimit.pacer_report(bucket, True)
    return result


# ----------------------------------------------------------------------
# Deployer resolution
# ----------------------------------------------------------------------

def _first_signer(tx: Dict[str, Any]) -> Optional[str]:
    """First signer of a transaction — the wallet that paid for (and in
    practice created) it. Works for pump.fun create txs and pool-create
    txs alike; the deployer always signs the launch transaction."""
    try:
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        first = keys[0] if keys else None
        pub = (first or {}).get("pubkey") or first
        return str(pub) if pub else None
    except Exception:
        return None


def resolve_deployer(mint: str, chain: str,
                     ctx: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Resolve the deployer wallet for one mint.

    ``ctx`` is the normalized launch source dict (as produced by
    ``sources.py`` / the launch candidate) — pump.fun mints carry
    ``extra.creator`` and resolve with ZERO RPC calls. ``chain`` should be
    the canonical chain name (solana/base/ethereum).

    Non-solana chains and mints without any resolvable source return None.
    Never raises.
    """
    if not mint:
        return None
    extra = (ctx or {}).get("extra") or {}
    creator = str(extra.get("creator") or "").strip()
    if creator:
        # pump.fun launch — creator shipped free by the source feed.
        return creator
    if (chain or "").lower() != "solana":
        return None
    # Direct pool launch: find the mint's earliest transaction; its first
    # signer is the deployer. getSignaturesForAddress is newest-first; the
    # last entry is the oldest we can see.
    try:
        sigs = _rpc("getSignaturesForAddress", [mint, {"limit": 100}]) or []
        if not sigs:
            return None
        if len(sigs) >= 100:
            # Too much history to reach the launch reliably — bounded.
            return None
        oldest = (sigs[-1] or {}).get("signature")
        if not oldest:
            return None
        tx = _rpc("getTransaction",
                  [oldest, {"encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0}]) or {}
        # meta.err gate (2026-11-17, Batch 3): a reverted oldest
        # tx names no deployer — the launch never happened.
        if not _txg.tx_succeeded(tx):
            return None
        return _first_signer(tx)
    except Exception as exc:
        logger.warning("resolve_deployer failed for %s: %s", mint, exc)
        return None


def cached_deployer(state: Dict[str, Any], mint: str, chain: str,
                    ctx: Optional[Mapping[str, Any]] = None,
                    degen: Any = None) -> Optional[str]:
    """Lazy, cached deployer resolution. Reads ``state["deployer_cache"]``
    first; resolves only when missing or older than
    ``degen.deployer_resolve_ttl_s`` (default 6h). Negative results are
    cached too.

    The INTEGRATOR must only call this off the hot path: for
    quality-passed coins (after ``_instant_verdict`` allowed them) or in a
    throttled background pass (one mint per cycle). Never call this in the
    per-candidate scan loop.
    """
    ttl = float(getattr(degen, "deployer_resolve_ttl_s", 21600.0))
    cache = state.setdefault(_CACHE_KEY, {})
    now = time.time()
    entry = cache.get(mint)
    if isinstance(entry, dict) and now - float(entry.get("ts") or 0) < ttl:
        return entry.get("deployer")
    deployer = resolve_deployer(mint, chain, ctx)
    # Bound the cache: evict the oldest entries past 2000.
    if len(cache) > 2000:
        for key in sorted(cache, key=lambda k: float(
                (cache.get(k) or {}).get("ts") or 0))[
                :len(cache) - 2000]:
            cache.pop(key, None)
    cache[mint] = {"deployer": deployer, "ts": now}
    return deployer


# ----------------------------------------------------------------------
# Registry (shared JSON file, 0600 — same convention as smartmoney.py)
# ----------------------------------------------------------------------

def load_registry() -> Dict[str, Dict[str, Any]]:
    """Deployer history from the shared JSON file (both daemons read it)."""
    try:
        data = json.loads(_REGISTRY_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_registry(hist: Dict[str, Dict[str, Any]]) -> bool:
    try:
        _REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REGISTRY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(hist))
        os.chmod(tmp, 0o600)
        tmp.replace(_REGISTRY_FILE)
        return True
    except OSError as exc:
        logger.warning("could not save deployer registry: %s", exc)
        return False


def _new_entry(now: float) -> Dict[str, Any]:
    return {"launches_seen": 0, "hits_2x": 0, "rugs": 0, "dead": 0,
            "first_seen": now, "last_seen": now, "launch_ts": []}


def record_launch(deployer: str) -> Optional[Dict[str, Any]]:
    """Count one launch for a deployer. Returns the updated entry."""
    deployer = (deployer or "").strip()
    if not deployer:
        return None
    now = time.time()
    hist = load_registry()
    entry = hist.get(deployer) or _new_entry(now)
    entry["launches_seen"] = int(entry.get("launches_seen") or 0) + 1
    entry["last_seen"] = now
    _record_launch_ts(entry, now)  # bounded serial-launcher history
    hist[deployer] = entry
    if len(hist) > _MAX_REGISTRY:
        # Evict least-recently-seen, never hot deployers. An entry
        # is evicted only after its archive append SUCCEEDS; a
        # blocked (unarchivable) candidate is SKIPPED, counted, and
        # the remaining eligible entries are tried within this
        # bounded pass - one blocked entry must not stall all
        # future eviction, and unarchivable history is never
        # silently deleted. When every candidate is blocked the
        # registry overflows the cap: recorded as
        # eviction_overflow_events, an explicit unavailable
        # eviction state (no hard-bound claim).
        evicted = False
        for dep in sorted(
                hist,
                key=lambda d: float((hist.get(d) or {}).get("last_seen")
                                    or 0))[:len(hist) - _MAX_REGISTRY]:
            if _entry_is_hot(hist.get(dep), 3, 0.30):
                continue
            if _archive_deployer(dep, hist.get(dep), now):
                hist.pop(dep, None)
                evicted = True
                break
            _meta_bump("eviction_skips_blocked")
        if not evicted:
            _meta_bump("eviction_overflow_events")
    save_registry(hist)
    return entry


def record_outcome(mint: str, deployer: str, outcome: str) -> bool:
    """Grade one closed launch: outcome in {"hit_2x","rug","dead"}.

    ``mint`` is recorded for audit only; grading keys off the deployer.
    Deployers seen here but never launched through ``record_launch`` are
    created with launches_seen=1 (first-seen assumption) so hit rates
    stay <= 1. Returns False on an unknown outcome (never raises).
    """
    if outcome not in OUTCOMES:
        logger.warning("record_outcome: unknown outcome %r for %s", outcome,
                       mint)
        return False
    deployer = (deployer or "").strip()
    if not deployer:
        return False
    now = time.time()
    hist = load_registry()
    entry = hist.get(deployer)
    if entry is None:
        entry = _new_entry(now)
        entry["launches_seen"] = 1
    else:
        entry["launches_seen"] = max(int(entry.get("launches_seen") or 0), 1)
    if outcome == "hit_2x":
        entry["hits_2x"] = int(entry.get("hits_2x") or 0) + 1
    elif outcome == "rug":
        entry["rugs"] = int(entry.get("rugs") or 0) + 1
    else:
        entry["dead"] = int(entry.get("dead") or 0) + 1
    entry["last_seen"] = now
    # Bounded per-outcome timestamps: depth over time per wallet
    # (when each outcome landed), capped like launch_ts.
    stamps = entry.get("outcome_ts")
    if not isinstance(stamps, list):
        stamps = []
    stamps.append({"ts": now, "outcome": outcome})
    entry["outcome_ts"] = stamps[-_MAX_OUTCOME_TS:]
    hist[deployer] = entry
    return save_registry(hist)


# ----------------------------------------------------------------------
# Deployer archive: persistence across registry eviction (4.2)
# ----------------------------------------------------------------------

@contextlib.contextmanager
def _archive_lock():
    _ARCHIVE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    f = open(_ARCHIVE_LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield f
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _is_num(v: Any) -> bool:
    """True only for a real finite number: bool is not a count and
    NaN/inf can never be ordered or merged safely."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and v not in (float("inf"), float("-inf")))


def _meta_bump(key: str, n: int = 1) -> None:
    """Increment a persistent meta counter under the archive lock.
    Best-effort; never raises."""
    try:
        with _archive_lock():
            meta = _meta_read()
            meta[key] = int(meta.get(key) or 0) + n
            _meta_write(meta)
    except Exception:
        pass


def _meta_read() -> Dict[str, Any]:
    try:
        d = json.loads(open(_ARCHIVE_META_FILE).read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _meta_write(meta: Dict[str, Any]) -> None:
    try:
        _ARCHIVE_META_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_ARCHIVE_META_FILE) + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(meta))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _ARCHIVE_META_FILE)
        try:
            os.chmod(_ARCHIVE_META_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass




_SEG_FIELDS = ("archived_ts", "first_seen", "last_seen",
               "launches_seen", "hits_2x", "rugs", "dead",
               "launch_ts", "outcome_ts", "evicted_reason",
               "memory_provenance", "partial", "regression_conflict")
_COUNTER_FIELDS = ("launches_seen", "hits_2x", "rugs", "dead")


def _seg_from_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: rec.get(k) for k in _SEG_FIELDS if k in rec}


def _merge_ts_lists(a, b, cap):
    """Union of two bounded timestamp digests, deduped by value,
    sorted by timestamp, capped. Never raises."""
    try:
        seen = {}
        for v in list(a or []) + list(b or []):
            try:
                seen[json.dumps(v, sort_keys=True)] = v
            except Exception:
                continue

        def _key(v):
            if isinstance(v, dict):
                t = v.get("ts")
                return float(t) if isinstance(t, (int, float)) else 0.0
            if isinstance(v, (int, float)):
                return float(v)
            return 0.0

        vals = sorted(seen.values(), key=_key)
        return vals[-cap:]
    except Exception:
        return list(a or [])[-cap:]


def _upsert_segment(state: Dict[str, Any], sid: str,
                    seg: Dict[str, Any]) -> None:
    """Merge one snapshot into a segment id's accumulated state.
    Append time is NOT the logical version (an unlocked registry
    read-modify-write can archive a stale snapshot after a fresh
    one): counters are cumulative, so the merge takes the
    monotonic maximum per counter; a DECREASE within the same id
    is an explicit regression_conflict label, never an invented
    total and never a silent overwrite of the larger value.
    Snapshot recency (archived_ts) and last_seen take the maximum;
    first_seen the minimum; timestamp digests union (bounded)."""
    old = state.get(sid)
    if old is None:
        state[sid] = seg
        return
    merged = dict(old)
    conflict = bool(old.get("regression_conflict")) \
        or bool(seg.get("regression_conflict"))
    for k in _COUNTER_FIELDS:
        a, b = old.get(k), seg.get(k)
        a_ok = isinstance(a, int) and not isinstance(a, bool)
        b_ok = isinstance(b, int) and not isinstance(b, bool)
        if a_ok and b_ok:
            merged[k] = max(a, b)
            if b < a:
                conflict = True
        else:
            keep = a if a_ok else b
            if isinstance(keep, int) and not isinstance(keep, bool):
                merged[k] = keep
            elif k in merged:
                merged.pop(k)
    for k, fn in (("archived_ts", max), ("last_seen", max),
                  ("first_seen", min)):
        vals = [v for v in (old.get(k), seg.get(k)) if _is_num(v)]
        if vals:
            merged[k] = fn(vals)
    merged["launch_ts"] = _merge_ts_lists(
        old.get("launch_ts"), seg.get("launch_ts"), _MAX_LAUNCH_TS)
    merged["outcome_ts"] = _merge_ts_lists(
        old.get("outcome_ts"), seg.get("outcome_ts"), _MAX_OUTCOME_TS)
    if bool(old.get("partial")) or bool(seg.get("partial")):
        merged["partial"] = True
    if conflict:
        merged["regression_conflict"] = True
    state[sid] = merged


def _fold_record(state: Dict[str, Any], rec: Dict[str, Any]) -> int:
    """Fold one file record (raw segment or compacted merged row)
    into per-deployer {segment_id: segment} state. Returns the
    number of UNUSABLE segments skipped (no id, or a non-numeric
    archived_ts, which can never be ordered or merged safely) so
    the caller can count them instead of letting one bad record
    poison the whole read."""
    dropped = 0
    segs = rec.get("segments")
    if isinstance(segs, dict) and segs:
        for sid, seg in segs.items():
            if not isinstance(seg, dict) or not _is_num(
                    seg.get("archived_ts")):
                dropped += 1
                continue
            _upsert_segment(state, sid, _seg_from_record(seg))
        return dropped
    sid = rec.get("segment_id")
    if not sid or not _is_num(rec.get("archived_ts")):
        return 1
    _upsert_segment(state, sid, _seg_from_record(rec))
    return 0


def _row_from_segments(deployer: str,
                       segs: Dict[str, Any]) -> Dict[str, Any]:
    """Display/compaction row for one deployer: per-segment detail
    preserved (dedup identity), totals summed only over verifiable
    int counters (any corrupt/missing counter marks the row
    partial, never silently summed)."""
    row = {"v": _ARCHIVE_SCHEMA_V, "record_type": "deployer_archive",
           "deployer": deployer, "segments": segs,
           "segment_ids": sorted(segs), "segments_merged": len(segs),
           "memory_provenance":
               "recorded_counters_and_bounded_timestamps"
               "_not_verified_skill"}
    partial = False
    for k in _COUNTER_FIELDS:
        total = 0
        for seg in segs.values():
            v = seg.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                total += v
            else:
                partial = True
        row[k] = total
    if any(seg.get("partial") for seg in segs.values()):
        partial = True
    row["partial"] = partial
    conflicts = sum(1 for seg in segs.values()
                    if seg.get("regression_conflict"))
    if conflicts:
        # The per-field max combination may never have existed as an
        # actual snapshot: regression conflicts make the row partial,
        # an unavoidable status for any consumer.
        row["regression_conflicts"] = conflicts
        row["partial"] = True
    first = [s.get("first_seen") for s in segs.values()]
    last = [s.get("last_seen") for s in segs.values()]
    arch = [s.get("archived_ts") for s in segs.values()]
    row["first_seen"] = min([v for v in first if _is_num(v)],
                            default=None)
    row["last_seen"] = max([v for v in last if _is_num(v)],
                           default=None)
    row["archived_ts"] = max([v for v in arch if _is_num(v)],
                             default=None)
    row["evicted_reason"] = "registry_cap"
    for k, cap in (("launch_ts", _MAX_LAUNCH_TS),
                   ("outcome_ts", _MAX_OUTCOME_TS)):
        vals = []
        for sid in sorted(segs):
            vals.extend(segs[sid].get(k) or [])
        row[k] = vals[-cap:]
    row["merged"] = len(segs) > 1
    return row


def _archive_append(rec: Dict[str, Any]) -> bool:
    """Append one archive segment under an exclusive lock. Repairs a
    torn tail first (crash mid-append must not glue the next record
    onto a partial line; the repair is counted), then compacts
    opportunistically past the byte cap. Duplicate segment ids are
    NOT skipped here: a retried eviction may carry UPDATED counters,
    and dedup is by per-id upsert (newer archived_ts replaces) at
    read and compaction time, where the identity cannot be erased.
    Returns True only when the segment is durable. Never raises."""
    try:
        _ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _archive_lock():
            meta = _meta_read()
            try:
                if os.path.exists(_ARCHIVE_FILE):
                    with open(_ARCHIVE_FILE, "rb+") as f:
                        f.seek(0, os.SEEK_END)
                        size = f.tell()
                        if size:
                            f.seek(size - 1)
                            if f.read(1) != b"\n":
                                f.seek(0)
                                data = f.read()
                                cut = data.rfind(b"\n") + 1
                                f.truncate(cut)
                                meta["torn_tail_repairs"] = int(
                                    meta.get("torn_tail_repairs")
                                    or 0) + 1
            except Exception:
                pass
            with open(_ARCHIVE_FILE, "a") as out:
                out.write(json.dumps(rec) + "\n")
                out.flush()
                os.fsync(out.fileno())
            _maybe_compact_archive(meta)
            _meta_write(meta)
        return True
    except Exception:
        return False


def _maybe_compact_archive(meta: Dict[str, Any]) -> None:
    """Past the byte cap, rewrite the archive as one row per
    deployer carrying PER-SEGMENT detail (the dedup identity
    compaction cannot erase): same-id records upsert by newer
    archived_ts, distinct ids are all preserved. Dropped corrupt or
    unidentifiable lines and compaction failures are counted in the
    meta file - a compacted file never looks complete when evidence
    was dropped. Enforceable invariant: one row per distinct
    deployer after every compaction; total size is bounded in
    practice by deployer/segment cardinality and activity, NOT by
    an enforceable constant. Called with the archive lock held;
    meta is written by the caller. Never raises."""
    try:
        if not os.path.exists(_ARCHIVE_FILE) \
                or os.path.getsize(_ARCHIVE_FILE) <= _ARCHIVE_MAX_BYTES:
            return
        states = {}
        dropped = 0
        with open(_ARCHIVE_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    dropped += 1
                    continue
                if not isinstance(rec, dict) \
                        or rec.get("v") != _ARCHIVE_SCHEMA_V \
                        or rec.get("record_type") != "deployer_archive":
                    dropped += 1
                    continue
                dep = rec.get("deployer")
                if not dep:
                    dropped += 1
                    continue
                dropped += _fold_record(states.setdefault(dep, {}), rec)
        if dropped:
            meta["dropped_unreadable_total"] = int(
                meta.get("dropped_unreadable_total") or 0) + dropped
        tmp = str(_ARCHIVE_FILE) + ".tmp"
        with open(tmp, "w") as out:
            for dep, state in states.items():
                if state:
                    out.write(json.dumps(
                        _row_from_segments(dep, state)) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, _ARCHIVE_FILE)
        try:
            os.chmod(_ARCHIVE_FILE, 0o600)
        except Exception:
            pass
        meta["compactions"] = int(meta.get("compactions") or 0) + 1
    except Exception:
        meta["compaction_failures"] = int(
            meta.get("compaction_failures") or 0) + 1


def _archive_deployer(deployer: str, entry: Optional[Dict[str, Any]],
                      now: float) -> bool:
    """Archive one evicted registry entry as a segment with a stable
    id (deployer + its registry first_seen, so a retried eviction
    UPSERTS - monotonic cumulative merge, never a double-count or a
    stale overwrite - and a re-appearing deployer's new entry forms
    a NEW segment instead of erasing the old one). An entry with no
    verifiable first_seen has no deterministic identity: archiving
    FAILS CLOSED (the caller retains the live entry) rather than
    fabricating recency. The archived counters and timestamps are
    RECORDED values, labeled as such - not verified skill. Never
    raises; returns True only when durable."""
    try:
        e = entry if isinstance(entry, dict) else {}
        first_seen = e.get("first_seen")
        if not _is_num(first_seen):
            # Fail closed: no deterministic identity, the caller
            # retains the live entry (counted, never a silent drop
            # and never a fabricated recency id).
            _meta_bump("archive_fail_closed")
            return False
        sid = "%s:%s" % (deployer, first_seen)
        rec = {"v": _ARCHIVE_SCHEMA_V, "record_type": "deployer_archive",
               "segment_id": sid,
               "deployer": deployer,
               "archived_ts": now,
               "first_seen": first_seen,
               "last_seen": e.get("last_seen"),
               "launches_seen": int(e.get("launches_seen") or 0),
               "hits_2x": int(e.get("hits_2x") or 0),
               "rugs": int(e.get("rugs") or 0),
               "dead": int(e.get("dead") or 0),
               "launch_ts": list(e.get("launch_ts") or [])[
                   -_MAX_LAUNCH_TS:],
               "outcome_ts": list(e.get("outcome_ts") or [])[
                   -_MAX_OUTCOME_TS:],
               "evicted_reason": "registry_cap",
               "memory_provenance":
                   "recorded_counters_and_bounded_timestamps"
                   "_not_verified_skill"}
        return _archive_append(rec)
    except Exception:
        return False


def deployer_archive(deployer: Optional[str] = None,
                     limit: int = 100) -> Dict[str, Any]:
    """Archived history per deployer, newest first: per-segment
    detail upserted by segment id (a retried eviction replaces its
    older contribution, never double-counts), totals summed only
    over verifiable counters (partial when any was corrupt). A
    missing archive file is UNKNOWN (absence cannot prove zero
    history), reported as ``unknown_missing``; unreadable lines,
    compaction-dropped corrupt lines, compaction failures and
    torn-tail repairs are counted in ``archive_status``, never a
    clean empty history. Returns {"records": [...],
    "archive_status": {...}}; never raises."""
    status = {"available": False, "unreadable_lines": 0,
              "reason": "unknown_missing"}
    try:
        meta = _meta_read()
        if meta:
            status["meta_available"] = True
            for k in ("dropped_unreadable_total",
                      "compaction_failures", "torn_tail_repairs",
                      "compactions", "archive_fail_closed",
                      "eviction_skips_blocked",
                      "eviction_overflow_events"):
                if k in meta:
                    status[k] = meta[k]
        else:
            status["meta_available"] = False
        if not os.path.exists(_ARCHIVE_FILE):
            return {"records": [], "archive_status": status}
        status["available"] = True
        status["reason"] = "ok"
        states = {}
        with open(_ARCHIVE_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    status["unreadable_lines"] += 1
                    continue
                if not isinstance(rec, dict) \
                        or rec.get("v") != _ARCHIVE_SCHEMA_V \
                        or rec.get("record_type") != "deployer_archive":
                    continue
                dep = rec.get("deployer")
                if not dep:
                    continue
                if deployer is not None and dep != deployer:
                    continue
                status["unusable_records"] = (
                    status.get("unusable_records") or 0) + \
                    _fold_record(states.setdefault(dep, {}), rec)
        recs = sorted(
            (_row_from_segments(dep, st)
             for dep, st in states.items() if st),
            key=lambda r: float(r.get("archived_ts") or 0),
            reverse=True)[:int(limit or 100)]
        return {"records": recs, "archive_status": status}
    except Exception:
        status["reason"] = "unreadable"
        return {"records": [], "archive_status": status}


# ----------------------------------------------------------------------
# Hot-deployer rule + fast-track trigger
# ----------------------------------------------------------------------

def hit_rate(entry: Optional[Dict[str, Any]]) -> float:
    """2x-hit rate of a registry entry (0.0 when ungraded/too few)."""
    if not entry:
        return 0.0
    launches = int(entry.get("launches_seen") or 0)
    if launches <= 0:
        return 0.0
    return float(entry.get("hits_2x") or 0) / launches


def _entry_is_hot(entry: Optional[Dict[str, Any]], min_launches: int,
                  hot_hit_rate: float) -> bool:
    if not entry:
        return False  # unknown deployer: NEUTRAL, never hot
    if int(entry.get("launches_seen") or 0) < min_launches:
        return False  # cold deployer: NEUTRAL, never hot
    return hit_rate(entry) >= hot_hit_rate


def is_hot(deployer: Optional[str], degen: Any) -> bool:
    """True only for proven deployers: launches_seen >=
    ``degen.deployer_min_launches`` (default 3) AND hit rate >=
    ``degen.deployer_hot_hit_rate`` (default 0.30).

    Unknown, first-time, and cold (1-2 launch) deployers are NEUTRAL —
    no boost, no penalty, ever.
    """
    if not deployer:
        return False
    min_launches = max(1, int(getattr(degen, "deployer_min_launches", 3)))
    hot_hit_rate = float(getattr(degen, "deployer_hot_hit_rate", 0.30))
    return _entry_is_hot(load_registry().get(deployer), min_launches,
                         hot_hit_rate)


def evaluate_deployer_fasttrack(state: Dict[str, Any], degen: Any, mint: str,
                                deployer: Optional[str],
                                safety_passed: bool = True
                                ) -> Optional[Dict[str, Any]]:
    """Decide whether a coin gets the hot-deployer fast-track.

    ``safety_passed`` is the result of ``_instant_verdict`` — the
    integrator MUST pass it, and the gate MUST have run first. History
    never overrides hard fails: a False here (honeypot, can't-sell,
    insider cluster, score below bar) returns None, no fast-track.

    Hot deployer + new-to-us mint -> dict with the facts the card needs::

        {"deployer": ..., "launches_seen": n, "hits_2x": h,
         "hit_rate": r, "deployer_line": "🏭 deployer 4/10 hit 2x"}

    Unknown/cold deployers, disabled tracking, and already-carded mints
    return None (NEUTRAL — no boost, no penalty). Never raises.
    """
    try:
        if not getattr(degen, "deployer_tracking_enabled", True):
            return None
        if not safety_passed:
            # Hard-fail override blocked: the safety gate's verdict wins.
            return None
        if not deployer or not mint:
            return None
        seen = state.setdefault(_SEEN_KEY, [])
        if mint in seen:
            return None  # new-to-us mints only
        if not is_hot(deployer, degen):
            return None
        entry = load_registry().get(deployer) or {}
        launches = int(entry.get("launches_seen") or 0)
        hits = int(entry.get("hits_2x") or 0)
        rate = hit_rate(entry)
        seen.append(mint)
        if len(seen) > 2000:
            del seen[:len(seen) - 2000]
        return {
            "deployer": deployer,
            "launches_seen": launches,
            "hits_2x": hits,
            "hit_rate": round(rate, 3),
            "deployer_line": "🏭 deployer %d/%d hit 2x" % (hits, launches),
        }
    except Exception as exc:
        logger.warning("deployer fast-track eval failed: %s", exc)
        return None


def hot_deployers(degen: Any, limit: int = 12) -> List[Dict[str, Any]]:
    """Top deployers by hit rate, honoring the min-launches filter."""
    min_launches = max(1, int(getattr(degen, "deployer_min_launches", 3)))
    rows = []
    for dep, entry in load_registry().items():
        if int(entry.get("launches_seen") or 0) < min_launches:
            continue
        rows.append({"deployer": dep,
                     "launches_seen": int(entry.get("launches_seen") or 0),
                     "hits_2x": int(entry.get("hits_2x") or 0),
                     "rugs": int(entry.get("rugs") or 0),
                     "dead": int(entry.get("dead") or 0),
                     "hit_rate": round(hit_rate(entry), 3),
                     "last_seen": float(entry.get("last_seen") or 0)})
    rows.sort(key=lambda r: (r["hit_rate"], r["launches_seen"]),
              reverse=True)
    return rows[:limit]


# ----------------------------------------------------------------------
# Phase 6 — intelligence layer: reputation tiers, serial-launcher flag,
# funder clustering, blacklist PROPOSALS (measurement only).
#
# Guarded-learning rule: everything here is DISPLAY + MEASUREMENT.
# Nothing in this section gates entries, changes alerts, or auto-adds
# to any registry. The blacklist file is human-managed; proposals are
# review-only. Hard gates stay exactly as they are.
# ----------------------------------------------------------------------

_MAX_LAUNCH_TS = 30          # bounded per-deployer launch timestamps
_FUNDER_CACHE_TTL_DAYS = 36500  # a wallet's first funder never changes

_FUNDERS_FILE = _HOME / "launch_scanner" / "deployer_funders.json"
_BLACKLIST_FILE = _HOME / "launch_scanner" / "deployer_blacklist.json"
_BL_PROPOSALS_FILE = _HOME / "launch_scanner" / "deployer_blacklist_proposals.json"
_INTEL_KEY = "deployer_intel"  # state ledger key for the daily pass


def _cfg(degen: Any, name: str, default: Any) -> Any:
    try:
        v = getattr(degen, name, default)
        return default if v is None else v
    except Exception:
        return default


def rug_rate(entry: Optional[Dict[str, Any]]) -> float:
    """Share of graded launches that rugged (0.0 when ungraded)."""
    if not entry:
        return 0.0
    launches = int(entry.get("launches_seen") or 0)
    if launches <= 0:
        return 0.0
    return float(entry.get("rugs") or 0) / launches


def tier_of(entry: Optional[Dict[str, Any]], degen: Any = None) -> str:
    """Reputation tier: hot | risky | cold | new | unknown.

    Display order is safety-first: a deployer that is BOTH hot and
    rug-heavy shows as risky — the rug rate is the louder fact.
    Unknown/first-time deployers are NEUTRAL ("new"/"unknown"), never
    penalized.
    """
    if not entry:
        return "unknown"
    min_launches = max(1, int(_cfg(degen, "deployer_min_launches", 3)))
    launches = int(entry.get("launches_seen") or 0)
    if launches < min_launches:
        return "new"
    rug_bar = float(_cfg(degen, "deployer_rug_blacklist_bar", 0.5) or 0.5)
    if rug_rate(entry) >= rug_bar:
        return "risky"
    hot_bar = float(_cfg(degen, "deployer_hot_hit_rate", 0.30) or 0.30)
    if hit_rate(entry) >= hot_bar:
        return "hot"
    return "cold"


def is_serial_launcher(entry: Optional[Dict[str, Any]],
                       now: Optional[float] = None,
                       window_s: float = 86400.0,
                       min_launches: int = 5) -> bool:
    """True when the deployer launched >= min_launches inside window_s.

    Serial launchers (many coins in a short window) are a classic
    low-effort/rug-farm fingerprint. Measurement only — flags, never
    gates.
    """
    if not entry:
        return False
    now = now if now is not None else time.time()
    stamps = entry.get("launch_ts") or []
    try:
        recent = sum(1 for t in stamps
                     if now - float(t) <= max(60.0, window_s))
    except (TypeError, ValueError):
        return False
    return recent >= max(2, min_launches)


def reputation_line(deployer: Optional[str], degen: Any = None,
                    now: Optional[float] = None) -> str:
    """Quiet measurement line for the card.

    Always honest: shows the tier + launches + rug rate when data
    exists, "unknown" when it doesn't. Never raises.
    """
    try:
        if not deployer:
            return "🧬 deployer: unknown"
        entry = load_registry().get(deployer)
        if not entry:
            return "🧬 deployer: unknown"
        tier = tier_of(entry, degen)
        launches = int(entry.get("launches_seen") or 0)
        rug_pct = int(round(rug_rate(entry) * 100))
        if tier == "new":
            line = "🧬 deployer new — %d launch%s" % (
                launches, "" if launches == 1 else "es")
        elif tier == "unknown":
            line = "🧬 deployer: unknown"
        else:
            line = "🧬 deployer %s — %d launches, %d%% rug" % (
                tier.upper(), launches, rug_pct)
        window_s = float(_cfg(degen, "deployer_serial_window_s", 86400.0)
                         or 86400.0)
        min_ser = int(_cfg(degen, "deployer_serial_min_launches", 5) or 5)
        if is_serial_launcher(entry, now=now, window_s=window_s,
                              min_launches=min_ser):
            line += " ⚡serial"
        return line
    except Exception:
        return "🧬 deployer: unknown"


def _record_launch_ts(entry: Dict[str, Any], now: float) -> None:
    """Append a launch timestamp, bounded. Never raises."""
    try:
        stamps = entry.get("launch_ts")
        if not isinstance(stamps, list):
            stamps = []
        stamps.append(now)
        entry["launch_ts"] = stamps[-_MAX_LAUNCH_TS:]
    except Exception:
        pass


# ----------------------------------------------------------------------
# Funder clustering (shared first-funder)
# ----------------------------------------------------------------------

def _load_json_0600(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json_0600(path: Path, data: Dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("could not save %s: %s", path.name, exc)
        return False


def load_funders() -> Dict[str, Dict[str, Any]]:
    """{deployer: {"funder": <addr|None>, "ts": <float>}} — 0600 file."""
    return _load_json_0600(_FUNDERS_FILE)


def _extract_funder(tx: Optional[Dict[str, Any]],
                    deployer: str) -> Optional[str]:
    """Heuristic first-funder from one transaction. Fail-soft."""
    try:
        if not isinstance(tx, dict):
            return None
        msg = ((tx.get("transaction") or {}).get("message") or {})
        keys = msg.get("accountKeys") or []
        addrs: List[str] = []
        for k in keys:
            if isinstance(k, dict):
                addrs.append(str(k.get("pubkey") or ""))
            else:
                addrs.append(str(k or ""))
        payer = addrs[0] if addrs else ""
        if payer and payer != deployer:
            return payer
        # Fee payer is the deployer itself: look for the first incoming
        # system transfer to the deployer.
        for ix in (msg.get("instructions") or []):
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed") or {}
            info = parsed.get("info") or {}
            if (str(ix.get("program") or parsed.get("type") or "")
                    .lower().find("system") < 0
                    and parsed.get("type") != "transfer"):
                continue
            if str(info.get("destination") or "") == deployer:
                src = str(info.get("source") or "")
                if src and src != deployer:
                    return src
        return None
    except Exception:
        return None


def _oldest_signature(deployer: str, rpc: Callable[[str, List[Any]], Any],
                     max_pages: int = 3) -> Optional[str]:
    """Oldest signature for a wallet, walking `before` cursors.

    Bounded: at most max_pages getSignaturesForAddress calls. Fail-soft.
    """
    try:
        before: Optional[str] = None
        oldest: Optional[str] = None
        for _ in range(max(1, max_pages)):
            params: List[Any] = [deployer, {"limit": 100}]
            if before:
                params[1]["before"] = before
            batch = rpc("getSignaturesForAddress", params) or []
            if not isinstance(batch, list) or not batch:
                break
            sig = batch[-1].get("signature") if isinstance(
                batch[-1], dict) else None
            if sig:
                oldest = sig
                before = sig
            if len(batch) < 100:
                break  # reached the end of history
        return oldest
    except Exception:
        return None


def funder_for(deployer: str,
               rpc: Optional[Callable[[str, List[Any]], Any]] = None,
               now: Optional[float] = None) -> Optional[str]:
    """First funder of a deployer wallet, cached forever (0600 file).

    A wallet's first funder never changes, so one resolution per
    deployer for all time. Returns None on any miss — never raises.
    """
    try:
        deployer = (deployer or "").strip()
        if not deployer or rpc is None:
            return None
        now = now if now is not None else time.time()
        funders = load_funders()
        hit = funders.get(deployer)
        if isinstance(hit, dict) and "funder" in hit:
            return hit.get("funder")
        sig = _oldest_signature(deployer, rpc)
        funder = None
        if sig:
            tx = rpc("getTransaction",
                     [sig, {"maxSupportedTransactionVersion": 0}])
            # meta.err gate (2026-11-17, Batch 3): a reverted tx
            # funds nothing — its balances never moved.
            if _txg.tx_succeeded(tx):
                funder = _extract_funder(tx, deployer)
        funders[deployer] = {"funder": funder, "ts": now}
        _save_json_0600(_FUNDERS_FILE, funders)
        return funder
    except Exception:
        return None


def funder_clusters(hist: Optional[Dict[str, Any]] = None,
                    funders: Optional[Dict[str, Dict[str, Any]]] = None
                    ) -> Dict[str, List[str]]:
    """{funder: [deployers]} for funders shared by >= 2 deployers.

    A shared first-funder across deployer wallets is the simplest
    bundle-farm fingerprint: one operator, many launch wallets.
    Measurement only.
    """
    try:
        hist = hist if hist is not None else load_registry()
        funders = funders if funders is not None else load_funders()
        by_funder: Dict[str, List[str]] = {}
        for dep in (hist or {}):
            f = (funders.get(dep) or {}).get("funder")
            if f:
                by_funder.setdefault(f, []).append(dep)
        return {f: deps for f, deps in by_funder.items() if len(deps) >= 2}
    except Exception:
        return {}


# ----------------------------------------------------------------------
# Blacklist — PROPOSAL ONLY. The blacklist file is human-managed;
# proposals are review-only. Nothing here gates, blocks, or auto-adds.
# ----------------------------------------------------------------------

def load_blacklist() -> List[str]:
    data = _load_json_0600(_BLACKLIST_FILE)
    deps = data.get("deployers")
    return [str(d) for d in deps] if isinstance(deps, list) else []


def is_blacklisted(deployer: Optional[str]) -> bool:
    return bool(deployer) and deployer in set(load_blacklist())


def load_bl_proposals() -> List[Dict[str, Any]]:
    data = _load_json_0600(_BL_PROPOSALS_FILE)
    items = data.get("proposals")
    return [x for x in (items or []) if isinstance(x, dict)]


def propose_blacklist(hist: Optional[Dict[str, Any]] = None,
                      now: Optional[float] = None,
                      degen: Any = None) -> List[Dict[str, Any]]:
    """Propose rug-heavy deployers for the human-managed blacklist.

    Criteria: >= deployer_blacklist_min_launches (default 3) launches
    AND rug rate >= deployer_rug_blacklist_bar (default 0.5), not
    already blacklisted. Returns NEW proposals only (never raises).
    """
    out: List[Dict[str, Any]] = []
    try:
        now = now if now is not None else time.time()
        hist = hist if hist is not None else load_registry()
        min_l = max(1, int(_cfg(degen, "deployer_blacklist_min_launches", 3)
                           or 3))
        rug_bar = float(_cfg(degen, "deployer_rug_blacklist_bar", 0.5)
                        or 0.5)
        blacklisted = set(load_blacklist())
        for dep, entry in (hist or {}).items():
            if not isinstance(entry, dict) or dep in blacklisted:
                continue
            if int(entry.get("launches_seen") or 0) < min_l:
                continue
            if rug_rate(entry) < rug_bar:
                continue
            out.append({
                "id": "bl-%s-%d" % (dep[:8], int(now)),
                "kind": "deployer_blacklist_add",
                "deployer": dep,
                "launches_seen": int(entry.get("launches_seen") or 0),
                "rugs": int(entry.get("rugs") or 0),
                "rug_rate": round(rug_rate(entry), 3),
                "hit_rate": round(hit_rate(entry), 3),
                "rationale": ("%d launches, %d rugs (%.0f%% rug rate). "
                              "Review before blacklisting — proposal "
                              "only, nothing auto-blocked."
                              % (int(entry.get("launches_seen") or 0),
                                 int(entry.get("rugs") or 0),
                                 rug_rate(entry) * 100)),
                "status": "open",
                "created_ts": now,
            })
    except Exception as exc:
        logger.warning("deployer blacklist proposals failed: %s", exc)
    return out


def save_bl_proposals(new: List[Dict[str, Any]],
                      now: Optional[float] = None,
                      ttl_days: float = 30.0) -> List[Dict[str, Any]]:
    """Merge new blacklist proposals; dedupe open ones; expire stale.

    Open proposals older than ttl_days are marked "expired" (kept for
    audit). Bounded to the newest 50. Returns proposals actually added.
    """
    now = now if now is not None else time.time()
    existing = load_bl_proposals()
    open_deps = {x.get("deployer") for x in existing
                 if x.get("status") == "open"}
    cutoff = now - max(1.0, ttl_days) * 86400.0
    kept: List[Dict[str, Any]] = []
    expired = 0
    for x in existing:
        if x.get("status") == "open" and float(
                x.get("created_ts") or 0) < cutoff:
            x["status"] = "expired"
            expired += 1
        kept.append(x)
    added: List[Dict[str, Any]] = []
    for prop in new or []:
        if not isinstance(prop, dict) or not prop.get("deployer"):
            continue
        if prop["deployer"] in open_deps:
            continue
        kept.append(prop)
        open_deps.add(prop["deployer"])
        added.append(prop)
    if expired:
        logger.warning("deployer blacklist: %d stale proposals expired",
                       expired)
    _save_json_0600(_BL_PROPOSALS_FILE, {"version": 1,
                                        "proposals": kept[-50:]})
    return added


# ----------------------------------------------------------------------
# Daily orchestrator (wiring only — called from the daemon's daily pass)
# ----------------------------------------------------------------------

def daily_intel(state: Dict[str, Any],
                degen: Any,
                rpc: Optional[Callable[[str, List[Any]], Any]] = None,
                now: Optional[float] = None) -> Dict[str, Any]:
    """One Phase-6 intelligence pass. Measurement only — never raises.

    Resolves first-funders for recently-seen deployers missing a cached
    funder (bounded per day), recomputes funder clusters, and generates
    blacklist proposals. Writes only its own ledger key and the 0600
    runtime files — never registries, never trading state.
    """
    now = now if now is not None else time.time()
    summary: Dict[str, Any] = {"ran": False, "funders_resolved": 0,
                               "clusters": 0, "new_bl_proposals": 0}
    try:
        if not bool(_cfg(degen, "deployer_intel_enabled", True)):
            summary["reason"] = "disabled"
            return summary
        ledger = state.setdefault(_INTEL_KEY, {})
        if not isinstance(ledger, dict):
            state[_INTEL_KEY] = ledger = {}
        interval = 86400.0
        if now - float(ledger.get("last_run_ts") or 0) < interval:
            summary["reason"] = "throttled"
            return summary
        ledger["last_run_ts"] = now
        summary["ran"] = True
        if rpc is None:
            try:
                rpc = _rpc
            except Exception:
                rpc = None
        hist = load_registry()
        max_new = max(1, int(_cfg(degen, "deployer_intel_max_funders_per_day",
                                  5) or 5))
        if rpc is not None:
            funders = load_funders()
            recent = sorted(
                ((d, e) for d, e in hist.items()
                 if isinstance(e, dict)
                 and now - float(e.get("last_seen") or 0) < 7 * 86400.0
                 and d not in funders),
                key=lambda de: float((de[1] or {}).get("last_seen") or 0),
                reverse=True)[:max_new]
            for dep, _e in recent:
                if helius_ratelimit.cooldown_remaining() > 0:
                    summary["rate_limited"] = True
                    break  # shared Helius cooldown: stop, retry tomorrow
                if funder_for(dep, rpc, now=now) is not None:
                    summary["funders_resolved"] += 1
        clusters = funder_clusters(hist)
        summary["clusters"] = len(clusters)
        summary["clustered_deployers"] = sum(len(v)
                                             for v in clusters.values())
        new_props = propose_blacklist(hist, now=now, degen=degen)
        added = save_bl_proposals(new_props, now=now)
        summary["new_bl_proposals"] = len(added)
        summary["open_bl_proposals"] = sum(
            1 for x in load_bl_proposals() if x.get("status") == "open")
        return summary
    except Exception as exc:
        logger.warning("deployer daily intel failed: %s", exc)
        return summary
