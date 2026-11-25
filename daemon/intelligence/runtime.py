"""Intelligence runtime: a durable acquisition -> event pipeline cycle.

Wires the pieces that already exist but nothing ran: routed acquisition
(`acquisition.routine`), signature-range recovery
(`acquisition.event_backfill.recover_signature_range`), the verified
event pipeline (`events.EventProcessor` + `EventStore`), the retry queue
and, optionally, shadow-only fusion.

Rules this module enforces (each is covered by tests/test_intelligence_runtime.py):

* Checkpoint AFTER store AND apply. The runtime never lets the processor
  write the checkpoint (it passes no stream_key); it advances the
  EventStore checkpoint itself only after the event is durably stored and,
  when it may update market state, durably marked applied.
* Restart safety. The in-memory TokenStateRegistry is rebuilt from the
  durable applied set at startup (the store's applied_events table would
  otherwise stop a restarted process from ever re-applying them), and
  events stored but not applied (crash between store and apply) are applied
  exactly once at startup.
* Incomplete data is never silent. A page budget that runs out, a failed
  or unusable provider response, a processing failure or an unreadable
  state file leaves the stream DEGRADED, with the gap persisted in
  ``intelligence/runtime_state.json``, until a later cycle proves recovery.
* Shadow only. Fusion output goes to its own journal; nothing here calls a
  trading path, changes a lane, stake or kill switch, or opens a socket.
  No import of the launch_scanner package. No network at import time:
  providers are reached only through an injected ``rpc_call``.
"""
from __future__ import annotations

import json
import logging
import os
import time

from .acquisition.event_backfill import (
    SIG_PAGE_LIMIT,
    fetch_transactions_for_signatures,
    recover_signature_range,
)
from .events.event_models import (
    EVENT_TYPE_POOL_CREATED,
    EVENT_TYPE_TOKEN_CREATED,
    EVENT_TYPE_TOKEN_GRADUATED,
    EVENT_TYPE_TOKEN_MIGRATED,
    EVENT_TYPE_TOKEN_MINT,
    EVENT_TYPE_TOKEN_TRADE,
    PROCESSING_APPLIED,
)
from .events.event_processor import EventProcessor
from .events.event_recovery import RetryQueue, reprocess_failed

logger = logging.getLogger("forex_agent.daemon.intelligence.runtime")

# Event types that carry market facts. A held (unverified or conflicting)
# event of one of these is NOT durable-complete: the checkpoint must not
# move past it. Held events of other types (unknown, failed transaction)
# are stored and may be passed.
MARKET_EVENT_TYPES = frozenset([
    EVENT_TYPE_TOKEN_CREATED, EVENT_TYPE_TOKEN_TRADE,
    EVENT_TYPE_TOKEN_GRADUATED, EVENT_TYPE_TOKEN_MIGRATED,
    EVENT_TYPE_POOL_CREATED, EVENT_TYPE_TOKEN_MINT])

STATE_DIR = "intelligence"
STATE_FILE = "runtime_state.json"
STATUS_FILE = "status.json"
DEFAULT_MAX_PAGES = 8
REBUILD_LIMIT = 200000
# commitment of a getTransaction call that names no commitment: the RPC
# default is finalized, and the observation must say what was requested.
TX_COMMITMENT = "finalized"

STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_UNCONFIGURED = "unconfigured"
STATE_STORE_ERROR = "store_error"


class StreamSpec(object):
    """One address whose transaction signatures are followed."""

    def __init__(self, key, address):
        if not key or not isinstance(key, str):
            raise ValueError("stream key required")
        if not isinstance(address, str) or not 32 <= len(address) <= 44 \
                or not address.isalnum():
            raise ValueError("stream %r: address is not a base58 key" % key)
        self.key = key
        self.address = address


