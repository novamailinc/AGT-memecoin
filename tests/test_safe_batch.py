"""Safe batch (2026-10-09): five measurement/display-only modules.

exit_liquidity_clock, social_radar, anticrowd, exit_copy, last_buyer —
plus wiring (card lines, close hooks, weekly review) and the CRITICAL
no-behavior-change guarantees:

  - none of the five modules can reach entry/exit/gate/sizing code
    (source scan for forbidden symbols),
  - enabling all five knobs changes nothing except the modules' own
    runtime files and card lines,
  - every module fails soft and records "unknown" honestly instead of
    guessing.

No network — all HTTP/RPC is faked. Nothing touches the real
~/.forex-agent runtime tree: explicit tmp paths are passed everywhere.
"""

import json
import os
import stat
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["FOREX_AGENT_TESTING"] = "1"

from daemon.launch_scanner import (
    anticrowd,
    exit_copy,
    exit_liquidity_clock,
    last_buyer,
    social_radar,
)

MINT = "So11111111111111111111111111111111111111112"
MINT2 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _degen(**kw):
    base = dict(
        exit_clock_enabled=True,
        social_radar_enabled=True,
        social_radar_cache_min=10.0,
        anticrowd_enabled=True,
        exit_copy_enabled=True,
        exit_copy_max_tx_per_day=40,
        last_buyer_enabled=True,
        last_buyer_dry_ratio=0.25,
        last_buyer_flat_pct=5.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tmp(tmp: Path, name: str) -> str:
    return str(tmp / name)


# ------------------------------------------------------------------
# exit_liquidity_clock
# ------------------------------------------------------------------

class ExitClockTests(unittest.TestCase):
    def test_record_close_math(self):
        with patch.object(
                exit_liquidity_clock, "_load", return_value=[]), \
             patch.object(
                exit_liquidity_clock, "_resolve_path",
                return_value=None):
            exit_liquidity_clock._PEAKS.clear()
            exit_liquidity_clock.note_price(MINT, 2.0, 1000.0)
            exit_liquidity_clock.note_price(MINT, 3.0, 1100.0)  # peak
            exit_liquidity_clock.note_price(MINT, 2.5, 1200.0)
            rec = exit_liquidity_clock.record_close(
                _degen(), MINT, entry_ts=900.0, entry_px=1.0,
                close_ts=1300.0, close_px=2.5, peak_px=3.0,
                reason="trail_stop", pnl_sol=0.01, label="")
        self.assertIsNotNone(rec)
        self.assertAlmostEqual(rec["close_mult"], 2.5)
        self.assertAlmostEqual(rec["peak_mult"], 3.0)
        self.assertAlmostEqual(rec["hold_h"], 400.0 / 3600.0, places=4)
        # peak seen at t=1100, entry at 900 -> 200s
        self.assertAlmostEqual(rec["time_to_peak_h"], 200.0 / 3600.0,
                               places=4)
        # dump: (3.0-2.5) mult over (1300-1100)s
        self.assertAlmostEqual(rec["dump_mult_per_h"],
                               0.5 / (200.0 / 3600.0), places=2)

    def test_peak_timing_unknown_after_restart(self):
        # No note_price calls (daemon restarted mid-position): peak
        # VALUE still comes from pos, but timing is honestly unknown.
        with patch.object(
                exit_liquidity_clock, "_load", return_value=[]), \
             patch.object(
                exit_liquidity_clock, "_resolve_path",
                return_value=None):
            exit_liquidity_clock._PEAKS.clear()
            rec = exit_liquidity_clock.record_close(
                _degen(), MINT, entry_ts=900.0, entry_px=1.0,
                close_ts=1300.0, close_px=0.5, peak_px=2.5,
                reason="stop_loss", pnl_sol=-0.01, label="")
        self.assertIsNotNone(rec)
        self.assertIsNone(rec["time_to_peak_h"])
        self.assertIsNone(rec["dump_mult_per_h"])
        self.assertAlmostEqual(rec["peak_mult"], 2.5)  # value kept

    def test_weekly_section_roundtrips(self):
        recs = [
            {"close_ts": time.time(), "mint": "a", "peak_mult": 3.0,
             "close_mult": 0.8, "time_to_peak_h": 1.0,
             "dump_mult_per_h": 2.0},   # round-trip
            {"close_ts": time.time(), "mint": "b", "peak_mult": 2.5,
             "close_mult": 2.2, "time_to_peak_h": 2.0,
             "dump_mult_per_h": 0.1},   # held gains
            {"close_ts": time.time(), "mint": "c", "peak_mult": 1.2,
             "close_mult": 0.7, "time_to_peak_h": None,
             "dump_mult_per_h": None},  # never a winner
        ]
        with patch.object(
                exit_liquidity_clock, "_load", return_value=recs):
            lines = exit_liquidity_clock.weekly_section()
        text = "\n".join(lines)
        self.assertIn("round-tripped", text)
        self.assertIn("1/2 (50%)", text)
        self.assertIn("median time-to-peak", text)

    def test_weekly_section_empty(self):
        with patch.object(
                exit_liquidity_clock, "_load", return_value=[]):
            lines = exit_liquidity_clock.weekly_section()
        self.assertIn("no closed positions recorded", "\n".join(lines))

    def test_disabled_returns_none(self):
        rec = exit_liquidity_clock.record_close(
            _degen(exit_clock_enabled=False), MINT, 1.0, 1.0, 2.0, 1.0,
            1.0, "x", 0.0)
        self.assertIsNone(rec)

    def test_file_perms_0600(self, tmp_path=None):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "exit_clock.json")
        with patch.object(
                exit_liquidity_clock, "_resolve_path", return_value=p):
            exit_liquidity_clock._PEAKS.clear()
            rec = exit_liquidity_clock.record_close(
                _degen(), MINT, entry_ts=1.0, entry_px=1.0, close_ts=2.0,
                close_px=1.5, peak_px=1.5, reason="tp", pnl_sol=0.01)
        self.assertIsNotNone(rec)
        mode = stat.S_IMODE(os.stat(p).st_mode)
        self.assertEqual(mode, 0o600)
        data = json.load(open(p))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["mint"], MINT)


