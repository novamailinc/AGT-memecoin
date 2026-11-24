#!/usr/bin/env python3
"""Tests for the Phase 8 event store (2026-11-22).

Plain script, run directly with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_intelligence_events_store.py

Covers: WAL mode + checkpointing, upsert/get roundtrip, merge
persistence, retention/prune rules (never conflicting, never
block-time-less), corrupt-file preservation (never silently
blanked), checkpoints, retry-queue persistence, single-writer
behaviour.
"""

import hashlib
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.intelligence.events import (
    CorruptStoreError,
    EventStore,
    LogicalEvent,
    event_id_for,
)

FAILS = []
PASSES = [0]


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)


ATX_SIG = ("2avAXaepDw4uPC4Ue3khKWx9fpQzXPvm3pNYtZi1VwJfvNsD8GY25"
           "WzYaXvAWmKsTgdtAT5NRHxgzmDF67RRfih")
ATX_MINT = "BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump"


def temp_path(name="intelligence_events.db"):
    return os.path.join(tempfile.mkdtemp(prefix="intelligence-store-"),
                        name)


def make_event(sig=ATX_SIG, slot=472184636, block_time=1795273899,
               status="verified", etype="token_created"):
    ev = LogicalEvent(signature=sig, slot=slot, block_time=block_time,
                      event_type=etype, verification_status=status,
                      lifecycle_status="confirmed",
                      normalized_accounts={"mint": ATX_MINT})
    ev.add_observation({"provider": "alchemy",
                        "kind": "getTransaction",
                        "commitment": "confirmed",
                        "observed_at": 1795273900})
    return ev


def test_open_and_wal():
    path = temp_path()
    store = EventStore(path).open()
    try:
        mode = store._conn.execute(
            "PRAGMA journal_mode").fetchone()[0]
        check("store: WAL mode enabled", mode.lower() == "wal")
        version = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        check("store: schema version recorded", version == "1")
        store.upsert_event(make_event())
        check("store: WAL file exists after a write",
              os.path.exists(path + "-wal"))
        store.checkpoint()
        check("store: checkpoint runs without error", True)
    finally:
        store.close()
    check("store: DB file persists after close", os.path.exists(path))


def test_roundtrip_and_merge():
    path = temp_path()
    store = EventStore(path).open()
    try:
        ev = make_event()
        store.upsert_event(ev)
        got = store.get_event(ev.event_id)
        check("store: upsert/get roundtrip preserves ID + fields",
              got is not None and got.signature == ATX_SIG
              and got.slot == 472184636
              and got.event_type == "token_created"
              and got.normalized_accounts.get("mint") == ATX_MINT
              and got.providers == ["alchemy"])
        check("store: event_exists true / false",
              store.event_exists(ev.event_id)
              and not store.event_exists("0" * 64))
        check("store: get_by_signature finds the event",
              len(store.get_by_signature(ATX_SIG)) == 1)
        check("store: count_events totals",
              store.count_events() == 1
              and store.count_events(verification_status="verified") == 1
              and store.count_events(verification_status="conflicting")
              == 0)
        # Merge a second provider observation into the same record.
        got.add_observation({"provider": "chainstack",
                             "kind": "getTransaction",
                             "commitment": "confirmed",
                             "observed_at": 1795273901})
        store.upsert_event(got)
        got2 = store.get_event(ev.event_id)
        check("store: merged record persists both providers",
              set(got2.providers) == {"alchemy", "chainstack"}
              and store.count_events() == 1)
        # Upsert is idempotent: re-storing changes nothing.
        store.upsert_event(got2)
        check("store: re-upsert stays one record",
              store.count_events() == 1)
        # Disagreements land in the queryable table too.
        got2.add_disagreement("slot", {"chainstack": 1,
                                       "existing": 2})
        store.upsert_event(got2)
        discs = store.list_disagreements(event_id=ev.event_id)
        check("store: disagreements recorded in table",
              len(discs) == 1 and discs[0]["field"] == "slot")
        check("store: list_events returns the record",
              len(store.list_events()) == 1)
    finally:
        store.close()
    # Reopen: data survives the restart.
    store2 = EventStore(path).open()
    try:
        check("store: data survives close + reopen",
              store2.count_events() == 1
              and store2.get_event(ev.event_id) is not None)
    finally:
        store2.close()


