"""Cross-provider event normalization (Phase 8, 2026-11-22).

One observation per provider per transaction; many observations
merge into ONE logical event. An observation is a plain dict:

    {
      "provider": "alchemy" | "chainstack" | "helius" | "public",
      "kind": "getTransaction" | "parsed" | "stream",
      "tx": <getTransaction result dict> (jsonParsed/json/base64),
      "parsed": <Helius enhanced-transaction dict> (optional),
      "commitment": "processed" | "confirmed" | "finalized",
      "observed_at": <epoch seconds>,
    }

Normalization runs the §11 verification rules on every merge:
  (1) raw transaction data present and well-formed;
  (2) own decoder output (core + pump.fun / PumpSwap decoders);
  (3) Helius parsed output where available — cross-checked
      against the own-decoder event type / mint / amounts;
  (4) account-state changes (pre/post token balances from meta)
      — cross-checked against decoded trade amounts where the
      decoder exposes them;
  (5) confirmed execution metadata (meta.err, confirmation
      status, commitment).

If providers disagree on any compared field the event is marked
"conflicting" and the disagreement is recorded — never silently
resolved to one interpretation. Decoder disagreements and
failures are tracked on the event (``disagreements``) and counted
by ``EventNormalizer.stats``.

A preliminary/unverified transaction is never promoted to
"verified": verification requires the own decoder to verify AND
the lifecycle to be confirmed/finalized AND no disagreement.
"""

from __future__ import annotations

import logging
import time

from ..decoders import pumpfun_decoder as _pf
from ..decoders import pumpswap_decoder as _ps
from ..decoders import solana_transaction as _core
from .event_models import (
    CONFIRMED_LIFECYCLES,
    DECODER_VERSION,
    EVENT_TYPE_TOKEN_CREATED,
    EVENT_TYPE_TOKEN_GRADUATED,
    EVENT_TYPE_TOKEN_MIGRATED,
    EVENT_TYPE_POOL_CREATED,
    EVENT_TYPE_TOKEN_MINT,
    EVENT_TYPE_TOKEN_TRADE,
    EVENT_TYPE_TRANSACTION_FAILED,
    EVENT_TYPE_UNKNOWN,
    PROCESSING_HELD,
    PROCESSING_STORED,
    VERIFICATION_CONFLICTING,
    VERIFICATION_FAILED,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
    LogicalEvent,
    event_id_for,
    raw_tx_reference,
)

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.events.event_normalizer")

# Pump.fun / PumpSwap decoder event names -> logical event type.
_PUMP_EVENT_TYPES = {
    "event:CreateEvent": EVENT_TYPE_TOKEN_CREATED,
    "event:TradeEvent": EVENT_TYPE_TOKEN_TRADE,
    "event:CompleteEvent": EVENT_TYPE_TOKEN_GRADUATED,
    "event:CompletePumpAmmMigrationEvent": EVENT_TYPE_TOKEN_MIGRATED,
    "event:PostCompleteBuyEvent": EVENT_TYPE_TOKEN_TRADE,
    "instruction:create": EVENT_TYPE_TOKEN_CREATED,
    "instruction:buy": EVENT_TYPE_TOKEN_TRADE,
    "instruction:sell": EVENT_TYPE_TOKEN_TRADE,
    "instruction:migrate": EVENT_TYPE_TOKEN_MIGRATED,
}
_PUMPSWAP_EVENT_TYPES = {
    "event:BuyEvent": EVENT_TYPE_TOKEN_TRADE,
    "event:SellEvent": EVENT_TYPE_TOKEN_TRADE,
    # A PumpSwap pool creation is NOT a token creation — the token
    # already exists (a migration creates its pool). It gets its
    # own type so it never outranks a migration event in the same
    # transaction.
    "event:CreatePoolEvent": EVENT_TYPE_POOL_CREATED,
}
_INSTRUCTION_MINT = ("instruction:create", "instruction:initialize_mint",
                     "instruction:initialize_mint2", "instruction:mint_to")

