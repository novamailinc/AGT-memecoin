"""Scan cycle wall-clock budget (2026-10-09).

When the network degrades, individually timeout-bounded calls accumulate
into multi-minute cycles, stalling saves, re-scores and alerts (observed:
a degraded-egress night froze full cycles). Past the budget, the
candidate loops defer remaining work to the next cycle — nothing is
decided or dropped on a skipped pass.
"""

import time
import unittest
import unittest.mock
from types import SimpleNamespace

from daemon.launch_scanner import LaunchScanner


def _degen(**kw):
    base = dict(scan_cycle_budget_s=240.0)
    base.update(kw)
    return SimpleNamespace(**base)


class CycleBudgetTest(unittest.TestCase):
    def _scanner(self):
        return LaunchScanner.__new__(LaunchScanner)

    def test_not_over_budget_by_default(self):
        sc = self._scanner()
        sc._cycle_deadline = time.time() + 240.0
        self.assertFalse(sc._cycle_over_budget())

    def test_over_budget_when_deadline_passed(self):
        sc = self._scanner()
        sc._cycle_deadline = time.time() - 1.0
        self.assertTrue(sc._cycle_over_budget())

    def test_unset_deadline_never_blocks(self):
        sc = self._scanner()
        # No _cycle_deadline attribute at all (e.g. _judge called
        # outside _scan in old tests) -> never over budget.
        if hasattr(sc, "_cycle_deadline"):
            del sc._cycle_deadline
        self.assertFalse(sc._cycle_over_budget())

    def test_judge_defers_remaining_when_over_budget(self):
        sc = self._scanner()
        sc._cycle_deadline = time.time() - 1.0  # already over
        cfg = SimpleNamespace(degen=_degen())
        state = {"candidates": {
            "k1": {"mint": "M1", "symbol": "A"},
            "k2": {"mint": "M2", "symbol": "B"},
            "k3": {"mint": "M3", "symbol": "C"},
        }}
        with unittest.mock.patch.object(
                sc, "_batch_refresh", return_value={}), \
             unittest.mock.patch.object(
                sc, "_verdict") as verdict_mock:
            sc._judge(cfg, None, state)
        # Nobody was judged...
        verdict_mock.assert_not_called()
        # ...and nobody was dropped: all candidates survive for the
        # next cycle.
        self.assertEqual(set(state["candidates"].keys()),
                         {"k1", "k2", "k3"})

    def test_judge_processes_when_within_budget(self):
        sc = self._scanner()
        sc._cycle_deadline = time.time() + 240.0
        cfg = SimpleNamespace(degen=_degen())
        state = {"candidates": {"k1": {"mint": "M1", "symbol": "A"}}}
        with unittest.mock.patch.object(
                sc, "_batch_refresh", return_value={}), \
             unittest.mock.patch.object(
                sc, "_verdict",
                return_value=("wait", {})) as verdict_mock, \
             unittest.mock.patch.object(
                sc, "_fingerprint_track", return_value=None), \
             unittest.mock.patch.object(sc, "_breakout_track"), \
             unittest.mock.patch.object(sc, "_sentiment_track"), \
             unittest.mock.patch.object(sc, "_narrative_attach"):
            sc._judge(cfg, None, state)
        self.assertEqual(verdict_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
