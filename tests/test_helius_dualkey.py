"""Dual Helius keys (2026-10-10): a research-only second key.

The wallet-discovery sweep saturated the main key's free-tier budget
(2026-10-10: 429s on single probes). A second key gets its own
rate-limit bucket so research never contends with live signals.

Rules under test:
- one guard per key: tripping the discovery bucket never touches the
  live bucket and vice versa
- module-level functions keep working (backward compatible)
- discovery key from env HELIUS_API_KEY_DISCOVERY or the 0600 file
- discovery_rpc falls back to the main key's _rpc when no research key
  is configured (with the contention risk logged, not hidden)
"""

import os
import tempfile
import unittest
import unittest.mock

# Isolated home (2026-11-22): the suite must not read live fleet
# state — with auto-quiet active in the real home, pacer_acquire
# refused and test_discovery_rpc_uses_own_bucket failed. Same
# pattern as test_armed_watch_guard.py.
_TMP_HOME = tempfile.mkdtemp(prefix="helius-dualkey-test-")
os.environ["FOREX_AGENT_HOME"] = _TMP_HOME

from daemon.launch_scanner import helius_ratelimit, smartmoney


class RateGuardTest(unittest.TestCase):
    def setUp(self):
        helius_ratelimit.reset_for_tests()

    def test_guards_are_independent(self):
        a = helius_ratelimit.new_guard()
        b = helius_ratelimit.new_guard()
        a.report_ratelimited()
        self.assertGreater(a.cooldown_remaining(), 0)
        self.assertEqual(b.cooldown_remaining(), 0)
        # and the shared module-level bucket is untouched too
        self.assertEqual(helius_ratelimit.cooldown_remaining(), 0)

    def test_backoff_ladder_per_guard(self):
        g = helius_ratelimit.new_guard()
        first = g.report_ratelimited()
        # force expiry then trip again: backoff doubles per guard
        g._cooldown_until = 0
        second = g.report_ratelimited()
        self.assertEqual(second, first * 2.0)
        g.report_success()
        g._cooldown_until = 0
        self.assertEqual(g.report_ratelimited(), first)

    def test_module_functions_still_work(self):
        helius_ratelimit.report_ratelimited()
        self.assertGreater(helius_ratelimit.cooldown_remaining(), 0)
        helius_ratelimit.report_success()
        helius_ratelimit.reset_for_tests()
        self.assertEqual(helius_ratelimit.cooldown_remaining(), 0)


class DiscoveryKeyTest(unittest.TestCase):
    def setUp(self):
        smartmoney.reset_key_caches()
        # Fresh guard + pacer singletons on the isolated home, so a
        # singleton created earlier (e.g. by another module in a
        # shared pytest process) cannot point at live fleet state.
        helius_ratelimit.reset_for_tests()
        helius_ratelimit.reset_pacer_for_tests()
        self._env = dict(os.environ)
        os.environ.pop("HELIUS_API_KEY_DISCOVERY", None)
        os.environ.pop("HELIUS_API_KEY", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        smartmoney.reset_key_caches()

    def test_no_key_anywhere_is_none(self):
        with unittest.mock.patch.object(
                smartmoney, "_DISCOVERY_KEY_FILE") as f:
            f.read_text.side_effect = OSError("no file")
            self.assertIsNone(smartmoney.discovery_api_key())
        self.assertFalse(smartmoney.discovery_key_available())

    def test_env_key_wins(self):
        os.environ["HELIUS_API_KEY_DISCOVERY"] = "disc-key-123"
        self.assertEqual(smartmoney.discovery_api_key(), "disc-key-123")
        self.assertTrue(smartmoney.discovery_key_available())

    def test_file_key_used_when_no_env(self):
        with unittest.mock.patch.object(smartmoney, "_DISCOVERY_KEY_FILE") as f:
            f.read_text.return_value = "  file-key-456\n"
            self.assertEqual(smartmoney.discovery_api_key(), "file-key-456")

    def test_key_cached(self):
        os.environ["HELIUS_API_KEY_DISCOVERY"] = "k1"
        self.assertEqual(smartmoney.discovery_api_key(), "k1")
        os.environ["HELIUS_API_KEY_DISCOVERY"] = "k2"
        # still cached until reset
        self.assertEqual(smartmoney.discovery_api_key(), "k1")
        smartmoney.reset_key_caches()
        self.assertEqual(smartmoney.discovery_api_key(), "k2")

    def test_discovery_rpc_falls_back_without_key(self):
        with unittest.mock.patch.object(
                smartmoney, "_DISCOVERY_KEY_FILE") as f, \
             unittest.mock.patch.object(
                 smartmoney, "_rpc", return_value={"slot": 1}) as main_rpc:
            f.read_text.side_effect = OSError("no file")
            out = smartmoney.discovery_rpc("getSlot", [])
        self.assertEqual(out, {"slot": 1})
        main_rpc.assert_called_once_with("getSlot", [])

    def test_discovery_rpc_uses_own_bucket(self):
        os.environ["HELIUS_API_KEY_DISCOVERY"] = "disc-key"
        # trip the MAIN bucket: discovery must stay clear
        helius_ratelimit.report_ratelimited()
        try:
            fake_resp = unittest.mock.MagicMock()
            fake_resp.read.return_value = b'{"result": 99}'
            fake_ctx = unittest.mock.MagicMock()
            fake_ctx.__enter__.return_value = fake_resp
            with unittest.mock.patch(
                    "urllib.request.urlopen",
                    return_value=fake_ctx) as uo:
                out = smartmoney.discovery_rpc("getSlot", [])
            self.assertEqual(out, 99)
            called_url = uo.call_args[0][0].full_url
            self.assertIn("api-key=disc-key", called_url)
        finally:
            helius_ratelimit.reset_for_tests()


if __name__ == "__main__":
    unittest.main()