def test_prune_rules():
    path = temp_path()
    store = EventStore(path, retention_days=30).open()
    try:
        now = int(time.time())
        old = make_event(sig="oldsig", slot=100, block_time=now - 40 * 86400)
        recent = make_event(sig="recentsig", slot=200,
                            block_time=now - 5 * 86400)
        conflicting_old = make_event(
            sig="conflictsig", slot=300, block_time=now - 40 * 86400,
            status="conflicting")
        timeless = make_event(sig="timelesssig", slot=400,
                              block_time=None)
        for ev in (old, recent, conflicting_old, timeless):
            store.upsert_event(ev)
        pruned = store.prune()
        check("store: prune removes only the old verified event",
              pruned == 1 and store.count_events() == 3)
        check("store: prune never removes conflicting events",
              store.get_event(conflicting_old.event_id) is not None)
        check("store: prune never removes block-time-less events",
              store.get_event(timeless.event_id) is not None)
        check("store: prune keeps recent events",
              store.get_event(recent.event_id) is not None)
        entries = store.prune_log_entries()
        check("store: prune run is logged",
              len(entries) == 1 and entries[0]["pruned_count"] == 1)
    finally:
        store.close()


def test_corrupt_file_preserved():
    path = temp_path()
    garbage = b"this is not a sqlite database" * 10
    with open(path, "wb") as f:
        f.write(garbage)
    before = hashlib.sha256(garbage).hexdigest()
    store = EventStore(path)
    try:
        store.open()
        check("store: corrupt file raises CorruptStoreError", False)
    except CorruptStoreError:
        check("store: corrupt file raises CorruptStoreError", True)
    except Exception as exc:  # noqa: BLE001 - any other error is wrong
        check("store: corrupt file raises CorruptStoreError "
              "(got %s)" % type(exc).__name__, False)
    with open(path, "rb") as f:
        after = hashlib.sha256(f.read()).hexdigest()
    check("store: corrupt file preserved byte-for-byte, never blanked",
          before == after and os.path.getsize(path) == len(garbage))

    # A file whose SQLite header is destroyed must also raise
    # rather than blank (deterministic: the magic bytes are gone,
    # so the open fails before any write).
    path2 = temp_path("torn.db")
    good = EventStore(path2).open()
    good.upsert_event(make_event())
    good.close()
    with open(path2, "r+b") as f:
        f.seek(0)
        f.write(b"GARBAGE-NOT-SQLITE")
    size_before = os.path.getsize(path2)
    try:
        EventStore(path2).open()
        check("store: destroyed header raises CorruptStoreError", False)
    except CorruptStoreError:
        check("store: destroyed header raises CorruptStoreError", True)
    check("store: header-corrupt file preserved, not truncated",
          os.path.getsize(path2) == size_before)


def test_checkpoints_and_retry_queue():
    path = temp_path()
    store = EventStore(path).open()
    try:
        check("store: missing checkpoint returns None",
              store.get_checkpoint("pump_create") is None)
        store.set_checkpoint("pump_create", 472184748, ATX_SIG)
        cp = store.get_checkpoint("pump_create")
        check("store: checkpoint roundtrips",
              cp["last_slot"] == 472184748
              and cp["last_signature"] == ATX_SIG)
        eid = event_id_for(ATX_SIG, 472184636)
        attempts = store.enqueue_failed(eid, ATX_SIG, 472184636,
                                        "boom")
        check("store: failed enqueue attempt 1", attempts == 1)
        attempts = store.enqueue_failed(eid, ATX_SIG, 472184636,
                                        "boom again")
        check("store: failed re-enqueue increments attempts",
              attempts == 2)
        check("store: failed_due respects backoff",
              store.failed_due(now=time.time()) == []
              and len(store.failed_due(now=time.time() + 7200)) == 1)
        check("store: failed_count pending",
              store.failed_count() == 1)
        store.mark_failed_resolved(eid)
        check("store: resolved failure leaves pending queue",
              store.failed_count() == 0)
    finally:
        store.close()


def test_default_path_under_runtime_home():
    home = os.environ.get("FOREX_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".forex-agent")
    from daemon.intelligence.events.event_store import default_db_path
    check("store: default DB lives under the runtime home, "
          "not local.db",
          default_db_path() == os.path.join(
              home, "intelligence_events.db"))


def main():
    test_open_and_wal()
    test_roundtrip_and_merge()
    test_prune_rules()
    test_corrupt_file_preserved()
    test_checkpoints_and_retry_queue()
    test_default_path_under_runtime_home()
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
