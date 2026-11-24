"""Tests for daemon/launch_scanner/breakouts.py.

Pure logic tests: series are hand-built, no network, no broker, no
real daemon state. Breakout detection must never have position
side-effects — it only writes "breakout_log" / "breakout_cooldown".
"""

import time
import unittest
from types import SimpleNamespace

from daemon.launch_scanner.breakouts import BreakoutDetector, log_trigger


def _degen(**kw):
    base = {
        "breakout_enabled": True,
        "breakout_quiet_polls": 4,
        "breakout_quiet_vol_max_usd": 500.0,
        "breakout_quiet_buys_max": 5.0,
        "breakout_vol_mult": 5.0,
        "breakout_vol_floor_usd": 2000.0,
        "breakout_holder_pct": 20.0,
        "breakout_require_holders": False,
        "breakout_cooldown_hours": 6.0,
        "breakout_log_max": 200,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _obs(ts, vol, buys, holders=None, mcap=100000.0):
    # (ts, mcap, price, vol_m5, buys, sells, liq, holders)
    return (float(ts), mcap, 0.001, float(vol), float(buys), 1.0,
            50000.0, float(holders) if holders is not None else None)


def _quiet_series(n=4, vol=300.0, buys=3.0, holders=100.0):
    return [_obs(1000 + i * 60, vol, buys, holders) for i in range(n)]


class QuietToBreakoutTest(unittest.TestCase):
    def test_volume_and_holder_breakout_fires(self):
        det = BreakoutDetector(_degen())
        series = _quiet_series() + [_obs(1300, 3000.0, 25.0, 130.0)]
        trig = det.update("MINT1", series, symbol="BRK")
        self.assertIsNotNone(trig)
        self.assertEqual(trig["trigger"], "volume_holder_breakout")
        self.assertEqual(trig["holder_evidence"], "confirmed")
        self.assertAlmostEqual(trig["vol_mult"], 10.0)
        self.assertEqual(trig["holder_growth_pct"], 30.0)

    def test_volume_only_breakout_logged_as_unknown(self):
        det = BreakoutDetector(_degen())
        series = _quiet_series(holders=None) + [_obs(1300, 3000.0, 25.0)]
        trig = det.update("MINT1", series)
        self.assertIsNotNone(trig)
        self.assertEqual(trig["trigger"], "volume_breakout")
        self.assertEqual(trig["holder_evidence"], "unknown")

    def test_holder_flat_blocks_even_with_volume(self):
        # The AND is strict whenever holder data covers the window.
        det = BreakoutDetector(_degen())
        series = _quiet_series() + [_obs(1300, 3000.0, 25.0, 105.0)]
        self.assertIsNone(det.update("MINT1", series))

    def test_require_holders_blocks_unknown(self):
        det = BreakoutDetector(_degen(breakout_require_holders=True))
        series = _quiet_series(holders=None) + [_obs(1300, 3000.0, 25.0)]
        self.assertIsNone(det.update("MINT1", series))

    def test_below_vol_floor_no_trigger(self):
        det = BreakoutDetector(_degen())
        series = _quiet_series() + [_obs(1300, 1500.0, 25.0, 130.0)]
        self.assertIsNone(det.update("MINT1", series))

    def test_below_vol_mult_no_trigger(self):
        det = BreakoutDetector(_degen())
        # Baseline at the quiet ceiling (500): latest 2000 clears the
        # floor but is only 4x < 5x -> no trigger.
        series = _quiet_series(vol=500.0) + [_obs(1300, 2000.0, 25.0,
                                                  130.0)]
        self.assertIsNone(det.update("MINT1", series))

    def test_not_quiet_baseline_no_trigger(self):
        det = BreakoutDetector(_degen())
        busy = [_obs(1000 + i * 60, 5000.0, 40.0, 500.0)
                for i in range(4)]
        trig = det.update("MINT1", busy + [_obs(1300, 30000.0, 200.0,
                                                650.0)])
        self.assertIsNone(trig)

    def test_too_few_polls_no_trigger(self):
        det = BreakoutDetector(_degen())
        series = _quiet_series(n=2) + [_obs(1200, 3000.0, 25.0, 130.0)]
        self.assertIsNone(det.update("MINT1", series))

    def test_garbage_never_raises(self):
        det = BreakoutDetector(_degen())
        self.assertIsNone(det.update("MINT1", None))
        self.assertIsNone(det.update("MINT1", []))
        self.assertIsNone(det.update("MINT1", [None, "junk", (1, 2)]))
        self.assertIsNone(det.update("", _quiet_series()))

    def test_disabled_no_trigger(self):
        det = BreakoutDetector(_degen(breakout_enabled=False))
        series = _quiet_series() + [_obs(1300, 3000.0, 25.0, 130.0)]
        self.assertIsNone(det.update("MINT1", series))

    def test_trigger_carries_measurement_inputs(self):
        det = BreakoutDetector(_degen())
        series = _quiet_series() + [_obs(1300, 3000.0, 25.0, 130.0,
                                         mcap=250000.0)]
        trig = det.update("MINT1", series, symbol="BRK", chain="solana")
        for key in ("baseline_vol_m5", "breakout_vol_m5",
                    "baseline_buys_m5", "breakout_buys_m5",
                    "holder_growth_pct", "baseline_holders",
                    "latest_holders", "mcap", "quiet_polls"):
            self.assertIn(key, trig)


class CooldownTest(unittest.TestCase):
    def test_per_coin_cooldown(self):
        det = BreakoutDetector(_degen())
        state = {}
        now = time.time()
        self.assertTrue(det.cooldown_ok(state, "MINT1", now))
        det.stamp_cooldown(state, "MINT1", now)
        self.assertFalse(det.cooldown_ok(state, "MINT1", now + 60))
        self.assertTrue(det.cooldown_ok(state, "MINT1",
                                        now + 6 * 3600 + 1))
        # other coins unaffected
        self.assertTrue(det.cooldown_ok(state, "MINT2", now + 60))

    def test_cooldown_never_raises(self):
        det = BreakoutDetector(_degen())
        self.assertTrue(det.cooldown_ok({}, "M", time.time()))
        det.stamp_cooldown({}, "M", time.time())


class LogTriggerTest(unittest.TestCase):
    def _trig(self):
        return {
            "mint": "MINT1", "symbol": "BRK", "chain": "solana",
            "ts": 1234.0, "trigger": "volume_holder_breakout",
            "vol_mult": 10.0, "baseline_vol_m5": 300.0,
            "breakout_vol_m5": 3000.0, "baseline_buys_m5": 3.0,
            "breakout_buys_m5": 25.0, "holder_evidence": "confirmed",
            "holder_growth_pct": 30.0, "baseline_holders": 100.0,
            "latest_holders": 130.0, "mcap": 250000.0,
            "quiet_polls": 4, "fp_verdict": "WARMING",
            "safety_score": 55.0,
        }

    def test_log_row_records_card_outcome(self):
        state = {}
        row = log_trigger(state, _degen(), self._trig(),
                          card_fired=True)
        self.assertTrue(row["card_fired"])
        self.assertIsNone(row["suppressed"])
        self.assertEqual(len(state["breakout_log"]), 1)

    def test_suppressed_reason_recorded(self):
        state = {}
        row = log_trigger(state, _degen(), self._trig(),
                          card_fired=False,
                          suppressed="below-safety-floor")
        self.assertFalse(row["card_fired"])
        self.assertEqual(row["suppressed"], "below-safety-floor")

    def test_log_is_bounded(self):
        state = {}
        d = _degen(breakout_log_max=30)
        for i in range(50):
            t = self._trig()
            t["mint"] = "M%d" % i
            log_trigger(state, d, t, card_fired=False,
                        suppressed="x")
        self.assertEqual(len(state["breakout_log"]), 30)
        self.assertEqual(state["breakout_log"][-1]["mint"], "M49")

    def test_log_has_twenty_row_floor(self):
        # A tiny/accidental knob can't shrink the measurement log to
        # uselessness: minimum 20 rows are always kept.
        state = {}
        d = _degen(breakout_log_max=5)
        for i in range(30):
            t = self._trig()
            t["mint"] = "M%d" % i
            log_trigger(state, d, t, card_fired=False,
                        suppressed="x")
        self.assertEqual(len(state["breakout_log"]), 20)

    def test_no_position_side_effects(self):
        # The breakout module must never create position/broker state.
        state = {}
        log_trigger(state, _degen(), self._trig(), card_fired=True)
        det = BreakoutDetector(_degen())
        det.stamp_cooldown(state, "MINT1", time.time())
        for key in ("positions", "degen_positions", "broker",
                    "paper_entries", "entries"):
            self.assertNotIn(key, state)
        self.assertEqual(set(state.keys()),
                         {"breakout_log", "breakout_cooldown"})

    def test_log_never_raises(self):
        row = log_trigger({}, _degen(), {}, card_fired=False)
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
