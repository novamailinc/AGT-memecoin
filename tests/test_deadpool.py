"""Tests for the dead-pool / pool-gone exit paths and orphan reconciliation.

Two coupled bugs (2026-10-05):
1. `_manage_exits` skipped any position whose quote was missing
   (`continue` before every exit check) — a dead pool could never
   close. Now: when the market feed is healthy for other coins in the
   same cycle but one pool is specifically dead, the time stop fires
   at ~0 ("dead_pool") instead of skipping forever.
2. `run_once()` reloaded daemon state from disk every cycle while the
   full save was throttled to 300s — position keys added/removed in a
   non-save cycle were lost on the next reload, orphaning broker
   tickets (never closable) and resurrecting closed ones (zombie close
   retries every cycle). degen_positions key changes now trigger an
   immediate save, like the daily-slot flags.

Phase 1b (2026-10-09):
3. Time exits are checked BEFORE the price fetch can gate them: a
   position past its time stop closes even when its own quote is gone,
   as reason "pool_gone" at ~0.0. Feed health no longer rests on
   market_alive alone (which conflated "all my tracked pools are dead"
   with "the feed is down") — SOL is always registered on a fresh
   adapter, so its quote is an independent feed check. A feed outage
   still never mass-closes positions.
4. Broker orphan tickets (held by the paper broker, absent from every
   degen_positions entry) are reconciled: past the longest configured
   hold, or pool-gone while the feed is healthy, they close the same
   way instead of sitting stuck forever. Tickets the scanner tracks
   under any key (including wave-suffixed keys) are never touched.
"""

import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import LaunchScanner
from daemon.launch_scanner.feedback import EXIT_REASONS


def _degen(**kw):
    d = dict(max_hold_hours=4.0, entry_sol=0.05,
             trail_trigger_pct=50.0, trail_drawdown_pct=30.0,
             leg_a_trail_trigger_pct=20.0, leg_a_trail_drawdown_pct=15.0,
             tier_b_max_hold_hours=24.0)
    d.update(kw)
    return SimpleNamespace(**d)


def _pos(ticket=7, symbol="TKN", age_h=5.0, **kw):
    p = dict(ticket=ticket, symbol=symbol, mint="MINT1",
             entry_price_sol=0.001, entry_ts=time.time() - age_h * 3600,
             sl=0.0007, tp=0.003, volume_sol=0.05, leg="A")
    p.update(kw)
    return p


def _broker_pos(ticket, symbol, age_h, volume=0.05, price_open=0.001):
    return SimpleNamespace(
        ticket=ticket, symbol=symbol, volume=volume,
        price_open=price_open,
        time_open=datetime.now(timezone.utc) - timedelta(hours=age_h))


class _Adapter:
    """price_map: symbol -> price (None = dead quote)."""

    def __init__(self, price_map):
        self.price_map = price_map
        self.closed_at = []  # (ticket, price, reason)

    def _price_sol_quiet(self, symbol):
        return self.price_map.get(symbol)

    def close_position(self, ticket):
        raise AssertionError("should not market-close in dead-pool tests")

    def close_position_at(self, ticket, price_sol, reason=""):
        self.closed_at.append((ticket, price_sol, reason))
        return price_sol


class _OrphanAdapter(_Adapter):
    """_Adapter plus a fake broker book for orphan reconciliation tests."""

    def __init__(self, price_map, broker_positions=()):
        super().__init__(price_map)
        self._broker_positions = list(broker_positions)

    def positions(self):
        return list(self._broker_positions)

    def close_position(self, ticket):
        self.closed_at.append((ticket, 0.0009, "market"))
        return 0.0009


