"""Tests for deployer tracking: registry math, hot-deployer rule, fast-track
ordering vs the safety gate, outcome recording, /deployers output, and the
registry file's 0600 permissions. No network — Helius RPC is mocked."""

import json
import os
import stat
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import deployers
from daemon.telegram import commands_deployers

DEP1 = "DeplAAAA11111111111111111111111111111111111"
DEP2 = "DeplBBBB22222222222222222222222222222222222"
DEP3 = "DeplCCCC33333333333333333333333333333333333"


def _degen(**kw):
    base = dict(deployer_tracking_enabled=True, deployer_min_launches=3,
                deployer_hot_hit_rate=0.30, deployer_resolve_ttl_s=21600.0)
    base.update(kw)
    return SimpleNamespace(**base)


class DeployerRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/deployer-test-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.reg = self.tmp / "deployer_history.json"
        self.p = patch.object(deployers, "_REGISTRY_FILE", self.reg)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_record_launches_and_outcomes_hit_rate_math(self):
        for _ in range(5):
            deployers.record_launch(DEP1)
        self.assertTrue(deployers.record_outcome("mintA", DEP1, "hit_2x"))
        self.assertTrue(deployers.record_outcome("mintB", DEP1, "hit_2x"))
        self.assertTrue(deployers.record_outcome("mintC", DEP1, "rug"))
        self.assertTrue(deployers.record_outcome("mintD", DEP1, "dead"))
        hist = deployers.load_registry()
        entry = hist[DEP1]
        self.assertEqual(entry["launches_seen"], 5)
        self.assertEqual(entry["hits_2x"], 2)
        self.assertEqual(entry["rugs"], 1)
        self.assertEqual(entry["dead"], 1)
        self.assertAlmostEqual(deployers.hit_rate(entry), 0.4)
        self.assertGreater(entry["first_seen"], 0)
        self.assertGreaterEqual(entry["last_seen"], entry["first_seen"])

    def test_registry_file_created_with_0600(self):
        deployers.record_launch(DEP1)
        self.assertTrue(self.reg.exists())
        mode = stat.S_IMODE(os.stat(self.reg).st_mode)
        self.assertEqual(mode, 0o600)
        # Save is atomic-ish (tmp file + 0600 + rename).
        self.assertFalse((self.tmp / "deployer_history.tmp").exists())

    def test_unknown_outcome_rejected(self):
        self.assertFalse(deployers.record_outcome("mintA", DEP1, "moon"))
        self.assertEqual(deployers.load_registry(), {})

    def test_empty_deployer_ignored(self):
        self.assertIsNone(deployers.record_launch(""))
        self.assertFalse(deployers.record_outcome("mintA", "", "hit_2x"))

    def test_outcome_without_prior_launch_keeps_rate_sane(self):
        deployers.record_outcome("mintA", DEP2, "hit_2x")
        entry = deployers.load_registry()[DEP2]
        self.assertLessEqual(deployers.hit_rate(entry), 1.0)


class HotDeployerRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/deployer-test-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.reg = self.tmp / "deployer_history.json"
        self.p = patch.object(deployers, "_REGISTRY_FILE", self.reg)
        self.p.start()
        self.addCleanup(self.p.stop)
        # Hot: 4 launches, 2 hits = 50% >= 30%.
        for _ in range(4):
            deployers.record_launch(DEP1)
        deployers.record_outcome("m1", DEP1, "hit_2x")
        deployers.record_outcome("m2", DEP1, "hit_2x")
        # Cold: 2 launches (below min 3).
        for _ in range(2):
            deployers.record_launch(DEP2)
        deployers.record_outcome("m3", DEP2, "hit_2x")

    def test_hot_deployer_fasttrack_eligible(self):
        degen = _degen()
        state = {}
        res = deployers.evaluate_deployer_fasttrack(
            state, degen, "freshmint", DEP1, safety_passed=True)
        self.assertIsNotNone(res)
        self.assertEqual(res["deployer"], DEP1)
        self.assertEqual(res["launches_seen"], 4)
        self.assertEqual(res["hits_2x"], 2)
        self.assertEqual(res["hit_rate"], 0.5)
        self.assertIn("🏭", res["deployer_line"])
        self.assertIn("2/4", res["deployer_line"])

    def test_cold_deployer_neutral(self):
        degen = _degen()
        self.assertFalse(deployers.is_hot(DEP2, degen))
        res = deployers.evaluate_deployer_fasttrack(
            {}, degen, "freshmint", DEP2, safety_passed=True)
        self.assertIsNone(res)  # no boost, no penalty

    def test_unknown_deployer_neutral(self):
        degen = _degen()
        self.assertFalse(deployers.is_hot(DEP3, degen))
        res = deployers.evaluate_deployer_fasttrack(
            {}, degen, "freshmint", DEP3, safety_passed=True)
        self.assertIsNone(res)

    def test_already_carded_mint_not_fasttracked(self):
        degen = _degen()
        state = {}
        first = deployers.evaluate_deployer_fasttrack(
            state, degen, "mintX", DEP1, safety_passed=True)
        self.assertIsNotNone(first)
        second = deployers.evaluate_deployer_fasttrack(
            state, degen, "mintX", DEP1, safety_passed=True)
        self.assertIsNone(second)  # new-to-us mints only

    def test_tracking_disabled_neutral(self):
        degen = _degen(deployer_tracking_enabled=False)
        res = deployers.evaluate_deployer_fasttrack(
            {}, degen, "freshmint", DEP1, safety_passed=True)
        self.assertIsNone(res)

    def test_custom_knobs_respected(self):
        # min_launches=5 -> 4 launches is cold under this knob.
        degen = _degen(deployer_min_launches=5)
        self.assertFalse(deployers.is_hot(DEP1, degen))
        self.assertIsNone(deployers.evaluate_deployer_fasttrack(
            {}, degen, "freshmint", DEP1, safety_passed=True))
        # hit rate 25% under a 0.30 bar -> not hot.
        for _ in range(4):
            deployers.record_launch(DEP3)
        deployers.record_outcome("mx", DEP3, "hit_2x")  # 1/4 = 25%
        self.assertFalse(deployers.is_hot(DEP3, _degen()))

    def test_hard_fail_override_blocked(self):
        # Hot deployer + honeypot/can't-sell: the safety gate's verdict
        # wins — history NEVER overrides hard fails.
        degen = _degen()
        self.assertTrue(deployers.is_hot(DEP1, degen))
        res = deployers.evaluate_deployer_fasttrack(
            {}, degen, "honeypotmint", DEP1, safety_passed=False)
        self.assertIsNone(res)


