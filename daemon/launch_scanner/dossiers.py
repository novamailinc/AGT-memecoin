#!/usr/bin/env python3
"""Birth dossiers (2026-10-23, P8 — scoped).

The Phase-1 design principle: do the homework DURING the bonding
curve, not at graduation. When the PumpPortal stream reports a
birth that passes a cheap payload filter (meaningful dev buy or
starting market cap), a background worker opens a dossier file at
~/.forex-agent/dossiers/<mint>.json and precomputes:

  * instant safety — safety.check_token (RugCheck report, else the
    instant_safety on-chain fallback), the exact function the grad
    lanes call at entry;
  * deployer — the creating wallet from the birth event, plus how
    many dossiers that deployer has opened in the TTL window;
  * holder-velocity proxy — bonding-curve virtual SOL reserve
    samples (open, then re-sampled on the mint's migration event):
    v_sol growth IS the curve-phase demand curve, readable free.

At migration, the grad lanes READ the dossier: a fresh, conclusive
safety result is reused instead of recomputed; a miss, a stale
dossier, or an inconclusive (unknown) result falls back to the
current live behavior EXACTLY — gates are unchanged, this is a pure
accelerator. TTL: dossiers are deleted 24h after birth; cleanup runs
opportunistically inside the event handlers (no standing timer).

Caps keep the precompute honest: at most MAX_OPEN_DOSSIERS live
dossiers and MAX_PER_DAY new ones per day — the stream sees ~30k
births/day and precomputing all of them would be waste, not signal.
Never raises.
"""
import contextlib
import fcntl
import json
import logging
import os
import queue
import threading
import time

try:
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import reserves as _res
except Exception:  # pragma: no cover - script-style import path
    try:
        import safety as _safety  # type: ignore
        import reserves as _res  # type: ignore
    except Exception:
        _safety = None  # type: ignore
        _res = None  # type: ignore

logger = logging.getLogger("forex_agent.daemon.launch_scanner.dossiers")

HOME = (os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser("~/.forex-agent"))
DOSSIER_DIR = os.path.join(HOME, "dossiers")

# Persistent launch memory (2026-11-23, request section 4.1):
# dossiers expire after 24h, which used to erase everything the
# bot learned about a launch. On TTL eviction the sweep now
# archives a compact record to an append-only JSONL that never
# expires: the launch is REMEMBERED (deployer, safety verdict
# summary, birth facts), even though the working dossier is gone.
# The archive is advisory memory only: no gate, entry or exit
# reads it in this slice, so behavior is unchanged. Volume: the
# MAX_PER_DAY dossier cap bounds launch-record inflow
# (<=400/day), outcomes are low-volume (closes only), and past
# ARCHIVE_MAX_BYTES the sweep compacts to the resolved kept set
# (latest launch per mint + all outcomes + anchor).
ARCHIVE_FILE = os.path.join(HOME, "memory", "launch_archive.jsonl")

# Memory semantics (code-defined, no interpretation layer):
# - every record carries a schema version ("v": 1); readers skip
#   versions they do not understand instead of misreading them;
# - conflict: two records for the same mint that disagree on the
#   safety verdict resolve to "conflicting", never a silent pick;
# - staleness: launch_memory() reports the record's age and a
#   stale flag past ARCHIVE_STALE_S; a stale record is labeled,
#   never silently treated as fresh;
# - availability: a missing archive file means genuine zero
#   history; an unreadable one means UNAVAILABLE (archive_status
#   and launch_memory say so) - corrupt memory never looks like
#   a clean empty history;
# - durability: a dossier is only evicted after its archive write
#   SUCCEEDS; writers append under an exclusive flock so
#   concurrent daemons never interleave a line;
# - retention, with ONE enforceable invariant and no blanket
#   "bounded" claim. The invariant: the per-mint LAUNCH set is
#   hard-capped at MAX_KEPT_LAUNCHES after every compaction
#   (older ones are first aggregated into per-deployer-per-year
#   launch_rollup records so deployer linkage survives). What is
#   NOT hard-bounded: raw outcomes are bounded only by the
#   OUTCOME_RAW_S time window, not by rate, so a high close rate
#   grows the file within the year; and rollup rows grow with
#   distinct (deployer, year) and (lane, year, deployer)
#   cardinality, which no constant caps. Total file size and
#   full-scan retrieval cost are therefore bounded in practice
#   by activity, NOT by an enforceable constant. Retrieval is a
#   full scan; a real index and a rollup-cardinality policy are
#   named future slices, not claimed here.
# - outcome provenance, two axes: every outcome is a SIMULATED
#   paper-lane result (never verified real money), AND each
#   carries exit_price_kind classifying the close price as a
#   current-price simulated fill, a stale-price estimate, a
#   zero write-down, or unknown (exit_provenance labels).
#   Win/loss counting uses ONLY current-price simulated fills;
#   stale/write-down/unknown outcomes are counted separately as
#   the unqualified samples they are.
ARCHIVE_SCHEMA_V = 1
ARCHIVE_STALE_S = 30 * 86400
ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
OUTCOME_RAW_S = 365 * 86400  # raw outcomes kept a year, then rolled up
LAUNCH_RAW_S = 365 * 86400   # per-mint launches kept a year, then rolled up
MAX_KEPT_LAUNCHES = 20000    # hard cap on per-mint launch records


