"""Commitment-aware observation dedup and held->verified promotion.

pytest mode, builtin fixtures only. WRITTEN, NOT RUN (source-only until a
bounded clearance). Fixtures are the recorded atx transactions; stores live in
tmp_path; no network. Expectations come from event_models / event_normalizer /
event_processor source, not from observed runs.
"""
import copy
import json
import os

from daemon.intelligence.events import EventProcessor, EventStore, LogicalEvent
from daemon.intelligence.events.event_models import (
    VERIFICATION_CONFLICTING, VERIFICATION_VERIFIED)

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "data", "intelligence")
MINT = "BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump"
P = "alchemy"


def _tx(name):
    with open(os.path.join(DATA, name)) as fh:
        wrap = json.load(fh)
    return wrap.get("transaction") or wrap.get("rpc_result") or wrap


def _proc(tmp_path):
    store = EventStore(path=str(tmp_path / "events.db"))
    store.open()
    calls = []
    proc = EventProcessor(store, on_market_update=lambda e, m: calls.append(m))
    return proc, store, calls


def _feed(proc, tx, commitment, provider=P):
    return proc.process_transaction(provider, tx, commitment=commitment)


def _the_event(store, tx):
    sig = tx["transaction"]["signatures"][0]
    evs = store.get_by_signature(sig)
    assert len(evs) == 1, "one signature must stay one logical record"
    return evs[0]


# ---- model level ---------------------------------------------------------
def test_dedup_key_is_provider_kind_and_commitment():
    ev = LogicalEvent(signature="s" * 64, slot=1)
    base = {"provider": P, "kind": "getTransaction"}
    assert ev.add_observation(dict(base, commitment="processed"))
    assert not ev.add_observation(dict(base, commitment="processed"))
    assert ev.add_observation(dict(base, commitment="confirmed"))
    assert ev.add_observation(dict(base, commitment="finalized"))
    assert not ev.add_observation(dict(base, commitment="finalized"))
    assert len(ev.observations) == 3
    # another provider or kind is still its own record
    assert ev.add_observation({"provider": "helius", "kind": "getTransaction",
                               "commitment": "finalized"})
    assert ev.add_observation(dict(base, kind="parsed", commitment="finalized"))


def test_missing_null_and_unknown_commitment_share_one_bounded_bucket():
    ev = LogicalEvent(signature="s" * 64, slot=1)
    base = {"provider": P, "kind": "getTransaction"}
    assert ev.add_observation(dict(base))                       # legacy: no key
    assert not ev.add_observation(dict(base, commitment=None))
    assert not ev.add_observation(dict(base, commitment="bogus"))
    assert not ev.add_observation(dict(base, commitment=["finalized"]))
    assert len(ev.observations) == 1
    # a known commitment is new information against the legacy record
    assert ev.add_observation(dict(base, commitment="finalized"))


def test_serialised_roundtrip_keeps_dedup_behaviour():
    ev = LogicalEvent(signature="s" * 64, slot=1)
    base = {"provider": P, "kind": "getTransaction"}
    ev.add_observation(dict(base, commitment="processed"))
    ev.add_observation(dict(base, commitment="finalized"))
    back = LogicalEvent.from_dict(json.loads(json.dumps(ev.to_dict())))
    assert not back.add_observation(dict(base, commitment="finalized"))
    assert not back.add_observation(dict(base, commitment="processed"))
    assert back.add_observation(dict(base, commitment="confirmed"))


def test_verdict_counts_only_at_its_own_confirmed_commitment():
    ev = LogicalEvent(signature="s" * 64, slot=1)
    base = {"provider": P, "kind": "getTransaction",
            "decoder_validation": "verified"}
    ev.add_observation(dict(base, commitment="processed"))
    assert not ev.has_confirmed_verified_observation()
    ev.add_observation({"provider": "helius", "kind": "getTransaction",
                        "commitment": "finalized",
                        "decoder_validation": "unverified"})
    assert not ev.has_confirmed_verified_observation()
    legacy = LogicalEvent(signature="t" * 64, slot=1)
    legacy.add_observation({"provider": P, "kind": "getTransaction",
                            "decoder_validation": "verified"})
    assert not legacy.has_confirmed_verified_observation()
    ev.add_observation({"provider": "chainstack", "kind": "getTransaction",
                        "commitment": "confirmed",
                        "decoder_validation": "verified"})
    assert ev.has_confirmed_verified_observation()