# Priority order when several decoded events share one transaction:
# creation beats trade beats migration beats pool creation beats
# graduation beats mint.
_EVENT_PRIORITY = {
    EVENT_TYPE_TOKEN_CREATED: 0,
    EVENT_TYPE_TOKEN_TRADE: 1,
    EVENT_TYPE_TOKEN_MIGRATED: 2,
    EVENT_TYPE_POOL_CREATED: 3,
    EVENT_TYPE_TOKEN_GRADUATED: 4,
    EVENT_TYPE_TOKEN_MINT: 5,
    EVENT_TYPE_TRANSACTION_FAILED: 6,
    EVENT_TYPE_UNKNOWN: 7,
}


def _helius_event_type(parsed):
    """Best-effort logical type from a Helius enhanced tx.

    Helius "type" values include TOKEN_MINT, NFT_MINT, TRANSFER,
    SWAP, etc. Only the values we can map confidently are mapped;
    anything else yields None (no invented event type).
    """
    if not isinstance(parsed, dict):
        return None
    htype = parsed.get("type")
    if htype == "TOKEN_MINT":
        return EVENT_TYPE_TOKEN_MINT
    if htype == "SWAP":
        return EVENT_TYPE_TOKEN_TRADE
    if htype == "TRANSFER":
        return None  # ambiguous: could be any token movement
    return None


# Helius type / own-decoder type pairs that describe the same
# on-chain fact under different names (e.g. a pump.fun create
# mints the token supply — Helius calls it TOKEN_MINT, the own
# decoder calls it token_created). These are compatible, not a
# disagreement.
_COMPATIBLE_EVENT_TYPES = frozenset([
    (EVENT_TYPE_TOKEN_MINT, EVENT_TYPE_TOKEN_CREATED),
])


def _helius_token_transfers(parsed):
    """Normalize Helius tokenTransfers to {mint, from, to, amount}."""
    out = []
    if not isinstance(parsed, dict):
        return out
    for t in parsed.get("tokenTransfers") or []:
        if not isinstance(t, dict):
            continue
        try:
            amount = int(t.get("tokenAmount") or 0)
        except (TypeError, ValueError):
            continue
        out.append({"mint": t.get("mint"),
                    "from": t.get("fromUserAccount"),
                    "to": t.get("toUserAccount"),
                    "amount": amount,
                    "authority": t.get("fromTokenAccount")})
    return out


