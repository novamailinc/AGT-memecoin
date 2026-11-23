"""Offline fixture tests for reserve_monitor (6.2 depth): durability,
honest availability, abnormal-movement and migration detection.
No network, no providers - the monitor ingests caller-supplied
snapshots only."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.launch_scanner import reserve_monitor as rm  # noqa: E402

POOL_A = "PoolAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
POOL_B = "PoolBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
MINT = "MintMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMMM"


class ReserveMonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="resmon_")
        self.home = self.tmp
        self.t0 = time.time() - 1000.0

    def _snap(self, n, quote=100.0, pool=POOL_A, mint=MINT, base=10.0,
              step=10.0, **kw):
        for i in range(n):
            self.assertTrue(rm.record_snapshot(
                pool, mint, self.t0 + i * step, base, quote,
                home=self.home, **kw))

    def test_record_and_read_roundtrip_ts_ordered(self):
        self._snap(3)
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertEqual(hist["status"], "ok")
        self.assertEqual(len(hist["samples"]), 3)
        ts = [s["ts"] for s in hist["samples"]]
        self.assertEqual(ts, sorted(ts))

    def test_invalid_inputs_rejected_counted_never_raised(self):
        now = time.time()
        bad = [
            ("", MINT, now, 1.0, 1.0),                 # empty pool
            (POOL_A, "", now, 1.0, 1.0),               # empty mint
            (POOL_A, MINT, True, 1.0, 1.0),            # bool ts
            (POOL_A, MINT, float("nan"), 1.0, 1.0),    # NaN ts
            (POOL_A, MINT, now + 99999.0, 1.0, 1.0),   # absurd future
            (POOL_A, MINT, now, -1.0, 1.0),            # negative reserve
            (POOL_A, MINT, now, 1.0, float("inf")),    # inf quote
            (POOL_A, MINT, now, False, 1.0),           # bool base
        ]
        for args in bad:
            self.assertFalse(rm.record_snapshot(*args, home=self.home))
        st = rm.monitor_status(home=self.home)
        self.assertEqual(st["counters"].get("snapshots_rejected_invalid"),
                         len(bad))
        self.assertFalse(rm._file(self.home).exists())

    def test_missing_file_is_unknown_missing_not_empty(self):
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertEqual(hist["status"], "unknown_missing")
        abn = rm.detect_abnormal(POOL_A, home=self.home)
        self.assertEqual(abn["status"], "unknown_missing")
        mig = rm.detect_migration(MINT, home=self.home)
        self.assertEqual(mig["status"], "unknown_missing")

    def test_insufficient_history_never_fabricates_baseline(self):
        self._snap(3)  # < min_baseline + 1
        out = rm.detect_abnormal(POOL_A, home=self.home)
        self.assertEqual(out["status"], "insufficient_history")
        self.assertIsNone(out["movement"])

    def test_drain_detected_with_evidence(self):
        self._snap(6, quote=100.0)
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 600.0, 10.0, 20.0, home=self.home))
        out = rm.detect_abnormal(POOL_A, home=self.home)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["movement"]["kind"], "drain")
        self.assertAlmostEqual(out["movement"]["ratio"], 0.2)
        self.assertEqual(out["movement"]["baseline_samples"], 6)

    def test_spike_detected(self):
        self._snap(6, quote=100.0)
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 600.0, 10.0, 250.0, home=self.home))
        out = rm.detect_abnormal(POOL_A, home=self.home)
        self.assertEqual(out["movement"]["kind"], "spike")

    def test_normal_latest_reports_no_movement(self):
        self._snap(7, quote=100.0)
        out = rm.detect_abnormal(POOL_A, home=self.home)
        self.assertEqual(out["status"], "ok")
        self.assertIsNone(out["movement"])

    def test_zero_median_baseline_is_unusable_not_evidence(self):
        self._snap(6, quote=0.0)
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 600.0, 10.0, 50.0, home=self.home))
        out = rm.detect_abnormal(POOL_A, home=self.home)
        self.assertEqual(out["status"], "baseline_unusable")
        self.assertIsNone(out["movement"])

    def test_migration_explicit_marker(self):
        self._snap(3, pool=POOL_A)
        self._snap(2, pool=POOL_B, step=10.0,
                   migration_event="pumpswap_graduation")
        out = rm.detect_migration(MINT, home=self.home)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(out["transitions"]), 1)
        tr = out["transitions"][0]
        self.assertEqual(tr["from_pool"], POOL_A)
        self.assertEqual(tr["to_pool"], POOL_B)
        self.assertEqual(tr["provenance"], "explicit")

    def test_migration_inferred_from_reserves(self):
        # Old pool drains to near zero as the new pool appears.
        self._snap(3, quote=100.0, pool=POOL_A)
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 500.0, 10.0, 2.0, home=self.home))
        self._snap(2, pool=POOL_B, step=10.0)
        # POOL_B first seen at t0 (before drain ts) - reorder: give B
        # later ts so appearance order is A then B.
        out = rm.detect_migration(MINT, home=self.home)
        tr = out["transitions"][0]
        self.assertEqual(tr["provenance"], "inferred_from_reserves")
        self.assertTrue(tr["old_pool_drained"])
        self.assertFalse(tr["conflict_both_active"])

    def test_migration_unrecorded_and_conflict_labeled(self):
        # Pool set changes with no marker and old pool stays active.
        self._snap(3, quote=100.0, pool=POOL_A)
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 500.0, 10.0, 95.0, home=self.home))
        self._snap(2, pool=POOL_B, step=10.0)
        out = rm.detect_migration(MINT, home=self.home)
        tr = out["transitions"][0]
        self.assertEqual(tr["provenance"], "unrecorded")
        self.assertTrue(tr["conflict_both_active"])

    def test_no_history_for_mint_is_no_history(self):
        self._snap(3, pool=POOL_A, mint="OtherMintXXXXXXXXXXXXXXXXXXXXX")
        out = rm.detect_migration(MINT, home=self.home)
        self.assertEqual(out["status"], "no_history")

    def test_torn_tail_repaired_and_counted(self):
        self._snap(2)
        with open(rm._file(self.home), "a") as f:
            f.write('{"v": 1, "record_type": "reserve_snap')  # torn
        self._snap(1)
        st = rm.monitor_status(home=self.home)
        self.assertEqual(st["counters"].get("torn_tail_repairs"), 1)
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertEqual(len(hist["samples"]), 3)

    def test_unreadable_lines_counted_not_poisoning(self):
        self._snap(2)
        with open(rm._file(self.home), "a") as f:
            f.write("not json\n")
            f.write(json.dumps({"record_type": "reserve_snapshot",
                                "pool": 123, "ts": 1.0}) + "\n")
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertEqual(hist["status"], "ok")
        self.assertEqual(len(hist["samples"]), 2)
        self.assertEqual(hist["unreadable"], 2)

    def test_out_of_order_appends_labeled_detection_uses_ts(self):
        self._snap(3, quote=100.0)
        # Append an OLDER sample after newer ones.
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 - 500.0, 10.0, 100.0, home=self.home))
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertEqual(hist["out_of_order_appends"], 1)
        ts = [s["ts"] for s in hist["samples"]]
        self.assertEqual(ts, sorted(ts))

    def test_concurrent_appends_never_interleave(self):
        errors = []

        def writer(tag):
            try:
                for i in range(25):
                    rm.record_snapshot(POOL_A, MINT, self.t0 + i,
                                       1.0, 10.0, source=tag,
                                       home=self.home)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,))
                   for t in ("a", "b", "c", "d")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(rm._file(self.home)) as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 100)
        for line in lines:
            json.loads(line)  # every line intact, never glued

    def test_compaction_bounds_samples_and_labels_drops(self):
        orig_bytes = rm._MAX_FILE_BYTES
        orig_cap = rm._MAX_SAMPLES_PER_POOL
        rm._MAX_FILE_BYTES = 400
        rm._MAX_SAMPLES_PER_POOL = 10
        try:
            self._snap(40)
        finally:
            rm._MAX_FILE_BYTES = orig_bytes
            rm._MAX_SAMPLES_PER_POOL = orig_cap
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertLessEqual(len(hist["samples"]), 10)
        # Newest samples survive.
        self.assertEqual(hist["samples"][-1]["ts"], self.t0 + 39 * 10.0)
        st = rm.monitor_status(home=self.home)
        self.assertGreaterEqual(
            st["counters"].get("samples_dropped_by_cap", 0), 1)
        self.assertGreaterEqual(st["counters"].get("compactions", 0), 1)

    def test_status_distinguishes_missing_meta(self):
        st = rm.monitor_status(home=self.home)
        self.assertEqual(st["history_file"], "missing")
        self.assertEqual(st["meta"], "unknown_missing")
        self._snap(1)
        st = rm.monitor_status(home=self.home)
        self.assertEqual(st["history_file"], "present")
        self.assertEqual(st["meta"], "present")
        self.assertEqual(st["counters"].get("snapshots_recorded"), 1)


class ArmedWatchWiringTest(unittest.TestCase):
    """Runtime wiring: the armed watcher feeds the monitor from
    vault resolutions it already made - no new RPC, and a monitor
    failure never breaks the watch loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="resmon_wire_")
        self._old_home = os.environ.get("FOREX_AGENT_HOME")
        os.environ["FOREX_AGENT_HOME"] = self.tmp

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("FOREX_AGENT_HOME", None)
        else:
            os.environ["FOREX_AGENT_HOME"] = self._old_home

    def test_set_positions_records_snapshot_from_existing_read(self):
        from unittest.mock import patch
        from daemon.launch_scanner import armed_watch as aw
        from daemon.launch_scanner import reserve_monitor as rmod
        vaults = {"base_vault": "BV", "quote_vault": "QV",
                  "quote_mint": "So11111111111111111111111111111111111111112",
                  "base_amount": 12.0, "quote_amount": 34.5,
                  "quote_virtual": 0}
        w = aw.ArmedWatcher(lambda *a, **k: None)
        with patch.object(aw._res, "resolve_vaults",
                          return_value=vaults), \
                patch.object(aw._res, "mint_decimals", return_value=6):
            w.set_positions({MINT: {"pair": POOL_A}})
        hist = rmod.pool_history(POOL_A, home=self.tmp)
        self.assertEqual(hist["status"], "ok")
        self.assertEqual(len(hist["samples"]), 1)
        s = hist["samples"][0]
        self.assertEqual(s["quote_amount"], 34.5)
        self.assertEqual(s["source"], "armed_watch")

    def test_monitor_failure_never_breaks_watch_loop(self):
        from unittest.mock import patch
        from daemon.launch_scanner import armed_watch as aw
        vaults = {"base_vault": "BV", "quote_vault": "QV",
                  "base_amount": 1.0, "quote_amount": 2.0,
                  "quote_virtual": 0}
        w = aw.ArmedWatcher(lambda *a, **k: None)
        with patch.object(aw._res, "resolve_vaults",
                          return_value=vaults), \
                patch.object(aw._res, "mint_decimals", return_value=6), \
                patch.object(aw._resmon, "record_snapshot",
                             side_effect=RuntimeError("boom")):
            # Must not raise, and the watcher must keep working.
            w.set_positions({MINT: {"pair": POOL_A}})
        self.assertIsNotNone(w.state)


if __name__ == "__main__":
    unittest.main()
