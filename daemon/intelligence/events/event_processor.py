"""Event processing pipeline (Phase 8, 2026-11-22).

Order: acquisition -> retrieval -> decoder -> normalization ->
dedup -> store -> market state update. Every step is idempotent:
re-processing the same observation merges into the existing
logical event instead of creating a second record.

Slot tracking + completeness: one acquisition GapDetector per
stream key watches the slot sequence; a jump past the tolerated
threshold records a gap (see ``slot_gaps``) for the recovery
layer to backfill. Gaps are recorded, never silently skipped.

Market-state gate: only a verified + confirmed/finalized event
reaches ``TokenStateRegistry`` (or the optional
``on_market_update`` callback). A preliminary/unverified or
conflicting event is stored with processing_status "held" and
never touches market state. Backfill is detection-only (Rule 1):
recovered events update token state only through the same
verified gate — backfill never enters a trade, and this module
never calls any trading path.

This module never imports the launch_scanner monolith.
"""

from __future__ import annotations

import logging
import time

from ..acquisition.event_backfill import GapDetector
from .event_deduplicator import EventDeduplicator
from .event_models import (
    EVENT_TYPE_TOKEN_CREATED,
    EVENT_TYPE_TOKEN_GRADUATED,
    EVENT_TYPE_TOKEN_MIGRATED,
    EVENT_TYPE_POOL_CREATED,
    EVENT_TYPE_TOKEN_TRADE,
    PROCESSING_APPLIED,
    PROCESSING_HELD,
    event_id_for,
)
from .event_normalizer import EventNormalizer

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.events.event_processor")


class TokenStateRegistry(object):
    """In-memory per-token state, updated ONLY by verified +
    confirmed/finalized events. This is market intelligence
    state, not a trading position: creation, trades, graduation
    and migration facts per mint."""

    def __init__(self):
        self.tokens = {}  # mint -> state dict

    def get(self, mint):
        return self.tokens.get(mint)

    def update(self, event):
        """Apply one verified logical event. Returns the mint
        updated, or None when the event carries no mint / is not
        a token event."""
        if not event.may_update_market_state():
            return None
        accounts = event.normalized_accounts or {}
        mint = accounts.get("mint")
        if not mint:
            # Trade events carry the mint in decoded fields; fall
            # back to the token movement's mint.
            for move in event.token_movement:
                if move.get("mint"):
                    mint = move["mint"]
                    break
        if not mint:
            return None
        state = self.tokens.setdefault(mint, {
            "mint": mint, "created_slot": None,
            "created_block_time": None, "creator": None,
            "name": None, "symbol": None, "bonding_curve": None,
            "pool": None, "graduated": False, "migrated": False,
            "trade_count": 0, "last_slot": None,
            "last_event_type": None, "last_block_time": None,
            "last_sol_amount": None, "last_token_amount": None})
        state["last_slot"] = event.slot
        state["last_event_type"] = event.event_type
        state["last_block_time"] = event.block_time
        if event.event_type == EVENT_TYPE_TOKEN_CREATED:
            state["created_slot"] = event.slot
            state["created_block_time"] = event.block_time
            state["creator"] = accounts.get("creator")
            state["bonding_curve"] = accounts.get("bonding_curve")
            for obs in event.observations:
                fields = obs.get("decoded_fields") or {}
                if fields.get("name"):
                    state["name"] = fields["name"]
                if fields.get("symbol"):
                    state["symbol"] = fields["symbol"]
        elif event.event_type == EVENT_TYPE_TOKEN_TRADE:
            state["trade_count"] += 1
            for obs in event.observations:
                fields = obs.get("decoded_fields") or {}
                if fields.get("sol_amount") is not None:
                    state["last_sol_amount"] = fields["sol_amount"]
                if fields.get("token_amount") is not None:
                    state["last_token_amount"] = fields["token_amount"]
                if fields.get("quote_amount") is not None:
                    state["last_sol_amount"] = fields["quote_amount"]
        elif event.event_type == EVENT_TYPE_TOKEN_GRADUATED:
            state["graduated"] = True
        elif event.event_type == EVENT_TYPE_TOKEN_MIGRATED:
            state["migrated"] = True
            if accounts.get("pool"):
                state["pool"] = accounts["pool"]
        elif event.event_type == EVENT_TYPE_POOL_CREATED:
            if accounts.get("pool"):
                state["pool"] = accounts["pool"]
        return mint