class ResolutionCacheTest(unittest.TestCase):
    def test_pumpfun_creator_from_ctx_zero_rpc(self):
        ctx = {"extra": {"creator": DEP1}}
        with patch.object(deployers, "_rpc") as rpc:
            dep = deployers.resolve_deployer("mintX", "solana", ctx)
        self.assertEqual(dep, DEP1)
        rpc.assert_not_called()  # creator came from the source feed

    def test_cached_deployer_skips_repeat_resolution(self):
        ctx = {"extra": {"creator": DEP1}}
        state = {}
        calls = []

        def fake_resolve(mint, chain, ctx):
            calls.append(mint)
            return "DEPL%03d" % len(calls)

        degen = _degen()
        first = deployers.cached_deployer(state, "mintA", "solana", ctx,
                                         degen=degen)
        self.assertEqual(first, DEP1)
        second = deployers.cached_deployer(state, "mintA", "solana", ctx,
                                          degen=degen)
        self.assertEqual(second, DEP1)
        self.assertIn("mintA", state["deployer_cache"])
        # TTL expiry forces a re-resolve.
        state["deployer_cache"]["mintB"] = {"deployer": None,
                                            "ts": time.time() - 99999}
        with patch.object(deployers, "resolve_deployer", fake_resolve):
            got = deployers.cached_deployer(state, "mintB", "solana", ctx,
                                           degen=degen)
        self.assertEqual(got, "DEPL001")
        self.assertEqual(len(calls), 1)

    def test_rpc_fallback_first_signer(self):
        sigs = [{"signature": "sig_new"}, {"signature": "sig_old"}]
        # Real getTransaction responses always carry meta; the
        # Batch 3 meta.err gate fails closed without it, so the
        # fixture must carry a success meta (err None).
        tx = {"meta": {"err": None},
              "transaction": {"signatures":["sig_old"], "message": {"accountKeys": [
                  {"pubkey": DEP2}, {"pubkey": "other"}]}}}
        with patch.object(deployers, "_rpc",
                           side_effect=[sigs, tx]) as rpc, \
             patch.object(deployers, "api_key", return_value="k"):
            dep = deployers.resolve_deployer("mintPool", "solana", {})
        self.assertEqual(dep, DEP2)

    def test_rpc_fallback_reverted_tx_names_no_deployer(self):
        # Batch 3 meta.err gate: a reverted oldest tx never names
        # a deployer — the launch never happened.
        sigs = [{"signature": "sig_new"}, {"signature": "sig_old"}]
        tx = {"meta": {"err": {"InstructionError": [0, "Custom"]}},
              "transaction": {"message": {"accountKeys": [
                  {"pubkey": DEP2}, {"pubkey": "other"}]}}}
        with patch.object(deployers, "_rpc",
                           side_effect=[sigs, tx]) as rpc, \
             patch.object(deployers, "api_key", return_value="k"):
            dep = deployers.resolve_deployer("mintPool", "solana", {})
        self.assertIsNone(dep)
        rpc.assert_called()

    def test_rpc_fallback_aborts_when_too_much_history(self):
        sigs = [{"signature": "sig%d" % i} for i in range(100)]
        with patch.object(deployers, "_rpc", return_value=sigs):
            self.assertIsNone(deployers.resolve_deployer("mintPool",
                                                         "solana", {}))


class DeployersCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/deployer-test-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.reg = self.tmp / "deployer_history.json"
        self.p = patch.object(deployers, "_REGISTRY_FILE", self.reg)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_empty_registry_renders_gracefully(self):
        text = commands_deployers.cmd_deployers(SimpleNamespace(), [])
        self.assertIn("No hot deployers yet", text)
        self.assertIn("neutral", text)

    def test_output_with_min_launches_filter(self):
        for _ in range(4):
            deployers.record_launch(DEP1)
        deployers.record_outcome("m1", DEP1, "hit_2x")
        deployers.record_outcome("m2", DEP1, "hit_2x")
        for _ in range(2):  # cold — below the default filter
            deployers.record_launch(DEP2)
        deployers.record_outcome("m3", DEP2, "hit_2x")
        text = commands_deployers.cmd_deployers(SimpleNamespace(), [])
        self.assertIn("2/4", text)
        self.assertIn("50%", text)
        self.assertNotIn(DEP2[:8], text)  # filtered: only 2 launches
        # Loosen the filter: the 2-launch deployer now shows.
        loose = commands_deployers.cmd_deployers(SimpleNamespace(), ["2"])
        self.assertIn(DEP2[:8], loose)


