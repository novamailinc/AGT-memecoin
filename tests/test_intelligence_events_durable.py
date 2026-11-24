#!/usr/bin/env python3
"""Durable applied-set tests (Phase 13 finalize, Phase 12 follow-up).

The Phase 12 failure-matrix pass found the `_applied_event_ids`
guard is per processor instance: a processor restarted against an
existing store rebuilds it empty, and reprocessing an
already-applied event would apply it again (double-counting, e.g.
trade_count += 1 twice for one trade). The fix is the store-level
applied_events table (EventStore.mark_applied / is_applied),
consulted by EventProcessor._apply_or_hold alongside the
in-memory guard.

Plain script, run directly with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_intelligence_events_durable.py

Fixtures are the real on-chain transactions under
tests/data/intelligence/ (atx_create / atx_buy). No network, no
live files: every store lives in a temp dir.
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.intelligence.events import (
    EventProcessor,
    EventStore,
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


def load_fixture(name):
    with open(os.path.join(DATA, name)) as f:
        wrap = json.load(f)
    return wrap.get("transaction") or wrap.get("rpc_result") or wrap


def temp_store():
    tmpdir = tempfile.mkdtemp(prefix="intelligence-durable-")
    return EventStore(os.path.join(tmpdir, "intelligence_events.db"))


# --- store-level applied set -------------------------------------------
store = temp_store().open()
try:
    tables = {r[0] for r in sqlite3.connect(store.path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("store: applied_events table exists after open",
          "applied_events" in tables)
    check("store: unknown event is not applied",
          store.is_applied("no-such-event") is False)
    store.mark_applied("evt-1")
    check("store: mark_applied then is_applied is True",
          store.is_applied("evt-1") is True)
    store.mark_applied("evt-1")  # idempotent
    check("store: mark_applied is idempotent (no error, still True)",
          store.is_applied("evt-1") is True)
    check("store: a different event is still not applied",
          store.is_applied("evt-2") is False)
finally:
    store.close()

# --- restart: a fresh processor must not re-apply ------------------------
store = temp_store().open()
try:
    calls = []

    def on_update(event, mint):
        calls.append((event.event_id, mint))

    proc1 = EventProcessor(store, on_market_update=on_update)
    tx = load_fixture("atx_create.json")
    outcome, event_id = proc1.process_transaction(
        "alchemy", tx, commitment="confirmed",
        stream_key="pump_create")
    check("restart: first process applies the create event",
          outcome == "applied")
    check("restart: callback fired exactly once on first apply",
          len(calls) == 1)
    check("restart: store records the event as applied",
          store.is_applied(event_id) is True)
    state1 = proc1.registry.get(ATX_MINT)
    check("restart: first registry has the token",
          state1 is not None and state1["mint"] == ATX_MINT)

    # Simulate a restart: new processor instance, fresh registry,
    # empty in-memory guard, SAME store file.
    calls2 = []

    def on_update2(event, mint):
        calls2.append((event.event_id, mint))

    proc2 = EventProcessor(store, on_market_update=on_update2)
    check("restart: fresh processor starts with an empty registry",
          proc2.registry.get(ATX_MINT) is None)
    outcome2, _ = proc2.reprocess_event(event_id)
    check("restart: reprocess after restart still reports applied",
          outcome2 == "applied")
    check("restart: reprocess after restart does NOT fire the "
          "callback again",
          len(calls2) == 0)
    check("restart: reprocess after restart does NOT rebuild "
          "registry state",
          proc2.registry.get(ATX_MINT) is None)

    # Trade double-count: a buy applied once must not count twice.
    tx_buy = load_fixture("atx_buy.json")
    outcome3, buy_id = proc1.process_transaction(
        "alchemy", tx_buy, commitment="confirmed",
        stream_key="pump_buy")
    check("restart: buy event applies", outcome3 == "applied")
    check("restart: trade_count is 1 after the first apply",
          (proc1.registry.get(ATX_MINT) or {}).get("trade_count") == 1)
    proc3 = EventProcessor(store, on_market_update=on_update2)
    proc3.reprocess_event(buy_id)
    check("restart: reprocessing the buy after restart does not "
          "double-count (registry stays empty, no callback)",
          proc3.registry.get(ATX_MINT) is None and len(calls2) == 0)
    # And the ORIGINAL processor's registry is untouched by the
    # reprocess (its trade_count is still exactly 1).
    check("restart: original registry trade_count still 1",
          (proc1.registry.get(ATX_MINT) or {}).get("trade_count") == 1)
finally:
    store.close()

print("\n%d passed, %d failed" % (PASSES[0], len(FAILS)))
if FAILS:
    for name in FAILS:
        print("  FAILED: %s" % name)
sys.exit(1 if FAILS else 0)
