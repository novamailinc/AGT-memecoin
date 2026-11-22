"""Logical event schema (Phase 8, 2026-11-22).

One event, one logical record. Providers may observe the same
transaction independently (Helius parsed stream, Alchemy jsonParsed
retrieval, Chainstack independent retrieval, own decoders); the
internal pipeline normalizes their outputs into ONE logical event
with multiple provider observations. Never three discovery systems.

Every logical record stores: signature, slot, block time,
commitment/lifecycle status, source providers, raw transaction
reference, parsed representation references, decoder version,
verification status, event type, normalized accounts, SOL movement,
token movement, processing status and observation timestamps.
Provenance is maintained on every field: ``provenance`` maps a
field name to the list of providers that supplied it.

Deterministic event IDs: ``event_id(signature, slot)`` is the
sha256 hex of ``"<signature>:<slot>"`` — the same signature at the
same slot always yields the same ID, so re-delivery and replay are
idempotent by construction. A signature without a slot (a
preliminary/unconfirmed observation) uses ``"<signature>:none"``;
once the slot is known the confirmed record replaces it under the
slotted ID (the processor merges on signature).

Verification statuses (spec §11):
- "verified"    — raw tx, own decoder, Helius parsed (where
                  available), account-state changes and execution
                  metadata all agree, and the transaction is
                  confirmed/finalized.
- "unverified"  — some check could not be run (decoder unverified,
                  commitment still "processed", no execution
                  metadata); the event is held, never applied to
                  confirmed market state.
- "conflicting" — providers disagree on a field. The event is
                  flagged, never silently resolved to one
                  interpretation.
- "failed"      — the transaction itself failed on chain
                  (meta.err non-null). The failure is a verified
                  fact; it never produces trade events.

Processing statuses:
- "stored"   — persisted, awaiting / passed the market-state gate.
- "applied"  — verified + confirmed and applied to market state.
- "held"     — unverified / conflicting / preliminary; stored but
               NOT applied to market state.
- "failed"   — processing error; queued for retry (see
               event_recovery.RetryQueue).

This package must NOT import the launch_scanner __init__ monolith
(dual-import hazard); it imports only stdlib + the sibling
intelligence decoders/acquisition modules.
"""

from __future__ import annotations

import hashlib
import json
import time

DECODER_VERSION = "1.0.0"  # own-decoder version this schema was built against

# Logical event types (normalized from pump.fun / PumpSwap /
# token-program decoder output).
EVENT_TYPE_TOKEN_CREATED = "token_created"
EVENT_TYPE_TOKEN_TRADE = "token_trade"
EVENT_TYPE_TOKEN_GRADUATED = "token_graduated"
EVENT_TYPE_TOKEN_MIGRATED = "token_migrated"
EVENT_TYPE_POOL_CREATED = "pool_created"
EVENT_TYPE_TOKEN_MINT = "token_mint"
EVENT_TYPE_TRANSACTION_FAILED = "transaction_failed"
EVENT_TYPE_UNKNOWN = "unknown"

EVENT_TYPES = frozenset([
    EVENT_TYPE_TOKEN_CREATED,
    EVENT_TYPE_TOKEN_TRADE,
    EVENT_TYPE_TOKEN_GRADUATED,
    EVENT_TYPE_TOKEN_MIGRATED,
    EVENT_TYPE_POOL_CREATED,
    EVENT_TYPE_TOKEN_MINT,
    EVENT_TYPE_TRANSACTION_FAILED,
    EVENT_TYPE_UNKNOWN,
])

VERIFICATION_VERIFIED = "verified"
VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_CONFLICTING = "conflicting"
VERIFICATION_FAILED = "failed"

VERIFICATION_STATUSES = frozenset([
    VERIFICATION_VERIFIED,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_CONFLICTING,
    VERIFICATION_FAILED,
])

PROCESSING_STORED = "stored"
PROCESSING_APPLIED = "applied"
PROCESSING_HELD = "held"
PROCESSING_FAILED = "failed"

PROCESSING_STATUSES = frozenset([
    PROCESSING_STORED,
    PROCESSING_APPLIED,
    PROCESSING_HELD,
    PROCESSING_FAILED,
])

# Lifecycle/commitment values carried from the provider observation.
LIFECYCLE_PROCESSED = "processed"
LIFECYCLE_CONFIRMED = "confirmed"
LIFECYCLE_FINALIZED = "finalized"

# Only these lifecycle states may update confirmed market reserves.
CONFIRMED_LIFECYCLES = frozenset([LIFECYCLE_CONFIRMED, LIFECYCLE_FINALIZED])


def event_id_for(signature, slot):
    """Deterministic event ID: sha256("<signature>:<slot>").

    Same signature + slot -> same ID, always. ``slot`` may be None
    (preliminary observation); the ID then uses "none" and the
    processor merges it into the slotted record once known.
    """
    if not signature:
        raise ValueError("event_id_for requires a signature")
    slot_part = "none" if slot is None else str(slot)
    return hashlib.sha256(
        ("%s:%s" % (signature, slot_part)).encode("utf-8")).hexdigest()