class DeadPoolTests(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner()

    def _run(self, degen, adapter, state):
        published = []
        import agent.events as events_pkg
        with patch.object(events_pkg, "publish",
                          side_effect=published.append):
            self.sc._manage_exits(SimpleNamespace(degen=degen),
                                  adapter, state)
        return published

    def test_dead_pool_closes_at_zero_when_market_alive(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {
            "MINT1": _pos(age_h=5.0),                       # dead, ancient
            "MINT2": _pos(ticket=8, symbol="OK", mint="MINT2",
                          age_h=0.1),                       # alive, young
        }}
        ad = _Adapter({"TKN": None, "OK": 0.0012})
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [(7, 0.0, "pool_gone")])
        self.assertNotIn("MINT1", state["degen_positions"])
        self.assertIn("MINT2", state["degen_positions"])  # untouched
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["reason"], "degen_pool_gone")
        # Total loss recorded honestly.
        self.assertAlmostEqual(published[0]["profit"], -0.05, places=9)

    def test_market_outage_skips_everything(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {"MINT1": _pos(age_h=50.0)}}
        ad = _Adapter({"TKN": None})  # nothing prices -> outage, not death
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])
        self.assertIn("MINT1", state["degen_positions"])

    def test_young_dead_position_waits(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {
            "MINT1": _pos(age_h=1.0),                        # dead but young
            "MINT2": _pos(ticket=8, symbol="OK", mint="MINT2", age_h=0.1),
        }}
        ad = _Adapter({"TKN": None, "OK": 0.0012})
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])
        self.assertIn("MINT1", state["degen_positions"])

    def test_alive_positions_still_exit_normally(self):
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {
            "MINT2": _pos(ticket=8, symbol="OK", mint="MINT2",
                          age_h=5.0),                        # alive, ancient
        }}

        class _A(_Adapter):
            def close_position(self, ticket):
                self.closed_at.append((ticket, 0.001, "market"))
                return 0.001

        ad = _A({"OK": 0.001})
        published = self._run(d, ad, state)
        self.assertEqual(len(ad.closed_at), 1)
        self.assertEqual(ad.closed_at[0][2], "market")  # not pool_gone
        self.assertIn("max_hold", published[0]["reason"])

    def test_exit_reasons_include_pool_gone(self):
        self.assertIn("pool_gone", EXIT_REASONS)
        # Historical "dead_pool" records stay analyzable.
        self.assertIn("dead_pool", EXIT_REASONS)


class TimeExitFirstTests(unittest.TestCase):
    """The time stop must fire even when the position's own quote fetch
    fails every cycle."""

    def setUp(self):
        self.sc = LaunchScanner()

    def _run(self, degen, adapter, state):
        published = []
        import agent.events as events_pkg
        with patch.object(events_pkg, "publish",
                          side_effect=published.append):
            self.sc._manage_exits(SimpleNamespace(degen=degen),
                                  adapter, state)
        return published

    def test_raising_quote_does_not_block_time_exit(self):
        class _Raising(_Adapter):
            def _price_sol_quiet(self, symbol):
                if symbol == "SOL":
                    return 1.0  # feed healthy
                raise RuntimeError("quote exploded")

        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {"MINT1": _pos(age_h=5.0)}}
        ad = _Raising({})
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [(7, 0.0, "pool_gone")])
        self.assertEqual(published[0]["reason"], "degen_pool_gone")

    def test_sol_probe_distinguishes_dead_pools_from_outage(self):
        # Every TRACKED pool is dead, but SOL prices: the feed is up,
        # so the dead pool must close (market_alive alone would say
        # "outage" and skip forever).
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {"MINT1": _pos(age_h=5.0)}}
        ad = _Adapter({"TKN": None, "SOL": 1.0})
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [(7, 0.0, "pool_gone")])
        self.assertEqual(len(published), 1)

    def test_sol_probe_down_is_still_an_outage(self):
        # Nothing prices, including SOL: outage, never mass-close.
        d = _degen(max_hold_hours=4.0)
        state = {"degen_positions": {
            "MINT1": _pos(age_h=50.0),
            "MINT2": _pos(ticket=8, symbol="OK", mint="MINT2", age_h=60.0),
        }}
        ad = _Adapter({"TKN": None, "OK": None, "SOL": None})
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])
        self.assertIn("MINT1", state["degen_positions"])
        self.assertIn("MINT2", state["degen_positions"])


