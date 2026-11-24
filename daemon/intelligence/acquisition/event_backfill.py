"""Gap detection + historical recovery (Phase 2).

A disconnected WebSocket must not silently miss a period. This
module:
- tracks the last seen slot per subscription key (slot_clock: ~2.5
  slots/s on mainnet, so a time gap converts to a slot range);
- detects a gap when the slot jumps forward by more than the
  tolerated number of slots, or when a subscription was
  disconnected/stale for longer than its expected interval;
- backfills the missed range via RPC ``getSignaturesForAddress``
  (bounded by the 251-signature page limit, paged with
  before/until cursors), returning the recovered signatures;
- dedupes by signature (a recovered event already seen live is
  dropped, never double-counted).

Also owns the incremental signature cursors the deployer and
smart-money workloads need (audit §5): instead of re-reading a
fixed window per poll, store ``last_seen_signature`` per wallet/mint
and page forward with before/until. Cursors are signature strings —
provider-independent, so a switch mid-flight loses nothing already
recorded.

Backfill is detection-only, never enters (Rule 1): this module
returns events; it never calls any trading path.
"""

from __future__ import annotations

import json
import logging
import os
import time

from .transaction_identity import response_signature_matches

logger = logging.getLogger("forex_agent.daemon.intelligence.acquisition.event_backfill")

SLOTS_PER_SECOND = 2.5
DEFAULT_TOLERATED_SLOT_JUMP = 10
SIG_PAGE_LIMIT = 251
CURSOR_FILE = "acquisition_cursors.json"


def slot_range_for_gap(disconnected_s, slack_slots=DEFAULT_TOLERATED_SLOT_JUMP):
    """Slots plausibly missed during a disconnect of ``disconnected_s``
    seconds. Returns (slots_to_cover, tolerated)."""
    return int(disconnected_s * SLOTS_PER_SECOND) + slack_slots, slack_slots


class GapDetector:
    """Per-subscription slot tracking."""

    def __init__(self, tolerated_jump=DEFAULT_TOLERATED_SLOT_JUMP):
        self.tolerated_jump = tolerated_jump
        self._last_slot = {}   # key -> last seen slot
        self._last_seen_ts = {}

    def observe(self, key, slot, ts=None):
        """Record a slot observation. Returns a gap dict when the
        jump from the previous slot exceeds the tolerated jump, else
        None. The FIRST observation for a key never reports a gap
        (there is no previous slot to jump from)."""
        ts = ts if ts is not None else time.time()
        prev = self._last_slot.get(key)
        self._last_slot[key] = slot
        self._last_seen_ts[key] = ts
        if prev is None:
            return None
        jump = slot - prev
        if jump > self.tolerated_jump:
            return {"key": key, "from_slot": prev, "to_slot": slot,
                    "missed_slots": jump, "ts": ts}
        return None

    def gap_for_disconnect(self, key, disconnected_s, last_slot=None):
        """A subscription was down for ``disconnected_s`` seconds.
        Returns a gap dict (or None when the outage is within
        tolerance). The missed-slot estimate is the outage length
        in slots; the slack is added to the range to COVER, not to
        the comparison — a short outage must not report a gap."""
        slots = int(disconnected_s * SLOTS_PER_SECOND)
        if slots <= self.tolerated_jump:
            return None
        return {"key": key, "from_slot": last_slot,
                "to_slot": None, "missed_slots": slots + self.tolerated_jump,
                "disconnected_s": disconnected_s}

    def last_slot(self, key):
        return self._last_slot.get(key)


class EventDeduplicator:
    """Dedupe by signature, bounded memory (FIFO eviction)."""

    def __init__(self, capacity=100_000):
        self.capacity = capacity
        self._seen = {}
        self._order = []

    def seen(self, signature):
        return signature in self._seen

    def add(self, signature):
        """Returns True when the signature is NEW, False when it is
        a duplicate."""
        if signature in self._seen:
            return False
        self._seen[signature] = True
        self._order.append(signature)
        if len(self._order) > self.capacity:
            old = self._order.pop(0)
            self._seen.pop(old, None)
        return True

    def __len__(self):
        return len(self._seen)


class CursorStore:
    """Persistent per-wallet / per-mint incremental cursors.

    File: <home>/acquisition_cursors.json (atomic replace, no lock —
    single writer per process; the deployer/smart-money passes are
    single-process background passes). A cursor is a signature
    string, provider-independent.
    """

    def __init__(self, home=None):
        self.home = home or os.environ.get("FOREX_AGENT_HOME") \
            or os.path.expanduser("~/.forex-agent")
        self._path = os.path.join(self.home, CURSOR_FILE)
        self._data = self._load()

    def _load(self):
        try:
            with open(self._path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = "%s.tmp.%d" % (self._path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(self._data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

    def get(self, kind, key):
        return (self._data.get(kind) or {}).get(key)

    def set(self, kind, key, signature):
        self._data.setdefault(kind, {})[key] = signature
        self._save()

    def page_params(self, kind, key, address, limit=100):
        """getSignaturesForAddress params that page FORWARD from the
        stored cursor: ``until`` = last seen signature, so only
        signatures NEWER than the cursor come back. No cursor yet:
        plain limit window (first poll)."""
        cursor = self.get(kind, key)
        params = [address, {"limit": limit}]
        if cursor:
            params[1]["until"] = cursor
        return params


def backfill_signatures(address, rpc_call, dedup=None, limit=SIG_PAGE_LIMIT,
                        max_pages=4):
    """Recover signatures for ``address`` over RPC, newest-first,
    paged with ``before`` cursors. ``rpc_call(method, params)`` is
    the provider call (injected so tests use a fake). Returns
    (signatures, pages_read). Duplicates (already seen) are dropped
    and counted in the returned stats via the dedup object.

    Stops early when a page comes back empty or shorter than the
    limit (no more history in range)."""
    signatures = []
    before = None
    pages = 0
    while pages < max_pages:
        params = [address, {"limit": limit}]
        if before:
            params[1]["before"] = before
        result = rpc_call("getSignaturesForAddress", params)
        pages += 1
        if result is None or result.get("kind") != "ok":
            break
        rows = result.get("result") or []
        if not rows:
            break
        for row in rows:
            sig = row.get("signature") if isinstance(row, dict) else None
            if not sig:
                continue
            if dedup is not None and not dedup.add(sig):
                continue
            signatures.append(sig)
        if len(rows) < limit:
            break
        before = rows[-1].get("signature")
        if not before:
            break
    return signatures, pages


def router_rpc_call(router, priority=None, hints=None):
    """Build an ``rpc_call(method, params)`` callable for
    ``backfill_signatures`` that routes through the central router
    (Phase 4) as a missed-event-recovery workload: Chainstack within
    its retention window, else Alchemy (archive) — Helius last in
    the shadow phase. Backfill is detection-only, never enters
    (Rule 1): this returns events; it never calls any trading path.
    """
    from .request_scheduler import PRIORITY_CARD

    def _call(method, params):
        return router.call(method, params,
                           priority=priority or PRIORITY_CARD,
                           hints={"workload": "missed_event_recovery",
                                  **(hints or {})})
    return _call


# ---------------------------------------------------------------------------
# Phase 6 — backfill checkpoints, unrecovered gaps, dedup sink,
# order reconciliation, signature-range recovery, driver.
#
# The event store must never contain duplicate logical events just
# because they arrived from multiple providers, and a missing-event
# range must never be silently discarded: a range the providers
# could not serve is recorded as an UNRECOVERED GAP and retried
# when service returns. Backfill is detection-only, never enters
# (Rule 1).
# ---------------------------------------------------------------------------

CHECKPOINT_FILE = "backfill_checkpoints.json"


class CorruptCheckpointStoreError(Exception):
    """The backfill checkpoint file exists but cannot be trusted.

    The file is preserved untouched. The caller must not treat this as
    an empty store: recorded checkpoints and unrecovered gaps are
    unknown until a person or a reviewed migration resolves it."""


class BackfillCheckpointStore:
    """Persistent per-key backfill checkpoints + unrecovered gaps.

    File: <home>/backfill_checkpoints.json (atomic replace, single
    writer per process — same discipline as CursorStore). A
    checkpoint is the last SUCCESSFULLY processed slot + signature
    for one key (address / subscription); it only ever advances
    past data that was actually recovered, never past a failed
    range. An unrecovered gap is a slot range the providers could
    not serve; it stays recorded until a retry closes it.

    File contents are slot numbers, signatures, and reasons only —
    no provider state, no keys.
    """

    def __init__(self, home=None):
        self.home = home or os.environ.get("FOREX_AGENT_HOME") \
            or os.path.expanduser("~/.forex-agent")
        self._path = os.path.join(self.home, CHECKPOINT_FILE)
        self._data = self._load()

    def _load(self):
        try:
            with open(self._path) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            data.setdefault("checkpoints", {})
            data.setdefault("unrecovered_gaps", [])
            if not isinstance(data["checkpoints"], dict) \
                    or not isinstance(data["unrecovered_gaps"], list):
                raise ValueError("wrong member types")
            return data
        except FileNotFoundError:
            return {"checkpoints": {}, "unrecovered_gaps": []}
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            # An unreadable or malformed file is NOT "no checkpoints":
            # silently starting empty would drop recorded gaps and
            # resume from nothing. Leave the file untouched and raise.
            raise CorruptCheckpointStoreError(
                "backfill checkpoint file %s unreadable or malformed: %s"
                % (self._path, type(exc).__name__))

    def _save(self):
        tmp = "%s.tmp.%d" % (self._path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(self._data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

    def get_checkpoint(self, key):
        """{"last_slot", "last_signature", "updated_ts"} or None."""
        rec = (self._data.get("checkpoints") or {}).get(key)
        return dict(rec) if rec else None

    def set_checkpoint(self, key, slot=None, signature=None):
        """Advance the checkpoint. Only call with data that was
        actually recovered — never past a failed range."""
        rec = (self._data.setdefault("checkpoints", {})
               .setdefault(key, {"last_slot": None,
                                 "last_signature": None,
                                 "updated_ts": None}))
        if slot is not None:
            rec["last_slot"] = slot
        if signature is not None:
            rec["last_signature"] = signature
        rec["updated_ts"] = time.time()
        self._save()
        return dict(rec)

    def record_unrecovered_gap(self, key, from_slot, to_slot, reason):
        """Record a range the providers could not serve. An identical
        open gap is bumped (attempts + last_attempt_ts), never
        duplicated — the range stays tracked until a retry closes it."""
        gaps = self._data.setdefault("unrecovered_gaps", [])
        now = time.time()
        for gap in gaps:
            if (gap.get("key") == key
                    and gap.get("from_slot") == from_slot
                    and gap.get("to_slot") == to_slot):
                gap["attempts"] = gap.get("attempts", 1) + 1
                gap["last_attempt_ts"] = now
                gap["reason"] = reason
                self._save()
                return dict(gap)
        gap = {"key": key, "from_slot": from_slot, "to_slot": to_slot,
               "reason": reason, "first_seen_ts": now,
               "last_attempt_ts": now, "attempts": 1}
        gaps.append(gap)
        self._save()
        return dict(gap)

    def clear_unrecovered_gap(self, key, from_slot, to_slot):
        """Close a gap once a retry has recovered it. Returns True
        when a gap was actually cleared."""
        gaps = self._data.get("unrecovered_gaps") or []
        kept = [g for g in gaps
                if not (g.get("key") == key
                        and g.get("from_slot") == from_slot
                        and g.get("to_slot") == to_slot)]
        cleared = len(kept) != len(gaps)
        if cleared:
            self._data["unrecovered_gaps"] = kept
            self._save()
        return cleared

    def unrecovered_gaps(self, key=None):
        gaps = self._data.get("unrecovered_gaps") or []
        out = [dict(g) for g in gaps
               if key is None or g.get("key") == key]
        return out


def event_id(event):
    """Logical identity of an event: its signature first, else an
    explicit ``id``. Two providers returning the same transaction
    produce the same id — that is what dedup keys on."""
    if not isinstance(event, dict):
        return None
    return event.get("signature") or event.get("id")


class DedupEventSink:
    """In-memory event store with duplicate elimination.

    The store must never contain duplicate logical events just
    because they arrived from multiple providers: ``add`` drops an
    event whose id is already stored and counts it. This is the
    Phase 6 sink — the durable SQLite event store is Phase 8's
    work; this sink proves the dedup guarantee the store must keep.
    """

    def __init__(self):
        self._events = []
        self._ids = set()
        self.duplicates_dropped = 0

    def seen(self, signature):
        return signature in self._ids

    def add(self, event):
        """Returns True when the event is NEW and stored, False when
        it is a duplicate (dropped, counted). Events without an id
        are stored as-is — they cannot be deduped, so callers must
        give every event a signature/id."""
        ident = event_id(event)
        if ident is not None:
            if ident in self._ids:
                self.duplicates_dropped += 1
                return False
            self._ids.add(ident)
        self._events.append(event)
        return True

    def __len__(self):
        return len(self._events)

    @property
    def events(self):
        return list(self._events)

    def ordered(self):
        """Stored events in reconciled (slot, transactionIndex)
        order."""
        return reconcile_event_order(self._events)


def reconcile_event_order(events):
    """Sort events into chain order: (slot, transactionIndex,
    signature). Events with no slot sort LAST (they cannot be
    placed — flag them upstream, never guess). Stable: equal keys
    keep arrival order."""
    def _key(ev):
        slot = ev.get("slot") if isinstance(ev, dict) else None
        idx = ev.get("transactionIndex") if isinstance(ev, dict) else None
        sig = ""
        if isinstance(ev, dict):
            sig = str(ev.get("signature") or ev.get("id") or "")
        return (slot is None,
                slot if isinstance(slot, int) else 0,
                idx if isinstance(idx, int) else (1 << 62),
                sig)
    return sorted(events, key=_key)


def recover_signature_range(address, rpc_call, until_signature,
                            seen=None, limit=SIG_PAGE_LIMIT, max_pages=4):
    """Recover the signature range NEWER than ``until_signature``
    (the last successfully processed signature — the checkpoint).

    Pages ``getSignaturesForAddress`` newest-first with ``before``
    cursors and an ``until`` bound, so only signatures newer than
    the checkpoint come back. Returns (rows, pages, stats) where
    rows are the raw signature-row dicts in CHRONOLOGICAL order
    (oldest first — the order the event store consumes), and stats
    carries pages / rows_seen / duplicates_dropped / failed.

    ``seen`` is an optional callable(signature)->bool (the sink):
    rows already seen are counted as duplicates and skipped, but
    paging continues — a seen signature does not end the range.
    A non-ok page ends the recovery (stats["failed"] records the
    kind); the caller records the unrecovered gap.
    """
    rows = []
    stats = {"pages": 0, "rows_seen": 0, "duplicates_dropped": 0,
             "failed": None}
    before = None
    pages = 0
    page_signatures = set()
    previous_slot = None
    while pages < max_pages:
        params = [address, {"limit": limit}]
        if until_signature:
            params[1]["until"] = until_signature
        if before:
            params[1]["before"] = before
        result = rpc_call("getSignaturesForAddress", params)
        pages += 1
        stats["pages"] = pages
        if result is None or result.get("kind") != "ok":
            stats["failed"] = (result or {}).get("kind") or "no_result"
            break
        page = result.get("result")
        if not isinstance(page, list) or any(
                not isinstance(row, dict)
                or not isinstance(row.get("signature"), str)
                or not row["signature"]
                or type(row.get("slot")) is not int or row["slot"] < 0
                or (row.get("blockTime") is not None
                    and (type(row["blockTime"]) is not int or row["blockTime"] < 0))
                for row in page):
            stats["failed"] = "malformed_signature_page"
            break
        if not page:
            break
        for row in page:
            if not isinstance(row, dict):
                continue
            sig = row.get("signature")
            slot = row['slot']
            if sig in page_signatures or (previous_slot is not None and slot > previous_slot):
                stats["failed"] = "signature_page_duplicate_or_position_regression"
                return list(reversed(rows)), pages, stats
            page_signatures.add(sig)
            previous_slot = slot
            stats["rows_seen"] += 1
            if sig == until_signature:
                # Defensive: ``until`` is exclusive, so this should
                # never appear; if it does, the range is closed.
                return list(reversed(rows)), pages, stats
            if seen is not None and seen(sig):
                stats["duplicates_dropped"] += 1
                continue
            rows.append(row)
        if len(page) < limit:
            break
        before = page[-1].get("signature")
        if not before:
            break
    else:
        stats["failed"] = "page_budget_exhausted"
    # Newest-first pages -> chronological for the store.
    return list(reversed(rows)), pages, stats


def fetch_transactions_for_signatures(rows, rpc_call):
    """Historical transaction retrieval for recovered signature rows.

    One ``getTransaction`` per row (jsonParsed,
    maxSupportedTransactionVersion 1). Returns (events, stats):
    an event is {"signature", "slot", "blockTime",
    "transactionIndex", "fee", "err", "result"}; a failed fetch
    produces NO event — the signature is listed in
    stats["failed"] with its slot so the caller can record the
    unrecovered range instead of silently dropping it.
    """
    events = []
    stats = {"requested": len(rows), "fetched": 0, "failed": []}
    for row in rows:
        sig = row.get("signature")
        if not sig:
            continue
        result = rpc_call("getTransaction", [sig, {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 1}])
        if result is None or result.get("kind") != "ok":
            stats["failed"].append({
                "signature": sig, "slot": row.get("slot"),
                "kind": (result or {}).get("kind") or "no_result"})
            continue
        tx = result.get("result")
        if not response_signature_matches(tx, sig):
            stats["failed"].append({
                "signature": sig, "slot": row.get("slot"),
                "kind": "response_identity_unavailable_or_mismatch"})
            continue
        contradictory = any(
            field in row and row[field] is not None
            and (field not in tx or tx[field] is None
                 or type(row[field]) is not type(tx[field]) or row[field] != tx[field])
            for field in ("slot", "blockTime"))
        if contradictory:
            stats["failed"].append({
                "signature": sig, "slot": row.get("slot"),
                "kind": "response_row_position_contradiction"})
            continue
        meta = tx.get("meta") or {}
        events.append({
            "signature": sig,
            "slot": row.get("slot"),
            "blockTime": tx.get("blockTime"),
            "transactionIndex": row.get("transactionIndex"),
            "fee": meta.get("fee"),
            "err": meta.get("err"),
            "result": tx,
        })
        stats["fetched"] += 1
    return events, stats


class BackfillDriver:
    """Missed-event-recovery driver (Phase 6).

    Ties the pieces together over the central router (the ONLY
    routing decision-maker — this driver never picks a provider):
    gap -> signature-range recovery (``until`` = checkpoint) ->
    historical transaction retrieval -> dedup sink -> reconciled
    order -> checkpoint advance. A provider failure records an
    UNRECOVERED GAP and leaves the checkpoint unmoved, so the range
    is retried when service returns instead of silently lost.
    """

    def __init__(self, router, home=None, checkpoints=None, sink=None,
                 now=None):
        self.router = router
        self.checkpoints = checkpoints or BackfillCheckpointStore(
            home=home)
        self.sink = sink or DedupEventSink()
        self._now = now

    def _time(self):
        return self._now() if self._now else time.time()

    def _rpc_call(self, recent=False):
        hints = {"recent": True} if recent else None
        return router_rpc_call(self.router, hints=hints)

    def recover_gap(self, key, address, gap, recent=False,
                    max_pages=4):
        """Recover one detected gap (a dict from GapDetector:
        from_slot / to_slot, either may be None).

        Returns a report dict. The checkpoint advances ONLY past
        data actually recovered; a failed signature page or failed
        transaction fetch records an unrecovered gap for the
        affected slot range and leaves the checkpoint where it was.
        """
        from_slot = gap.get("from_slot") if gap else None
        to_slot = gap.get("to_slot") if gap else None
        t0 = time.monotonic()
        rpc = self._rpc_call(recent=recent)
        checkpoint = self.checkpoints.get_checkpoint(key) or {}
        until = checkpoint.get("last_signature")
        rows, pages, sig_stats = recover_signature_range(
            address, rpc, until, seen=self.sink.seen,
            max_pages=max_pages)
        report = {"key": key, "address": address,
                  "from_slot": from_slot, "to_slot": to_slot,
                  "signatures_found": len(rows), "pages": pages,
                  "rows_seen": sig_stats["rows_seen"],
                  "duplicates_dropped": sig_stats["duplicates_dropped"],
                  "events_new": 0, "fetch_failed": [],
                  "checkpoint": dict(checkpoint),
                  "unrecovered_gap": None, "elapsed_s": 0.0}
        if sig_stats["failed"] is not None:
            # The signature page itself failed: the whole gap range
            # is unrecovered — record it, do not advance anything.
            report["unrecovered_gap"] = \
                self.checkpoints.record_unrecovered_gap(
                    key, from_slot, to_slot,
                    "getSignaturesForAddress failed: %s"
                    % sig_stats["failed"])
            report["elapsed_s"] = round(time.monotonic() - t0, 4)
            return report
        in_range = []
        for row in rows:
            slot = row.get("slot")
            if (from_slot is not None and isinstance(slot, int)
                    and slot < from_slot):
                continue
            if (to_slot is not None and isinstance(slot, int)
                    and slot > to_slot):
                continue
            in_range.append(row)
        report["rows_in_range"] = len(in_range)
        events, tx_stats = fetch_transactions_for_signatures(
            in_range, rpc)
        report["fetch_failed"] = tx_stats["failed"]
        for event in events:
            if self.sink.add(event):
                report["events_new"] += 1
            else:
                report["duplicates_dropped"] += 1
        if tx_stats["failed"]:
            # Some transactions could not be fetched: record the
            # failed slot range as unrecovered and do NOT advance
            # the checkpoint past it.
            failed_slots = [f["slot"] for f in tx_stats["failed"]
                            if isinstance(f.get("slot"), int)]
            gap_from = min(failed_slots) if failed_slots else from_slot
            gap_to = max(failed_slots) if failed_slots else to_slot
            report["unrecovered_gap"] = \
                self.checkpoints.record_unrecovered_gap(
                    key, gap_from, gap_to,
                    "getTransaction failed for %d signature(s)"
                    % len(tx_stats["failed"]))
        elif rows:
            # Everything recovered: advance past the newest row.
            newest = rows[-1]
            report["checkpoint"] = self.checkpoints.set_checkpoint(
                key, slot=newest.get("slot"),
                signature=newest.get("signature"))
        report["elapsed_s"] = round(time.monotonic() - t0, 4)
        return report

    def recover_from_checkpoint(self, key, address, recent=False,
                                max_pages=4):
        """Recover everything newer than the stored checkpoint (no
        explicit gap — the normal steady-state catch-up)."""
        checkpoint = self.checkpoints.get_checkpoint(key) or {}
        gap = {"key": key,
               "from_slot": checkpoint.get("last_slot"),
               "to_slot": None}
        return self.recover_gap(key, address, gap, recent=recent,
                                max_pages=max_pages)

    def retry_unrecovered_gaps(self, addresses, recent=False,
                               max_pages=4):
        """Retry every recorded unrecovered gap. ``addresses`` maps
        key -> address. A gap whose retry recovers its range is
        cleared; a gap that fails again stays recorded (attempts
        bumped). Returns the list of per-gap reports."""
        reports = []
        for gap in self.checkpoints.unrecovered_gaps():
            key = gap.get("key")
            address = (addresses or {}).get(key)
            if not address:
                continue
            report = self.recover_gap(
                key, address,
                {"key": key, "from_slot": gap.get("from_slot"),
                 "to_slot": gap.get("to_slot")},
                recent=recent, max_pages=max_pages)
            if report["unrecovered_gap"] is None:
                self.checkpoints.clear_unrecovered_gap(
                    key, gap.get("from_slot"), gap.get("to_slot"))
            reports.append(report)
        return reports

