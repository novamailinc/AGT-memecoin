#!/usr/bin/env python3
"""Tests for the Phase 8 event pipeline (2026-11-22).

Plain script, run directly with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_intelligence_events.py

Covers: event_models (deterministic IDs, market-state gate),
event_normalizer (single + merged multi-provider observations,
§11 verification, conflict flagging), event_deduplicator,
event_processor (full pipeline from real fixtures, duplicate
delivery, market-state gate, slot-gap tracking), event_recovery
(retry queue, reprocessing, disconnect backfill via the
acquisition layer's event_backfill).

Fixtures are the real on-chain transactions under
tests/data/intelligence/ (atx_create / atx_buy / atx_migrate /
notai_create). The Helius enhanced-transaction observations used
here are locally constructed from each fixture's own meta
(pre/post token balances) — no real Helius parsed fixture exists
in-repo — and are labelled as such where built.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.intelligence.events import (
    CheckpointTracker,
    EventDeduplicator,
    EventNormalizer,
    EventProcessor,
    EventStore,
    LogicalEvent,
    RetryQueue,
    backfill_after_disconnect,
    event_id_for,
    reprocess_failed,
)
from daemon.intelligence.events.event_models import (
    EVENT_TYPE_TOKEN_CREATED,
    EVENT_TYPE_TOKEN_MIGRATED,
    EVENT_TYPE_TOKEN_TRADE,
    EVENT_TYPE_TRANSACTION_FAILED,
    VERIFICATION_CONFLICTING,
    VERIFICATION_FAILED,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
)

FAILS = []
PASSES = [0]


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)


DATA = os.path.join(os.path.dirname(__file__), "data", "intelligence")

ATX_MINT = "BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump"
ATX_POOL = "3Y7Knabn84dpB6yy5LbCFt574E8ifTMeWkBMTPvGZCwE"
ATX_CREATE_SIG = ("2avAXaepDw4uPC4Ue3khKWx9fpQzXPvm3pNYtZi1VwJfvNsD8GY25"
                  "WzYaXvAWmKsTgdtAT5NRHxgzmDF67RRfih")
ATX_CREATE_SLOT = 472184636
ATX_BUY_SIG = "2o4dRrK92ea1xLf68EaG"
ATX_MIGRATE_SIG = "56zVpuKUKGWBodxDdnrk"
NOTAI_MINT = "8bGA7uQRGURruJLacPopiTSJtzS49VvLL7C5yDBpump"


def load_fixture(name):
    with open(os.path.join(DATA, name)) as f:
        wrap = json.load(f)
    return wrap.get("transaction") or wrap.get("rpc_result") or wrap


def signature_of(tx):
    return tx["transaction"]["signatures"][0]


def helius_parsed_from_fixture(tx, htype, amount_scale=1):
    """Helius-shaped enhanced transaction built from the fixture's
    own meta token-balance changes (labelled: locally constructed,
    no real Helius parsed fixture exists in-repo)."""
    transfers = []
    for bal in tx.get("meta", {}).get("postTokenBalances") or []:
        pre = next((b for b in tx["meta"].get("preTokenBalances") or []
                    if b.get("accountIndex") == bal.get("accountIndex")),
                   None)
        pre_amt = int(pre["uiTokenAmount"]["amount"]) if pre else 0
        post_amt = int(bal["uiTokenAmount"]["amount"])
        delta = post_amt - pre_amt
        if delta:
            transfers.append({
                "mint": bal.get("mint"),
                "fromUserAccount": bal.get("owner"),
                "toUserAccount": bal.get("owner"),
                "tokenAmount": abs(delta) * amount_scale})
    return {"signature": signature_of(tx),
            "slot": tx.get("slot"),
            "timestamp": tx.get("blockTime"),
            "type": htype,
            "source": "PUMP_FUN",
            "tokenTransfers": transfers,
            "nativeTransfers": []}


def temp_store():
    tmpdir = tempfile.mkdtemp(prefix="intelligence-events-")
    return EventStore(os.path.join(tmpdir, "intelligence_events.db"))


# ---------------------------------------------------------------- models
def test_models():
    sig = ATX_CREATE_SIG
    id_a = event_id_for(sig, ATX_CREATE_SLOT)
    id_b = event_id_for(sig, ATX_CREATE_SLOT)
    check("models: same signature+slot -> same deterministic ID",
          id_a == id_b and len(id_a) == 64)
    check("models: different slot -> different ID",
          event_id_for(sig, ATX_CREATE_SLOT + 1) != id_a)
    check("models: slot-less observation gets a 'none' ID",
          event_id_for(sig, None) != id_a)
    try:
        event_id_for(None, 1)
        check("models: missing signature raises", False)
    except ValueError:
        check("models: missing signature raises", True)

    ev = LogicalEvent(signature=sig, slot=ATX_CREATE_SLOT,
                      event_type=EVENT_TYPE_TOKEN_CREATED,
                      verification_status=VERIFICATION_VERIFIED,
                      lifecycle_status="confirmed")
    check("models: verified+confirmed may update market state",
          ev.may_update_market_state())
    ev.lifecycle_status = "processed"
    check("models: processed (preliminary) must NOT update market state",
          not ev.may_update_market_state())
    ev.lifecycle_status = "confirmed"
    ev.verification_status = VERIFICATION_UNVERIFIED
    check("models: unverified must NOT update market state",
          not ev.may_update_market_state())
    ev.verification_status = VERIFICATION_CONFLICTING
    check("models: conflicting must NOT update market state",
          not ev.may_update_market_state())

    ev2 = LogicalEvent(signature=sig, slot=ATX_CREATE_SLOT)
    check("models: observation dedup by provider+kind",
          ev2.add_observation({"provider": "alchemy",
                               "kind": "getTransaction"})
          and not ev2.add_observation({"provider": "alchemy",
                                       "kind": "getTransaction"})
          and ev2.add_observation({"provider": "helius",
                                   "kind": "parsed"}))
    ev2.note_provenance("slot", "alchemy")
    ev2.note_provenance("slot", "alchemy")
    check("models: provenance deduped per field",
          ev2.provenance["slot"] == ["alchemy"])
    roundtrip = LogicalEvent.from_dict(ev2.to_dict())
    check("models: to_dict/from_dict roundtrip preserves ID + fields",
          roundtrip.event_id == ev2.event_id
          and roundtrip.signature == sig
          and roundtrip.providers == ev2.providers)


# -------------------------------------------------------------- normalizer
def test_normalizer_single():
    norm = EventNormalizer()
    tx = load_fixture("atx_create.json")
    obs = {"provider": "alchemy", "kind": "getTransaction", "tx": tx,
           "commitment": "confirmed", "observed_at": 1795273900}
    ev = norm.normalize_observation(obs)
    check("normalizer: create tx -> token_created",
          ev.event_type == EVENT_TYPE_TOKEN_CREATED)
    check("normalizer: signature/slot/block_time from raw tx",
          ev.signature == ATX_CREATE_SIG
          and ev.slot == ATX_CREATE_SLOT
          and ev.block_time == 1795273899)
    check("normalizer: verified create (decoder verified + confirmed)",
          ev.verification_status == VERIFICATION_VERIFIED)
    check("normalizer: normalized accounts carry mint + creator",
          ev.normalized_accounts.get("mint") == ATX_MINT
          and ev.normalized_accounts.get("creator"))
    check("normalizer: token movement present from meta",
          any(m.get("mint") == ATX_MINT for m in ev.token_movement))
    check("normalizer: SOL movement present from meta",
          len(ev.sol_movement) > 0)
    check("normalizer: provenance records alchemy for key fields",
          "alchemy" in ev.provenance.get("slot", [])
          and "alchemy" in ev.provenance.get("event_type", []))
    check("normalizer: raw tx reference stores sha256, not bytes",
          ev.raw_tx_ref is not None
          and len(ev.raw_tx_ref.get("raw_sha256", "")) == 64)

    # Preliminary (processed) observation is held, never verified.
    obs_proc = dict(obs)
    obs_proc["commitment"] = "processed"
    ev_proc = norm.normalize_observation(obs_proc)
    check("normalizer: processed commitment -> unverified + held",
          ev_proc.verification_status == VERIFICATION_UNVERIFIED
          and ev_proc.processing_status == "held"
          and not ev_proc.may_update_market_state())

    # Failed transaction: verified failure, no trade events.
    tx_failed = load_fixture("atx_sell_failed.json")
    ev_fail = norm.normalize_observation(
        {"provider": "helius", "kind": "getTransaction",
         "tx": tx_failed, "commitment": "finalized"})
    check("normalizer: failed tx -> transaction_failed, verified failure",
          ev_fail.event_type == EVENT_TYPE_TRANSACTION_FAILED
          and ev_fail.verification_status == VERIFICATION_FAILED)

    # No signature -> error, never an invented event.
    try:
        norm.normalize_observation({"provider": "alchemy", "tx": {}})
        check("normalizer: signature-less observation raises", False)
    except ValueError:
        check("normalizer: signature-less observation raises", True)


def test_normalizer_merge():
    norm = EventNormalizer()
    tx = load_fixture("atx_create.json")
    helius = helius_parsed_from_fixture(tx, "TOKEN_MINT")
    obs_alchemy = {"provider": "alchemy", "kind": "getTransaction",
                   "tx": tx, "commitment": "confirmed"}
    obs_chainstack = {"provider": "chainstack", "kind": "getTransaction",
                      "tx": tx, "commitment": "confirmed"}
    obs_helius = {"provider": "helius", "kind": "parsed", "tx": tx,
                  "parsed": helius, "commitment": "finalized"}
    merged = norm.merge_observations(
        [obs_alchemy, obs_chainstack, obs_helius])
    check("merge: three providers -> ONE logical event",
          len(merged.providers) == 3
          and set(merged.providers) == {"alchemy", "chainstack", "helius"}
          and len(merged.observations) == 3)
    check("merge: agreeing providers stay verified",
          merged.verification_status == VERIFICATION_VERIFIED)
    check("merge: lifecycle takes strongest confirmation (finalized)",
          merged.lifecycle_status == "finalized")
    check("merge: helius parsed ref recorded",
          any(r.get("provider") == "helius"
              for r in merged.parsed_refs))
    check("merge: no disagreements when providers agree",
          merged.disagreements == [])

    # Conflicting amounts: Helius transfer 10x the own-decoder delta.
    helius_bad = helius_parsed_from_fixture(tx, "TOKEN_MINT",
                                            amount_scale=10)
    merged_bad = norm.merge_observations(
        [obs_alchemy,
         {"provider": "helius", "kind": "parsed", "tx": tx,
          "parsed": helius_bad, "commitment": "finalized"}])
    check("merge: 10x amount disagreement -> conflicting, never resolved",
          merged_bad.verification_status == VERIFICATION_CONFLICTING
          and merged_bad.disagreements
          and merged_bad.disagreements[0]["field"] == "token_movement")
    check("merge: conflicting event is held from market state",
          merged_bad.processing_status == "held"
          and not merged_bad.may_update_market_state())

    # Conflicting slot across providers, merged explicitly.
    obs_other_slot = dict(obs_chainstack)
    tx_other = json.loads(json.dumps(tx))
    tx_other["slot"] = ATX_CREATE_SLOT + 5
    obs_other_slot["tx"] = tx_other
    merged_slot = norm.merge_observations([obs_alchemy, obs_other_slot])
    check("merge: slot disagreement flagged conflicting",
          merged_slot.verification_status == VERIFICATION_CONFLICTING
          and any(d["field"] == "slot"
                  for d in merged_slot.disagreements))

    # Different signatures must not merge.
    tx2 = load_fixture("notai_create.json")
    try:
        norm.merge_observations(
            [obs_alchemy,
             {"provider": "chainstack", "kind": "getTransaction",
              "tx": tx2, "commitment": "confirmed"}])
        check("merge: different signatures raise", False)
    except ValueError:
        check("merge: different signatures raise", True)

    # Trade + migration fixtures map to the right types.
    ev_buy = norm.normalize_observation(
        {"provider": "alchemy", "kind": "getTransaction",
         "tx": load_fixture("atx_buy.json"), "commitment": "confirmed"})
    check("normalizer: buy tx -> token_trade (priority over graduation)",
          ev_buy.event_type == EVENT_TYPE_TOKEN_TRADE)
    ev_mig = norm.normalize_observation(
        {"provider": "alchemy", "kind": "getTransaction",
         "tx": load_fixture("atx_migrate.json"),
         "commitment": "confirmed"})
    check("normalizer: migrate tx -> token_migrated with pool",
          ev_mig.event_type == EVENT_TYPE_TOKEN_MIGRATED
          and ev_mig.normalized_accounts.get("pool") == ATX_POOL)


# --------------------------------------------------------------- deduplicator
def test_deduplicator():
    dedup = EventDeduplicator(capacity=3)
    eid = event_id_for(ATX_CREATE_SIG, ATX_CREATE_SLOT)
    check("dedup: first add is new", dedup.add(eid))
    check("dedup: second add is a duplicate", not dedup.add(eid))
    check("dedup: seen() reflects membership", dedup.seen(eid))
    for i in range(3):
        dedup.add(event_id_for("sig%d" % i, i))
    check("dedup: bounded capacity evicts oldest (FIFO)",
          len(dedup) == 3 and not dedup.seen(eid))


# ---------------------------------------------------------------- processor
def test_processor_full_pipeline():
    store = temp_store().open()
    try:
        processor = EventProcessor(store)
        tx = load_fixture("atx_create.json")
        outcome, event_id = processor.process_transaction(
            "alchemy", tx, commitment="confirmed",
            stream_key="pump_create")
        check("processor: create event travels full pipeline -> applied",
              outcome == "applied")
        stored = store.get_event(event_id)
        check("processor: stored event is verified + applied",
              stored is not None
              and stored.verification_status == VERIFICATION_VERIFIED
              and stored.processing_status == "applied")
        state = processor.registry.get(ATX_MINT)
        check("processor: token state updated with correct mint/name/symbol",
              state is not None
              and state["mint"] == ATX_MINT
              and state["name"] == "PROJECT ATX"
              and state["symbol"] == "ATX"
              and state["created_slot"] == ATX_CREATE_SLOT)

        # Duplicate delivery (same provider, same tx) -> one event.
        outcome2, event_id2 = processor.process_transaction(
            "alchemy", tx, commitment="confirmed",
            stream_key="pump_create")
        check("processor: duplicate delivery -> duplicate, same ID",
              outcome2 == "duplicate" and event_id2 == event_id)
        check("processor: duplicate delivery -> still ONE stored event",
              store.count_events() == 1)

        # Second provider, same tx -> merged into the same record.
        outcome3, event_id3 = processor.process_transaction(
            "chainstack", tx, commitment="confirmed",
            stream_key="pump_create")
        check("processor: second provider merges, same event ID",
              outcome3 in ("applied", "merged")
              and event_id3 == event_id)
        merged = store.get_event(event_id)
        check("processor: merged record carries both providers",
              set(merged.providers) == {"alchemy", "chainstack"}
              and store.count_events() == 1)

        # Helius parsed observation merges in too.
        helius = helius_parsed_from_fixture(tx, "TOKEN_MINT")
        outcome4, _ = processor.process_observation(
            {"provider": "helius", "kind": "parsed", "tx": tx,
             "parsed": helius, "commitment": "finalized"},
            stream_key="pump_create")
        check("processor: helius parsed merges, stays one record",
              outcome4 in ("applied", "merged")
              and store.count_events() == 1)
        check("processor: merged record stays verified after helius agree",
              store.get_event(event_id).verification_status
              == VERIFICATION_VERIFIED)

        # Trade + migration update the same token state.
        processor.process_transaction(
            "alchemy", load_fixture("atx_buy.json"),
            commitment="confirmed", stream_key="pump_create")
        processor.process_transaction(
            "alchemy", load_fixture("atx_migrate.json"),
            commitment="confirmed", stream_key="pump_create")
        state = processor.registry.get(ATX_MINT)
        check("processor: trade+migration update token state",
              state is not None and state["trade_count"] == 1
              and state["migrated"] is True
              and state["pool"] == ATX_POOL)
        check("processor: checkpoint tracks last processed slot",
              store.get_checkpoint("pump_create") is not None
              and store.get_checkpoint("pump_create")["last_slot"]
              == 472184748)

        # Conflicting multi-provider observation is held, never applied.
        tx_notai = load_fixture("notai_create.json")
        processor.process_transaction(
            "alchemy", tx_notai, commitment="confirmed",
            stream_key="pump_create")
        helius_bad = helius_parsed_from_fixture(
            tx_notai, "TOKEN_MINT", amount_scale=10)
        outcome_bad, bad_id = processor.process_observation(
            {"provider": "helius", "kind": "parsed", "tx": tx_notai,
             "parsed": helius_bad, "commitment": "finalized"},
            stream_key="pump_create")
        check("processor: conflicting observation -> held, not applied",
              outcome_bad == "held")
        bad = store.get_event(bad_id)
        check("processor: conflicting event flagged in store",
              bad.verification_status == VERIFICATION_CONFLICTING)
        state_notai = processor.registry.get(NOTAI_MINT)
        check("processor: conflicting event did NOT create token state "
              "from the helius merge (state stays from verified create)",
              state_notai is not None
              and state_notai["created_slot"] == 472082477)

        # Preliminary observation (fresh signature, never seen
        # confirmed) never updates market state.
        tx_fresh = load_fixture("atx_sell.json")
        outcome_p, id_p = processor.process_observation(
            {"provider": "public", "kind": "getTransaction",
             "tx": tx_fresh, "commitment": "processed"},
            stream_key="pump_create")
        check("processor: processed-only observation is held",
              outcome_p == "held")
        check("processor: held event recorded in store",
              store.get_event(id_p) is not None)
        check("processor: held preliminary event did NOT touch "
              "token state",
              processor.registry.get(ATX_MINT)["trade_count"] == 1)
    finally:
        store.close()


def test_processor_slot_gaps():
    store = temp_store().open()
    try:
        processor = EventProcessor(store)
        check("processor: first slot observation reports no gap",
              processor.observe_slot("s", 100) is None)
        check("processor: small jump reports no gap",
              processor.observe_slot("s", 105) is None)
        gap = processor.observe_slot("s", 200)
        check("processor: jump past tolerance records a gap",
              gap is not None and gap["from_slot"] == 105
              and gap["to_slot"] == 200)
        check("processor: slot_gaps() returns recorded gaps",
              len(processor.slot_gaps("s")) == 1)
    finally:
        store.close()


# ----------------------------------------------------------------- recovery
def test_recovery_retry_queue():
    store = temp_store().open()
    try:
        queue = RetryQueue(store)
        eid = event_id_for(ATX_CREATE_SIG, ATX_CREATE_SLOT)
        attempts = queue.enqueue(eid, ATX_CREATE_SIG, ATX_CREATE_SLOT,
                                 "decoder blew up")
        check("recovery: enqueue records attempt 1", attempts == 1)
        check("recovery: nothing due before backoff expires",
              queue.due(now=time.time()) == [])
        due = queue.due(now=time.time() + 3601)
        check("recovery: event due after backoff",
              len(due) == 1 and due[0]["event_id"] == eid)
        queue.resolve(eid)
        check("recovery: resolved event leaves the pending queue",
              queue.pending_count() == 0)
        tracker = CheckpointTracker(store)
        tracker.record("pump_create", 472184748, ATX_MIGRATE_SIG)
        check("recovery: checkpoint roundtrips slot + signature",
              tracker.last_slot("pump_create") == 472184748
              and tracker.last_signature("pump_create")
              == ATX_MIGRATE_SIG)
    finally:
        store.close()


def test_recovery_reprocess():
    store = temp_store().open()
    try:
        processor = EventProcessor(store)
        queue = RetryQueue(store)
        tx = load_fixture("atx_create.json")
        eid = event_id_for(ATX_CREATE_SIG, ATX_CREATE_SLOT)
        queue.enqueue(eid, ATX_CREATE_SIG, ATX_CREATE_SLOT,
                      "transient decode failure")
        calls = {"n": 0}

        def fetch(event_id, signature, slot):
            calls["n"] += 1
            return {"provider": "alchemy", "kind": "getTransaction",
                    "tx": tx, "commitment": "confirmed"}

        resolved, failing = reprocess_failed(
            processor, fetch, now=time.time() + 3601,
            retry_queue=queue)
        check("recovery: reprocess resolves the failed event",
              resolved == [eid] and failing == [])
        check("recovery: reprocessed event is stored + applied",
              store.get_event(eid) is not None
              and processor.registry.get(ATX_MINT) is not None)
        check("recovery: fetch was called once", calls["n"] == 1)

        # A fetch that keeps failing stays queued.
        eid2 = event_id_for(ATX_BUY_SIG, 472184744)
        queue.enqueue(eid2, ATX_BUY_SIG, 472184744, "still broken")

        def fetch_none(event_id, signature, slot):
            return None

        resolved2, failing2 = reprocess_failed(
            processor, fetch_none, now=time.time() + 7200,
            retry_queue=queue)
        check("recovery: unrecoverable event stays failing",
              resolved2 == [] and failing2 == [eid2])
    finally:
        store.close()


def test_recovery_backfill_gap():
    """Simulated disconnect + reconnect: the acquisition layer's
    gap detector finds the missing range and backfill_signatures
    recovers the signatures; the processor then runs them through
    the normal pipeline. Gap detection is verified with a known
    missing range."""
    store = temp_store().open()
    try:
        processor = EventProcessor(store)
        tx_create = load_fixture("atx_create.json")
        tx_buy = load_fixture("atx_buy.json")
        tx_migrate = load_fixture("atx_migrate.json")
        by_sig = {signature_of(tx_create): tx_create,
                  signature_of(tx_buy): tx_buy,
                  signature_of(tx_migrate): tx_migrate}
        # Live path saw only the create; the disconnect missed the
        # buy + migrate. The fake RPC returns exactly the missed
        # signatures (known missing range: 2 signatures).
        missed = [signature_of(tx_buy), signature_of(tx_migrate)]

        def fake_rpc(method, params):
            if method == "getSignaturesForAddress":
                return {"kind": "ok",
                        "result": [{"signature": s} for s in missed]}
            if method == "getTransaction":
                return {"kind": "ok", "result": by_sig.get(params[0])}
            return {"kind": "error", "error": "unknown method"}

        sigs, outcomes = backfill_after_disconnect(
            processor, "pump_create", "6EF8rrecthR5Dk",
            fake_rpc, disconnected_s=120,
            fetch_transaction=lambda sig: by_sig.get(sig),
            provider="chainstack", commitment="confirmed")
        check("recovery: backfill recovers the known missing range",
              sigs == missed)
        check("recovery: backfilled events process through the pipeline",
              outcomes.get(signature_of(tx_buy)) == "applied"
              and outcomes.get(signature_of(tx_migrate)) == "applied")
        state = processor.registry.get(ATX_MINT)
        check("recovery: backfilled events update token state",
              state is not None and state["migrated"] is True
              and state["trade_count"] == 1)
        check("recovery: backfill stored exactly the missed events",
              store.count_events() == 2)
    finally:
        store.close()


def main():
    test_models()
    test_normalizer_single()
    test_normalizer_merge()
    test_deduplicator()
    test_processor_full_pipeline()
    test_processor_slot_gaps()
    test_recovery_retry_queue()
    test_recovery_reprocess()
    test_recovery_backfill_gap()
    print("\n%d passed, %d failed" % (PASSES[0], len(FAILS)))
    if FAILS:
        print("FAILURES:")
        for name in FAILS:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


if __name__ != "__main__":
    from tests.recorded_checks import recorded_check_guard