class OrphanReconcileTests(unittest.TestCase):
    """Broker-held tickets the scanner no longer tracks must close
    once stale instead of sitting stuck forever."""

    def setUp(self):
        self.sc = LaunchScanner()

    def _run(self, degen, adapter, state):
        published = []
        import agent.events as events_pkg
        with patch.object(events_pkg, "publish",
                          side_effect=published.append):
            self.sc._manage_exits(SimpleNamespace(degen=degen),
                                  adapter, state)
        return published

    def test_orphan_past_hold_with_live_quote_market_closes(self):
        d = _degen()
        state = {"degen_positions": {}}
        ad = _OrphanAdapter(
            {"ORPH": 0.0009, "SOL": 1.0},
            [_broker_pos(99, "ORPH", age_h=200.0)])
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [(99, 0.0009, "market")])
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["reason"], "degen_max_hold")
        self.assertTrue(published[0]["orphan"])
        self.assertEqual(published[0]["ticket"], 99)

    def test_orphan_pool_gone_closes_at_zero(self):
        d = _degen()
        state = {"degen_positions": {}}
        ad = _OrphanAdapter(
            {"ORPH": None, "SOL": 1.0},
            [_broker_pos(99, "ORPH", age_h=200.0)])
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [(99, 0.0, "pool_gone")])
        self.assertEqual(published[0]["reason"], "degen_pool_gone")
        self.assertTrue(published[0]["orphan"])
        # Total loss recorded honestly.
        self.assertAlmostEqual(published[0]["profit"], -0.05, places=9)

    def test_orphan_outage_never_closes(self):
        d = _degen()
        state = {"degen_positions": {}}
        ad = _OrphanAdapter(
            {"ORPH": None, "SOL": None},
            [_broker_pos(99, "ORPH", age_h=200.0)])
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])

    def test_tracked_ticket_never_touched(self):
        # The scanner manages ticket 99 under a wave-suffixed key; the
        # broker book also holds it. Reconciliation must not touch it.
        d = _degen()
        state = {"degen_positions": {
            "second_wave:MINT9": _pos(ticket=99, symbol="WAVE",
                                      mint="MINT9", age_h=0.1),
        }}
        ad = _OrphanAdapter(
            {"WAVE": 0.0012, "SOL": 1.0},
            [_broker_pos(99, "WAVE", age_h=200.0)])
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])
        self.assertIn("second_wave:MINT9", state["degen_positions"])

    def test_young_healthy_orphan_left_alone(self):
        d = _degen()
        state = {"degen_positions": {}}
        ad = _OrphanAdapter(
            {"ORPH": 0.0009, "SOL": 1.0},
            [_broker_pos(99, "ORPH", age_h=0.5)])
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])

    def test_young_quoteless_orphan_gets_grace_period(self):
        # A just-opened orphan may simply not be indexed yet — the entry
        # path creates the broker ticket BEFORE recording scanner state,
        # so a crash in that window strands a young orphan. Never
        # pool-gone-close inside the grace period.
        d = _degen()
        state = {"degen_positions": {}}
        ad = _OrphanAdapter(
            {"ORPH": None, "SOL": 1.0},
            [_broker_pos(99, "ORPH", age_h=0.5)])
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [])
        self.assertEqual(published, [])

    def test_reconcile_runs_with_empty_tracked_positions(self):
        # No tracked positions at all: orphans are still reconciled and
        # the SOL probe still determines feed health.
        d = _degen()
        state = {}
        ad = _OrphanAdapter(
            {"ORPH": None, "SOL": 1.0},
            [_broker_pos(99, "ORPH", age_h=200.0)])
        published = self._run(d, ad, state)
        self.assertEqual(ad.closed_at, [(99, 0.0, "pool_gone")])


if __name__ == "__main__":
    unittest.main()