# ---- processor level -----------------------------------------------------
def test_same_provider_finalized_promotes_held_event_and_applies_once(tmp_path):
    proc, store, calls = _proc(tmp_path)
    tx = _tx("atx_create.json")
    out, eid = _feed(proc, tx, "processed")
    assert out == "held" and not store.is_applied(eid) and calls == []
    out2, eid2 = _feed(proc, tx, "finalized")
    assert out2 == "applied" and eid2 == eid
    ev = _the_event(store, tx)
    assert ev.verification_status == VERIFICATION_VERIFIED
    assert ev.lifecycle_status == "finalized" and ev.commitment == "finalized"
    assert store.is_applied(eid) and len(calls) == 1
    assert sorted(o["commitment"] for o in ev.observations) == [
        "finalized", "processed"]
    # replay of either commitment is a duplicate: no re-apply, no new record
    assert _feed(proc, tx, "finalized")[0] == "duplicate"
    assert _feed(proc, tx, "processed")[0] == "duplicate"
    assert len(calls) == 1 and store.count_events() == 1


def test_promotion_survives_processor_restart_applied_exactly_once(tmp_path):
    proc, store, calls = _proc(tmp_path)
    tx = _tx("atx_create.json")
    _feed(proc, tx, "processed")
    _feed(proc, tx, "finalized")
    assert len(calls) == 1
    proc2 = EventProcessor(store, on_market_update=lambda e, m: calls.append(m))
    _feed(proc2, tx, "confirmed")        # new commitment for this provider
    assert len(calls) == 1               # durable applied mark prevents re-apply


def test_late_weaker_observation_does_not_downgrade_or_reapply(tmp_path):
    proc, store, calls = _proc(tmp_path)
    create = _tx("atx_create.json")
    buy = _tx("atx_buy.json")
    _feed(proc, create, "finalized")
    _feed(proc, buy, "finalized")
    assert proc.registry.get(MINT)["trade_count"] == 1
    out, _ = _feed(proc, buy, "processed")     # late, weaker, same provider
    ev = _the_event(store, buy)
    assert out == "applied"                    # merge path, already applied
    assert ev.lifecycle_status == "finalized"
    assert ev.verification_status == VERIFICATION_VERIFIED
    assert proc.registry.get(MINT)["trade_count"] == 1
    assert store.is_applied(ev.event_id)


def test_finalized_failure_does_not_borrow_earlier_verified_verdict(tmp_path):
    # processed (decodes verified, but held) then the same provider reports the
    # tx FAILED on chain at finalized: must not become verified / applied.
    proc, store, calls = _proc(tmp_path)
    tx = _tx("atx_create.json")
    failed = copy.deepcopy(tx)
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    _feed(proc, tx, "processed")
    out, eid = _feed(proc, failed, "finalized")
    ev = _the_event(store, tx)
    assert out == "held" and not store.is_applied(eid) and calls == []
    assert ev.verification_status != VERIFICATION_VERIFIED
    assert any(d["field"] == "execution_status" for d in ev.disagreements)


def test_finalized_failure_after_confirmed_is_a_disagreement_not_silent(tmp_path):
    proc, store, calls = _proc(tmp_path)
    tx = _tx("atx_create.json")
    failed = copy.deepcopy(tx)
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    out0, eid = _feed(proc, tx, "confirmed")
    assert out0 == "applied" and len(calls) == 1
    out, _ = _feed(proc, failed, "finalized")
    ev = _the_event(store, tx)
    assert out == "held"
    assert ev.verification_status == VERIFICATION_CONFLICTING
    assert any(d["field"] == "execution_status" for d in ev.disagreements)
    assert len(calls) == 1   # earlier apply cannot be undone here; no second one


def test_slot_disagreement_at_new_commitment_stays_held(tmp_path):
    proc, store, calls = _proc(tmp_path)
    tx = _tx("atx_create.json")
    moved = copy.deepcopy(tx)
    moved["slot"] = tx["slot"] + 1
    _feed(proc, tx, "processed")
    out, _ = _feed(proc, moved, "finalized")
    ev = _the_event(store, tx)
    assert out == "held" and calls == []
    assert ev.verification_status == VERIFICATION_CONFLICTING
    assert any(d["field"] == "slot" for d in ev.disagreements)
