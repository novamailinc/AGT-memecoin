"""Adversarial tests: timestamp-correct replay (audit F1/F2, 2026-11-23).

Proves that in v2-timestamp-correct mode:
- a safety report refreshed LATER can never move a simulated entry
  earlier than its own evaluation time (F1);
- a record with a real safety score but no trustworthy safety_eval_t is
  explicitly ineligible, never backfilled with a guessed time;
- a rug label written AFTER the snapshots (or with no provenance) is
  kept OUT of trade eligibility - the trade is still simulated, the
  label is counted, never gated on;
- a rug label provenanced at/before the first snapshot still gates.

v1-legacy is retained for comparison and pinned here as the OLD
behavior so the two models can never silently converge.
"""

import importlib.util
import unittest
from pathlib import Path

_BT = (Path(__file__).resolve().parents[1]
       / "daemon" / "launch_scanner" / "backtest.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "launch_scanner_backtest_ts", str(_BT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backtest = _load()

_PRICES = [1.0, 2.0, 4.0, 8.0, 0.5, 0.25]  # 60s apart, t=0,60,...,300


def _snaps():
    return [{"t": i * 60.0, "price_usd": p, "mcap_usd": 10000.0,
             "vol5m_usd": 1000.0, "buys5m": 10, "sells5m": 5,
             "liq_usd": 20000.0} for i, p in enumerate(_PRICES)]


def _hist(safety_score=80.0, unknown=False, rug=False,
          safety_eval_t=None, outcome_recorded_t=None,
          outcome_end_t=None):
    outcome = {"peak_mult": max(_PRICES), "rug": rug}
    if outcome_recorded_t is not None:
        outcome["recorded_t"] = outcome_recorded_t
    if outcome_end_t is not None:
        outcome["end_t"] = outcome_end_t
    h = {
        "mint": "MINTX", "symbol": "T", "chain": "solana",
        "source": "pumpfun", "t0": 0.0, "snapshots": _snaps(),
        "safety": {"score": safety_score,
                   "passed": safety_score is not None
                   and safety_score >= 50.0,
                   "fails": [], "unknown": unknown},
        "narratives": ["other"], "outcome": outcome, "verdict": "confirm",
    }
    if safety_eval_t is not None:
        h["safety_eval_t"] = safety_eval_t
    return h


def _engine(model):
    return backtest.BacktestEngine(replay_model=model)


class TestSafetyTimingFloor(unittest.TestCase):
    def test_v2_never_enters_before_safety_eval(self):
        # Safety report evaluated at t=180 (snapshot index 3, price 8.0).
        rep = _engine(backtest.REPLAY_MODEL_V2).run(
            [_hist(safety_eval_t=180.0)])
        self.assertEqual(rep["n_trades"], 1)
        self.assertEqual(rep["trades"][0]["entry_price"], 8.0)

    def test_v1_legacy_enters_at_first_snapshot(self):
        # Pin the OLD behavior so the models can never silently converge.
        rep = _engine(backtest.REPLAY_MODEL_V1).run(
            [_hist(safety_eval_t=180.0)])
        self.assertEqual(rep["n_trades"], 1)
        self.assertEqual(rep["trades"][0]["entry_price"], 1.0)

    def test_v2_late_score_value_cannot_move_entry_earlier(self):
        # Adversarial: the SAME late-evaluated report, passing vs
        # barely-passing score - neither may produce an entry before
        # the evaluation time.
        for score in (50.0, 99.0):
            rep = _engine(backtest.REPLAY_MODEL_V2).run(
                [_hist(safety_score=score, safety_eval_t=240.0)])
            self.assertEqual(rep["n_trades"], 1)
            self.assertEqual(rep["trades"][0]["entry_price"], 0.5)

    def test_v2_score_without_eval_time_ineligible(self):
        # Legacy record: real score, no safety_eval_t -> explicitly
        # ineligible, NEVER backfilled.
        rep = _engine(backtest.REPLAY_MODEL_V2).run([_hist()])
        self.assertEqual(rep["n_trades"], 0)
        self.assertEqual(
            rep["per_gate_blocked"].get("safety-timing-unknown"), 1)

    def test_v2_eval_after_last_snapshot_no_entry(self):
        rep = _engine(backtest.REPLAY_MODEL_V2).run(
            [_hist(safety_eval_t=999999.0)])
        self.assertEqual(rep["n_trades"], 0)
        self.assertEqual(rep["per_gate_blocked"].get(
            "safety-timing-no-eligible-snapshot"), 1)

    def test_v2_eval_at_first_snapshot_enters_normally(self):
        rep = _engine(backtest.REPLAY_MODEL_V2).run(
            [_hist(safety_eval_t=0.0)])
        self.assertEqual(rep["n_trades"], 1)
        self.assertEqual(rep["trades"][0]["entry_price"], 1.0)

    def test_v2_unknown_safety_no_floor_needed(self):
        # No real score => no information to leak; the safety gate's own
        # rules decide (here: unknown allowed, min score 0).
        eng = backtest.BacktestEngine(
            entry_rules={"min_safety_score": 0.0,
                         "require_known_safety": False},
            replay_model=backtest.REPLAY_MODEL_V2)
        rep = eng.run([_hist(safety_score=None, unknown=True)])
        self.assertEqual(rep["n_trades"], 1)


class TestRugProvenanceGuard(unittest.TestCase):
    def test_v2_future_rug_label_kept_out_of_eligibility(self):
        # Rug label written at t=600, after every snapshot: real-time
        # logic would have entered, so v2 simulates the trade and
        # counts the ignored label - the trade is NOT gated away.
        rep = _engine(backtest.REPLAY_MODEL_V2).run(
            [_hist(rug=True, outcome_recorded_t=600.0,
                   safety_eval_t=0.0)])
        self.assertEqual(rep["n_trades"], 1)
        self.assertEqual(rep["n_rug_future_label_ignored"], 1)
        self.assertNotIn("rug", rep["per_gate_blocked"])

    def test_v1_legacy_gates_any_rug_label(self):
        rep = _engine(backtest.REPLAY_MODEL_V1).run(
            [_hist(rug=True, outcome_recorded_t=600.0)])
        self.assertEqual(rep["n_trades"], 0)
        self.assertEqual(rep["per_gate_blocked"].get("rug"), 1)

    def test_v2_unprovenanced_rug_label_ignored(self):
        # No recorded_t, no end_t: availability cannot be established,
        # so the label must not decide eligibility.
        rep = _engine(backtest.REPLAY_MODEL_V2).run(
            [_hist(rug=True, safety_eval_t=0.0)])
        self.assertEqual(rep["n_trades"], 1)
        self.assertEqual(rep["n_rug_future_label_ignored"], 1)

    def test_v2_contemporaneous_rug_label_still_gates(self):
        # Label provenanced at/before the first snapshot (exact
        # recorded_t provenance, not the end_t heuristic).
        rep = _engine(backtest.REPLAY_MODEL_V2).run(
            [_hist(rug=True, outcome_recorded_t=0.0,
                   safety_eval_t=0.0)])
        self.assertEqual(rep["n_trades"], 0)
        self.assertEqual(rep["per_gate_blocked"].get("rug"), 1)
        self.assertEqual(rep["n_rug_future_label_ignored"], 0)

    def test_v2_end_t_fallback_provenance(self):
        # recorded_t absent: end_t is the fallback provenance; a label
        # existing at the first snapshot still gates.
        rep = _engine(backtest.REPLAY_MODEL_V2).run(
            [_hist(rug=True, outcome_end_t=0.0, safety_eval_t=0.0)])
        self.assertEqual(rep["n_trades"], 0)
        self.assertEqual(rep["per_gate_blocked"].get("rug"), 1)


class TestSeriesCompletenessLabels(unittest.TestCase):
    def test_incomplete_series_surfaced_on_trades(self):
        # A record with middle gaps / truncation can never qualify as a
        # complete series: the engine flags the trade and counts it.
        h = _hist(safety_eval_t=0.0)
        h["series_gaps"] = 2
        h["series_complete"] = False
        rep = _engine(backtest.REPLAY_MODEL_V2).run([h])
        self.assertEqual(rep["n_trades"], 1)
        self.assertEqual(rep["n_incomplete_series_trades"], 1)
        self.assertFalse(rep["trades"][0]["series_complete"])
        self.assertEqual(rep["trades"][0]["series_gaps"], 2)

    def test_legacy_unlabeled_record_is_incomplete(self):
        # Pre-journal records carry NO series labels: their history is
        # window-truncated and irrecoverable - they must not qualify as
        # complete by silence.
        rep = _engine(backtest.REPLAY_MODEL_V2).run(
            [_hist(safety_eval_t=0.0)])
        self.assertEqual(rep["trades"][0]["series_complete"], False)
        self.assertEqual(rep["n_incomplete_series_trades"], 1)

    def test_complete_series_labeled(self):
        h = _hist(safety_eval_t=0.0)
        h["series_source"] = "journal"
        h["series_gaps"] = 0
        h["window_truncated"] = False
        h["series_complete"] = True
        rep = _engine(backtest.REPLAY_MODEL_V2).run([h])
        self.assertTrue(rep["trades"][0]["series_complete"])
        self.assertEqual(rep["n_incomplete_series_trades"], 0)


class TestReplayModelVersioning(unittest.TestCase):
    def test_report_carries_replay_model(self):
        for model in backtest.REPLAY_MODELS:
            rep = _engine(model).run([_hist(safety_eval_t=0.0)])
            self.assertEqual(rep["replay_model"], model)

    def test_unknown_model_rejected(self):
        with self.assertRaises(ValueError):
            backtest.BacktestEngine(replay_model="v3-imaginary")

    def test_default_is_timestamp_correct(self):
        self.assertEqual(backtest.BacktestEngine().replay_model,
                         backtest.REPLAY_MODEL_V2)


if __name__ == "__main__":
    unittest.main()