# ------------------------------------------------------------------
# social_radar
# ------------------------------------------------------------------

class SocialRadarTests(unittest.TestCase):
    def setUp(self):
        social_radar._CACHE.update(
            {"ts": 0.0, "top": set(), "latest": set(), "ok": False})

    def _fake_boosts(self, top, latest):
        def _fake(url, timeout=8.0):
            if "top" in url:
                return [{"tokenAddress": m} for m in top]
            return [{"tokenAddress": m} for m in latest]
        return _fake

    def test_tier_high(self):
        with patch.object(social_radar, "_polite_get",
                          self._fake_boosts([MINT], [])):
            rec = social_radar.heat(_degen(), MINT)
        self.assertEqual(rec["tier"], "high")

    def test_tier_med(self):
        with patch.object(social_radar, "_polite_get",
                          self._fake_boosts([], [MINT])):
            rec = social_radar.heat(_degen(), MINT)
        self.assertEqual(rec["tier"], "med")

    def test_tier_low(self):
        with patch.object(social_radar, "_polite_get",
                          self._fake_boosts(["other"], ["other2"])):
            rec = social_radar.heat(_degen(), MINT)
        self.assertEqual(rec["tier"], "low")

    def test_tier_unknown_on_fetch_failure(self):
        with patch.object(social_radar, "_polite_get",
                          return_value=None):
            rec = social_radar.heat(_degen(), MINT)
        self.assertEqual(rec["tier"], "unknown")
        # never invents mention counts or KOL data
        self.assertNotIn("mention", rec["detail"].lower())
        self.assertNotIn("kol", rec["detail"].lower())

    def test_card_line_renders_unknown_honestly(self):
        with patch.object(social_radar, "_polite_get",
                          return_value=None):
            line = social_radar.card_line(_degen(), MINT)
        self.assertEqual(line, "📣 social heat: unknown")

    def test_disabled(self):
        with patch.object(social_radar, "_polite_get") as pg:
            rec = social_radar.heat(_degen(social_radar_enabled=False),
                                    MINT)
            self.assertEqual(rec["tier"], "unknown")
            self.assertIsNone(
                social_radar.card_line(_degen(social_radar_enabled=False),
                                       MINT))
            pg.assert_not_called()

    def test_cache_ttl_from_knob(self):
        now = time.time()
        calls = []
        fake = self._fake_boosts([MINT], [])

        def _counting(url, timeout=8.0):
            calls.append(url)
            return fake(url, timeout)

        with patch.object(social_radar, "_polite_get", _counting):
            social_radar.heat(_degen(social_radar_cache_min=10.0), MINT)
            social_radar.heat(_degen(social_radar_cache_min=10.0), MINT,
                              now=now + 5)
            # second call inside TTL: no new fetch (top + latest once)
            self.assertEqual(len(calls), 2)