class ApiKeyFallbackTest(unittest.TestCase):
    """2026-11-22 dead-key fix: when the main Helius key file is
    absent (quarantined after it started returning HTTP 401),
    api_key() falls back to the discovery key so deployer checks
    keep working instead of going dark. No network — key files
    only."""

    def setUp(self):
        self.tmp = Path("/tmp/deployer-key-test-%d"
                        % int(time.time() * 1e6))
        (self.tmp / "helius").mkdir(parents=True)
        self.main = self.tmp / "helius" / "api_key"
        self.disc = self.tmp / "helius" / "api_key_discovery"
        self._old_key = deployers._key_cache
        self._old_checked = deployers._key_checked
        self._old_env = (os.environ.pop("HELIUS_API_KEY", None),
                         os.environ.pop("HELIUS_API_KEY_DISCOVERY",
                                        None))
        self.p1 = patch.object(deployers, "_KEY_FILE", self.main)
        self.p2 = patch.object(deployers, "_DISCOVERY_KEY_FILE",
                               self.disc)
        self.p1.start()
        self.p2.start()
        self.addCleanup(self.p1.stop)
        self.addCleanup(self.p2.stop)

    def tearDown(self):
        deployers._key_cache = self._old_key
        deployers._key_checked = self._old_checked
        for name, val in (("HELIUS_API_KEY", self._old_env[0]),
                          ("HELIUS_API_KEY_DISCOVERY",
                           self._old_env[1])):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val

    def _reset(self):
        deployers._key_cache = None
        deployers._key_checked = False

    def test_main_key_wins_when_present(self):
        self.main.write_text("main-key-123\n")
        self.disc.write_text("disc-key-456\n")
        self._reset()
        self.assertEqual(deployers.api_key(), "main-key-123")

    def test_discovery_fallback_when_main_absent(self):
        self.disc.write_text("disc-key-456\n")
        self._reset()
        self.assertEqual(deployers.api_key(), "disc-key-456")

    def test_none_when_both_absent(self):
        self._reset()
        self.assertIsNone(deployers.api_key())