def _classify_exit_price_kind(closed_reason):
    """exit_provenance label for a close reason; "unknown" on any
    doubt. Never raises."""
    try:
        try:
            from daemon.launch_scanner import exit_provenance as _pk
        except Exception:
            import exit_provenance as _pk  # type: ignore
        return _pk.classify_reason(closed_reason)
    except Exception:
        return "unknown"


def _archive_lock_path():
    return ARCHIVE_FILE + ".lock"


@contextlib.contextmanager
def _archive_write_lock():
    """Exclusive cross-process lock for archive appends (the
    verdict_ledger events.jsonl.lock pattern). Never raises: on
    any lock failure the writer proceeds WITHOUT the lock - the
    reader tolerates a torn line (skips it) and the alternative
    is losing the record entirely."""
    fd = None
    try:
        fd = os.open(_archive_lock_path(), os.O_CREAT | os.O_RDWR,
                     0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass

TTL_S = 24 * 3600
SAFETY_FRESH_S = 1200.0     # reuse window for precomputed safety
MAX_OPEN_DOSSIERS = 300
MAX_PER_DAY = 400
MIN_INITIAL_BUY_SOL = 1.0   # cheap birth filter: real dev conviction
MIN_MARKET_CAP_SOL = 30.0   # ...or a starting cap that matters

_lock = threading.Lock()
_queue = queue.Queue(maxsize=500)
_worker = None
_day = {"date": "", "count": 0}
_last_sweep = 0.0


def _path(mint):
    return os.path.join(DOSSIER_DIR, mint + ".json")


def _write(mint, d):
    try:
        os.makedirs(DOSSIER_DIR, exist_ok=True)
        tmp = _path(mint) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _path(mint))
        return True
    except Exception:
        return False


def read_dossier(mint, now=None):
    """The dossier dict for a mint, or None (miss / expired /
    unreadable). Never raises."""
    try:
        now = time.time() if now is None else now
        with open(_path(mint)) as f:
            d = json.load(f)
        if now - float(d.get("born_ts") or 0) > TTL_S:
            return None
        return d
    except Exception:
        return None


def read_safety(mint, now=None):
    """A safety.check_token-shaped report from the dossier when it
    is fresh AND conclusive; None otherwise (caller falls back to
    the live check — behavior identical on any doubt)."""
    try:
        now = time.time() if now is None else now
        d = read_dossier(mint, now)
        if not d:
            return None
        s = d.get("safety") or {}
        rep = s.get("report")
        if not isinstance(rep, dict) or rep.get("unknown"):
            return None
        ts = float(s.get("ts") or 0)
        age = now - ts
        # Same existing window. Written as a positive test so NaN, infinite and future-dated stamps fall through to
        # the live check instead of passing the old "age > window" comparison.
        if not (0.0 <= age <= SAFETY_FRESH_S):
            return None
        out = dict(rep)
        out["source"] = "dossier:" + str(rep.get("source") or "")
        # Provenance only: LOCAL dossier store time, not an upstream observation time. No new threshold.
        out["safety_ts"] = ts
        out["safety_ts_kind"] = "dossier_local_store_time"
        return out
    except Exception:
        return None


def _birth_filter(item):
    """Cheap payload gate for opening a dossier. Pure."""
    try:
        extra = (item or {}).get("extra") or {}
        return float(extra.get("initial_buy_sol") or 0) \
            >= MIN_INITIAL_BUY_SOL or \
            float(extra.get("market_cap_sol") or 0) \
            >= MIN_MARKET_CAP_SOL
    except Exception:
        return False


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _archive_append(rec):
    """One locked append to the archive. Returns True only when
    the record is durably written. Never raises."""
    try:
        os.makedirs(os.path.dirname(ARCHIVE_FILE), exist_ok=True)
        with _archive_write_lock():
            with open(ARCHIVE_FILE, "a") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def _classify_curve_samples(samples):
    """Durable launch curve kind. The current birth reserve sample is
    only a preliminary bonding-curve snapshot, so at dossier expiry
    one sample cannot establish sustained growth. Two or more reserve
    samples give a bounded sequence without inventing pump/dump labels.
    Missing acquisition is unknown, never a clean stable curve."""
    try:
        n = len(samples or [])
    except Exception:
        return "unknown_curve"
    if n <= 0:
        return "unknown_curve"
    if n == 1:
        return "single_preliminary_snapshot"
    return "multi_sample_curve_sequence"


def _migrated_evidence(d, now=None):
    """Migration provenance as (value, conflict), never invented
    either way. A stamp is VALID only when finite, positive and
    not after the record's own archive time - the only field the
    production observer writes. True: an explicit flag, or a valid
    stamp. False: an explicit recorded negative with NO valid
    stamp contradicting it. An explicit negative contradicted by a
    valid stamp is a CONFLICT: (None, True) - labeled unrecorded,
    never a silently trusted False. Absent, zero, non-finite or
    future-dated evidence: (None, False), unrecorded."""
    m = d.get("migrated")
    stamped = False
    ts = d.get("migrated_ts")
    if not isinstance(ts, bool):
        try:
            t = float(ts or 0)
            stamped = 0 < t != float("inf") \
                and (now is None or t <= now)
        except (TypeError, ValueError):
            stamped = False
    if m is True:
        return True, False
    if m is False:
        return (None, True) if stamped else (False, False)
    return (True, False) if stamped else (None, False)


CURVE_HISTORY_KEPT = 8  # retained samples per archived launch