class EventProcessor(object):
    """The pipeline: normalize -> dedup -> store -> market state."""

    def __init__(self, store, normalizer=None, dedup=None,
                 registry=None, on_market_update=None):
        self.store = store
        self.normalizer = normalizer or EventNormalizer()
        self.dedup = dedup or EventDeduplicator()
        self.registry = registry if registry is not None \
            else TokenStateRegistry()
        self.on_market_update = on_market_update
        self._gap_detectors = {}  # stream_key -> GapDetector
        self._gaps = {}  # stream_key -> [gap dicts]
        self._applied_event_ids = set()  # event_ids already applied
        # to market state by THIS processor instance. A second
        # provider observation of the same transaction merges into
        # the same logical record; re-applying it would double-count
        # (e.g. trade_count += 1 twice for one trade). The outcome
        # stays "applied" — only the registry mutation + callback
        # are skipped on the repeat. The durable counterpart is the
        # store's applied_events table (Phase 13 finalize):
        # _apply_or_hold consults it too, so a processor restarted
        # against an existing store does not re-apply.
        self.stats = {"processed": 0, "duplicates": 0, "held": 0,
                      "applied": 0, "failed": 0, "merged": 0}

    # -- slot tracking ------------------------------------------------
    def observe_slot(self, stream_key, slot, ts=None):
        """Feed a slot observation; returns a gap dict when the
        jump from the previous slot exceeds the tolerated jump
        (first observation for a key never reports a gap)."""
        detector = self._gap_detectors.setdefault(
            stream_key, GapDetector())
        gap = detector.observe(stream_key, slot, ts=ts)
        if gap is not None:
            self._gaps.setdefault(stream_key, []).append(gap)
        return gap

    def slot_gaps(self, stream_key=None):
        if stream_key is not None:
            return list(self._gaps.get(stream_key, []))
        return {k: list(v) for k, v in self._gaps.items()}

    # -- pipeline ------------------------------------------------------
    def process_observation(self, observation, stream_key=None):
        """Process one provider observation end to end.

        Returns (outcome, event_id) where outcome is one of
        "stored", "duplicate", "merged", "held", "applied",
        "failed". "duplicate" means the identical provider
        observation was already recorded; "merged" means a new
        provider observation was folded into an existing event.
        """
        try:
            event = self.normalizer.normalize_observation(observation)
        except Exception as exc:
            logger.warning("normalization failed: %s", exc)
            self.stats["failed"] += 1
            return "failed", None
        self.stats["processed"] += 1
        if stream_key is not None and event.slot is not None:
            self.observe_slot(stream_key, event.slot)

        existing = self.store.get_event(event.event_id)
        if existing is None:
            # One signature, one logical record: a stored event
            # with the same signature but a different slot (or a
            # preliminary slot-less record) is the SAME event
            # observed differently — merge into it so the slot
            # disagreement is flagged, never stored twice.
            for candidate in self.store.get_by_signature(
                    event.signature):
                if candidate.event_id != event.event_id:
                    existing = candidate
                    break
        if existing is not None:
            # Same event already stored. A genuinely new provider
            # observation merges in; a repeat is a duplicate.
            is_new = existing.add_observation(
                event.observations[0]) if event.observations else False
            if not is_new:
                self.stats["duplicates"] += 1
                return "duplicate", event.event_id
            old_id = existing.event_id
            merged = self.normalizer.merge_into(existing, observation)
            if merged.event_id != old_id:
                # A preliminary (slot-less) record gained its slot:
                # re-key by deleting the old row.
                self.store.delete_event(old_id)
            self.store.upsert_event(merged)
            self.stats["merged"] += 1
            return self._apply_or_hold(merged)
        if self.dedup.seen(event.event_id):
            # In-memory fast path says seen but the store has no
            # record (e.g. store was reopened empty): treat as a
            # duplicate only if the store agrees; otherwise store.
            if self.store.event_exists(event.event_id):
                self.stats["duplicates"] += 1
                return "duplicate", event.event_id
        self.dedup.add(event.event_id)
        self.store.upsert_event(event)
        if stream_key is not None:
            self.store.set_checkpoint(stream_key, event.slot,
                                      event.signature)
        return self._apply_or_hold(event)

    def process_transaction(self, provider, tx, commitment="confirmed",
                            kind="getTransaction", stream_key=None,
                            parsed=None, observed_at=None):
        """Convenience wrapper: build an observation from a raw
        getTransaction result (+ optional Helius parsed form) and
        run the pipeline."""
        observation = {"provider": provider, "kind": kind, "tx": tx,
                       "commitment": commitment,
                       "observed_at": observed_at or time.time()}
        if parsed is not None:
            observation["parsed"] = parsed
        return self.process_observation(observation,
                                        stream_key=stream_key)

    def _apply_or_hold(self, event):
        if event.may_update_market_state():
            # Durable applied check (Phase 13 finalize): the
            # in-memory _applied_event_ids guard is per processor
            # instance — a processor restarted against an existing
            # store rebuilds it empty and would re-apply an
            # already-applied event (double-counting). The store's
            # applied_events table survives restarts. Stores that
            # predate the table (duck-typed fakes in tests) fall
            # back to the in-memory guard only.
            store_applied = getattr(self.store, "is_applied", None)
            mark_applied = getattr(self.store, "mark_applied", None)
            already = event.event_id in self._applied_event_ids
            if not already and callable(store_applied):
                try:
                    already = bool(store_applied(event.event_id))
                except Exception as exc:
                    logger.warning(
                        "durable applied check failed: %s", exc)
            if not already:
                mint = self.registry.update(event)
                if mint and self.on_market_update is not None:
                    try:
                        self.on_market_update(event, mint)
                    except Exception as exc:
                        logger.warning(
                            "market update callback failed: %s", exc)
                self._applied_event_ids.add(event.event_id)
                if callable(mark_applied):
                    try:
                        mark_applied(event.event_id)
                    except Exception as exc:
                        logger.warning(
                            "durable applied mark failed: %s", exc)
            event.processing_status = PROCESSING_APPLIED
            self.store.upsert_event(event)
            self.stats["applied"] += 1
            return "applied", event.event_id
        event.processing_status = PROCESSING_HELD
        self.store.upsert_event(event)
        self.stats["held"] += 1
        return "held", event.event_id

    def reprocess_event(self, event_id):
        """Re-run the market-state gate for a stored event (used
        by recovery after a held event becomes verified, e.g. a
        later finalized observation merges in)."""
        event = self.store.get_event(event_id)
        if event is None:
            return "failed", event_id
        return self._apply_or_hold(event)
