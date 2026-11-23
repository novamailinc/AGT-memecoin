"""Regime tagging tests (Stage A, 2026-11-23).

Proves the declared contemporaneous rules: labels come ONLY from
snapshots at or before the trade's entry - mutating post-entry prices
must never change a label.
"""

import importlib.util
import unittest
from pathlib import Path

_BT = (Path(__file__).resolve().parents[1]
       / "daemon" / "launch_scanner" / "backtest.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "launch_scanner_backtest_regimes", str(_BT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bt = _load()


def _hist(prices, liq=20000.0, t_step=60.0):
    snaps = [{"t": i * t_step, "price_usd": p, "mcap_usd": 10000.0,
              "vol5m_usd": 1000.0, "buys5m": 10, "sells5m": 5,
              "liq_usd": liq} for i, p in enumerate(prices)]
    return {
        "mint": "MR", "symbol": "T", "chain": "solana",
        "source": "pumpfun", "t0": 0.0, "snapshots": snaps,
        "safety_eval_t": 0.0,
        "safety": {"score": 80.0, "passed": True, "fails": [],
                   "unknown": False},
        "narratives": ["other"],
        "outcome": {"peak_mult": max(prices), "peak_t": 0.0,
                    "rug": False, "end_t": 0.0, "recorded_t": 0.0},
        "verdict": "confirm",
    }


def _labels(prices, liq=20000.0):
    rep = bt.BacktestEngine().run([_hist(prices, liq)])
    assert rep["n_trades"] == 1, rep["per_gate_blocked"]
    return rep["trades"][0]["regimes"], rep


class TestDeclaredRules(unittest.TestCase):
    def test_uptrend_lowvol_normal_liq(self):
        labels, _ = _labels([1.0, 1.01, 1.02, 1.3], liq=100000.0)
        # entry at t=0 (first snapshot): only one pre-entry point.
        self.assertEqual(labels["trend"], "unknown")
        self.assertEqual(labels["liquidity"], "normal-liq")

    def test_labels_at_floored_entry_use_pre_entry_only(self):
        # With the safety floor at t=120 (entry price 1.02), pre-entry
        # series is [1.0, 1.01, 1.02]: mild up-trend... not >= 1.2x.
        h = _hist([1.0, 1.01, 1.02, 5.0, 9.0])
        h["safety_eval_t"] = 120.0
        rep = bt.BacktestEngine().run([h])
        self.assertEqual(rep["n_trades"], 1)
        labels = rep["trades"][0]["regimes"]
        self.assertEqual(rep["trades"][0]["entry_price"], 1.02)
        # Post-entry moon (5.0, 9.0) must NOT leak into labels.
        self.assertEqual(labels["trend"], "flat")
        self.assertEqual(labels["volatility"], "low-vol")
        self.assertEqual(labels["selloff"], "no-selloff")

    def test_high_vol_and_selloff_detected_pre_entry(self):
        h = _hist([1.0, 2.0, 0.5, 0.6, 0.55, 0.6])
        h["safety_eval_t"] = 300.0  # enter at price 0.6 (t=300)
        rep = bt.BacktestEngine().run([h])
        labels = rep["trades"][0]["regimes"]
        # mean |ret| over [1->2, 2->0.5, 0.5->0.6, 0.6->0.55, 0.55->0.6]
        # = (1.0 + 0.75 + 0.2 + 0.0833 + 0.0909)/5 = 0.425 >> 0.10
        self.assertEqual(labels["volatility"], "high-vol")
        # peak 2.0 -> entry 0.6: 70% drawdown >= 40%
        self.assertEqual(labels["selloff"], "selloff")
        self.assertEqual(labels["trend"], "down")  # 0.6/1.0 <= 0.8

    def test_low_liq_label(self):
        h = _hist([1.0, 1.01, 1.02, 1.03], liq=9000.0)
        h["safety_eval_t"] = 180.0
        rep = bt.BacktestEngine().run([h])
        self.assertEqual(rep["trades"][0]["regimes"]["liquidity"],
                         "low-liq")

    def test_missing_liq_unknown_not_guessed(self):
        h = _hist([1.0, 1.01, 1.02], liq=0.0)
        h["safety_eval_t"] = 120.0
        rep = bt.BacktestEngine().run([h])
        self.assertEqual(rep["trades"][0]["regimes"]["liquidity"],
                         "unknown")


class TestNoHindsight(unittest.TestCase):
    def test_post_entry_mutation_cannot_change_labels(self):
        base = [1.0, 1.01, 1.02, 1.03, 1.04, 1.05]
        h1 = _hist(base)
        h1["safety_eval_t"] = 180.0  # entry at 1.03
        h2 = _hist(base[:4] + [100.0, 0.001])  # wild post-entry
        h2["safety_eval_t"] = 180.0
        r1 = bt.BacktestEngine().run([h1])
        r2 = bt.BacktestEngine().run([h2])
        self.assertEqual(r1["trades"][0]["regimes"],
                         r2["trades"][0]["regimes"])

    def test_per_regime_tallies(self):
        hs = []
        for i, (prices, floor) in enumerate([
                ([1.0, 1.01, 1.02, 1.03], 180.0),
                ([1.0, 2.0, 0.5, 0.6, 0.55, 0.6], 300.0)]):
            h = _hist(prices)
            h["mint"] = "M%d" % i
            h["safety_eval_t"] = floor
            hs.append(h)
        rep = bt.BacktestEngine().run(hs)
        self.assertEqual(rep["per_regime"]["volatility"]
                         ["low-vol"]["trades"], 1)
        self.assertEqual(rep["per_regime"]["volatility"]
                         ["high-vol"]["trades"], 1)
        self.assertEqual(rep["per_regime"]["selloff"]
                         ["selloff"]["trades"], 1)
        self.assertIn("cross-mint", rep["regime_context_note"])

    def test_rules_declared_and_overridable(self):
        self.assertIn("high_vol_mean_abs_ret", bt.REGIME_RULES)
        custom = bt._regime_labels(
            _hist([1.0, 1.3, 1.69])["snapshots"], 2,
            rules={"trend_up_mult": 1.5})
        self.assertEqual(custom["trend"], "up")  # 1.69 >= 1.5


if __name__ == "__main__":
    unittest.main()
