"""Durable milestone-watch backstop (watch_store.py).

Regression test for the 2026-10-09 verified loss: run_once() reloads the
daemon state blob from the journal every cycle while the full save is
throttled to 300s, so watch records added between saves were dropped on
the next reload (3 mints tracked 21:42-21:48 absent from the 22:41 save).
The slow-cooker reads this watch — the loss is why it never evaluated
QUBIT.

No network, no real runtime tree: every test passes an explicit tmp path
(the module never resolves the default path under FOREX_AGENT_TESTING).
"""

import json
import os
import stat
import tempfile
import time
import unittest

from daemon.launch_scanner import watch_store


def _rec(mint, card_ts=None, **kw):
    r = {"symbol": mint[:6], "chain": "solana", "anchor": 20000.0,
         "card_ts": time.time() if card_ts is None else card_ts,
         "hit": [], "source": "launch.early_digest"}
    r.update(kw)
    return r


class WatchStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "watch.json")

    def test_persist_restore_roundtrip(self):
        state = {"milestone_watch": {"M1": _rec("M1"), "M2": _rec("M2")}}
        self.assertTrue(watch_store.persist(state, self.path))
        # Permissions: private runtime data, 0600 like the other moat files.
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        # Fresh state, blob lost everything -> restore brings it back.
        state2 = {"milestone_watch": {}}
        self.assertTrue(watch_store.restore(state2, self.path))
        self.assertEqual(set(state2["milestone_watch"].keys()),
                         {"M1", "M2"})

    def test_qubit_scenario_restart_mid_window(self):
        # The verified loss, replayed: record tracked at 22:13, journal
        # blob reloaded at 22:14 WITHOUT it (save had not run yet), disk
        # backstop has it -> the slow-cooker window still sees it.
        tracked = {"QUBITMINT": _rec("QUBITMINT", card_ts=time.time())}
        self.assertTrue(
            watch_store.persist({"milestone_watch": tracked}, self.path))
        reloaded_blob_state = {"milestone_watch": {}}  # reload lost it
        self.assertTrue(
            watch_store.restore(reloaded_blob_state, self.path))
        self.assertIn("QUBITMINT",
                      reloaded_blob_state["milestone_watch"])

    def test_restore_merges_newest_wins(self):
        now = time.time()
        old = _rec("M1", card_ts=now - 200.0, anchor=10000.0)
        new = _rec("M1", card_ts=now - 100.0, anchor=20000.0)
        self.assertTrue(
            watch_store.persist({"milestone_watch": {"M1": new}},
                                self.path))
        state = {"milestone_watch": {"M1": old}}
        # Disk is newer -> disk wins.
        self.assertTrue(watch_store.restore(state, self.path))
        self.assertEqual(state["milestone_watch"]["M1"]["anchor"], 20000.0)
        # Memory is newer -> memory wins, disk never clobbers live data.
        self.assertTrue(
            watch_store.persist({"milestone_watch": {"M1": old}},
                                self.path))
        state2 = {"milestone_watch": {"M1": new}}
        self.assertFalse(watch_store.restore(state2, self.path))
        self.assertEqual(state2["milestone_watch"]["M1"]["anchor"],
                         20000.0)

    def test_expired_records_never_resurrected(self):
        stale = _rec("OLD", card_ts=time.time() - 25 * 3600)
        self.assertTrue(
            watch_store.persist({"milestone_watch": {"OLD": stale}},
                                self.path))
        state = {"milestone_watch": {}}
        self.assertFalse(watch_store.restore(state, self.path))
        self.assertNotIn("OLD", state["milestone_watch"])

    def test_restore_no_file_is_quiet(self):
        state = {"milestone_watch": {"M1": _rec("M1")}}
        self.assertFalse(watch_store.restore(
            state, os.path.join(self.tmp.name, "missing.json")))
        self.assertIn("M1", state["milestone_watch"])

    def test_membership_helper(self):
        self.assertEqual(
            watch_store.membership({"milestone_watch": {"A": {}, "B": {}}}),
            {"A", "B"})
        self.assertEqual(watch_store.membership({}), set())
        self.assertEqual(watch_store.membership(None), set())

    def test_never_raises(self):
        # Garbage in -> False, never an exception (cycle safety).
        self.assertFalse(watch_store.persist(None, self.path))
        self.assertFalse(watch_store.restore(None, self.path))
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        self.assertEqual(watch_store.load(bad), {})
        state = {"milestone_watch": {}}
        self.assertFalse(watch_store.restore(state, bad))


if __name__ == "__main__":
    unittest.main()
