"""Wiring tests: PumpPortal sources -> scanner pipeline (2a/2b).

Covers LaunchScanner._qualify for the new sources and the migration
annotation (PumpSwap graduation coverage). No network, no daemon
state; never touches the real ~/.forex-agent tree.
"""

import time
import unittest
from types import SimpleNamespace

from daemon.launch_scanner import LaunchScanner, milestones
from daemon.launch_scanner.pumpportal import parse_migration, parse_new_token


def _degen(**kw):
    base = dict(min_usd_market_cap=10000.0, max_age_minutes=10.0,
                velocity_multiplier=3.0, velocity_window_minutes=10.0,
                early_safety_flash=False)
    base.update(kw)
    return SimpleNamespace(**base)


class QualifyPumpPortalTest(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _entry(self):
        return {"first_ts": time.time(), "mcap": [], "alerted": False}

    def test_new_token_qualifies_like_logstream(self):
        c = parse_new_token({
            "signature": "s1", "mint": "NEWMINT1",
            "traderPublicKey": "t", "txType": "create",
            "name": "N", "symbol": "N", "uri": ""})
        self.assertEqual(
            self.sc._qualify(_degen(), c, self._entry(), time.time()),
            "logstream")

    def test_migration_qualifies_like_logstream(self):
        # A graduation carries no USD mcap on the WS event — it must
        # NOT be held to the "graduated" mcap floor; the judge prices
        # it from the new pool's DexScreener stats.
        c = parse_migration({
            "signature": "s2", "mint": "MIGMINT1",
            "traderPublicKey": "t", "txType": "migrate",
            "name": "M", "symbol": "M", "uri": "", "pool": "pumpswap"})
        self.assertEqual(c["usd_market_cap"], 0.0)
        self.assertEqual(
            self.sc._qualify(_degen(), c, self._entry(), time.time()),
            "logstream")

    def test_unknown_source_still_rejected(self):
        c = {"mint": "X", "symbol": "", "name": "", "chain": "solana",
             "source": "something-else", "created_ts": 0,
             "usd_market_cap": 0, "extra": {}, "url": ""}
        self.assertIsNone(
            self.sc._qualify(_degen(), c, self._entry(), time.time()))


class NoteMigrationTest(unittest.TestCase):
    def test_annotates_seen_entry(self):
        state = {"seen": {"MIGMINT1": {"first_ts": 1.0, "mcap": [],
                                      "alerted": True}}}
        LaunchScanner._note_migration(
            state, "MIGMINT1",
            {"migration_pool": "pumpswap", "market_id": "MKT1"})
        mig = state["seen"]["MIGMINT1"]["extra"]["migration"]
        self.assertEqual(mig["pool"], "pumpswap")
        self.assertEqual(mig["market_id"], "MKT1")
        self.assertIn("ts", mig)

    def test_annotates_milestone_record(self):
        state = {}
        degen = SimpleNamespace()
        milestones.track_signal(state, degen, {
            "event": "launch.early", "mint": "MIGMINT1",
            "symbol": "M", "chain": "solana",
            "usd_market_cap": 50000.0})
        LaunchScanner._note_migration(
            state, "MIGMINT1", {"migration_pool": "pumpswap"})
        rec = milestones.tracked(state)["MIGMINT1"]
        self.assertEqual(rec["migration_pool"], "pumpswap")
        # The anchor is untouched — annotation only, no retracking.
        self.assertEqual(rec["anchor"], 50000.0)

    def test_unknown_mint_is_noop(self):
        state = {"seen": {}}
        LaunchScanner._note_migration(state, "NEVERTSEEN",
                                      {"migration_pool": "pumpswap"})
        self.assertEqual(state["seen"], {})

    def test_never_raises(self):
        LaunchScanner._note_migration({}, "M", {})
        LaunchScanner._note_migration({"seen": None}, "M", None)
        LaunchScanner._note_migration(
            {"seen": {"M": "not-a-dict"}}, "M", {"migration_pool": "x"})


if __name__ == "__main__":
    unittest.main()
