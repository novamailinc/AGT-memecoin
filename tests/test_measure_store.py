"""Tests for the measurement-ledger durable store (phase 1a).

Covers: atomic write + reload round-trip, 0600 file permissions,
restore merges with per-mint newest-ts-wins, the anti-clobber guard
(never persist an empty ledger over a populated one), and the
~60s write throttle.

CRITICAL: all tests use tmp_path for the store location — the real
/home/hatch/.forex-agent tree is never touched from unit tests.
"""

import json
import os
import stat
import time
import unittest

from daemon.launch_scanner import measure_store


def _rec(ts, tag="r"):
    return {"ts": ts, "tag": tag}


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        measure_store._last_write.clear()

    def test_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "measurement.json")
            now = time.time()
            ledgers = {
                "proof_checks": {"M1": _rec(now, "a"),
                                 "M2": _rec(now - 10, "b")},
                "footprint_signals": {"M1": _rec(now, "c")},
            }
            self.assertTrue(measure_store.save(p, ledgers))
            back = measure_store.load(p)
            self.assertEqual(back["proof_checks"]["M1"], _rec(now, "a"))
            self.assertEqual(back["proof_checks"]["M2"], _rec(now - 10, "b"))
            self.assertEqual(back["footprint_signals"]["M1"],
                             _rec(now, "c"))

    def test_missing_file_loads_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                measure_store.load(os.path.join(d, "nope.json")), {})

    def test_corrupt_file_loads_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "measurement.json")
            with open(p, "w") as fh:
                fh.write("{not json")
            self.assertEqual(measure_store.load(p), {})

    def test_stale_records_pruned_on_load(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "measurement.json")
            now = time.time()
            old = now - 8 * 24 * 3600  # > 7-day TTL
            measure_store.save(p, {"proof_checks": {
                "OLD": _rec(old), "NEW": _rec(now)}})
            back = measure_store.load(p)
            self.assertNotIn("OLD", back["proof_checks"])
            self.assertIn("NEW", back["proof_checks"])


class PermissionsTests(unittest.TestCase):
    def test_written_file_is_0600(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "measurement.json")
            self.assertTrue(measure_store.save(
                p, {"proof_checks": {"M1": _rec(time.time())}}))
            mode = stat.S_IMODE(os.stat(p).st_mode)
            self.assertEqual(mode, 0o600)

    def test_no_temp_file_left_behind(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "measurement.json")
            measure_store.save(p, {"proof_checks": {}})
            leftovers = [f for f in os.listdir(d) if ".tmp." in f]
            self.assertEqual(leftovers, [])

    def test_failed_serialize_leaves_existing_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "measurement.json")
            now = time.time()
            measure_store.save(p, {"proof_checks": {"M1": _rec(now)}})
            # Unserializable payload (a set inside a dict record):
            # save must fail and the existing file stays untouched.
            self.assertFalse(measure_store.save(
                p, {"proof_checks": {"M1": {"ts": now, "bad": {1, 2}}}}))
            back = measure_store.load(p)
            self.assertEqual(back["proof_checks"]["M1"], _rec(now))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        measure_store._last_write.clear()

    def _store(self, d, ledgers):
        p = os.path.join(str(d), "measurement.json")
        self.assertTrue(measure_store.save(p, ledgers))
        return p

    def test_restore_empty_blob(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            p = self._store(d, {"proof_checks": {"M1": _rec(now, "disk")}})
            state = {}
            self.assertTrue(measure_store.restore(state, p))
            self.assertEqual(state["proof_checks"]["M1"], _rec(now, "disk"))

    def test_restore_newest_ts_wins_per_mint(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            # Blob is missing the key entirely -> full restore path, but
            # also exercise merge_newest directly for the overlap case.
            p = self._store(d, {"proof_checks": {"M1": _rec(now, "disk")}})
            blob = {"M1": _rec(now - 100, "stale")}
            merged = measure_store.merge_newest(blob,
                                                {"M1": _rec(now, "disk")})
            self.assertEqual(merged["M1"]["tag"], "disk")
            merged2 = measure_store.merge_newest(
                {"M1": _rec(now, "blob")}, {"M1": _rec(now - 100, "disk")})
            self.assertEqual(merged2["M1"]["tag"], "blob")
            # restore() itself only fires on missing/empty blob ledgers:
            state = {"proof_checks": {}}
            self.assertTrue(measure_store.restore(state, p))
            self.assertEqual(state["proof_checks"]["M1"]["tag"], "disk")

    def test_restore_leaves_populated_blob_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            p = self._store(d, {"proof_checks": {"M1": _rec(now, "disk")}})
            state = {"proof_checks": {"M9": _rec(now, "live")}}
            self.assertFalse(measure_store.restore(state, p))
            self.assertEqual(list(state["proof_checks"]), ["M9"])

    def test_restore_empty_store_noop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "measurement.json")
            state = {}
            self.assertFalse(measure_store.restore(state, p))
            self.assertEqual(state, {})


class AntiClobberTests(unittest.TestCase):
    def setUp(self):
        measure_store._last_write.clear()

    def test_empty_in_memory_never_wipes_populated_store(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(str(d), "measurement.json")
            now = time.time()
            measure_store.save(p, {"proof_checks": {"M1": _rec(now)}})
            # Simulate the wipe: in-memory ledgers come back empty.
            state = {"proof_checks": {}, "footprint_signals": {}}
            measure_store.maybe_persist(state, p, now=now + 61)
            back = measure_store.load(p)
            self.assertIn("M1", back["proof_checks"])

    def test_guard_refills_outgoing_blob(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(str(d), "measurement.json")
            now = time.time()
            measure_store.save(p, {"proof_checks": {"M1": _rec(now)}})
            # The pre-save guard: empty blob + populated store ->
            # the outgoing blob carries the records again.
            outgoing = {}
            measure_store.restore(outgoing, p)
            self.assertIn("M1", outgoing["proof_checks"])


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        measure_store._last_write.clear()

    def test_rapid_calls_write_at_most_once_per_gap(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(str(d), "measurement.json")
            now = time.time()
            state = {"proof_checks": {"M1": _rec(now)}}
            writes = [measure_store.maybe_persist(state, p, now=now + i)
                      for i in range(10)]
            self.assertEqual(sum(writes), 1)
            # After the gap, a changed ledger writes again.
            state["proof_checks"]["M2"] = _rec(now + 61)
            self.assertTrue(
                measure_store.maybe_persist(state, p, now=now + 61))
            back = measure_store.load(p)
            self.assertIn("M2", back["proof_checks"])

    def test_unchanged_content_not_rewritten(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(str(d), "measurement.json")
            now = time.time()
            state = {"proof_checks": {"M1": _rec(now)}}
            self.assertTrue(measure_store.maybe_persist(state, p, now=now))
            mtime = os.stat(p).st_mtime
            time.sleep(0.02)
            # Gap elapsed but content identical -> no rewrite.
            self.assertFalse(
                measure_store.maybe_persist(state, p, now=now + 61))
            self.assertEqual(os.stat(p).st_mtime, mtime)


if __name__ == "__main__":
    unittest.main()
