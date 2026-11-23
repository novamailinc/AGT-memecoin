"""Tests for the durable full-series journal (Stage A audit F3).

The in-memory deque is truncated to fp_window_polls; the journal must
retain the full series (bounded by retention compaction) so backtest
records carry complete, timestamp-correct snapshots.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_FP = (Path(__file__).resolve().parents[1]
       / "daemon" / "launch_scanner" / "fingerprints.py")


def _load():
    spec = importlib.util.spec_from_file_location(
        "launch_scanner_fingerprints_journal", str(_FP))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fp = _load()


class _Cfg:
    def __init__(self, path, window=3, enabled=True):
        self.fp_enabled = True
        self.fp_window_polls = window
        self.fp_series_journal_enabled = enabled
        self.fp_series_journal_path = path


def _stats(mcap=1000.0):
    return {"usd_market_cap": mcap, "price_usd": mcap / 1e6,
            "vol_m5": 100.0, "buys_m5": 5, "sells_m5": 2,
            "usd_liquidity": 500.0}


class TestSeriesJournal(unittest.TestCase):
    def test_full_series_beyond_deque_window(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            tr = fp.FingerprintTracker(_Cfg(path, window=3))
            for i in range(10):
                tr.observe("MINTA", _stats(1000.0 + i), now=1000.0 + i)
            self.assertEqual(len(tr._series["MINTA"]), 3)  # truncated
            full = tr.journal_series("MINTA")
            self.assertIsNotNone(full)
            self.assertEqual(len(full), 10)  # journal has everything
            self.assertEqual(full[0][0], 1000.0)
            self.assertEqual(full[-1][0], 1009.0)
            ts = [o[0] for o in full]
            self.assertEqual(ts, sorted(ts))  # chronological

    def test_journal_disabled_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            tr = fp.FingerprintTracker(_Cfg(path, enabled=False))
            tr.observe("MINTA", _stats(), now=1000.0)
            self.assertIsNone(tr.journal_series("MINTA"))
            self.assertFalse(os.path.exists(path))

    def test_unknown_mint_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            tr = fp.FingerprintTracker(_Cfg(path))
            tr.observe("MINTA", _stats(), now=1000.0)
            self.assertIsNone(tr.journal_series("OTHER"))

    def test_malformed_lines_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            tr = fp.FingerprintTracker(_Cfg(path))
            tr.observe("MINTA", _stats(), now=1000.0)
            with open(path, "a") as fh:
                fh.write("not json\n{\"mint\": 7}\n\n")
            full = tr.journal_series("MINTA")
            self.assertIsNotNone(full)
            self.assertEqual(len(full), 1)

    def test_compaction_bounds_retention(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            tr = fp.FingerprintTracker(_Cfg(path))
            # Old entries (beyond retention) plus fresh ones.
            with open(path, "w") as fh:
                fh.write(json.dumps(
                    {"mint": "OLD", "obs": [10.0, 1, 1, 1, 1, 1, 1, None]}
                ) + "\n")
            tr._journal_appends = fp._COMPACT_EVERY - 1
            # Make the file big enough to trigger compaction.
            with open(path, "a") as fh:
                pad = json.dumps(
                    {"mint": "PAD", "obs": [10.0] + [1] * 6 + [None],
                     "pad": "x" * 200})
                for _ in range(6000):
                    fh.write(pad + "\n")
            tr.observe("FRESH", _stats(), now=999999.0)  # triggers compact
            with open(path) as fh:
                body = fh.read()
            self.assertNotIn('"OLD"', body)  # aged out
            self.assertNotIn('"PAD"', body)   # aged out too
            self.assertIn('"FRESH"', body)
            # Bounded: after compaction only the fresh entry remains.
            self.assertLess(os.path.getsize(path), 10_000)

    def test_journal_never_raises_on_bad_path(self):
        cfg = _Cfg("/nonexistent-dir-\x00/x.jsonl")
        tr = fp.FingerprintTracker(cfg)
        tr.observe("MINTA", _stats(), now=1.0)  # must not raise
        self.assertIsNone(tr.journal_series("MINTA"))

    def test_failures_surfaced_in_status_not_silent(self):
        # Never-raises must NOT mean silent success: write and read
        # failures increment status counters with last_error.
        cfg = _Cfg("/nonexistent-dir-\x00/x.jsonl")
        tr = fp.FingerprintTracker(cfg)
        tr.observe("MINTA", _stats(), now=1.0)
        tr.observe("MINTA", _stats(), now=2.0)
        st = tr.journal_status()
        self.assertEqual(st["write_failures"], 2)
        self.assertIsNotNone(st["last_error"])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            tr2 = fp.FingerprintTracker(_Cfg(path))
            os.mkdir(path)  # a directory where the file should be
            tr2.observe("MINTA", _stats(), now=1.0)
            st2 = tr2.journal_status()
            self.assertGreaterEqual(st2["write_failures"], 1)

    def test_idle_no_compaction_no_silent_loss(self):
        # Idle daemon: compaction runs on the append cadence only, so
        # aged data is RETAINED when nothing new arrives (safe side).
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            tr = fp.FingerprintTracker(_Cfg(path))
            tr.observe("OLDMINT", _stats(), now=10.0)  # ancient
            full = tr.journal_series("OLDMINT", now=9e9)
            self.assertIsNotNone(full)  # still there, not compacted away
            self.assertEqual(full[0][0], 10.0)

    def test_concurrent_writers_no_torn_lines(self):
        # Two tracker instances appending to the same path: every line
        # stays valid JSON and no entry is lost.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.jsonl")
            a = fp.FingerprintTracker(_Cfg(path))
            b = fp.FingerprintTracker(_Cfg(path))
            for i in range(50):
                a.observe("MINTA", _stats(1.0 + i), now=1000.0 + i)
                b.observe("MINTB", _stats(2.0 + i), now=1000.0 + i)
            with open(path) as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 100)
            for ln in lines:
                json.loads(ln)  # raises on a torn line
            self.assertEqual(len(a.journal_series("MINTA")), 50)
            self.assertEqual(len(b.journal_series("MINTB")), 50)


class TestSeriesGaps(unittest.TestCase):
    def _snaps(self, ts):
        return [{"t": t, "price_usd": 1.0} for t in ts]

    def test_regular_series_no_gaps(self):
        self.assertEqual(fp.count_series_gaps(
            self._snaps([0, 60, 120, 180, 240])), 0)

    def test_missing_middle_interval_detected(self):
        # Median 60s; the 600s hole exceeds 5x median and 300s floor.
        self.assertEqual(fp.count_series_gaps(
            self._snaps([0, 60, 120, 720, 780])), 1)

    def test_short_series_needs_three_points(self):
        self.assertEqual(fp.count_series_gaps(self._snaps([0, 9999])), 0)

    def test_unsorted_input_handled(self):
        self.assertEqual(fp.count_series_gaps(
            self._snaps([720, 0, 120, 60, 780])), 1)


if __name__ == "__main__":
    unittest.main()