def _helius_native_transfers(parsed):
    out = []
    if not isinstance(parsed, dict):
        return out
    for t in parsed.get("nativeTransfers") or []:
        if not isinstance(t, dict):
            continue
        try:
            amount = int(t.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        out.append({"from": t.get("fromUserAccount"),
                    "to": t.get("toUserAccount"),
                    "amount_lamports": amount})
    return out


class EventNormalizer(object):
    """Normalize provider observations into logical events."""

    def __init__(self):
        self.stats = {"normalized": 0, "merged": 0, "conflicts": 0,
                      "decoder_failures": 0, "unverified": 0}

    # -- per-observation decoding -----------------------------------
    def _decode_own(self, tx):
        """Run the own decoders over one getTransaction result.

        Returns (event_type, fields, validation, core_decoded,
        pump_events, program). ``fields`` are the decoded event fields
        of the highest-priority pump event (or {} when none).
        ``program`` is that same winning event's program_id (None
        when nothing decoded) - the event record's program identity
        (request section 5) comes from the decoder, never guessed.
        """
        core = _core.decode_transaction(tx) if tx is not None else None
        pump_events = []
        if tx is not None:
            try:
                pump_events = _pf.decode_transaction(tx) or []
            except Exception as exc:  # decoder must never crash the pipe
                logger.warning("pumpfun decoder failed: %s", exc)
                self.stats["decoder_failures"] += 1
                pump_events = []
            try:
                pump_events = pump_events + (
                    _ps.decode_transaction(tx) or [])
            except Exception as exc:
                logger.warning("pumpswap decoder failed: %s", exc)
                self.stats["decoder_failures"] += 1
        event_type = EVENT_TYPE_UNKNOWN
        fields = {}
        validation = "unknown"
        if core is not None and (core.get("meta") or {}).get("err") is not None:
            return (EVENT_TYPE_TRANSACTION_FAILED, {}, "unverified",
                    core, pump_events, None)
        best_priority = _EVENT_PRIORITY[EVENT_TYPE_UNKNOWN]
        best_is_event = False
        best_program = None
        for ev in pump_events:
            etype = ev.get("event_type") or ""
            mapped = _PUMP_EVENT_TYPES.get(etype) \
                or _PUMPSWAP_EVENT_TYPES.get(etype)
            if mapped is None and etype in _INSTRUCTION_MINT:
                mapped = EVENT_TYPE_TOKEN_MINT
            if mapped is None:
                continue
            prio = _EVENT_PRIORITY[mapped]
            is_event = etype.startswith("event:")
            # An emitted event carries the full field set; the
            # matching instruction often carries none. At equal
            # priority the event wins over the instruction.
            if prio < best_priority or (prio == best_priority
                                        and is_event
                                        and not best_is_event):
                best_priority = prio
                best_is_event = is_event
                event_type = mapped
                fields = ev.get("fields") or {}
                validation = ev.get("validation") or "unknown"
                best_program = ev.get("program_id")
        return event_type, fields, validation, core, pump_events, \
            best_program

    def _movement_from_core(self, core):
        """SOL + token movement from the core decoder's meta."""
        sol_movement = []
        token_movement = []
        if core is None:
            return sol_movement, token_movement
        meta = core.get("meta") or {}
        for change in meta.get("balance_changes") or []:
            delta = change.get("delta")
            if delta:
                sol_movement.append({
                    "account": change.get("account"),
                    "account_index": change.get("account_index"),
                    "delta_lamports": delta})
        for change in meta.get("token_balance_changes") or []:
            if change.get("delta"):
                token_movement.append({
                    "mint": change.get("mint"),
                    "owner": change.get("owner"),
                    "account_index": change.get("account_index"),
                    "delta": change.get("delta"),
                    "pre_amount": change.get("pre_amount"),
                    "post_amount": change.get("post_amount")})
        return sol_movement, token_movement

    def _normalized_accounts(self, core, fields):
        """Normalized account map: role -> address, from decoder."""
        accounts = {}
        if core is not None:
            keys = core.get("account_keys") or []
            if keys:
                accounts["fee_payer"] = core.get("fee_payer") or keys[0]
                accounts["all"] = list(keys)
        for role in ("mint", "bonding_curve", "user", "creator",
                     "pool", "quote_mint", "token_program"):
            value = (fields or {}).get(role)
            if value:
                accounts[role] = value
        # PumpSwap names the token "base_mint", not "mint".
        if "mint" not in accounts and (fields or {}).get("base_mint"):
            accounts["mint"] = fields["base_mint"]
        return accounts

    # -- single observation -> partial logical event -----------------
    def normalize_observation(self, observation):
        """Normalize ONE provider observation into a LogicalEvent.

        The event carries only this observation; merging several
        observations of the same signature happens in
        ``merge_observations`` / ``merge_into``.
        """
        if not isinstance(observation, dict):
            raise ValueError("observation must be a dict")
        provider = observation.get("provider") or "unknown"
        tx = observation.get("tx")
        parsed = observation.get("parsed")
        commitment = observation.get("commitment")
        observed_at = observation.get("observed_at") or time.time()

        signature = None
        slot = None
        block_time = None
        if isinstance(tx, dict):
            sigs = None
            envelope = tx.get("transaction")
            if isinstance(envelope, dict):
                sigs = envelope.get("signatures")
            signature = (sigs or [None])[0] if sigs else None
            slot = tx.get("slot")
            block_time = tx.get("blockTime")
        if signature is None and isinstance(parsed, dict):
            signature = parsed.get("signature")
        if slot is None and isinstance(parsed, dict):
            slot = parsed.get("slot")
        if block_time is None and isinstance(parsed, dict):
            block_time = parsed.get("timestamp")
        if not signature:
            raise ValueError(
                "observation carries no signature (provider=%s)" % provider)

        event_type, fields, validation, core, _pump, program = \
            self._decode_own(tx)
        sol_movement, token_movement = self._movement_from_core(core)
        accounts = self._normalized_accounts(core, fields)

        # (5) execution metadata: failed tx is a verified failure.
        meta_err = None
        if core is not None:
            meta_err = (core.get("meta") or {}).get("err")
        elif isinstance(tx, dict):
            meta_err = (tx.get("meta") or {}).get("err")
        if meta_err is not None:
            event_type = EVENT_TYPE_TRANSACTION_FAILED
            validation = "unverified"
            # A failed transaction still has program identity when the
            # decoders recognised one; only the outcome changes.

        # Recognition latency (request section 5): on-chain event time
        # (block_time) to the bot's first recognition of it (this
        # observation's receipt). Raw difference - clock skew shows up
        # as a negative value and is itself the evidence; unknown
        # block_time leaves it None, never zero.
        recognition_latency_s = None
        if block_time is not None:
            try:
                recognition_latency_s = round(
                    float(observed_at) - float(block_time), 3)
            except (TypeError, ValueError):
                recognition_latency_s = None

        event = LogicalEvent(
            signature=signature, slot=slot, block_time=block_time,
            event_type=event_type,
            program=program,
            recognition_latency_s=recognition_latency_s,
            commitment=commitment,
            lifecycle_status=commitment,
            decoder_version=DECODER_VERSION,
            raw_tx_ref=raw_tx_reference(provider, signature, slot, tx)
            if tx is not None else None,
            normalized_accounts=accounts,
            sol_movement=sol_movement,
            token_movement=token_movement)
        event.note_provenance("signature", provider)
        if slot is not None:
            event.note_provenance("slot", provider)
        if block_time is not None:
            event.note_provenance("block_time", provider)
        event.note_provenance("event_type", provider)
        if program is not None:
            event.note_provenance("program", provider)
        if recognition_latency_s is not None:
            event.note_provenance("recognition_latency_s", provider)
        event.note_provenance("normalized_accounts", provider)
        event.note_provenance("sol_movement", provider)
        event.note_provenance("token_movement", provider)
        if parsed is not None:
            event.parsed_refs.append({
                "provider": provider, "kind": "helius_enhanced",
                "signature": signature, "slot": slot,
                "type": parsed.get("type")})
            event.note_provenance("parsed_refs", provider)

        obs_record = {"provider": provider,
                      "kind": observation.get("kind") or "getTransaction",
                      "commitment": commitment,
                      "observed_at": observed_at,
                      "decoder_validation": validation,
                      "decoded_event_type": event_type,
                      "decoded_fields": fields}
        event.add_observation(obs_record)

        # (3) Helius parsed cross-check, where available.
        helius_type = _helius_event_type(parsed)
        if parsed is not None and helius_type is not None \
                and event_type not in (EVENT_TYPE_UNKNOWN,
                                       EVENT_TYPE_TRANSACTION_FAILED):
            if helius_type != event_type and (
                    helius_type, event_type) not in _COMPATIBLE_EVENT_TYPES:
                event.add_disagreement("event_type", {
                    provider: helius_type, "own_decoder": event_type})
                self.stats["conflicts"] += 1
        if parsed is not None:
            self._cross_check_helius_amounts(event, parsed, provider)

        # Verification (§11): verified only when the own decoder
        # verifies, execution metadata is clean, lifecycle is
        # confirmed/finalized, and nothing disagrees.
        if event_type == EVENT_TYPE_TRANSACTION_FAILED:
            event.verification_status = VERIFICATION_FAILED
            event.processing_status = PROCESSING_STORED
        elif event.disagreements:
            event.verification_status = VERIFICATION_CONFLICTING
            event.processing_status = PROCESSING_HELD
        elif validation == "verified" \
                and commitment in CONFIRMED_LIFECYCLES:
            event.verification_status = VERIFICATION_VERIFIED
            event.processing_status = PROCESSING_STORED
        else:
            event.verification_status = VERIFICATION_UNVERIFIED
            event.processing_status = PROCESSING_HELD
            self.stats["unverified"] += 1
        self.stats["normalized"] += 1
        return event

    def _cross_check_helius_amounts(self, event, parsed, provider):
        """(3)+(4) Compare Helius transfers against own-decoder
        token movement. A Helius transfer amount is consistent
        when it matches one individual own-decoder delta for the
        same mint (within 0.1%) or the net delta for that mint.
        Anything else is a disagreement, not a silent override."""
        helius_transfers = _helius_token_transfers(parsed)
        if not helius_transfers:
            return
        own_deltas = {}
        for move in event.token_movement:
            own_deltas.setdefault(move.get("mint"), []).append(
                abs(move.get("delta") or 0))
        for transfer in helius_transfers:
            mint = transfer.get("mint")
            amount = transfer.get("amount") or 0
            deltas = own_deltas.get(mint)
            if not deltas:
                event.add_disagreement("token_movement", {
                    provider: {"mint": mint, "amount": amount},
                    "own_decoder": {"mint": mint, "amount": 0}})
                self.stats["conflicts"] += 1
                continue
            net = abs(sum((m.get("delta") or 0)
                          for m in event.token_movement
                          if m.get("mint") == mint))
            tolerance = max(1, amount // 1000)
            consistent = any(abs(d - amount) <= tolerance
                             for d in deltas) \
                or abs(net - amount) <= tolerance
            if not consistent:
                event.add_disagreement("token_movement", {
                    provider: {"mint": mint, "amount": amount},
                    "own_decoder": {"mint": mint,
                                    "deltas": sorted(deltas)}})
                self.stats["conflicts"] += 1

    # -- merging -----------------------------------------------------
    def merge_into(self, event, observation):
        """Merge a new observation into an existing logical event.

        Returns the (mutated) event. Field-level disagreements
        across providers are recorded, never silently resolved.
        """
        other = self.normalize_observation(observation)
        if other.signature != event.signature:
            raise ValueError(
                "merge_into: observation signature %s does not match "
                "event signature %s" % (other.signature, event.signature))
        self.stats["merged"] += 1
        event.add_observation(
            other.observations[0] if other.observations else
            {"provider": observation.get("provider"),
             "kind": observation.get("kind")})
        provider = observation.get("provider") or "unknown"

        # Slot / block time must agree across providers.
        if other.slot is not None and event.slot is not None \
                and other.slot != event.slot:
            event.add_disagreement("slot", {
                provider: other.slot, "existing": event.slot})
            self.stats["conflicts"] += 1
        elif other.slot is not None and event.slot is None:
            event.slot = other.slot
            event.event_id = event_id_for(event.signature, event.slot)
            event.note_provenance("slot", provider)
        if other.block_time is not None and event.block_time is not None \
                and other.block_time != event.block_time:
            event.add_disagreement("block_time", {
                provider: other.block_time,
                "existing": event.block_time})
            self.stats["conflicts"] += 1
        elif other.block_time is not None and event.block_time is None:
            event.block_time = other.block_time
            event.note_provenance("block_time", provider)

        # Event type must agree (unknown is not a disagreement —
        # one provider may simply not decode it).
        if other.event_type not in (EVENT_TYPE_UNKNOWN,
                                    EVENT_TYPE_TRANSACTION_FAILED) \
                and event.event_type not in (
                    EVENT_TYPE_UNKNOWN, EVENT_TYPE_TRANSACTION_FAILED) \
                and other.event_type != event.event_type:
            event.add_disagreement("event_type", {
                provider: other.event_type,
                "existing": event.event_type})
            self.stats["conflicts"] += 1
        elif event.event_type == EVENT_TYPE_UNKNOWN \
                and other.event_type != EVENT_TYPE_UNKNOWN:
            event.event_type = other.event_type
            event.note_provenance("event_type", provider)

        # Execution outcome: a later observation that says the transaction
        # FAILED on chain, against a record that holds a decoded non-failed
        # event, is a disagreement (never silently resolved, never borrowed
        # as verified by the earlier observation). Unknown stays neutral.
        if other.event_type == EVENT_TYPE_TRANSACTION_FAILED \
                and event.event_type not in (
                    EVENT_TYPE_UNKNOWN, EVENT_TYPE_TRANSACTION_FAILED):
            event.add_disagreement("execution_status", {
                provider: "failed", "existing": "executed"})
            self.stats["conflicts"] += 1

        # Program identity: unknown never overwrites known; two known
        # programs that disagree are a recorded disagreement, never a
        # silent pick.
        if other.program is not None and event.program is not None \
                and other.program != event.program:
            event.add_disagreement("program", {
                provider: other.program,
                "existing": event.program})
            self.stats["conflicts"] += 1
        elif event.program is None and other.program is not None:
            event.program = other.program
            event.note_provenance("program", provider)

        # Recognition latency: the honest metric is the EARLIEST
        # recognition, so a later observation can only lower it
        # (its receipt was earlier relative to the same block time).
        if other.recognition_latency_s is not None and (
                event.recognition_latency_s is None
                or other.recognition_latency_s
                < event.recognition_latency_s):
            event.recognition_latency_s = other.recognition_latency_s
            event.note_provenance("recognition_latency_s", provider)

        # Lifecycle: take the strongest confirmation seen.
        order = {"processed": 0, "confirmed": 1, "finalized": 2}
        if other.lifecycle_status in order and (
                event.lifecycle_status not in order
                or order[other.lifecycle_status]
                > order[event.lifecycle_status]):
            event.lifecycle_status = other.lifecycle_status
            event.commitment = other.lifecycle_status
            event.note_provenance("lifecycle_status", provider)

        # Movement / accounts: union, provenance-noted. A second
        # provider's movement replaces nothing — it is kept as a
        # separate observation; the merged record keeps the first
        # verified movement and notes the extra provider.
        if not event.sol_movement and other.sol_movement:
            event.sol_movement = other.sol_movement
            event.note_provenance("sol_movement", provider)
        if not event.token_movement and other.token_movement:
            event.token_movement = other.token_movement
            event.note_provenance("token_movement", provider)
        for role, addr in other.normalized_accounts.items():
            if role not in event.normalized_accounts:
                event.normalized_accounts[role] = addr
                event.note_provenance("normalized_accounts", provider)
            elif event.normalized_accounts[role] != addr \
                    and role != "all":
                event.add_disagreement("normalized_accounts." + role, {
                    provider: addr,
                    "existing": event.normalized_accounts[role]})
                self.stats["conflicts"] += 1
        for ref in other.parsed_refs:
            if ref not in event.parsed_refs:
                event.parsed_refs.append(ref)
        if other.raw_tx_ref and not event.raw_tx_ref:
            event.raw_tx_ref = other.raw_tx_ref
        for disc in other.disagreements:
            if disc not in event.disagreements:
                event.disagreements.append(disc)

        # Re-derive verification after the merge.
        if event.event_type == EVENT_TYPE_TRANSACTION_FAILED:
            event.verification_status = VERIFICATION_FAILED
            event.processing_status = PROCESSING_STORED
        elif event.disagreements:
            event.verification_status = VERIFICATION_CONFLICTING
            event.processing_status = PROCESSING_HELD
        elif event.has_confirmed_verified_observation() \
                and event.lifecycle_status in CONFIRMED_LIFECYCLES:
            event.verification_status = VERIFICATION_VERIFIED
            event.processing_status = PROCESSING_STORED
        else:
            event.verification_status = VERIFICATION_UNVERIFIED
            event.processing_status = PROCESSING_HELD
        event.updated_at = time.time()
        return event

    def merge_observations(self, observations):
        """Normalize a list of observations of the SAME transaction
        into one logical event. Raises ValueError on an empty list
        or on observations of different signatures."""
        if not observations:
            raise ValueError("merge_observations needs >= 1 observation")
        event = self.normalize_observation(observations[0])
        for obs in observations[1:]:
            event = self.merge_into(event, obs)
        return event