def parse_streams(text):
    """``key=address,key2=address2`` -> ([StreamSpec], [error names]).

    Malformed entries are returned as errors (they make the runtime
    degraded), never guessed or dropped silently."""
    streams, errors = [], []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, address = part.partition("=")
        try:
            streams.append(StreamSpec(key.strip(), address.strip()))
        except ValueError:
            errors.append("bad_stream_spec:%s" % (key.strip() or "?"))
    return streams, errors


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class RuntimeState(object):
    """Persistent per-stream gaps and degraded reasons.

    An unreadable or malformed file is moved aside (never silently reset)
    and every stream starts DEGRADED ("state_file_corrupt") until a full
    catch-up from the EventStore checkpoint succeeds. The store
    checkpoint, not this file, is the authority on position."""

    def __init__(self, home, clock=time.time):
        self.path = os.path.join(home, STATE_DIR, STATE_FILE)
        self._clock = clock
        self.load_error = None
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or not isinstance(
                    data.get("streams"), dict):
                raise ValueError("malformed")
            return data
        except FileNotFoundError:
            return {"v": 1, "streams": {}}
        except (OSError, ValueError, TypeError) as exc:
            self.load_error = type(exc).__name__
            try:
                os.replace(self.path, "%s.corrupt.%d" % (
                    self.path, int(self._clock())))
            except OSError:
                pass
            return {"v": 1, "streams": {}, "state_file_corrupt": True}

    def stream(self, key):
        s = self.data["streams"].setdefault(
            key, {"gaps": [], "reasons": [], "last_ok_ts": None})
        s.setdefault("gaps", [])
        s.setdefault("reasons", [])
        return s

    def save(self):
        _atomic_write(self.path, self.data)

    def add_reason(self, key, reason):
        s = self.stream(key)
        if reason not in s["reasons"]:
            s["reasons"].append(reason)

    def clear_reason(self, key, reason):
        s = self.stream(key)
        s["reasons"] = [r for r in s["reasons"] if r != reason]

    def add_gap(self, key, from_slot, to_slot, reason):
        s = self.stream(key)
        now = self._clock()
        for gap in s["gaps"]:
            if gap["from_slot"] == from_slot and gap["to_slot"] == to_slot:
                gap["attempts"] += 1
                gap["last_attempt_ts"] = now
                gap["reason"] = reason
                return gap
        gap = {"from_slot": from_slot, "to_slot": to_slot, "reason": reason,
               "first_seen_ts": now, "last_attempt_ts": now, "attempts": 1}
        s["gaps"].append(gap)
        return gap

    def degraded(self, key):
        s = self.stream(key)
        return bool(s["gaps"] or s["reasons"])


class IntelligenceRuntime(object):
    def __init__(self, home, store, rpc_call, streams, clock=time.time,
                 max_pages=DEFAULT_MAX_PAGES, fusion_runner=None,
                 signal_provider=None, config_errors=(),
                 provider_of=None, processor=None, page_limit=None):
        self.home = home
        self.store = store
        self.rpc_call = rpc_call
        self.streams = list(streams)
        self._clock = clock
        self.max_pages = max_pages
        self.page_limit = page_limit or SIG_PAGE_LIMIT
        self.fusion = fusion_runner
        self.signal_provider = signal_provider
        self.config_errors = list(config_errors)
        self._provider_of = provider_of or (lambda: "router")
        self.processor = processor or EventProcessor(store)
        self.retry = RetryQueue(store)
        self.state = RuntimeState(home, clock=clock)
        self.counters = {"cycles": 0, "processed": 0, "held": 0,
                         "applied_new": 0, "failed": 0, "fusion_journaled": 0,
                         "fusion_errors": 0, "reconciled": 0}
        self.last_error = None
        self._started = False
        self.rebuilt = 0
        self.rebuild_truncated = False

    # -- startup: rebuild + reconcile ------------------------------------
    def startup(self):
        """Rebuild the in-memory registry from the durable applied set and
        apply any verified event that was stored but never applied.

        Order matters: rebuild first (state from events already applied,
        no callbacks, nothing re-marked), then apply the rest, so each
        event reaches the registry exactly once."""
        total = self.store.count_events(verification_status="verified")
        events = self.store.list_events(
            limit=REBUILD_LIMIT, verification_status="verified")
        self.rebuild_truncated = total > len(events)
        events.sort(key=lambda e: (e.slot is None, e.slot or 0,
                                   e.block_time or 0, e.event_id))
        for event in events:
            if event.may_update_market_state() \
                    and self.store.is_applied(event.event_id):
                self.processor.registry.update(event)
                self.processor._applied_event_ids.add(event.event_id)
                self.rebuilt += 1
        for event in events:
            if event.may_update_market_state() \
                    and not self.store.is_applied(event.event_id):
                outcome, _ = self.processor.reprocess_event(event.event_id)
                if outcome == "applied":
                    self.counters["reconciled"] += 1
        self._started = True

    # -- one durable step --------------------------------------------------
    def _durable(self, event_id):
        """True when the event is stored and, if it feeds market state,
        durably applied. The only condition that lets a checkpoint move."""
        event = self.store.get_event(event_id)
        if event is None:
            return False
        if event.may_update_market_state():
            return (self.store.is_applied(event_id)
                    and event.processing_status == PROCESSING_APPLIED)
        if event.event_type in MARKET_EVENT_TYPES:
            return False  # held market event: stored, not complete
        return True

    def _process_row(self, stream, row):
        """Fetch, process and verify one signature row. Returns
        (ok, reason). Never advances a checkpoint itself."""
        sig = row["signature"]
        events, stats = fetch_transactions_for_signatures([row],
                                                          self.rpc_call)
        if stats["failed"]:
            return False, "fetch_failed:%s" % stats["failed"][0]["kind"]
        tx = events[0]["result"]
        was_applied = False
        for prior in self.store.get_by_signature(sig):
            was_applied = was_applied or self.store.is_applied(
                prior.event_id)
        outcome, event_id = self.processor.process_transaction(
            self._provider_of(), tx, commitment=TX_COMMITMENT,
            kind="getTransaction", stream_key=None)
        if outcome == "failed" or event_id is None:
            self.retry.enqueue("failed:" + sig, sig, row.get("slot"),
                               "pipeline_failed")
            self.counters["failed"] += 1
            return False, "pipeline_failed"
        self.counters["processed"] += 1
        if outcome == "held":
            self.counters["held"] += 1
        if not self._durable(event_id):
            return False, "not_durable:%s" % outcome
        if outcome == "applied" and not was_applied:
            self.counters["applied_new"] += 1
            self._shadow_fuse(event_id)
        return True, outcome

    def _shadow_fuse(self, event_id):
        if self.fusion is None:
            return
        try:
            event = self.store.get_event(event_id)
            if event is None or event.event_type != EVENT_TYPE_TOKEN_CREATED:
                return
            mint = (event.normalized_accounts or {}).get("mint")
            if not mint:
                return
            self.fusion.run(mint, lane="shadow",
                            signal_provider=self.signal_provider)
            self.counters["fusion_journaled"] += 1
        except Exception:
            self.counters["fusion_errors"] += 1
            logger.exception("shadow fusion failed (ignored, shadow only)")

    # -- one stream ----------------------------------------------------------
    def _catch_up(self, stream):
        key = stream.key
        cp = self.store.get_checkpoint(key)
        until = cp["last_signature"] if cp else None
        rows, pages, st = recover_signature_range(
            stream.address, self.rpc_call, until, seen=None,
            max_pages=self.max_pages, limit=self.page_limit)
        bootstrap = cp is None
        if st["failed"] == "page_budget_exhausted" and bootstrap:
            # First run: no checkpoint means no known position, so no
            # history is claimed and a full page budget is expected. Start
            # from the newest pages; gaps exist only relative to a
            # checkpoint.
            logger.warning("stream %s bootstrapped without history", key)
        elif st["failed"] is not None:
            slots = [r["slot"] for r in rows]
            from_slot = cp["last_slot"] if cp else None
            to_slot = (min(slots) - 1) if slots else None
            self.state.add_gap(key, from_slot, to_slot,
                               "signature_recovery:%s" % st["failed"])
            self.state.add_reason(key, "catchup_failed")
            return
        for row in rows:
            ok, reason = self._process_row(stream, row)
            if not ok:
                self.state.add_reason(key, "processing_stopped")
                self.state.add_gap(key, row["slot"], row["slot"], reason)
                return
            self.store.set_checkpoint(key, row["slot"], row["signature"])
        # Everything newer than the checkpoint was recovered, processed and
        # made durable: only now are earlier gaps and reasons closed.
        self.state.clear_reason(key, "catchup_failed")
        self.state.clear_reason(key, "processing_stopped")
        if not bootstrap:
            # A lost state file cannot say which gaps existed, but the
            # checkpoint only ever advanced past durable events, so a
            # clean catch-up from it covers everything. Without a
            # checkpoint nothing proves that, so the stream stays degraded.
            self.state.clear_reason(key, "state_file_corrupt")
        self.state.stream(key)["gaps"] = []
        self.state.stream(key)["last_ok_ts"] = self._clock()

    # -- retry queue -----------------------------------------------------------
    def _fetch_observation(self, event_id, signature, slot):
        if not signature:
            return None
        res = self.rpc_call("getTransaction", [signature, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        if not isinstance(res, dict) or res.get("kind") != "ok" \
                or not isinstance(res.get("result"), dict):
            return None
        return {"provider": self._provider_of(), "kind": "getTransaction",
                "tx": res["result"], "commitment": TX_COMMITMENT,
                "observed_at": self._clock()}

    # -- the cycle -----------------------------------------------------------------
    def run_cycle(self):
        """One acquisition cycle. Returns the status dict (also written
        to ``intelligence/status.json``). Store failures degrade the
        status and re-raise nothing: the next cycle retries."""
        self.counters["cycles"] += 1
        if self.state.data.pop("state_file_corrupt", None):
            for s in self.streams:
                self.state.add_reason(s.key, "state_file_corrupt")
        try:
            if not self._started:
                self.startup()
            for stream in self.streams:
                try:
                    self._catch_up(stream)
                except Exception as exc:
                    if _is_store_error(exc):
                        raise
                    logger.exception("stream %s cycle failed", stream.key)
                    self.state.add_reason(stream.key, "cycle_exception")
                else:
                    self.state.clear_reason(stream.key, "cycle_exception")
            reprocess_failed(self.processor, self._fetch_observation,
                             now=self._clock(), retry_queue=self.retry)
            self.last_error = None
        except Exception as exc:
            if not _is_store_error(exc):
                raise
            self.last_error = type(exc).__name__
            logger.exception("event store failure; checkpoints not advanced")
        status = self.status()
        try:
            self.state.save()
            _atomic_write(os.path.join(self.home, STATE_DIR, STATUS_FILE),
                          status)
        except OSError:
            logger.exception("could not persist runtime state/status")
        return status

    def status(self):
        streams = {}
        degraded = bool(self.config_errors)
        for s in self.streams:
            st = self.state.stream(s.key)
            cp = None
            try:
                cp = self.store.get_checkpoint(s.key)
            except Exception:
                pass
            streams[s.key] = {
                "checkpoint_slot": (cp or {}).get("last_slot"),
                "gaps": list(st["gaps"]),
                "reasons": list(st["reasons"]),
                "degraded": self.state.degraded(s.key),
                "last_ok_ts": st.get("last_ok_ts"),
            }
            degraded = degraded or streams[s.key]["degraded"]
        failed_pending = None
        try:
            failed_pending = self.retry.pending_count()
        except Exception:
            pass
        if failed_pending:
            degraded = True
        if self.last_error:
            state = STATE_STORE_ERROR
        elif not self.streams:
            state = STATE_DEGRADED if self.config_errors \
                else STATE_UNCONFIGURED
        else:
            state = STATE_DEGRADED if degraded else STATE_OK
        return {
            "v": 1, "ts": self._clock(), "state": state,
            "streams": streams, "config_errors": list(self.config_errors),
            "failed_pending": failed_pending,
            "registry_tokens": len(self.processor.registry.tokens),
            "rebuilt_from_store": self.rebuilt,
            "rebuild_truncated": self.rebuild_truncated,
            "fusion": {"enabled": self.fusion is not None,
                       "mode": "shadow"},
            "counters": dict(self.counters),
            "last_error": self.last_error,
        }


def _is_store_error(exc):
    import sqlite3
    from .events.event_store import CorruptStoreError
    types = [sqlite3.Error, CorruptStoreError]
    try:
        from .acquisition.event_backfill import CorruptCheckpointStoreError
        types.append(CorruptCheckpointStoreError)
    except ImportError:
        pass  # base without the shared checkpoint delta
    return isinstance(exc, tuple(types))


# -- consumer side -------------------------------------------------------------
def read_status(home):
    try:
        with open(os.path.join(home, STATE_DIR, STATUS_FILE)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def intelligence_usable(home, max_age_s=300.0, now=None):
    """(ok, reason) for any consumer that must treat incomplete
    intelligence as unavailable. Fails closed: missing, unreadable,
    stale, unconfigured, degraded or store-error status is NOT ok.
    Nothing in the repo calls this yet; wiring it into an entry gate is a
    separate reviewed change that can only make entries stricter."""
    status = read_status(home)
    if status is None:
        return False, "no_status"
    now = time.time() if now is None else now
    ts = status.get("ts")
    if not isinstance(ts, (int, float)) or now - ts > max_age_s:
        return False, "stale_status"
    if status.get("state") != STATE_OK:
        return False, "state_%s" % status.get("state")
    return True, "ok"


# -- production wiring (lazy: nothing here runs at import) ----------------------
def routed_rpc(call=None):
    """rpc_call(method, params) over the central router, as
    normal-scanning background work. Anything that is not a usable,
    standard, non-retention-limited response is reported as not ok, so
    callers treat it as incomplete instead of as an answer."""
    if call is None:
        from .acquisition import routine
        call = routine.call

    def rpc(method, params):
        res = call(method, params, timeout=20, critical=False,
                   background=True, hints={"purpose": "normal_scanning"})
        res = res if isinstance(res, dict) else {}
        kind = res.get("kind")
        if kind == "ok" and (res.get("decision_usable") is False
                             or res.get("completeness")
                             == "retention-limited"):
            kind = "unusable"
        rpc.last_provider = res.get("provider") or "router"
        return {"kind": kind or "no_result", "result": res.get("result"),
                "provider": rpc.last_provider}

    rpc.last_provider = "router"
    return rpc


def build_runtime(home, streams_env=None, rpc_call=None, clock=time.time):
    """Open the durable stores and build the runtime. Fusion runs only in
    shadow mode with its own journal."""
    from .events.event_store import EventStore
    from .signals.decision_journal import DecisionJournal
    from .signals.fusion import ShadowFusionRunner
    from .signals.shadow_reader import close_signal_provider
    text = streams_env if streams_env is not None else os.environ.get(
        "INTELLIGENCE_STREAMS", "")
    streams, errors = parse_streams(text)
    store = EventStore(path=os.path.join(home, STATE_DIR, "events.db"))
    store.open()
    journal = DecisionJournal(os.path.join(home, STATE_DIR,
                                           "fusion_shadow.db"))
    journal.open()
    rpc = rpc_call or routed_rpc()
    return IntelligenceRuntime(
        home, store, rpc, streams, clock=clock,
        fusion_runner=ShadowFusionRunner(event_store=store, journal=journal),
        signal_provider=close_signal_provider(home),
        config_errors=errors,
        provider_of=lambda: getattr(rpc, "last_provider", "router"))
