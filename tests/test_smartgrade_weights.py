"""Grade-weighted smart wallets (2026-10-09).

The sniper's smart-wallet signal used to count every tracked wallet
equally. Now each buying wallet is weighted by its reconciled grade
(median peak multiple, shrunk toward 1.0, clamped [0.5, 1.5]).

Honesty rules under test:
- thin/unknown history -> 1.0 (neutral, never punished)
- small samples stay near 1.0 (shrinkage)
- clamped to [0.5, 1.5] (modest by design)
- score-only: the rank bar and safety gate are untouched
"""

import time
import unittest
import unittest.mock
from types import SimpleNamespace

from daemon.launch_scanner import sniper, smartgrade


def _grades(wallet, peak_mults, now=None):
    now = now if now is not None else time.time()
    return {wallet: {"buys": [
        {"mint": "M%d" % i, "buy_ts": now - 3600,
         "anchor_mcap": 20000.0, "peak_mult": m,
         "hit_2x": m >= 2.0, "last_check_ts": now}
        for i, m in enumerate(peak_mults)
    ], "last_active_ts": now}}


def _degen(**kw):
    base = dict(sniper_enabled=True, sniper_tier_threshold=50.0,
                sniper_weight_smart=35.0, sniper_smart_grade_enabled=True,
                smart_grade_min_graded=5, smart_grade_shrink_k=10.0,
                smart_grade_window_days=30.0)
    base.update(kw)
    return SimpleNamespace(**base)


class WalletWeightTest(unittest.TestCase):
    def test_unknown_wallet_is_neutral(self):
        self.assertEqual(
            smartgrade.wallet_weight("NOBODY", _degen(), grades={}), 1.0)

    def test_thin_history_is_neutral(self):
        g = _grades("W1", [0.5, 0.6, 0.7])  # only 3 graded (< 5)
        self.assertEqual(
            smartgrade.wallet_weight("W1", _degen(), grades=g), 1.0)

    def test_bad_wallet_weighs_less_than_one(self):
        # 17 buys, median peak 0.73x: shrunk = (17*.73+10)/27 ~= 0.83
        g = _grades("W1", [0.73] * 17)
        w = smartgrade.wallet_weight("W1", _degen(), grades=g)
        self.assertLess(w, 1.0)
        self.assertGreater(w, 0.5)
        self.assertAlmostEqual(w, (17 * 0.73 + 10.0) / 27.0, places=2)

    def test_good_wallet_weighs_more_than_one(self):
        # 50 buys, median peak 1.5x: shrunk = (50*1.5+10)/60 ~= 1.42
        g = _grades("W1", [1.5] * 50)
        w = smartgrade.wallet_weight("W1", _degen(), grades=g)
        self.assertGreater(w, 1.0)
        self.assertLessEqual(w, 1.5)

    def test_clamp_bounds(self):
        g = _grades("WHI", [9.0] * 50)
        self.assertEqual(
            smartgrade.wallet_weight("WHI", _degen(), grades=g), 1.5)
        g = _grades("WLO", [0.05] * 50)
        self.assertEqual(
            smartgrade.wallet_weight("WLO", _degen(), grades=g), 0.5)

    def test_never_raises(self):
        self.assertEqual(smartgrade.wallet_weight(None), 1.0)
        self.assertEqual(smartgrade.wallet_weight("W", grades=None), 1.0)
        self.assertEqual(
            smartgrade.wallet_weight("W", grades={"W": "garbage"}), 1.0)

    def test_weights_cache(self):
        smartgrade.clear_weights_cache()
        try:
            with unittest.mock.patch.object(
                    smartgrade, "load_grades",
                    return_value=_grades("W1", [1.5] * 50)) as lg:
                w1 = smartgrade.wallet_weights(_degen())
                w2 = smartgrade.wallet_weights(_degen())
            # Disk read once; second call served from cache.
            self.assertEqual(lg.call_count, 1)
            self.assertEqual(w1, w2)
            self.assertGreater(w1["W1"], 1.0)
        finally:
            smartgrade.clear_weights_cache()


class WeightedRankTest(unittest.TestCase):
    def _rank(self, eff, raw=None):
        signals = {"smart_buys": eff}
        if raw is not None:
            signals["smart_buys_raw"] = raw
        return sniper.rank_candidate(_degen(), signals)

    def test_two_effective_is_full_weight(self):
        # Backward compat: 2 neutral wallets -> 35.0, exactly as before.
        r = self._rank(2.0, raw=2)
        self.assertEqual(r["score"], 35.0)

    def test_one_effective_is_half_weight(self):
        r = self._rank(1.0, raw=1)
        self.assertEqual(r["score"], 17.5)

    def test_proven_single_wallet_beats_raw_single(self):
        # One proven wallet (1.5 eff) outscores one raw wallet.
        r = self._rank(1.5, raw=1)
        self.assertEqual(r["score"], 26.2)  # 35 * 0.75, rounded
        self.assertIn("grade-weighted", " ".join(r["reasons"]))

    def test_two_unproven_wallets_no_longer_full(self):
        # The fix: two bad wallets (0.5 each = 1.0 eff) get half, not
        # the full 35 the old headcount gave them.
        r = self._rank(1.0, raw=2)
        self.assertEqual(r["score"], 17.5)

    def test_zero_stays_zero(self):
        r = self._rank(0.0, raw=0)
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["reasons"], [])

    def test_caps_at_full_weight(self):
        r = self._rank(5.0, raw=4)
        self.assertEqual(r["score"], 35.0)

    def test_disabled_grading_falls_back_to_count(self):
        # sniper_smart_grade_enabled=False -> collect_signals uses the
        # raw count; the rank math is identical at weight 1.0.
        r = sniper.rank_candidate(
            _degen(sniper_smart_grade_enabled=False),
            {"smart_buys": 2, "smart_buys_raw": 2})
        self.assertEqual(r["score"], 35.0)


class WeightedCollectTest(unittest.TestCase):
    def test_collect_weights_buyers(self):
        smartgrade.clear_weights_cache()
        try:
            state = {"smart_activity": {"M1": ["W_GOOD", "W_BAD"]}}
            degen = _degen()
            with unittest.mock.patch.object(
                    smartgrade, "wallet_weights",
                    return_value={"W_GOOD": 1.4, "W_BAD": 0.6}):
                sig = sniper.collect_signals(
                    SimpleNamespace(), state, degen, "M1", None)
            self.assertEqual(sig["smart_buys_raw"], 2)
            self.assertAlmostEqual(sig["smart_buys"], 2.0, places=1)
        finally:
            smartgrade.clear_weights_cache()

    def test_collect_unknown_wallets_neutral(self):
        smartgrade.clear_weights_cache()
        try:
            state = {"smart_activity": {"M1": ["W_NEW"]}}
            with unittest.mock.patch.object(
                    smartgrade, "wallet_weights", return_value={}):
                sig = sniper.collect_signals(
                    SimpleNamespace(), state, _degen(), "M1", None)
            # Unknown wallet -> weight 1.0 -> same as raw count.
            self.assertEqual(sig["smart_buys"], 1.0)
            self.assertEqual(sig["smart_buys_raw"], 1)
        finally:
            smartgrade.clear_weights_cache()


if __name__ == "__main__":
    unittest.main()
