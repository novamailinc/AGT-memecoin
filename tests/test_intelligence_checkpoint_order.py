"""Checkpoint ordering in EventProcessor and fail-closed checkpoint file.

pytest mode, builtin fixtures only. Fixtures are the recorded atx
transactions; stores live in tmp_path. No network.
"""
import json
import os

import pytest

from daemon.intelligence.acquisition.event_backfill import (
    BackfillCheckpointStore, CorruptCheckpointStoreError, CHECKPOINT_FILE)
from daemon.intelligence.events import EventProcessor, EventStore

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "data", "intelligence")


def _tx(name):
    with open(os.path.join(DATA, name)) as fh:
        wrap = json.load(fh)
    return wrap.get("transaction") or wrap


def _store(tmp_path):
    store = EventStore(path=str(tmp_path / "events.db"))
    store.open()
    return store


def test_checkpoint_set_only_after_apply_is_durable(tmp_path):
    store = _store(tmp_path)
    seen = []
    real = store.set_checkpoint

    def spy(key, slot, sig=None):
        ev = store.get_by_signature(sig)
        seen.append(all(store.is_applied(e.event_id) for e in ev) and
                    bool(ev))
        return real(key, slot, sig)
    store.set_checkpoint = spy
    proc = EventProcessor(store)
    outcome, _ = proc.process_transaction(
        "alchemy", _tx("atx_create.json"), commitment="finalized",
        stream_key="s")
    assert outcome == "applied"
    assert seen == [True]
    assert store.get_checkpoint("s")["last_slot"] == 472184636
    store.close()


def test_failed_durable_mark_leaves_checkpoint_behind(tmp_path):
    store = _store(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    store.mark_applied = boom
    proc = EventProcessor(store)
    proc.process_transaction("alchemy", _tx("atx_create.json"),
                             commitment="finalized", stream_key="s")
    # The apply step swallows the failed mark, so the outcome alone is
    # not proof: the checkpoint must not move past it.
    assert store.get_checkpoint("s") is None
    store.close()


def test_crash_in_apply_leaves_checkpoint_behind(tmp_path):
    store = _store(tmp_path)
    proc = EventProcessor(store)

    def crash(event, mint):
        raise KeyboardInterrupt()
    proc.registry.update = crash
    with pytest.raises(KeyboardInterrupt):
        proc.process_transaction("alchemy", _tx("atx_create.json"),
                                 commitment="finalized", stream_key="s")
    assert store.get_checkpoint("s") is None
    # The event was stored before the crash; recovery applies it once.
    stored = store.list_events(limit=10)
    assert len(stored) == 1 and not store.is_applied(stored[0].event_id)
    proc2 = EventProcessor(store)
    outcome, _ = proc2.reprocess_event(stored[0].event_id)
    assert outcome == "applied"
    assert store.is_applied(stored[0].event_id)
    store.close()


def test_held_event_is_stored_then_checkpointed(tmp_path):
    store = _store(tmp_path)
    proc = EventProcessor(store)
    outcome, eid = proc.process_observation(
        {"provider": "public", "kind": "getTransaction",
         "tx": _tx("atx_sell.json"), "commitment": "processed"},
        stream_key="s")
    assert outcome == "held"
    assert store.get_event(eid) is not None
    assert store.get_checkpoint("s") is not None
    store.close()


def test_missing_checkpoint_file_is_empty(tmp_path):
    cs = BackfillCheckpointStore(home=str(tmp_path))
    assert cs.get_checkpoint("k") is None
    assert cs.unrecovered_gaps() == []


@pytest.mark.parametrize("content", [
    "{not json", "", "[]", '{"checkpoints": [], "unrecovered_gaps": []}',
    '{"checkpoints": {}, "unrecovered_gaps": {}}'])
def test_corrupt_checkpoint_file_raises_and_is_preserved(tmp_path, content):
    path = tmp_path / CHECKPOINT_FILE
    path.write_text(content)
    with pytest.raises(CorruptCheckpointStoreError):
        BackfillCheckpointStore(home=str(tmp_path))
    assert path.read_text() == content


def test_valid_checkpoint_file_roundtrips(tmp_path):
    cs = BackfillCheckpointStore(home=str(tmp_path))
    cs.set_checkpoint("k", 5, "sig5")
    cs.record_unrecovered_gap("k", 6, 9, "x")
    cs2 = BackfillCheckpointStore(home=str(tmp_path))
    assert cs2.get_checkpoint("k")["last_signature"] == "sig5"
    assert len(cs2.unrecovered_gaps("k")) == 1