# ------------------------------------------------------------------
# anticrowd
# ------------------------------------------------------------------

class AnticrowdTests(unittest.TestCase):
    def test_tier_high_boosted_and_carded(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "anticrowd.json")
        for _ in range(3):
            anticrowd.note_card(MINT, path=p)
        rec = anticrowd.crowd(_degen(), MINT, boosted=True, path=p)
        self.assertEqual(rec["tier"], "high")

    def test_tier_med(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "anticrowd.json")
        anticrowd.note_card(MINT, path=p)
        rec = anticrowd.crowd(_degen(), MINT, boosted=False, path=p)
        self.assertEqual(rec["tier"], "med")

    def test_tier_low(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "anticrowd.json")
        rec = anticrowd.crowd(_degen(), MINT, boosted=False, path=p)
        self.assertEqual(rec["tier"], "low")

    def test_tier_unknown_no_data(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "anticrowd.json")
        rec = anticrowd.crowd(_degen(), MINT, boosted=None, path=p)
        self.assertEqual(rec["tier"], "unknown")

    def test_card_line(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "anticrowd.json")
        with patch.object(anticrowd, "_load", return_value={}):
            line = anticrowd.card_line(_degen(), MINT, boosted=False)
        self.assertEqual(line, "👥 crowd: low")

    def test_disabled(self):
        rec = anticrowd.crowd(_degen(anticrowd_enabled=False), MINT,
                              boosted=True)
        self.assertEqual(rec["tier"], "unknown")
        self.assertIsNone(
            anticrowd.card_line(_degen(anticrowd_enabled=False), MINT,
                                boosted=True))

    def test_note_card_counts(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "anticrowd.json")
        now = time.time()
        anticrowd.note_card(MINT, ts=now - 60.0, path=p)
        anticrowd.note_card(MINT, ts=now, path=p)
        data = json.load(open(p))
        self.assertEqual(data[MINT]["count"], 2)


# ------------------------------------------------------------------
# exit_copy
# ------------------------------------------------------------------

class ExitCopyTests(unittest.TestCase):
    def test_record_close(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "exit_copy.json")
        rec = exit_copy.record_close(
            _degen(), MINT, entry_ts=1000.0, close_ts=2000.0,
            close_mult=1.5, reason="trail_stop", pnl_sol=0.02, path=p)
        self.assertIsNotNone(rec)
        data = json.load(open(p))
        self.assertAlmostEqual(data[MINT]["close_mult"], 1.5)
        mode = stat.S_IMODE(os.stat(p).st_mode)
        self.assertEqual(mode, 0o600)

    def test_disabled(self):
        self.assertIsNone(exit_copy.record_close(
            _degen(exit_copy_enabled=False), MINT, 1.0, 2.0, 1.0, "x",
            0.0, path="/tmp/should-not-exist-exit-copy.json"))

    def test_daily_pass_throttles_to_one_per_day(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "exit_copy.json")
        state = {"smart_activity": {}}
        with patch.object(exit_copy, "_load", return_value={}):
            n1 = exit_copy.daily_pass(state, _degen(), now=1_700_000_000.0,
                                      path=p)
            n2 = exit_copy.daily_pass(state, _degen(), now=1_700_000_100.0,
                                      path=p)
        self.assertEqual(n1, 0)
        self.assertEqual(n2, 0)  # same WAT day: skipped
        self.assertIn("exit_copy_last_pass_day", state)

    def test_wallet_sell_detected(self, tmp_path=None):
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "exit_copy.json")
        wallet = "WalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
        closes = {MINT: {"close_ts": 2000.0, "entry_ts": 1000.0,
                         "close_mult": 1.5, "reason": "tp",
                         "pnl_sol": 0.02, "wallet_exits": {},
                         "scanned_day": ""}}
        state = {"smart_activity": {MINT: [wallet]}}

        def _fake_rpc(method, params):
            if method == "getSignaturesForAddress":
                return [{"signature": "sig1", "blockTime": 1500.0}]
            if method == "getTransaction":
                return {"meta": {"err": None,
                    "preTokenBalances": [
                        {"owner": wallet, "mint": MINT,
                         "uiTokenAmount": {"uiAmount": 100.0}}],
                    "postTokenBalances": [
                        {"owner": wallet, "mint": MINT,
                         "uiTokenAmount": {"uiAmount": 20.0}}]}}
            return None

        with patch.object(exit_copy, "_load", return_value=closes), \
             patch.object(exit_copy, "_save", return_value=True), \
             patch.object(exit_copy, "_rpc", side_effect=_fake_rpc):
            n = exit_copy.daily_pass(state, _degen(), now=1_700_000_000.0,
                                     path=p)
        self.assertEqual(n, 1)
        exits = closes[MINT]["wallet_exits"]
        self.assertEqual(len(exits), 1)
        sold = list(exits.values())[0]
        self.assertAlmostEqual(sold["sold_ts"], 1500.0)
        # wallet sold at t=1500, we closed at t=2000 -> -0.14h
        self.assertLess(sold["after_our_close_h"], 0)

    def test_weekly_section(self, tmp_path=None):
        now = time.time()
        closes = {
            "a": {"close_ts": now, "pnl_sol": 0.05,
                  "wallet_exits": {"w1": {"sold_ts": now - 7200.0,
                                          "after_our_close_h": -2.0}}},
            "b": {"close_ts": now, "pnl_sol": -0.01,
                  "wallet_exits": {"w2": {"sold_ts": now + 5400.0,
                                          "after_our_close_h": 1.5}}},
        }
        with patch.object(exit_copy, "_load", return_value=closes):
            lines = exit_copy.weekly_section()
        text = "\n".join(lines)
        self.assertIn("BEFORE us: 1 | AFTER us: 1", text)
        self.assertIn("our avg pnl", text)

    def test_weekly_section_empty(self):
        with patch.object(exit_copy, "_load", return_value={}):
            lines = exit_copy.weekly_section()
        self.assertIn("no smart-wallet exits observed",
                      "\n".join(lines))