def _curve_history(d):
    """Bounded actual curve-sample content for the archive record:
    the newest CURVE_HISTORY_KEPT observed samples plus the total
    seen, labeled preliminary (scanner snapshots, never verified
    fills or a complete curve). Legacy reserve_samples entries have
    no per-sample schema: they stay a count only, never dressed up
    as curve history. None when there is nothing honest to keep."""
    samples = d.get("curve_samples")
    if not isinstance(samples, list) or not samples:
        return None
    pts = [x for x in samples if isinstance(x, dict)]
    if not pts:
        return None
    kept = pts[-CURVE_HISTORY_KEPT:]
    return {"samples": kept,
            "samples_total": len(samples),
            "retained": len(kept),
            "preliminary": True}


def _archive_dossier(d, now):
    """Append one compact long-term record for an expiring dossier
    to the persistent archive. Returns True when the record is
    durably written; the caller MUST NOT evict the dossier on
    False (memory loss is worse than a stale dossier). Never
    raises."""
    try:
        if not isinstance(d, dict) or not d.get("mint"):
            return False
        safety = d.get("safety") or {}
        report = safety.get("report")
        verdict = None
        if isinstance(report, dict):
            verdict = "unknown" if report.get("unknown") else "conclusive"
        rec = {
            "v": ARCHIVE_SCHEMA_V,
            "record_type": "launch",
            "mint": d.get("mint"),
            "born_ts": d.get("born_ts"),
            "archived_ts": now,
            "deployer": d.get("deployer"),
            "safety_verdict": verdict,
            "safety_source": (report or {}).get("source")
            if isinstance(report, dict) else None,
            "initial_buy_sol": d.get("initial_buy_sol"),
            "market_cap_sol": d.get("market_cap_sol"),
            "migrated": _migrated_evidence(d, now)[0],
            "reserve_samples": len(d.get("curve_samples")
                if "curve_samples" in d else (d.get("reserve_samples") or [])),
            "curve_kind": _classify_curve_samples(d.get("curve_samples")
                if "curve_samples" in d else (d.get("reserve_samples") or [])),
            "reserve_acquisition": d.get("reserve_acquisition") or {
                "status": "unknown", "reason": "legacy_unrecorded"},
        }
        if _migrated_evidence(d, now)[1]:
            # Explicit negative contradicted by a valid stamp:
            # labeled as a conflict, never silently False.
            rec["migration_conflict"] = True
        ch = _curve_history(d)
        if ch:
            rec["curve_history"] = ch
        return _archive_append(rec)
    except Exception:
        return False


def record_outcome(mint, lane, ts=None, pnl_usd=None,
                   realized_mult=None, closed_reason=None):
    """Record one resolved signal outcome (a lane close) in the
    durable archive. Called from the verdict-ledger funnel
    (kind=\"close\"). Observability-grade: returns True when the
    record is durably written, False otherwise; never raises,
    never touches a decision path."""
    try:
        if not mint:
            return False
        rec = {
            "v": ARCHIVE_SCHEMA_V,
            "record_type": "outcome",
            "mint": mint,
            "lane": lane,
            "archived_ts": time.time() if ts is None else ts,
            "pnl_usd": pnl_usd,
            "realized_mult": realized_mult,
            "closed_reason": closed_reason,
            # Today's closes are simulated paper-lane results.
            # The label travels with the record so no consumer
            # can mistake it for a verified real-money outcome.
            "provenance": "simulated",
            "qualified": False,
            # And WITHIN simulated: whether the close price was a
            # current observed quote or a stale/estimated/write-
            # down figure (the qualification blocker axis).
            "exit_price_kind": _classify_exit_price_kind(
                closed_reason),
        }
        return _archive_append(rec)
    except Exception:
        return False


def _verdict_ledger_has_history():
    """True when the verdict ledger holds records, meaning the
    bot HAS traded/closed before - so a missing archive is lost
    history, not genuine first use. Never raises."""
    try:
        p = os.path.join(HOME, "verdict_ledger", "events.jsonl")
        return os.path.exists(p) and os.path.getsize(p) > 0
    except Exception:
        return False