class DeployerArchiveTest(unittest.TestCase):
    """Persistence depth (4.2): archive-before-eviction, fail-closed
    durability, bounded outcome timestamps, compaction, honest
    availability labels."""

    def setUp(self):
        self.tmp = Path("/tmp/deployer-archive-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.reg = self.tmp / "deployer_history.json"
        self.arc = self.tmp / "deployer_archive.jsonl"
        self.lock = self.tmp / "deployer_archive.lock"
        self.meta = self.tmp / "deployer_archive_meta.json"
        for name, val in (("_REGISTRY_FILE", self.reg),
                          ("_ARCHIVE_FILE", self.arc),
                          ("_ARCHIVE_LOCK_FILE", self.lock),
                          ("_ARCHIVE_META_FILE", self.meta),
                          ("_MAX_REGISTRY", 2)):
            p = patch.object(deployers, name, val)
            p.start()
            self.addCleanup(p.stop)

    def test_eviction_archives_before_pop(self):
        deployers.record_launch(DEP1)
        deployers.record_outcome("m1", DEP1, "rug")
        deployers.record_launch(DEP2)
        deployers.record_launch(DEP3)  # exceeds the cap of 2
        hist = deployers.load_registry()
        self.assertNotIn(DEP1, hist)
        self.assertIn(DEP2, hist)
        self.assertIn(DEP3, hist)
        out = deployers.deployer_archive()
        self.assertTrue(out["archive_status"]["available"])
        self.assertEqual(len(out["records"]), 1)
        rec = out["records"][0]
        self.assertEqual(rec["deployer"], DEP1)
        self.assertEqual(rec["launches_seen"], 1)
        self.assertEqual(rec["rugs"], 1)
        self.assertEqual(rec["evicted_reason"], "registry_cap")
        self.assertEqual(
            rec["memory_provenance"],
            "recorded_counters_and_bounded_timestamps_not_verified_skill")
        self.assertGreater(rec["archived_ts"], 0)
        self.assertEqual(rec["segments_merged"], 1)

    def test_reappearing_deployer_segments_merged_never_erased(self):
        deployers.record_launch(DEP1)
        deployers.record_outcome("m1", DEP1, "rug")
        deployers.record_launch(DEP2)
        deployers.record_launch(DEP3)  # over cap: evicts DEP1 (seg 1)
        deployers.record_launch(DEP1)  # reappears, fresh entry (evicts DEP2)
        deployers.record_outcome("m2", DEP1, "hit_2x")
        deployers.record_launch(DEP2)  # evicts DEP3
        deployers.record_launch(DEP3)  # evicts DEP1 again (seg 2)
        out = deployers.deployer_archive(DEP1)
        self.assertEqual(len(out["records"]), 1)
        rec = out["records"][0]
        # Earlier segment NOT erased by the later one.
        self.assertEqual(rec["launches_seen"], 2)
        self.assertEqual(rec["rugs"], 1)
        self.assertEqual(rec["hits_2x"], 1)
        self.assertEqual(rec["segments_merged"], 2)
        self.assertEqual(len(rec["segment_ids"]), 2)
        # Live counters were never rehydrated from the archive.
        self.assertNotIn(DEP1, deployers.load_registry())

    def test_duplicate_segment_upserts_never_double_counts(self):
        entry = {"first_seen": 100.0, "last_seen": 200.0,
                 "launches_seen": 3, "hits_2x": 1, "rugs": 0, "dead": 0}
        self.assertTrue(deployers._archive_deployer(DEP1, entry, 300.0))
        # Eviction retried after a registry-save failure: same segment
        # id, SAME counters - must not double-count.
        self.assertTrue(deployers._archive_deployer(DEP1, entry, 301.0))
        out = deployers.deployer_archive(DEP1)
        self.assertEqual(out["records"][0]["launches_seen"], 3)
        self.assertEqual(out["records"][0]["segments_merged"], 1)

    def test_same_id_counter_update_replaces_stale_value(self):
        # Eviction archives launches=3, registry save fails, 6 more
        # launches accrue, retry archives the SAME segment id with
        # launches=9: the updated value replaces, never keeps 3 or
        # sums to 12.
        e3 = {"first_seen": 100.0, "launches_seen": 3,
              "hits_2x": 1, "rugs": 0, "dead": 0}
        e9 = dict(e3, launches_seen=9)
        self.assertTrue(deployers._archive_deployer(DEP1, e3, 300.0))
        self.assertTrue(deployers._archive_deployer(DEP1, e9, 301.0))
        out = deployers.deployer_archive(DEP1)
        self.assertEqual(out["records"][0]["launches_seen"], 9)
        self.assertEqual(out["records"][0]["segments_merged"], 1)
        # And after compaction the identity still cannot be erased.
        with patch.object(deployers, "_ARCHIVE_MAX_BYTES", 10):
            deployers._archive_deployer(
                DEP2, {"first_seen": 1.0, "launches_seen": 1}, 302.0)
        out2 = deployers.deployer_archive(DEP1)
        self.assertEqual(out2["records"][0]["launches_seen"], 9)
        self.assertEqual(out2["records"][0]["segments_merged"], 1)

    def test_stale_snapshot_never_overwrites_fresh_counters(self):
        # Unlocked registry RMW makes this reachable: eviction
        # archives launches=9, a stale retry of the SAME segment id
        # lands later with launches=3. Monotonic merge keeps the
        # larger cumulative value and labels the regression.
        e9 = {"first_seen": 100.0, "launches_seen": 9,
              "hits_2x": 2, "rugs": 1, "dead": 0}
        e3 = dict(e9, launches_seen=3, hits_2x=1)
        self.assertTrue(deployers._archive_deployer(DEP1, e9, 300.0))
        self.assertTrue(deployers._archive_deployer(DEP1, e3, 301.0))
        out = deployers.deployer_archive(DEP1)
        rec = out["records"][0]
        self.assertEqual(rec["launches_seen"], 9)
        self.assertEqual(rec["hits_2x"], 2)
        self.assertEqual(rec["regression_conflicts"], 1)
        # The max combination may never have existed as an actual
        # snapshot: a regression conflict makes the row partial so
        # no consumer treats it as a verified record.
        self.assertTrue(rec["partial"])
        self.assertEqual(rec["segments_merged"], 1)
        # Same result when the stale snapshot is read after
        # compaction, and when it arrives first.
        with patch.object(deployers, "_ARCHIVE_MAX_BYTES", 10):
            deployers._archive_deployer(
                DEP2, {"first_seen": 1.0, "launches_seen": 1}, 302.0)
        out2 = deployers.deployer_archive(DEP1)
        self.assertEqual(out2["records"][0]["launches_seen"], 9)
        self.assertEqual(
            out2["records"][0].get("regression_conflicts"), 1)

    def test_malformed_archived_ts_isolated_not_poisoning(self):
        self.arc.write_text(
            json.dumps({"v": 1, "record_type": "deployer_archive",
                        "segment_id": "bad:1", "deployer": DEP1,
                        "archived_ts": "not-a-number",
                        "launches_seen": 7})
            + "\n"
            + json.dumps({"v": 1, "record_type": "deployer_archive",
                          "segment_id": "ok:1", "deployer": DEP2,
                          "archived_ts": 5.0, "launches_seen": 2})
            + "\n")
        out = deployers.deployer_archive()
        # Good deployers stay visible; the poison record is counted.
        self.assertEqual(len(out["records"]), 1)
        self.assertEqual(out["records"][0]["deployer"], DEP2)
        self.assertEqual(
            out["archive_status"].get("unusable_records"), 1)
        # Compaction drops it with a persistent count, not silently.
        with patch.object(deployers, "_ARCHIVE_MAX_BYTES", 10):
            deployers._archive_deployer(
                DEP3, {"first_seen": 1.0, "launches_seen": 1}, 10.0)
        out2 = deployers.deployer_archive()
        self.assertEqual(len(out2["records"]), 2)
        self.assertGreaterEqual(
            out2["archive_status"].get("dropped_unreadable_total"),
            1)

    def test_missing_first_seen_fails_closed_no_fabricated_id(self):
        self.assertFalse(deployers._archive_deployer(
            DEP1, {"launches_seen": 4}, 100.0))
        self.assertFalse(self.arc.exists())
        # Entry retained on eviction attempt: nothing is dropped.
        deployers.record_launch(DEP2)
        deployers.record_launch(DEP3)
        hist = deployers.load_registry()
        dep1 = {"launches_seen": 4}  # no first_seen
        hist[DEP1] = dep1
        deployers.save_registry(hist)
        deployers.record_launch("DeplDDDD44444444444444444444444444444444444")
        self.assertIn(DEP1, deployers.load_registry())

    def test_blocked_candidate_skipped_eviction_continues(self):
        # Unarchivable oldest entry (no first_seen): skipped and
        # counted, later eligible entries still evicted in a bounded
        # pass, never silently deleted; all-blocked pass records an
        # explicit overflow event.
        deployers.save_registry({
            "DepBAD": {"launches_seen": 9, "hits_2x": 0, "rugs": 9,
                       "dead": 0, "first_seen": None,
                       "last_seen": 1.0, "launch_ts": []}})
        deployers.record_launch(DEP2)
        deployers.record_launch(DEP3)  # over cap: only bad candidate
        hist = deployers.load_registry()
        self.assertIn("DepBAD", hist)
        self.assertIn(DEP2, hist)  # slice covered only the bad one
        out = deployers.deployer_archive()
        self.assertGreaterEqual(
            out["archive_status"].get("eviction_overflow_events"), 1)
        deployers.record_launch(DEP1)  # pass widens: DEP2 evicted
        hist = deployers.load_registry()
        self.assertIn("DepBAD", hist)
        self.assertNotIn(DEP2, hist)
        out2 = deployers.deployer_archive()
        self.assertGreaterEqual(
            out2["archive_status"].get("eviction_skips_blocked"), 1)
        recs = {r["deployer"]: r for r in out2["records"]}
        self.assertIn(DEP2, recs)

    def test_bool_and_nan_timestamps_counted_unusable(self):
        self.arc.write_text(
            json.dumps({"v": 1, "record_type": "deployer_archive",
                        "segment_id": "b:1", "deployer": DEP1,
                        "archived_ts": True, "launches_seen": 1})
            + "\n"
            + json.dumps({"v": 1, "record_type": "deployer_archive",
                          "segment_id": "n:1", "deployer": DEP2,
                          "archived_ts": float("nan"),
                          "launches_seen": 1})
            + "\n")
        out = deployers.deployer_archive()
        self.assertEqual(out["records"], [])
        self.assertEqual(
            out["archive_status"].get("unusable_records"), 2)

    def test_many_segments_survive_repeated_compactions(self):
        with patch.object(deployers, "_ARCHIVE_MAX_BYTES", 10):
            for i in range(60):
                deployers._archive_deployer(
                    DEP1, {"first_seen": float(i), "launches_seen": 1,
                           "hits_2x": 0, "rugs": 0, "dead": 0},
                    100.0 + i)
            deployers._archive_deployer(
                DEP2, {"first_seen": 1.0, "launches_seen": 1}, 500.0)
        out = deployers.deployer_archive(DEP1)
        rec = out["records"][0]
        self.assertEqual(rec["launches_seen"], 60)
        self.assertEqual(rec["segments_merged"], 60)
        self.assertEqual(len(rec["segment_ids"]), 60)
        # A retry of an OLD segment id after many compactions still
        # upserts that id, never adds a phantom 61st segment.
        deployers._archive_deployer(
            DEP1, {"first_seen": 0.0, "launches_seen": 5,
                   "hits_2x": 0, "rugs": 0, "dead": 0}, 600.0)
        out2 = deployers.deployer_archive(DEP1)
        rec2 = out2["records"][0]
        self.assertEqual(rec2["segments_merged"], 60)
        self.assertEqual(rec2["launches_seen"], 64)

    def test_torn_tail_repaired_under_lock_before_append(self):
        self.arc.write_text(
            json.dumps({"v": 1, "record_type": "deployer_archive",
                        "segment_id": "x:1", "deployer": DEP2,
                        "archived_ts": 5, "launches_seen": 2})
            + "\n{\"partial\"")  # crash mid-append, no newline
        self.assertTrue(deployers._archive_deployer(
            DEP1, {"first_seen": 1.0, "launches_seen": 1}, 10.0))
        out = deployers.deployer_archive()
        self.assertEqual(len(out["records"]), 2)
        self.assertEqual(
            out["archive_status"].get("torn_tail_repairs"), 1)
        self.assertEqual(out["archive_status"]["unreadable_lines"], 0)

    def test_compaction_merges_and_counts_dropped_corruption(self):
        self.arc.write_text(
            json.dumps({"v": 1, "record_type": "deployer_archive",
                        "segment_id": "a:1", "deployer": DEP1,
                        "archived_ts": 100.0, "launches_seen": 5,
                        "hits_2x": 0, "rugs": 0, "dead": 0})
            + "\ngarbage-line\n")
        with patch.object(deployers, "_ARCHIVE_MAX_BYTES", 10):
            deployers._archive_deployer(
                DEP1, {"first_seen": 200.0, "launches_seen": 1,
                       "hits_2x": 1, "rugs": 0, "dead": 0}, 300.0)
        out = deployers.deployer_archive(DEP1)
        rec = out["records"][0]
        self.assertEqual(rec["launches_seen"], 6)  # merged, not erased
        self.assertEqual(rec["segments_merged"], 2)
        self.assertEqual(
            out["archive_status"].get("dropped_unreadable_total"), 1)
        self.assertEqual(out["archive_status"]["unreadable_lines"], 0)

    def test_compaction_failure_visible_in_status(self):
        self.arc.write_text(
            json.dumps({"v": 1, "record_type": "deployer_archive",
                        "segment_id": "a:1", "deployer": DEP1,
                        "archived_ts": 100.0, "launches_seen": 5})
            + "\n")
        real_replace = os.replace

        def boom(src, dst):
            if str(dst) == str(self.arc):
                raise OSError("simulated compaction failure")
            return real_replace(src, dst)

        with patch.object(deployers, "_ARCHIVE_MAX_BYTES", 10), \
                patch.object(deployers.os, "replace", side_effect=boom):
            self.assertTrue(deployers._archive_deployer(
                DEP2, {"first_seen": 1.0, "launches_seen": 1}, 10.0))
        out = deployers.deployer_archive()
        self.assertEqual(
            out["archive_status"].get("compaction_failures"), 1)
        # The appended record is still durable despite the failure.
        self.assertTrue(out["archive_status"]["available"])

    def test_concurrent_appenders_all_durable(self):
        import threading
        errors = []

        def work(i):
            try:
                deployers._archive_deployer(
                    "DepT%d" % i, {"first_seen": float(i),
                                   "launches_seen": 1}, 100.0 + i)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        out = deployers.deployer_archive()
        self.assertEqual(len(out["records"]), 8)

    def test_eviction_fail_closed_when_archive_fails(self):
        deployers.record_launch(DEP1)
        deployers.record_launch(DEP2)
        with patch.object(deployers, "_archive_append",
                          return_value=False):
            deployers.record_launch(DEP3)
        hist = deployers.load_registry()
        self.assertIn(DEP1, hist)  # never silently dropped
        self.assertEqual(len(hist), 3)

    def test_archive_unreadable_counted_not_clean_empty(self):
        self.arc.write_text(
            json.dumps({"v": 1, "record_type": "deployer_archive",
                        "segment_id": "x:1", "deployer": DEP1,
                        "archived_ts": 5, "launches_seen": 4})
            + "\nnot-json\n")
        out = deployers.deployer_archive()
        self.assertEqual(len(out["records"]), 1)
        self.assertEqual(out["archive_status"]["unreadable_lines"], 1)
        # Missing file cannot prove zero history: labeled unknown.
        self.arc.unlink()
        out2 = deployers.deployer_archive()
        self.assertEqual(out2["records"], [])
        self.assertFalse(out2["archive_status"]["available"])
        self.assertEqual(out2["archive_status"]["reason"],
                         "unknown_missing")

    def test_compaction_merges_segments_per_deployer(self):
        with patch.object(deployers, "_ARCHIVE_MAX_BYTES", 10):
            deployers._archive_deployer(
                DEP1, {"first_seen": 1.0, "launches_seen": 1}, 100.0)
            deployers._archive_deployer(
                DEP1, {"first_seen": 50.0, "launches_seen": 9,
                       "rugs": 2}, 200.0)
            deployers._archive_deployer(
                DEP2, {"first_seen": 2.0, "launches_seen": 3}, 150.0)
        out = deployers.deployer_archive()
        self.assertEqual(len(out["records"]), 2)
        by_dep = {r["deployer"]: r for r in out["records"]}
        self.assertEqual(by_dep[DEP1]["launches_seen"], 10)
        self.assertEqual(by_dep[DEP1]["segments_merged"], 2)
        self.assertEqual(by_dep[DEP1]["archived_ts"], 200.0)
        # Corrupt counter segment: merged row marked partial.
        self.arc.write_text(
            json.dumps({"v": 1, "record_type": "deployer_archive",
                        "segment_id": "a:1", "deployer": DEP1,
                        "archived_ts": 100.0, "launches_seen": "bad"})
            + "\n")
        with patch.object(deployers, "_ARCHIVE_MAX_BYTES", 10):
            deployers._archive_deployer(
                DEP1, {"first_seen": 9.0, "launches_seen": 2}, 300.0)
        out2 = deployers.deployer_archive(DEP1)
        self.assertTrue(out2["records"][0]["partial"])

    def test_outcome_ts_bounded_history(self):
        deployers.record_launch(DEP1)
        for i in range(35):
            deployers.record_outcome("mint%d" % i, DEP1, "rug")
        entry = deployers.load_registry()[DEP1]
        self.assertEqual(len(entry["outcome_ts"]), 30)
        self.assertEqual(entry["outcome_ts"][-1]["outcome"], "rug")
        self.assertEqual(entry["rugs"], 35)  # counters never truncated


if __name__ == "__main__":
    unittest.main()