def raw_tx_reference(provider, signature, slot, raw_tx=None):
    """Reference to the raw transaction (not the raw bytes).

    The full raw transaction is large and provider-shaped; the
    store keeps a canonical sha256 of it plus where to re-fetch
    it, never a second copy of the bytes.
    """
    ref = {"provider": provider, "signature": signature, "slot": slot}
    if raw_tx is not None:
        try:
            canonical = json.dumps(raw_tx, sort_keys=True, default=str)
        except Exception:
            canonical = str(raw_tx)
        ref["raw_sha256"] = hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()
    return ref


class LogicalEvent(object):
    """One logical event: many provider observations, one record."""

    def __init__(self, signature, slot=None, block_time=None,
                 event_type=EVENT_TYPE_UNKNOWN,
                 commitment=None, lifecycle_status=None,
                 verification_status=VERIFICATION_UNVERIFIED,
                 processing_status=PROCESSING_STORED,
                 decoder_version=DECODER_VERSION,
                 providers=None, observations=None,
                 raw_tx_ref=None, parsed_refs=None,
                 normalized_accounts=None, sol_movement=None,
                 token_movement=None, provenance=None,
                 disagreements=None, event_id=None,
                 created_at=None, updated_at=None):
        self.event_id = event_id or event_id_for(signature, slot)
        self.signature = signature
        self.slot = slot
        self.block_time = block_time
        self.event_type = event_type
        self.commitment = commitment
        self.lifecycle_status = lifecycle_status
        self.verification_status = verification_status
        self.processing_status = processing_status
        self.decoder_version = decoder_version
        self.providers = list(providers or [])
        self.observations = list(observations or [])
        self.raw_tx_ref = raw_tx_ref
        self.parsed_refs = list(parsed_refs or [])
        self.normalized_accounts = dict(normalized_accounts or {})
        self.sol_movement = list(sol_movement or [])
        self.token_movement = list(token_movement or [])
        self.provenance = dict(provenance or {})
        self.disagreements = list(disagreements or [])
        now = time.time()
        self.created_at = created_at if created_at is not None else now
        self.updated_at = updated_at if updated_at is not None else now

    # -- provenance -------------------------------------------------
    def note_provenance(self, field, provider):
        """Record that ``provider`` supplied ``field``."""
        owners = self.provenance.setdefault(field, [])
        if provider not in owners:
            owners.append(provider)

    def add_observation(self, observation):
        """Attach one provider observation (deduped by provider+kind).

        Returns True when the observation was new, False when an
        identical provider observation was already recorded.
        """
        provider = observation.get("provider")
        kind = observation.get("kind")
        for existing in self.observations:
            if existing.get("provider") == provider \
                    and existing.get("kind") == kind:
                return False
        self.observations.append(dict(observation))
        if provider and provider not in self.providers:
            self.providers.append(provider)
        return True

    def add_disagreement(self, field, values_by_provider):
        """Record a cross-provider disagreement (never resolved
        silently). ``values_by_provider`` maps provider -> value."""
        entry = {"field": field,
                 "values": dict(values_by_provider),
                 "recorded_at": time.time()}
        self.disagreements.append(entry)
        self.verification_status = VERIFICATION_CONFLICTING
        return entry

    # -- market-state gate ------------------------------------------
    def may_update_market_state(self):
        """True only for a verified, confirmed/finalized event.

        A preliminary/unverified transaction must NEVER update
        confirmed market reserves. A conflicting event must never
        be silently resolved into market state either.
        """
        return (self.verification_status == VERIFICATION_VERIFIED
                and self.lifecycle_status in CONFIRMED_LIFECYCLES)

    # -- serialisation ----------------------------------------------
    def to_dict(self):
        return {
            "event_id": self.event_id,
            "signature": self.signature,
            "slot": self.slot,
            "block_time": self.block_time,
            "event_type": self.event_type,
            "commitment": self.commitment,
            "lifecycle_status": self.lifecycle_status,
            "verification_status": self.verification_status,
            "processing_status": self.processing_status,
            "decoder_version": self.decoder_version,
            "providers": list(self.providers),
            "observations": [dict(o) for o in self.observations],
            "raw_tx_ref": dict(self.raw_tx_ref)
            if self.raw_tx_ref else None,
            "parsed_refs": [dict(r) for r in self.parsed_refs],
            "normalized_accounts": dict(self.normalized_accounts),
            "sol_movement": [dict(m) for m in self.sol_movement],
            "token_movement": [dict(m) for m in self.token_movement],
            "provenance": {k: list(v)
                           for k, v in self.provenance.items()},
            "disagreements": [dict(d) for d in self.disagreements],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            signature=data["signature"],
            slot=data.get("slot"),
            block_time=data.get("block_time"),
            event_type=data.get("event_type", EVENT_TYPE_UNKNOWN),
            commitment=data.get("commitment"),
            lifecycle_status=data.get("lifecycle_status"),
            verification_status=data.get(
                "verification_status", VERIFICATION_UNVERIFIED),
            processing_status=data.get(
                "processing_status", PROCESSING_STORED),
            decoder_version=data.get("decoder_version", DECODER_VERSION),
            providers=data.get("providers") or [],
            observations=data.get("observations") or [],
            raw_tx_ref=data.get("raw_tx_ref"),
            parsed_refs=data.get("parsed_refs") or [],
            normalized_accounts=data.get("normalized_accounts") or {},
            sol_movement=data.get("sol_movement") or [],
            token_movement=data.get("token_movement") or [],
            provenance=data.get("provenance") or {},
            disagreements=data.get("disagreements") or [],
            event_id=data.get("event_id"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )
