"""Event recovery: checkpoints, retry queues, reprocessing and
backfill after disconnects (Phase 8, 2026-11-22).

Recovery coordinates with the acquisition layer — it CALLS
``daemon.intelligence.acquisition.event_backfill`` (GapDetector +
backfill_signatures) and never duplicates that logic. Backfill is
detection-only (Rule 1): recovered events flow through the same
verified pipeline gate; recovery never enters a trade and never
calls any trading path.

Checkpoints live in the event store (``checkpoints`` table):
one last-processed slot per stream key, updated on every stored
event. After a restart, processing resumes from the checkpoint
instead of re-reading history.

Failed events live in the store's ``failed_events`` table with
exponential backoff (60s * 2**attempts, capped at 1h). Reprocess
only retries events whose next_retry_at has passed.
"""

from __future__ import annotations

import logging
import time

from ..acquisition.transaction_identity import response_signature_matches
from ..acquisition.event_backfill import (
    GapDetector,
    backfill_signatures,
    slot_range_for_gap,
)

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.events.event_recovery")


class CheckpointTracker(object):
    """Last-processed-slot checkpoints, persisted in the store."""

    def __init__(self, store):
        self.store = store

    def record(self, stream_key, slot, signature=None):
        self.store.set_checkpoint(stream_key, slot, signature)

    def last_slot(self, stream_key):
        checkpoint = self.store.get_checkpoint(stream_key)
        return checkpoint["last_slot"] if checkpoint else None

    def last_signature(self, stream_key):
        checkpoint = self.store.get_checkpoint(stream_key)
        return checkpoint["last_signature"] if checkpoint else None


class RetryQueue(object):
    """Failed-event retry queue backed by the store."""

    def __init__(self, store):
        self.store = store

    def enqueue(self, event_id, signature, slot, error):
        """Record a failure; returns the attempt count."""
        return self.store.enqueue_failed(event_id, signature, slot,
                                         error)

    def due(self, now=None):
        """Failed events whose backoff has expired."""
        return self.store.failed_due(now=now)

    def resolve(self, event_id):
        self.store.mark_failed_resolved(event_id)

    def pending_count(self):
        return self.store.failed_count(status="pending")


def reprocess_failed(processor, fetch_observation, now=None,
                     retry_queue=None):
    """Reprocess every due failed event.

    ``fetch_observation(event_id, signature, slot)`` returns an
    observation dict for the event (injected so tests use
    fixtures; production passes a retrieval closure). Returns
    (resolved, still_failing) lists of event IDs. A reprocessed
    event that still fails is re-enqueued with backoff.
    """
    store = processor.store
    queue = retry_queue or RetryQueue(store)
    resolved = []
    still_failing = []
    for failed in queue.due(now=now):
        event_id = failed["event_id"]
        try:
            observation = fetch_observation(
                event_id, failed.get("signature"), failed.get("slot"))
            if observation is None:
                raise ValueError("no observation available for %s"
                                 % event_id)
            outcome, _ = processor.process_observation(observation)
            if outcome == "failed":
                raise ValueError("pipeline failed for %s" % event_id)
            queue.resolve(event_id)
            resolved.append(event_id)
        except Exception as exc:
            logger.warning("reprocess of %s failed: %s", event_id, exc)
            queue.enqueue(event_id, failed.get("signature"),
                          failed.get("slot"), exc)
            still_failing.append(event_id)
    return resolved, still_failing


def backfill_after_disconnect(processor, stream_key, address,
                              rpc_call, disconnected_s,
                              fetch_transaction=None,
                              provider="chainstack",
                              commitment="confirmed"):
    """Backfill the gap left by a disconnect, then process the
    recovered events through the normal pipeline.

    ``rpc_call(method, params)`` is the provider call (injected;
    production passes the router-backed call from
    ``event_backfill.router_rpc_call``). ``fetch_transaction`` maps
    a signature to a getTransaction result dict; when omitted it
    defaults to ``rpc_call("getTransaction", ...)``. Returns
    (recovered_signatures, outcomes) where outcomes maps
    signature -> pipeline outcome.

    Gap detection uses the acquisition layer's GapDetector and
    ``slot_range_for_gap``; signature recovery uses its
    ``backfill_signatures`` — this module adds only the
    process-through-the-pipeline step.
    """
    detector = GapDetector()
    slots_to_cover, _tolerated = slot_range_for_gap(disconnected_s)
    logger.info("backfill_after_disconnect stream=%s disconnected=%ss "
                "slots_to_cover=%d", stream_key, disconnected_s,
                slots_to_cover)
    if fetch_transaction is None:
        def fetch_transaction(sig):
            result = rpc_call("getTransaction", [sig, {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 1}])
            if not isinstance(result, dict) \
                    or result.get("kind") != "ok":
                return None
            return result.get("result")
    signatures, _pages = backfill_signatures(
        address, rpc_call, dedup=None)
    outcomes = {}
    for sig in signatures:
        tx = fetch_transaction(sig)
        if not response_signature_matches(tx, sig):
            outcomes[sig] = "failed"
            continue
        outcome, _event_id = processor.process_transaction(
            provider, tx, commitment=commitment,
            kind="getTransaction", stream_key=stream_key)
        outcomes[sig] = outcome
    return signatures, outcomes