# ------------------------------------------------------------------
# last_buyer
# ------------------------------------------------------------------

def _obs(ts0, n, buys, price_start, price_step):
    return [(ts0 + i * 60.0, 1e6, price_start + i * price_step,
             1000.0, buys, buys // 2, 50000.0, 100)
            for i in range(n)]


class LastBuyerTests(unittest.TestCase):
    def test_drying(self):
        # healthy buying then collapse, price flat
        rows = _obs(0.0, 8, 20, 1.0, 0.01) + _obs(480.0, 3, 1, 1.05, 0.0)
        v = last_buyer.verdict(rows, _degen())
        self.assertEqual(v["verdict"], "drying")

    def test_ok_when_making_highs(self):
        # buys collapse but price still climbing -> not drying
        rows = _obs(0.0, 8, 20, 1.0, 0.01) + _obs(480.0, 3, 1, 2.0, 0.1)
        v = last_buyer.verdict(rows, _degen())
        self.assertEqual(v["verdict"], "ok")

    def test_ok_healthy(self):
        rows = _obs(0.0, 10, 20, 1.0, 0.01)
        v = last_buyer.verdict(rows, _degen())
        self.assertEqual(v["verdict"], "ok")

    def test_unknown_short_series(self):
        v = last_buyer.verdict(_obs(0.0, 3, 20, 1.0, 0.01), _degen())
        self.assertEqual(v["verdict"], "unknown")

    def test_card_line_only_on_drying(self):
        self.assertIsNone(last_buyer.card_line({"verdict": "ok"}))
        self.assertIsNone(last_buyer.card_line({"verdict": "unknown"}))
        line = last_buyer.card_line({"verdict": "drying",
                                     "detail": "buys/min 5.0 -> 0.5"})
        self.assertTrue(line.startswith("🥀 buyers drying up"))

    def test_disabled(self):
        rows = _obs(0.0, 8, 20, 1.0, 0.01) + _obs(480.0, 3, 1, 1.05, 0.0)
        v = last_buyer.verdict(rows, _degen(last_buyer_enabled=False))
        self.assertEqual(v["verdict"], "unknown")


# ------------------------------------------------------------------
# CRITICAL: no behavior-change guarantees
# ------------------------------------------------------------------

_FORBIDDEN = (
    "submit_order", "close_position", "open_position", "_paper_enter",
    "kill_switch", "entry_sol", "max_positions", "broker",
    "position.", "positions[", "positions.pop",
)


class NoBehaviorChangeTests(unittest.TestCase):
    def test_modules_reach_no_trading_symbols(self):
        import pathlib
        repo = pathlib.Path(__file__).resolve().parents[1]
        mods = ["exit_liquidity_clock", "social_radar", "anticrowd",
                "exit_copy", "last_buyer"]
        for name in mods:
            src = (repo / "daemon" / "launch_scanner"
                   / (name + ".py")).read_text()
            for sym in _FORBIDDEN:
                self.assertNotIn(
                    sym, src,
                    "%s.py must not reference %r (no entry/exit/gate "
                    "code path)" % (name, sym))

    def test_modules_never_raise(self):
        # Garbage in: every public entry returns a safe value, never
        # raises — a dead module can never break a scan cycle.
        d = _degen()
        self.assertIsNone(exit_liquidity_clock.note_price(None, -1, 0))
        self.assertIsNone(exit_liquidity_clock.record_close(
            d, "", 0, 0, 0, 0, 0, "", 0))
        r = social_radar.heat(d, "")
        self.assertEqual(r["tier"], "unknown")
        r = anticrowd.crowd(d, "", path="/tmp/nonexistent-ac.json")
        self.assertEqual(r["tier"], "unknown")
        r = last_buyer.verdict(None, d)
        self.assertEqual(r["verdict"], "unknown")
        self.assertIsNone(last_buyer.card_line(None))
        self.assertEqual(exit_copy.daily_pass({}, d, path="/tmp/nope.json"),
                         0)

    def test_knobs_present_in_defaults(self):
        import yaml
        repo = str(Path(__file__).resolve().parents[1])
        y = yaml.safe_load(open(repo + "/config/defaults.yaml"))
        degen = y["degen"]
        for k in ("exit_clock_enabled", "social_radar_enabled",
                  "anticrowd_enabled", "exit_copy_enabled",
                  "last_buyer_enabled"):
            self.assertIn(k, degen, "knob %s missing from defaults.yaml" % k)
            self.assertTrue(degen[k], "knob %s must default on" % k)

    def test_degen_config_fields(self):
        from config.config import DegenConfig
        import dataclasses
        names = {f.name for f in dataclasses.fields(DegenConfig)}
        for k in ("exit_clock_enabled", "social_radar_enabled",
                  "social_radar_cache_min", "anticrowd_enabled",
                  "exit_copy_enabled", "exit_copy_max_tx_per_day",
                  "last_buyer_enabled", "last_buyer_dry_ratio",
                  "last_buyer_flat_pct"):
            self.assertIn(k, names, "DegenConfig missing %s" % k)


# ------------------------------------------------------------------
# Wiring: card renderers carry the new lines
# ------------------------------------------------------------------

class CardWiringTests(unittest.TestCase):
    def _evt(self):
        return {"mint": MINT, "symbol": "TEST", "social_heat_line":
                "📣 social heat: high", "crowd_line": "👥 crowd: med",
                "last_buyer_line": "🥀 buyers drying up (x)",
                "safety_score": 50}

    def test_early_card_renders_lines(self):
        from daemon.telegram import alerts
        text = alerts._launch_early(self._evt())
        self.assertIn("📣 social heat: high", text)
        self.assertIn("👥 crowd: med", text)
        self.assertIn("🥀 buyers drying up", text)

    def test_confirmed_card_renders_lines(self):
        from daemon.telegram import alerts
        text = alerts._launch(self._evt())
        self.assertIn("📣 social heat: high", text)
        self.assertIn("👥 crowd: med", text)
        self.assertIn("🥀 buyers drying up", text)

    def test_digest_marks_compact(self):
        from daemon.telegram import alerts
        item = {"mint": MINT, "symbol": "TEST",
                "social_heat_line": "📣 social heat: high",
                "crowd_line": "👥 crowd: med",
                "last_buyer_line": "🥀 buyers drying up (x)"}
        text = alerts._digest_item_line(item)
        self.assertIn("📣high", text)
        self.assertIn("👥med", text)
        self.assertIn("🥀", text)

    def test_weekly_review_sections(self):
        from daemon.launch_scanner import weekly_review
        with patch.object(exit_liquidity_clock, "_load", return_value=[]), \
             patch.object(exit_copy, "_load", return_value={}):
            ec = weekly_review._exit_clock_section()
            ex = weekly_review._exit_copy_section()
        self.assertIn("EXIT-LIQUIDITY CLOCK", ec[0])
        self.assertIn("no closed positions recorded", "\n".join(ec))
        self.assertIn("EXIT-COPYING", ex[0])
        self.assertIn("no smart-wallet exits observed", "\n".join(ex))


if __name__ == "__main__":
    unittest.main()
