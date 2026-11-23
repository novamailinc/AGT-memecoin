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
        self.t0 = time.time() - 10000.0

    def _snap(self, n, quote=100.0, pool=POOL_A, mint=MINT, base=10.0,
              step=10.0, start=None, **kw):
        t = self.t0 if start is None else start
        for i in range(n):
            self.assertTrue(rm.record_snapshot(
                pool, mint, t + i * step, base, quote,
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

    def test_migration_explicit_marker_bound_to_transition(self):
        self._snap(3, pool=POOL_A)
        self._snap(2, pool=POOL_B, start=self.t0 + 500.0,
                   migration_event="pumpswap_graduation")
        out = rm.detect_migration(MINT, home=self.home)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(out["transitions"]), 1)
        tr = out["transitions"][0]
        self.assertEqual(tr["from_pool"], POOL_A)
        self.assertEqual(tr["to_pool"], POOL_B)
        self.assertEqual(tr["provenance"], "marker_association_unverified")
        self.assertFalse(tr["conflict_both_active"])

    def test_marker_never_launders_a_later_transition(self):
        POOL_C = "PoolCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
        self._snap(2, pool=POOL_A)
        # Marker on B near the A->B anchor binds that transition only.
        self._snap(2, pool=POOL_B, start=self.t0 + 500.0,
                   migration_event="pumpswap_graduation")
        # B stays active after C appears: B->C is NOT explicit, and
        # the still-active old pool is a labeled conflict.
        self._snap(2, pool=POOL_C, start=self.t0 + 2000.0)
        self.assertTrue(rm.record_snapshot(
            POOL_B, MINT, self.t0 + 2010.0, 10.0, 95.0, home=self.home))
        out = rm.detect_migration(MINT, home=self.home)
        trs = {(t["from_pool"], t["to_pool"]): t
               for t in out["transitions"]}
        self.assertEqual(trs[(POOL_A, POOL_B)]["provenance"], "marker_association_unverified")
        later = trs[(POOL_B, POOL_C)]
        self.assertNotEqual(later["provenance"], "explicit")
        self.assertTrue(later["conflict_both_active"])

    def test_migration_inferred_from_reserves(self):
        # Causal baseline (pre-transition) drained in the LATEST
        # post-transition observation.
        self._snap(3, quote=100.0, pool=POOL_A)
        self._snap(2, pool=POOL_B, start=self.t0 + 500.0)
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 510.0, 10.0, 2.0, home=self.home))
        out = rm.detect_migration(MINT, home=self.home)
        tr = out["transitions"][0]
        self.assertEqual(tr["provenance"], "inferred_from_reserves")
        self.assertTrue(tr["old_pool_drained"])
        self.assertFalse(tr["conflict_both_active"])
        self.assertEqual(tr["baseline_peak"], 100.0)
        self.assertEqual(tr["latest_old_quote"], 2.0)

    def test_drained_then_recovered_old_pool_is_conflict(self):
        # A 100 -> B appears -> A dips to 10 -> A recovers to 100:
        # historical minimum must NOT hide the recovery; latest state
        # governs and both-active is a labeled conflict.
        self._snap(3, quote=100.0, pool=POOL_A)
        self._snap(2, pool=POOL_B, start=self.t0 + 500.0)
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 510.0, 10.0, 10.0, home=self.home))
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 520.0, 10.0, 100.0, home=self.home))
        out = rm.detect_migration(MINT, home=self.home)
        tr = out["transitions"][0]
        self.assertTrue(tr["conflict_both_active"])
        self.assertFalse(tr["old_pool_drained"])
        self.assertNotEqual(tr["provenance"], "inferred_from_reserves")

    def test_no_post_transition_observation_is_unknown_not_inference(self):
        # Old pool never observed after the new one appears: pool
        # appearance alone is not canonical proof - unknown, never
        # an inference presented as authority.
        self._snap(3, quote=100.0, pool=POOL_A)
        self._snap(2, pool=POOL_B, start=self.t0 + 500.0)
        out = rm.detect_migration(MINT, home=self.home)
        tr = out["transitions"][0]
        self.assertEqual(tr["provenance"], "unknown")
        self.assertIsNone(tr["old_pool_drained"])

    def test_migration_unrecorded_and_conflict_labeled(self):
        # Old pool observed after the transition, still near baseline.
        self._snap(3, quote=100.0, pool=POOL_A)
        self._snap(2, pool=POOL_B, start=self.t0 + 500.0)
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0 + 510.0, 10.0, 95.0, home=self.home))
        out = rm.detect_migration(MINT, home=self.home)
        tr = out["transitions"][0]
        self.assertEqual(tr["provenance"], "unrecorded")
        self.assertTrue(tr["conflict_both_active"])

    def test_missing_mint_counted_unreadable_never_keyerror(self):
        self._snap(2)
        with open(rm._file(self.home), "a") as f:
            f.write(json.dumps({"record_type": "reserve_snapshot",
                                "pool": POOL_B, "ts": self.t0,
                                "base_amount": 1.0,
                                "quote_amount": 1.0}) + "\n")
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertEqual(hist["unreadable"], 1)
        out = rm.detect_migration(MINT, home=self.home)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["unreadable"], 1)
        self.assertEqual(out["pools_seen"], 1)

    def test_retention_propagates_to_history_and_detectors(self):
        orig_bytes, orig_cap = rm._MAX_FILE_BYTES, rm._MAX_SAMPLES_PER_POOL
        rm._MAX_FILE_BYTES, rm._MAX_SAMPLES_PER_POOL = 400, 10
        try:
            self._snap(40)
        finally:
            rm._MAX_FILE_BYTES, rm._MAX_SAMPLES_PER_POOL = (
                orig_bytes, orig_cap)
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertTrue(hist["retention"]["partial"])
        self.assertGreaterEqual(
            hist["retention"]["samples_dropped_by_cap_total"], 1)
        abn = rm.detect_abnormal(POOL_A, home=self.home)
        self.assertTrue(abn["retention"]["partial"])
        mig = rm.detect_migration(MINT, home=self.home)
        self.assertTrue(mig["retention"]["partial"])

    def test_detector_carries_observed_age_and_threshold_provenance(self):
        self._snap(7, quote=100.0)
        now = self.t0 + 1000.0
        out = rm.detect_abnormal(POOL_A, home=self.home, now=now)
        self.assertEqual(out["last_observed_ts"], self.t0 + 60.0)
        self.assertEqual(out["observed_age_s"], 940.0)
        self.assertEqual(out["thresholds_provenance"],
                         "unvalidated_advisory_heuristic")
        mig = rm.detect_migration(MINT, home=self.home, now=now)
        self.assertEqual(mig["last_observed_ts"], self.t0 + 60.0)
        self.assertEqual(mig["observed_age_s"], 940.0)

    def test_optional_context_recorded_only_when_grounded(self):
        self.assertTrue(rm.record_snapshot(
            POOL_A, MINT, self.t0, 1.0, 2.0,
            quote_mint="So11111111111111111111111111111111111111112",
            decimals=6, slot=12345, base_ts=self.t0 - 5.0,
            quote_ts=self.t0, home=self.home))
        s = rm.pool_history(POOL_A, home=self.home)["samples"][0]
        self.assertEqual(s["decimals"], 6)
        self.assertEqual(s["slot"], 12345)
        self.assertEqual(s["base_ts"], self.t0 - 5.0)
        # Supplied-but-invalid optional context rejects the record.
        self.assertFalse(rm.record_snapshot(
            POOL_A, MINT, self.t0, 1.0, 2.0, decimals=True,
            home=self.home))
        self.assertFalse(rm.record_snapshot(
            POOL_A, MINT, self.t0, 1.0, 2.0, slot=-1, home=self.home))
        self.assertFalse(rm.record_snapshot(
            POOL_A, MINT, self.t0, 1.0, 2.0,
            base_ts=float("nan"), home=self.home))

    def test_bare_marker_near_two_transitions_binds_neither(self):
        POOL_C = "PoolCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
        self._snap(2, pool=POOL_A, start=self.t0)
        # Bare marker (no target identity) on B, within tolerance of
        # BOTH the A->B and B->C anchors: ambiguous, binds neither.
        self._snap(1, pool=POOL_B, start=self.t0 + 500.0,
                   migration_event="graduation")
        self._snap(1, pool=POOL_C, start=self.t0 + 900.0)
        self.assertTrue(rm.record_snapshot(
            POOL_B, MINT, self.t0 + 1200.0, 10.0, 95.0,
            home=self.home))
        self.assertTrue(rm.record_snapshot(
            POOL_C, MINT, self.t0 + 1300.0, 10.0, 90.0,
            home=self.home))
        out = rm.detect_migration(MINT, home=self.home)
        for tr in out["transitions"]:
            self.assertNotEqual(tr["provenance"], "explicit",
                                tr)

    def test_marker_with_target_identity_binds_named_transition(self):
        POOL_C = "PoolCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
        self._snap(2, pool=POOL_A, start=self.t0)
        self._snap(1, pool=POOL_B, start=self.t0 + 500.0)
        self._snap(1, pool=POOL_C, start=self.t0 + 900.0)
        # Marker names the B->C target explicitly.
        self.assertTrue(rm.record_snapshot(
            POOL_B, MINT, self.t0 + 950.0, 10.0, 1.0,
            migration_event="graduation", migration_to=POOL_C,
            migration_from=POOL_B, migration_event_id="fixture-event-1",
            migration_event_ts=self.t0 + 940.0,
            home=self.home))
        out = rm.detect_migration(MINT, home=self.home)
        trs = {(t["from_pool"], t["to_pool"]): t
               for t in out["transitions"]}
        self.assertEqual(trs[(POOL_B, POOL_C)]["provenance"],
                         "explicit")
        self.assertNotEqual(trs[(POOL_A, POOL_B)]["provenance"],
                            "explicit")

    def test_destination_only_marker_is_not_explicit(self):
        self._snap(2, pool=POOL_A)
        self._snap(1, pool=POOL_B, start=self.t0 + 500,
                   migration_event="graduation", migration_to=POOL_B)
        tr = rm.detect_migration(MINT, home=self.home)["transitions"][0]
        self.assertEqual(tr["provenance"], "marker_association_unverified")

    def test_corrupt_sidecar_isolated_never_crashes_reads(self):
        self._snap(3)
        rm._meta_file(self.home).write_text(json.dumps({
            "samples_dropped_by_cap": "many",
            "torn_tail_repairs": None,
            "compaction_dropped_unreadable": True}))
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertEqual(hist["status"], "ok")
        self.assertTrue(hist["retention"]["meta_corrupt"])
        self.assertTrue(hist["retention"]["partial"])
        abn = rm.detect_abnormal(POOL_A, home=self.home)
        self.assertTrue(abn["retention"]["meta_corrupt"])
        mig = rm.detect_migration(MINT, home=self.home)
        self.assertTrue(mig["retention"]["meta_corrupt"])
        # A later write resets the corrupt value, never propagates.
        rm._meta_bump("snapshots_recorded", home=self.home)
        meta = rm._meta_read(self.home)
        self.assertIsInstance(meta.get("samples_dropped_by_cap"), int)
        for result in (rm.pool_history(POOL_A, home=self.home),
                       rm.detect_abnormal(POOL_A, home=self.home),
                       rm.detect_migration(MINT, home=self.home)):
            self.assertTrue(result["retention"]["partial"])
            self.assertGreater(result["retention"]["meta_values_reset_total"], 0)

    def test_torn_repair_marks_retention_partial(self):
        self._snap(2)
        with open(rm._file(self.home), "a") as f:
            f.write('{"v": 1, "record_type": "reserve_snap')
        self._snap(1)
        hist = rm.pool_history(POOL_A, home=self.home)
        self.assertTrue(hist["retention"]["partial"])
        self.assertEqual(
            hist["retention"]["torn_tail_repairs_total"], 1)

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

    def _push_msg(self, sub, amount):
        import base64 as b64
        raw = bytearray(165)
        raw[0:32] = b"\x01" * 32
        raw[64:72] = int(amount).to_bytes(8, "little")
        return json.dumps({
            "method": "accountNotification",
            "params": {"subscription": sub,
                       "result": {"value": {
                           "data": [b64.b64encode(bytes(raw)).decode(),
                                    "base64"]}}}})

    def test_push_notification_ingests_both_sides_grounded(self):
        from daemon.launch_scanner import armed_watch as aw
        from daemon.launch_scanner import reserve_monitor as rmod
        w = aw.ArmedWatcher(lambda *a, **k: None)
        w._want[MINT] = {"pool": POOL_A, "decimals": 6}
        w._subs[11] = (MINT, "base", POOL_A, w._generation)
        w._subs[22] = (MINT, "quote", POOL_A, w._generation)
        # One side only: unobserved side is unrecorded, never zero.
        w._handle(None, self._push_msg(11, 1000))
        hist = rmod.pool_history(POOL_A, home=self.tmp)
        self.assertEqual(len(hist["samples"]), 0)
        w._handle(None, self._push_msg(22, 5000))
        hist = rmod.pool_history(POOL_A, home=self.tmp)
        self.assertEqual(len(hist["samples"]), 1)
        s = hist["samples"][0]
        self.assertEqual(s["source"], "armed_watch_ws_push")
        self.assertEqual(s["base_amount"], 1000.0)
        self.assertEqual(s["quote_amount"], 5000.0)
        self.assertEqual(s["decimals"], 6)
        # Per-side observation times recorded; base side is older.
        self.assertLess(s["base_ts"], s["quote_ts"])
        self.assertIn("pair_skew_s", s)

    def test_stale_generation_push_discarded_position_change(self):
        from unittest.mock import patch
        from daemon.launch_scanner import armed_watch as aw
        from daemon.launch_scanner import reserve_monitor as rmod
        POOL_NEW = "PoolNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN"
        vaults_a = {"base_vault": "BV1", "quote_vault": "QV1",
                    "base_amount": 1.0, "quote_amount": 2.0,
                    "quote_virtual": 0}
        vaults_new = {"base_vault": "BV2", "quote_vault": "QV2",
                      "base_amount": 3.0, "quote_amount": 4.0,
                      "quote_virtual": 0}
        w = aw.ArmedWatcher(lambda *a, **k: None)
        with patch.object(aw._res, "resolve_vaults",
                          return_value=vaults_a), \
                patch.object(aw._res, "mint_decimals", return_value=6):
            w.set_positions({MINT: {"pair": POOL_A}})
        gen1 = w._generation
        # Old subscription captured under generation 1.
        w._subs[11] = (MINT, "base", POOL_A, gen1)
        # Position changes to a different pool: new generation.
        with patch.object(aw._res, "resolve_vaults",
                          return_value=vaults_new), \
                patch.object(aw._res, "mint_decimals", return_value=6):
            w.set_positions({MINT: {"pair": POOL_NEW}})
        self.assertGreater(w._generation, gen1)
        # Cached amounts for the old pool identity are purged, and
        # the fresh resolve re-seeded them under the new identity.
        self.assertEqual(w._amounts[MINT]["base"], 3.0)
        drops_before = w._stale_sub_drops
        # Delayed notification on the OLD subscription: discarded,
        # never stored against the new pool or paired with the new
        # other side.
        w._handle(None, self._push_msg(11, 999999))
        self.assertEqual(w._stale_sub_drops, drops_before + 1)
        self.assertEqual(w._amounts[MINT]["base"], 3.0)
        hist = rmod.pool_history(POOL_NEW, home=self.tmp)
        self.assertNotIn(999999.0,
                         [s["base_amount"] for s in hist["samples"]])

    def test_same_identity_rearm_keeps_generation(self):
        from unittest.mock import patch
        from daemon.launch_scanner import armed_watch as aw
        vaults = {"base_vault": "BV1", "quote_vault": "QV1",
                  "base_amount": 1.0, "quote_amount": 2.0,
                  "quote_virtual": 0}
        w = aw.ArmedWatcher(lambda *a, **k: None)
        with patch.object(aw._res, "resolve_vaults",
                          return_value=vaults), \
                patch.object(aw._res, "mint_decimals", return_value=6):
            w.set_positions({MINT: {"pair": POOL_A}})
            gen1 = w._generation
            # Re-arm with the SAME identity (reconnect path): want
            # unchanged, generation stable, pushes still accepted.
            w.set_positions({MINT: {"pair": POOL_A}})
            self.assertEqual(w._generation, gen1)
        w._subs[11] = (MINT, "base", POOL_A, gen1)
        w._handle(None, self._push_msg(11, 4242))
        self.assertEqual(w._amounts[MINT]["base"], 4242)

    def test_reconcile_rejects_identity_changed_inside_resolver(self):
        from unittest.mock import patch
        from daemon.launch_scanner import armed_watch as aw
        w = aw.ArmedWatcher(lambda *a: None, sol_usd_fn=lambda: 100)
        va = {"base_vault": "ba", "quote_vault": "qa",
              "base_amount": 10, "quote_amount": 20, "quote_virtual": 0,
              "quote_effective": 20}
        vb = dict(va, base_vault="bb", quote_vault="qb",
                  base_amount=30, quote_amount=40, quote_effective=40)
        with patch.object(aw._res, "resolve_vaults", return_value=va), \
                patch.object(aw._res, "mint_decimals", return_value=6):
            w.set_positions({MINT: {"pair": POOL_A}})
        def change_during_read(*args):
            with patch.object(aw._res, "resolve_vaults", return_value=vb), \
                    patch.object(aw._res, "mint_decimals", return_value=6):
                w.set_positions({MINT: {"pair": POOL_B}})
            return va
        with patch.object(aw._res, "resolve_vaults", side_effect=change_during_read):
            w._reconcile(10)
        self.assertEqual(w._amounts[MINT]["quote"], 40)
        self.assertEqual(w._monitor_amounts[MINT]["quote"], 40)
        self.assertGreater(w._stale_sub_drops, 0)
        w._subs[23] = (MINT, "base", POOL_B, w._generation)
        w._handle(None, self._push_msg(23, 300))
        latest = rm.pool_history(POOL_B, home=self.tmp)["samples"][-1]
        self.assertEqual(latest["quote_amount"], 40)

    def test_failed_reconcile_does_not_refresh_capture_times(self):
        from unittest.mock import patch
        from daemon.launch_scanner import armed_watch as aw
        w = aw.ArmedWatcher(lambda *a: None)
        w._want[MINT] = {"pool": POOL_A, "base_vault": "ba",
                          "quote_vault": "qa", "decimals": 6}
        old = {"base": 1, "quote": 2, "ts": 100,
               "base_ts": 99, "quote_ts": 100}
        w._amounts[MINT] = dict(old)
        w._monitor_amounts[MINT] = dict(old)
        with patch.object(aw._res, "resolve_vaults", return_value=None):
            w._reconcile(10)
        self.assertEqual(w._monitor_amounts[MINT], old)
        self.assertEqual(w._amounts[MINT], old)

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
