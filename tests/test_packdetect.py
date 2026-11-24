"""Wallet pack detection (packdetect.py) — 2026-10-10.

Packs = wallets that habitually buy the same mints in the same poll
window (likely one operator / coordinated group). Feature-flagged:
off -> measure -> on. Measure mode logs detections but never touches
scoring; on mode adds a capped bonus in rank_candidate.

No network: grade weights stubbed via unittest.mock.
"""

import time
import unittest
import unittest.mock
from types import SimpleNamespace

from daemon.launch_scanner import packdetect
from daemon.launch_scanner import sniper


def _degen(**kw):
    base = dict(
        packdetect_mode="measure",
        packdetect_min_cobuys=2,
        packdetect_min_members=2,
        sniper_pack_bonus_max=8.0,
        sniper_enabled=True,
        sniper_tier_threshold=50.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _fresh(*mints_wallets):
    """_fresh(("M1", ["A", "B"]), ...) -> {mint: [wallets]}."""
    return {m: list(w) for m, w in mints_wallets}


class ModeTest(unittest.TestCase):
    def test_unknown_mode_fails_safe_to_measure(self):
        self.assertEqual(packdetect.mode(_degen(packdetect_mode="bogus")),
                         "measure")

    def test_off_records_nothing(self):
        state = {}
        n = packdetect.record_fresh(state, _degen(packdetect_mode="off"),
                                    _fresh(("M1", ["A", "B"])))
        self.assertEqual(n, 0)
        self.assertNotIn(packdetect.EVENTS_KEY, state)

    def test_measure_records_events_and_cobuys(self):
        state = {}
        n = packdetect.record_fresh(state, _degen(),
                                    _fresh(("M1", ["A", "B", "C"])))
        self.assertEqual(n, 3)
        self.assertEqual(len(state[packdetect.EVENTS_KEY]), 3)
        # 3 wallets -> 3 pairs, each co-bought once.
        self.assertEqual(len(state[packdetect.COBUYS_KEY]), 3)


class PackFormationTest(unittest.TestCase):
    def _two_shared_mints(self, state, degen):
        # A+B co-buy on M1 and M2 -> pair count 2 -> pack forms.
        packdetect.record_fresh(state, degen, _fresh(("M1", ["A", "B"])))
        packdetect.record_fresh(state, degen, _fresh(("M2", ["A", "B"])))

    def test_pack_forms_at_min_cobuys(self):
        state, degen = {}, _degen()
        self._two_shared_mints(state, degen)
        packs = packdetect.recompute_packs(state, degen, force=True)
        self.assertEqual(len(packs), 1)
        pack = next(iter(packs.values()))
        self.assertEqual(sorted(pack["members"]), ["A", "B"])

    def test_no_pack_below_threshold(self):
        state, degen = {}, _degen()
        packdetect.record_fresh(state, degen, _fresh(("M1", ["A", "B"])))
        packs = packdetect.recompute_packs(state, degen, force=True)
        self.assertEqual(packs, {})

    def test_union_find_merges_chain(self):
        # A-B and B-C co-buy -> one pack {A, B, C}.
        state, degen = {}, _degen()
        for m in ("M1", "M2"):
            packdetect.record_fresh(state, degen, _fresh((m, ["A", "B"])))
        for m in ("M3", "M4"):
            packdetect.record_fresh(state, degen, _fresh((m, ["B", "C"])))
        packs = packdetect.recompute_packs(state, degen, force=True)
        self.assertEqual(len(packs), 1)
        members = next(iter(packs.values()))["members"]
        self.assertEqual(sorted(members), ["A", "B", "C"])

    def test_recompute_throttled(self):
        state, degen = {}, _degen()
        self._two_shared_mints(state, degen)
        p1 = packdetect.recompute_packs(state, degen, force=True)
        # Second call without force inside the throttle window returns
        # the cached object unchanged.
        p2 = packdetect.recompute_packs(state, degen)
        self.assertIs(p1, p2)


class SignalTest(unittest.TestCase):
    def _state_with_pack(self):
        state, degen = {}, _degen()
        for m in ("M1", "M2"):
            packdetect.record_fresh(state, degen, _fresh((m, ["A", "B"])))
        packdetect.recompute_packs(state, degen, force=True)
        return state, degen

    def test_signal_fires_with_two_members(self):
        state, degen = self._state_with_pack()
        state["smart_activity"] = {"M9": ["A", "B", "Z"]}
        sig = packdetect.pack_signal_for_mint(state, degen, "M9")
        self.assertGreater(sig["strength"], 0)
        self.assertEqual(sorted(sig["members"]), ["A", "B"])
        self.assertEqual(len(sig["packs"]), 1)

    def test_signal_silent_with_one_member(self):
        state, degen = self._state_with_pack()
        state["smart_activity"] = {"M9": ["A", "Z"]}
        sig = packdetect.pack_signal_for_mint(state, degen, "M9")
        self.assertEqual(sig["strength"], 0.0)

    def test_signal_off_mode(self):
        state, degen = self._state_with_pack()
        degen.packdetect_mode = "off"
        state["smart_activity"] = {"M9": ["A", "B"]}
        sig = packdetect.pack_signal_for_mint(state, degen, "M9")
        self.assertEqual(sig["strength"], 0.0)

    def test_grade_weighted_strength(self):
        state, degen = self._state_with_pack()
        state["smart_activity"] = {"M9": ["A", "B"]}
        with unittest.mock.patch(
                "daemon.launch_scanner.smartgrade.wallet_weights",
                return_value={"A": 1.5, "B": 1.5}):
            packdetect.recompute_packs(state, degen, force=True)
            sig = packdetect.pack_signal_for_mint(state, degen, "M9")
        # (1.5 + 1.5) / 2 -> capped at 1.0
        self.assertEqual(sig["strength"], 1.0)


class MeasureModeTest(unittest.TestCase):
    def test_measure_logs_but_scoring_untouched(self):
        state, degen = {}, _degen(packdetect_mode="measure")
        for m in ("M1", "M2"):
            packdetect.record_fresh(state, degen, _fresh((m, ["A", "B"])))
        packdetect.recompute_packs(state, degen, force=True)
        state["smart_activity"] = {"M9": ["A", "B"]}
        sig = packdetect.pack_signal_for_mint(state, degen, "M9")
        self.assertTrue(packdetect.measure(state, degen, "M9", sig))
        det = state[packdetect.DETECT_KEY]
        self.assertEqual(len(det), 1)
        self.assertEqual(det[0]["mint"], "M9")
        self.assertEqual(sorted(det[0]["members"]), ["A", "B"])
        # Scoring untouched in measure mode: no bonus, no reason.
        r = sniper.rank_candidate(degen, {"pack_strength": sig["strength"],
                                         "pack_members": 2})
        self.assertNotIn("pack buy",
                         " ".join(r.get("reasons") or []))

    def test_measure_no_signal_no_log(self):
        state, degen = {}, _degen()
        self.assertFalse(
            packdetect.measure(state, degen, "M9", {"strength": 0.0}))
        self.assertNotIn(packdetect.DETECT_KEY, state)


class OnModeTest(unittest.TestCase):
    def test_on_mode_adds_capped_bonus(self):
        degen = _degen(packdetect_mode="on")
        r = sniper.rank_candidate(degen, {"pack_strength": 1.0,
                                         "pack_members": 2})
        reasons = " ".join(r.get("reasons") or [])
        self.assertIn("pack buy", reasons)
        self.assertLessEqual(r["score"], 8.0)  # only the pack bonus

    def test_on_mode_scales_with_strength(self):
        degen = _degen(packdetect_mode="on")
        r_half = sniper.rank_candidate(degen, {"pack_strength": 0.5,
                                              "pack_members": 2})
        r_full = sniper.rank_candidate(degen, {"pack_strength": 1.0,
                                              "pack_members": 2})
        self.assertLess(r_half["score"], r_full["score"])
        self.assertAlmostEqual(r_full["score"], 8.0)

    def test_pack_bonus_points_gating(self):
        self.assertEqual(
            packdetect.pack_bonus_points(_degen(packdetect_mode="off"),
                                         1.0), 0.0)
        self.assertEqual(
            packdetect.pack_bonus_points(_degen(packdetect_mode="measure"),
                                         1.0), 0.0)
        self.assertEqual(
            packdetect.pack_bonus_points(_degen(packdetect_mode="on"),
                                         1.0), 8.0)


class BoundsTest(unittest.TestCase):
    def test_events_bounded(self):
        state, degen = {}, _degen()
        for i in range(40):
            packdetect.record_fresh(
                state, degen, _fresh(("M%d" % i, ["W%d" % i, "X%d" % i])))
        self.assertLessEqual(len(state[packdetect.EVENTS_KEY]),
                             packdetect._MAX_EVENTS)

    def test_detection_log_ttl(self):
        state, degen = {}, _degen()
        old = time.time() - 8 * 24 * 3600
        state[packdetect.DETECT_KEY] = [
            {"mint": "OLD", "ts": old, "strength": 1.0,
             "members": ["A"], "packs": []}
            for _ in range(int(packdetect._MAX_DETECT * 0.85))
        ]
        packdetect.measure(state, degen, "NEW",
                           {"strength": 0.5, "members": ["A", "B"],
                            "packs": [{"pack_id": "pack-0"}]})
        mints = [e["mint"] for e in state[packdetect.DETECT_KEY]]
        self.assertNotIn("OLD", mints)
        self.assertIn("NEW", mints)


class CollectSignalsTest(unittest.TestCase):
    def test_collect_signals_includes_pack_keys(self):
        state, degen = {}, _degen()
        owner = SimpleNamespace(_fp_tracker=None)
        sig = sniper.collect_signals(owner, state, degen, "MINTX",
                                     {"mcap": []})
        self.assertIn("pack_strength", sig)
        self.assertIn("pack_members", sig)
        self.assertEqual(sig["pack_strength"], 0.0)


if __name__ == "__main__":
    unittest.main()