def archive_status():
    """Availability + integrity of the durable memory, so no
    consumer ever mistakes a corrupt store for a clean empty
    history. Never raises.

    available=False means the memory cannot be read or is known
    to be incomplete: consumers must treat history as UNKNOWN,
    never as zero. A missing file is available=True with
    records=0 ONLY when the verdict ledger is also empty
    (genuine first use); a missing archive alongside an existing
    ledger is lost history -> available=False. unreadable_lines
    > 0 means the history is PARTIAL: torn/corrupt records were
    skipped, so what remains is not the complete record."""
    st = {"available": True, "records": 0, "unreadable_lines": 0,
          "error": None}
    try:
        if not os.path.exists(ARCHIVE_FILE):
            if _verdict_ledger_has_history():
                st["available"] = False
                st["error"] = "archive_missing_with_existing_history"
            return st
        with open(ARCHIVE_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    st["unreadable_lines"] += 1
                    continue
                if isinstance(rec, dict) \
                        and rec.get("v") == ARCHIVE_SCHEMA_V:
                    st["records"] += 1
                else:
                    st["unreadable_lines"] += 1
    except Exception as e:
        st["available"] = False
        st["error"] = type(e).__name__
    return st


def launch_archive(mint=None, deployer=None, limit=100):
    """Persistent launch memory, newest first. Filter by mint
    and/or deployer. Returns [] on any failure; never raises.
    Advisory only: no gate consults this in the current slice."""
    out = []
    try:
        with open(ARCHIVE_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("v") != ARCHIVE_SCHEMA_V:
                    continue  # unknown schema: skip, never misread
                if mint is not None and rec.get("mint") != mint:
                    continue
                if deployer is not None \
                        and rec.get("deployer") != deployer:
                    continue
                out.append(rec)
    except Exception:
        return []
    out.reverse()
    return out[:limit]


def outcome_records(mint=None, limit=1000):
    """Outcome records (lane closes), newest first. Same reader
    discipline as launch_archive; never raises."""
    return [r for r in launch_archive(mint=mint, limit=limit)
            if r.get("record_type") == "outcome"]


def launch_memory(mint, now=None):
    """The resolved durable record for one mint, or None.

    Resolution is code-defined: the record with the newest
    archived_ts wins; records that DISAGREE on the safety verdict
    resolve to "conflicting" (never a silent pick); age_s and a
    stale flag (past ARCHIVE_STALE_S) are attached so a consumer
    reads labeled staleness, never silent freshness. Never
    raises."""
    try:
        now = time.time() if now is None else now
        st = archive_status()
        if not st.get("available"):
            # Corrupt/unreadable memory is NOT a clean empty
            # history: say so explicitly.
            return {"unavailable": True,
                    "error": st.get("error") or "unreadable"}
        partial = st.get("unreadable_lines", 0) > 0
        recs = [r for r in launch_archive(mint=mint, limit=1000)
                if r.get("record_type", "launch") == "launch"]
        outcomes = outcome_records(mint=mint)
        if not recs and not outcomes:
            return None
        out = {}
        if recs:
            newest = max(recs, key=lambda r: float(
                r.get("archived_ts") or 0))
            verdicts = {r.get("safety_verdict") for r in recs}
            conflict = len(verdicts) > 1
            out = dict(newest)
            out["entries"] = len(recs)
            out["conflict"] = conflict
            if conflict:
                out["safety_verdict"] = "conflicting"
            age = now - float(newest.get("archived_ts") or now)
            out["age_s"] = int(max(0.0, age))
            out["stale"] = age > ARCHIVE_STALE_S
        else:
            age = now - float(outcomes[0].get("archived_ts") or now)
            out = {"mint": mint, "entries": 0, "conflict": False,
                   "age_s": int(max(0.0, age)),
                   "stale": age > ARCHIVE_STALE_S}
        # Skipped torn/corrupt lines mean this history is PARTIAL,
        # never a complete clean record.
        out["partial"] = partial
        out["outcomes"] = outcomes
        out["resolved_lanes"] = sorted(
            {str(o.get("lane")) for o in outcomes if o.get("lane")})
        return out
    except Exception:
        return None


_last_registry_refresh = 0.0
REGISTRY_REFRESH_S = int(os.environ.get(
    "DOSSIER_REGISTRY_REFRESH_S", "900"))


def _maybe_refresh_registry(now):
    """Advisory registry UPDATE RULE (2026-11-23, section 4 memory
    semantics): nothing else in the runtime refreshes the rugger
    registry - the only refresher was a manually-run script, so
    the advisory record could go arbitrarily stale. The sweep
    re-derives it from the evidence files (including this
    archive) at most once per REGISTRY_REFRESH_S. Advisory only:
    the registry stays measurement/display, no rule or threshold
    changes, and a refresh failure changes nothing. Never
    raises."""
    global _last_registry_refresh
    try:
        if now - _last_registry_refresh < REGISTRY_REFRESH_S:
            return
        try:
            from daemon.launch_scanner import rugger_registry as _rr
        except Exception:
            import rugger_registry as _rr  # type: ignore
        _rr.refresh()
        # Stamp AFTER the attempt: a failed attempt (import or
        # internal failure) must not suppress the next retry for
        # a whole cadence.
        _last_registry_refresh = now
    except Exception:
        pass


def _sweep(now):
    """Opportunistic TTL cleanup + deployer census. Runs inside
    event handlers at most once per 300s of wall time."""
    global _last_sweep
    if now - _last_sweep < 300:
        return
    _last_sweep = now
    # Each phase runs in its own guard: an eviction-loop failure
    # must not skip compaction or the registry refresh.
    try:
        os.makedirs(DOSSIER_DIR, exist_ok=True)
        for fn in os.listdir(DOSSIER_DIR):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(DOSSIER_DIR, fn)
            try:
                with open(p) as f:
                    d = json.load(f)
                if now - float(d.get("born_ts") or 0) > TTL_S:
                    # Remember before forgetting (section 4.1):
                    # evict ONLY after the archive write lands -
                    # a failed archive keeps the dossier, because
                    # losing the memory is worse than a stale
                    # dossier.
                    d.setdefault("mint", fn[:-5])
                    if _archive_dossier(d, now):
                        os.unlink(p)
            except Exception:
                continue
    except Exception:
        pass
    try:
        _maybe_compact_archive(now)
    except Exception:
        pass
    _maybe_refresh_registry(now)
    try:
        # Lever 3 (2026-11-16): clean per-mint lock files whose
        # dossier is gone/expired. Only unlink when no builder
        # holds the lock (flock EX|NB succeeds) — a held lock
        # means a build is in flight right now. The narrow race
        # (a builder opening the path just as it is unlinked)
        # can only drop one best-effort dossier, never corrupt
        # one: dossiers are accelerators, a miss falls back to
        # the live check.
        for fn in os.listdir(DOSSIER_DIR):
            if not fn.endswith(".lock"):
                continue
            mint = fn[:-5]
            p = os.path.join(DOSSIER_DIR, fn)
            try:
                stale = True
                jp = os.path.join(DOSSIER_DIR, mint + ".json")
                if os.path.exists(jp):
                    with open(jp) as f:
                        d = json.load(f)
                    stale = now - float(d.get("born_ts") or 0) > TTL_S
                if not stale:
                    continue
                fd = os.open(p, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    os.close(fd)
                    continue
                try:
                    os.unlink(p)
                finally:
                    os.close(fd)
            except Exception:
                continue
    except Exception:
        pass


_KIND_KEYS = ("current_price_simulated_fill",
              "stale_price_estimate", "write_down_zero", "unknown")


def _outcome_rollup_row(lane, year, deployer):
    row = {"v": ARCHIVE_SCHEMA_V, "record_type": "outcome_rollup",
           "lane": lane, "year": year, "deployer": deployer or None,
           "count": 0, "wins": 0, "losses": 0,
           "provenance": "simulated", "qualified": False}
    for k in _KIND_KEYS:
        row["kind_" + k] = 0
    return row


def _rollup_add_outcome(row, rec):
    """Fold one raw outcome into a rollup row. Win/loss counting
    uses ONLY current-price simulated fills; stale/write-down/
    unknown kinds are counted as the unqualified samples they
    are, never as wins or losses."""
    row["count"] += 1
    kind = rec.get("exit_price_kind")
    if kind not in _KIND_KEYS:
        kind = "unknown"  # legacy record with no label
    row["kind_" + kind] = row.get("kind_" + kind, 0) + 1
    if kind == "current_price_simulated_fill":
        m = rec.get("realized_mult")
        if isinstance(m, (int, float)):
            if m > 1.0:
                row["wins"] += 1
            else:
                row["losses"] += 1


def _launch_rollup_row(deployer, year):
    return {"v": ARCHIVE_SCHEMA_V, "record_type": "launch_rollup",
            "deployer": deployer or None, "year": year,
            "launches": 0, "conclusive": 0, "unknown": 0}


def _rollup_add_launch(row, rec):
    row["launches"] += 1
    sv = rec.get("safety_verdict")
    if sv == "conclusive":
        row["conclusive"] += 1
    elif sv == "unknown":
        row["unknown"] += 1
    if rec.get("migrated") is True:
        row["migrated"] = row.get("migrated", 0) + 1
    elif rec.get("migrated") is False:
        row["not_migrated_recorded"] = row.get(
            "not_migrated_recorded", 0) + 1
    elif rec.get("migration_conflict") is True:
        # Contradictory evidence is its own count, never lumped
        # into recorded negatives or clean unrecorded.
        row["migration_conflict"] = row.get(
            "migration_conflict", 0) + 1
    else:
        row["migration_unrecorded"] = row.get(
            "migration_unrecorded", 0) + 1
    kind = rec.get("curve_kind")
    if not kind:
        kind = "unknown_curve"  # no curve/reserve evidence recorded
    row["curve_kind_" + str(kind)] = row.get(
        "curve_kind_" + str(kind), 0) + 1


def _maybe_compact_archive(now):
    """Retention with an enforceable invariant. Compaction runs
    when the file passes ARCHIVE_MAX_BYTES OR the per-mint
    launch records pass MAX_KEPT_LAUNCHES; afterwards the file
    holds only: one anchor, <= MAX_KEPT_LAUNCHES newest per-mint
    launches (older and >LAUNCH_RAW_S ones folded into
    per-deployer-per-year launch_rollup rows so the mint->deployer
    linkage survives aggregation), raw outcomes younger than
    OUTCOME_RAW_S (older folded into per-(lane, year, deployer)
    outcome_rollup rows, deployer resolved from the launch
    records present in this same pass), and rollup rows whose
    cardinality grows only with distinct key pairs. The WHOLE
    read+rewrite runs under the write lock, so a concurrent
    append can never be silently discarded by the rename.
    Best-effort; never raises."""
    try:
        if not os.path.exists(ARCHIVE_FILE):
            return
        launch_lines = 0
        try:
            with open(ARCHIVE_FILE) as f:
                for line in f:
                    if '"record_type": "outcome"' not in line \
                            and '"record_type":"outcome"' not in line:
                        launch_lines += 1
        except Exception:
            launch_lines = 0
        if os.path.getsize(ARCHIVE_FILE) <= ARCHIVE_MAX_BYTES \
                and launch_lines <= MAX_KEPT_LAUNCHES:
            return
        tmp = ARCHIVE_FILE + ".compact-tmp"
        # Lock across read AND rename: an append landing between
        # the read and the rename would otherwise be silently
        # discarded. The lock is the same one _archive_append
        # takes, so appends queue behind the compaction.
        with _archive_write_lock():
            launches, outcomes, rollups = {}, [], {}
            launch_rollups = {}
            dropped = 0
            with open(ARCHIVE_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        dropped += 1
                        continue
                    if not isinstance(rec, dict) \
                            or rec.get("v") != ARCHIVE_SCHEMA_V:
                        dropped += 1
                        continue
                    rtype = rec.get("record_type", "launch")
                    if rtype == "compaction_anchor":
                        # Superseded bookkeeping, replaced by the
                        # new anchor below: not a lost record.
                        continue
                    if rtype == "outcome":
                        try:
                            age = now - float(
                                rec.get("archived_ts") or 0)
                        except Exception:
                            age = 0.0
                        if age > OUTCOME_RAW_S:
                            # Resolve the deployer from the launch
                            # record seen in this same pass so the
                            # mint->deployer linkage survives the
                            # rollup; unresolved -> None key,
                            # labeled by absence, never guessed.
                            lr = launches.get(
                                str(rec.get("mint") or "")) or {}
                            year = time.strftime(
                                "%Y", time.gmtime(float(
                                    rec.get("archived_ts") or 0)))
                            key = (str(rec.get("lane") or ""),
                                   year,
                                   str(lr.get("deployer") or ""))
                            ru = rollups.setdefault(
                                key, _outcome_rollup_row(*key))
                            _rollup_add_outcome(ru, rec)
                        else:
                            outcomes.append(rec)
                    elif rtype == "outcome_rollup":
                        key = (str(rec.get("lane") or ""),
                               str(rec.get("year") or ""),
                               str(rec.get("deployer") or ""))
                        ru = rollups.setdefault(
                            key, _outcome_rollup_row(*key))
                        for k in ("count", "wins", "losses") \
                                + tuple("kind_" + x
                                        for x in _KIND_KEYS):
                            try:
                                ru[k] += int(rec.get(k) or 0)
                            except Exception:
                                pass
                    elif rtype == "launch_rollup":
                        key = (str(rec.get("deployer") or ""),
                               str(rec.get("year") or ""))
                        lru = launch_rollups.setdefault(
                            key, _launch_rollup_row(*key))
                        for k, v in rec.items():
                            if k in ("v", "record_type", "deployer",
                                     "year"):
                                continue
                            if k == "partial":
                                # Provenance flag, not a counter:
                                # propagate, never sum, never count
                                # as a dropped record.
                                if v is True:
                                    lru["partial"] = True
                                continue
                            try:
                                if isinstance(v, bool) or v is None:
                                    raise ValueError("unmergeable")
                                iv = int(v)
                                # Only true non-negative integer
                                # counters merge: negatives,
                                # fractions, non-numerics and
                                # non-finite values would violate
                                # the totals silently - mark
                                # partial instead.
                                if iv != v or iv < 0:
                                    raise ValueError("unmergeable")
                            except Exception:
                                dropped += 1
                                lru["partial"] = True
                                continue
                            lru[k] = lru.get(k, 0) + iv
                    elif rtype == "launch" and rec.get("mint"):
                        launches[rec["mint"]] = rec  # newest wins
                    else:
                        dropped += 1
            # Launch retention: raw per-mint records older than
            # LAUNCH_RAW_S fold into per-deployer-per-year
            # rollups; then the hard cap MAX_KEPT_LAUNCHES folds
            # the oldest remaining until the cap holds. The
            # per-mint set is therefore always bounded by a
            # constant, not by mint cardinality.
            def _lts(rec):
                try:
                    return float(rec.get("archived_ts") or 0)
                except Exception:
                    return 0.0
            kept_launches = {}
            for mint, rec in launches.items():
                ts = _lts(rec)
                if ts and now - ts > LAUNCH_RAW_S:
                    key = (str(rec.get("deployer") or ""),
                           time.strftime("%Y", time.gmtime(ts)))
                    lru = launch_rollups.setdefault(
                        key, _launch_rollup_row(*key))
                    _rollup_add_launch(lru, rec)
                else:
                    kept_launches[mint] = rec
            if len(kept_launches) > MAX_KEPT_LAUNCHES:
                oldest = sorted(kept_launches.values(), key=_lts)
                for rec in oldest[:len(kept_launches)
                                  - MAX_KEPT_LAUNCHES]:
                    ts = _lts(rec)
                    key = (str(rec.get("deployer") or ""),
                           time.strftime("%Y", time.gmtime(ts)))
                    lru = launch_rollups.setdefault(
                        key, _launch_rollup_row(*key))
                    _rollup_add_launch(lru, rec)
                    del kept_launches[rec["mint"]]
            kept = list(kept_launches.values()) + outcomes \
                + list(rollups.values()) \
                + list(launch_rollups.values())
            anchor = {"v": ARCHIVE_SCHEMA_V,
                      "record_type": "compaction_anchor",
                      "archived_ts": now,
                      "dropped_records": dropped,
                      "kept_records": len(kept)}
            with open(tmp, "w") as f:
                f.write(json.dumps(anchor, sort_keys=True) + "\n")
                for rec in kept:
                    f.write(json.dumps(rec, sort_keys=True) + "\n")
            os.replace(tmp, ARCHIVE_FILE)
    except Exception:
        pass


def _count_open(now):
    try:
        n = 0
        for fn in os.listdir(DOSSIER_DIR):
            if fn.endswith(".json"):
                n += 1
        return n
    except Exception:
        return MAX_OPEN_DOSSIERS  # unknown -> fail closed on volume


def _deployer_count(deployer, now):
    n = 0
    try:
        for fn in os.listdir(DOSSIER_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(DOSSIER_DIR, fn)) as f:
                    d = json.load(f)
                if d.get("deployer") == deployer:
                    n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


def _intel_fields(deployer):
    """Display-only intel surfacing (2026-10-23): serial-rugger
    summary + funding cluster for the deployer, read from the
    measurement registries (rugger_registry / funding_clusters).

    These fields are NEVER consulted by the safety precompute or
    any verdict — they exist so a human reading the dossier sees
    the deployer's record. Missing/corrupt registries -> fields
    omitted. Never raises."""
    out = {}
    try:
        if not deployer:
            return out
        try:
            from daemon.launch_scanner import rugger_registry as _rr
            from daemon.launch_scanner import funding_clusters as _fc
        except Exception:
            import rugger_registry as _rr  # type: ignore
            import funding_clusters as _fc  # type: ignore
        # Read-path freshness: on a quiet stream the sweep may not
        # run for a while; the CONSUMER of the advisory record
        # maintains its own cadence-bound update (same throttle,
        # advisory only, never raises).
        _maybe_refresh_registry(time.time())
        try:
            r = _rr.lookup(deployer)
            if r and int(r.get("launches") or 0) > 0:
                out["deployer_rugger"] = {
                    "flagged": bool(r.get("flagged")),
                    "launches": int(r.get("launches") or 0),
                    "dead": int(r.get("dead") or 0),
                }
            try:
                unjoined = int((_rr.stats() or {}).get(
                    "unjoined_dead_mints") or 0)
            except Exception:
                unjoined = 0
            if unjoined > 0:
                # Dead-mint evidence exists that NO source could
                # join to a deployer: the registry is incomplete.
                # Surface the missingness on every dossier intel
                # view instead of letting it read as complete.
                out["registry_incomplete"] = {
                    "unjoined_dead_mints": unjoined}
        except Exception:
            pass
        try:
            c = _fc.cluster_of(deployer)
            if c:
                out["funding_cluster"] = {
                    "cluster_id": c.get("cluster_id"),
                    "size": int(c.get("size") or 0),
                }
        except Exception:
            pass
        # Long-term memory consumption (2026-11-23, section 4.1):
        # the persistent launch archive (survives the 24h dossier
        # TTL) is READ here so the deployer's durable record is
        # visible on every new dossier. Display-only like the rest
        # of this function: never consulted by the safety
        # precompute or any verdict.
        try:
            st = archive_status()
            if st.get("available"):
                archived = launch_archive(deployer=deployer,
                                          limit=1000)
                launches = [r for r in archived
                            if r.get("record_type", "launch")
                            == "launch"]
                launch_rollups = [r for r in archived
                                  if r.get("record_type")
                                  == "launch_rollup"]
                if launches or launch_rollups:
                    mints = {r.get("mint") for r in launches}
                    outcomes = [o for o in outcome_records()
                                if o.get("mint") in mints]
                    # Win/loss counting uses ONLY current-price
                    # simulated fills; stale/estimated, write-down
                    # and unlabeled legacy outcomes are counted
                    # separately as unqualified samples.
                    cur = [o for o in outcomes
                           if o.get("exit_price_kind")
                           == "current_price_simulated_fill"]
                    mults = [o.get("realized_mult") for o in cur
                             if isinstance(o.get("realized_mult"),
                                           (int, float))]
                    out["deployer_history"] = {
                        "archived_launches": len(launches) + sum(
                            int(r.get("launches") or 0)
                            for r in launch_rollups),
                        # Rollup rows folded older launches into
                        # aggregates: totals include them, and the
                        # flag says this is not a pure raw record.
                        "includes_rollup": bool(launch_rollups),
                        "conclusive": sum(
                            1 for r in launches
                            if r.get("safety_verdict")
                            == "conclusive") + sum(
                            int(r.get("conclusive") or 0)
                            for r in launch_rollups),
                        "unknown": sum(
                            1 for r in launches
                            if r.get("safety_verdict") == "unknown")
                            + sum(int(r.get("unknown") or 0)
                                  for r in launch_rollups),
                        "outcomes": len(outcomes),
                        # Today's outcomes are SIMULATED paper-lane
                        # results: counted and labeled as such,
                        # never presented as verified real-money
                        # wins/losses. Code-defined: recorded
                        # realized multiple > 1.0 is a win, and
                        # only a current-price simulated fill
                        # counts.
                        "simulated_wins": sum(
                            1 for m in mults if m > 1.0),
                        "simulated_losses": sum(
                            1 for m in mults if m <= 1.0),
                        "outcomes_not_current_price": sum(
                            1 for o in outcomes
                            if o.get("exit_price_kind")
                            != "current_price_simulated_fill"),
                        "outcomes_provenance": "simulated",
                        "outcomes_qualified": False,
                        # Partial memory (skipped torn lines or a
                        # corrupt-counter rollup fold) or rolled-up
                        # older history: labeled, never shown as a
                        # complete clean record.
                        "partial": st.get("unreadable_lines", 0) > 0
                        or any(r.get("partial") is True
                               for r in launch_rollups),
                        "partial_rollups": sum(
                            1 for r in launch_rollups
                            if r.get("partial") is True),
                    }
            # unavailable -> field omitted: unknown history is
            # never shown as zero history.
        except Exception:
            pass
    except Exception:
        return {}
    return out


@contextlib.contextmanager
def _mint_claim(mint):
    """Cross-process claim for one mint's dossier build (Lever 3,
    2026-11-16). Four trader processes race the same births; without
    a claim, all four build the same dossier (up to ~6 Helius calls
    each). Yields True when this process may build (lock held, or
    locking unavailable — fail-open so a build is never lost to a
    lock error), False when another process is already building this
    mint. flock releases on process death, so a crash cannot wedge
    the mint. Never raises."""
    fd = None
    try:
        os.makedirs(DOSSIER_DIR, exist_ok=True)
        fd = os.open(os.path.join(DOSSIER_DIR, mint + ".lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        yield False
        return
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        yield True
        return
    try:
        yield True
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def _build(item):
    """Worker body: create one dossier (safety precompute is the
    slow part, ~seconds). Claims the mint cross-process first so
    four racing traders build it once, and skips when another
    process already built it. Never raises."""
    try:
        mint = str(item.get("mint") or "")
        if not mint:
            return
        with _mint_claim(mint) as claimed:
            if not claimed:
                return
            if read_dossier(mint):
                return
            _build_inner(item)
    except Exception as e:
        logger.warning("dossier build failed: %s", e)


def _build_inner(item):
    """Build one dossier file (called once per mint, under the
    cross-process claim). Never raises."""
    try:
        mint = str(item.get("mint") or "")
        if not mint:
            return
        extra = item.get("extra") or {}
        now = time.time()
        d = {
            "mint": mint,
            "symbol": str(item.get("symbol") or "?")[:32],
            "born_ts": float(item.get("created_ts") or now),
            "opened_ts": now,
            "deployer": str(extra.get("trader") or ""),
            "bonding_curve": str(extra.get("bonding_curve") or ""),
            "initial_buy_sol": float(extra.get("initial_buy_sol")
                                     or 0),
            "market_cap_sol": float(extra.get("market_cap_sol")
                                    or 0),
            "safety": {"ts": 0.0, "report": None},
            "curve_samples": [],
            "reserve_acquisition": {"status": "unavailable", "reason": "module_unavailable", "ts": now},
            "migrated_ts": 0.0,
        }
        if _res is not None:
            # Lever 3 (2026-11-16): curve_price_usd's return value
            # was discarded — it re-fetched this same curve account
            # (plus a mint-decimals fetch) for nothing. The single
            # get_account below serves the sample. A reserve/RPC
            # failure must NOT abort the build: the dossier is
            # still written, with no fabricated samples
            # (2026-11-23, fixture failure-path finding).
            try:
                _o, data = _res.get_account(
                    _res.derive_curve_pda(mint) or "")
                c = _res.parse_bonding_curve(data)
                d["reserve_acquisition"] = {
                    "status": "available" if c else "unknown",
                    "reason": "parsed" if c else "unreadable_response", "ts": now}
                if c:
                    d["curve_samples"].append(
                        {"ts": now, "v_sol": c["v_sol"],
                         "v_token": c["v_token"]})
            except Exception:
                d["reserve_acquisition"] = {"status": "unavailable",
                    "reason": "acquisition_failed", "ts": now}
        _write(mint, d)
        rep = None
        if _safety is not None:
            try:
                rep = _safety.check_token("solana", mint, timeout=10)
            except Exception:
                rep = None
        d2 = read_dossier(mint) or d
        d2["safety"] = {"ts": time.time(),
                        "report": rep if isinstance(rep, dict)
                        else None}
        if d2["deployer"]:
            d2["deployer_dossiers_live"] = _deployer_count(
                d2["deployer"], time.time())
            # Display-only intel (never part of the safety verdict).
            try:
                d2.update(_intel_fields(d2["deployer"]))
            except Exception:
                pass
        _write(mint, d2)
    except Exception as e:
        logger.warning("dossier build failed: %s", e)


def _worker_loop():
    while True:
        try:
            item = _queue.get()
        except Exception:
            return
        if item is None:
            return
        _build(item)


def _ensure_worker():
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop,
                                       name="dossier-build",
                                       daemon=True)
            _worker.start()


def observe_births(items, now=None):
    """Feed drained PumpPortal items; births passing the cheap
    filter get a dossier built in the background. Non-blocking;
    returns how many were queued. Never raises."""
    queued = 0
    try:
        now = time.time() if now is None else now
        _sweep(now)
        for item in items or []:
            try:
                if (item or {}).get("source") != "pumpportal-new":
                    continue
                mint = str(item.get("mint") or "")
                if not mint or read_dossier(mint, now):
                    continue
                if not _birth_filter(item):
                    continue
                day = _today()
                with _lock:
                    if _day["date"] != day:
                        _day.update(date=day, count=0)
                    if _day["count"] >= MAX_PER_DAY:
                        break
                    if _count_open(now) >= MAX_OPEN_DOSSIERS:
                        break
                    _day["count"] += 1
                _ensure_worker()
                try:
                    _queue.put_nowait(item)
                    queued += 1
                except queue.Full:
                    break
            except Exception:
                continue
    except Exception:
        pass
    return queued


def observe_migrations(items, now=None):
    """On a mint's migration event: stamp the dossier and take a
    final curve sample set (velocity = v_sol growth over the curve
    phase). Never raises."""
    try:
        now = time.time() if now is None else now
        for item in items or []:
            try:
                if (item or {}).get("source") \
                        != "pumpportal-migration":
                    continue
                mint = str(item.get("mint") or "")
                d = read_dossier(mint, now)
                if not d:
                    continue
                d["migrated_ts"] = now
                if _res is not None:
                    _o, data = _res.get_account(
                        _res.derive_curve_pda(mint) or "")
                    c = _res.parse_bonding_curve(data)
                    if c:
                        d.setdefault("curve_samples", []).append(
                            {"ts": now, "v_sol": c["v_sol"],
                             "v_token": c["v_token"]})
                samples = d.get("curve_samples") or []
                if len(samples) >= 2:
                    dt = samples[-1]["ts"] - samples[0]["ts"]
                    if dt > 0:
                        d["curve_velocity_sol_s"] = round(
                            (samples[-1]["v_sol"]
                             - samples[0]["v_sol"]) / 1e9 / dt, 6)
                _write(mint, d)
            except Exception:
                continue
    except Exception:
        pass
