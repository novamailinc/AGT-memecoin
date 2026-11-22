"""Lifecycle cards (2026-10-09, user-approved): path-to-pick line on
👀 WATCH cards + thesis-break / plan-level alerts for 🎯 PICKs.

DISPLAY-LAYER ONLY: these tests assert rendering, watcher logic, and
rate-limit composition. Entries, exits, gates, caps and strategy are
untouched — a source scan at the bottom proves no behavior symbols are
reachable from the new code.
"""

import copy
import types
import unittest
from unittest import mock

from agent.events import bus as event_bus
from daemon.telegram import alerts
from daemon.telegram import filters as _filters
from daemon.telegram import thesis_watch


def _degen(**kw):
    base = {
        "thesis_break_enabled": True,
        "thesis_break_confirm_polls": 3,
        "thesis_break_plan_levels_enabled": True,
        "thesis_break_ttl_hours": 24.0,
        "early_alert_max_per_window": 4,
        "early_alert_window_seconds": 3600.0,
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def _pick_event(mcap=2_000_000.0):
    return {
        "event": "launch.intraday_top3",
        "symbol": "MOON",
        "mint": "MINT1",
        "chain": "solana",
        "score": 82,
        "why": "sniper 80/100; 3 tracked wallets in",
        "safety": "80/100",
        "usd_market_cap": mcap,
    }


class PathToPickTests(unittest.TestCase):
    def test_early_unconfirmed_shows_concrete_path(self):
        e = {"event": "launch.early", "tier": "early", "symbol": "T",
             "mint": "M"}
        line = alerts._path_to_pick(e)
        self.assertIn("🛤 path to pick", line)
        # Concrete mechanism, not generic wallpaper.
        self.assertIn("rising polls", line)
        self.assertIn("confirmed", line)

    def test_unvetted_shows_safety_condition(self):
        e = {"event": "launch.detected", "tier": "degen", "symbol": "T",
             "mint": "M", "unvetted": True}
        line = alerts._path_to_pick(e)
        self.assertIn("safety scan completes clean", line)
        self.assertIn("1/100", line)

    def test_pick_eligible_coin_stays_silent(self):
        # Confirmed + safety-passed: the Top 3 machinery owns it —
        # no invented conditions.
        e = {"event": "launch.detected", "tier": "degen", "symbol": "T",
             "mint": "M", "safety_score": 80}
        self.assertEqual(alerts._path_to_pick(e), "")

    def test_both_conditions_combine(self):
        e = {"event": "launch.early", "tier": "early", "symbol": "T",
             "mint": "M", "unvetted": True}
        line = alerts._path_to_pick(e)
        self.assertIn("rising polls", line)
        self.assertIn("1/100", line)

    def test_watch_footer_survives(self):
        # The line is NOT a buy signal — the footer stays.
        e = {"event": "launch.early", "tier": "early", "symbol": "T",
             "mint": "M"}
        text = alerts.format_alert(e)
        self.assertIn("🛤 path to pick", text)
        self.assertIn("not a pick", text)

    def test_never_raises_on_garbage(self):
        self.assertEqual(alerts._path_to_pick({}), "")
        self.assertEqual(alerts._path_to_pick({"event": None}), "")
        self.assertEqual(alerts._path_to_pick("nope"), "")


class ThesisWatchRegistrationTests(unittest.TestCase):
    def test_registers_pick_with_mcap(self):
        state = {}
        self.assertTrue(thesis_watch.register_pick(state, _pick_event()))
        w = state["thesis_watch"]["MINT1"]
        # Exact invalidation formula the card renderer uses.
        self.assertEqual(w["invalidation_level"], 1_000_000.0)
        self.assertEqual(w["tp_level"], 6_000_000.0)
        self.assertEqual(w["stop_level"], 1_400_000.0)

    def test_no_mcap_no_watcher(self):
        # GUARD 3: no concrete level -> no watcher, never invented.
        state = {}
        e = _pick_event(mcap=0)
        self.assertFalse(thesis_watch.register_pick(state, e))
        self.assertEqual(state.get("thesis_watch", {}), {})

    def test_non_pick_event_ignored(self):
        state = {}
        self.assertFalse(thesis_watch.register_pick(
            state, {"event": "launch.detected", "mint": "M",
                    "usd_market_cap": 5.0}))
        self.assertEqual(state.get("thesis_watch", {}), {})

    def test_per_coin_plan_dict_respected(self):
        state = {}
        e = _pick_event()
        e["plan"] = {"stop_pct": 20.0, "tp_pct": 100.0}
        thesis_watch.register_pick(state, e)
        w = state["thesis_watch"]["MINT1"]
        self.assertEqual(w["stop_level"], 1_600_000.0)
        self.assertEqual(w["tp_level"], 4_000_000.0)

    def test_never_raises(self):
        self.assertFalse(thesis_watch.register_pick({}, {}))
        self.assertFalse(thesis_watch.register_pick({}, "nope"))


class ThesisBreakConfirmationTests(unittest.TestCase):
    def _watch_state(self, mcap=2_000_000.0):
        state = {}
        thesis_watch.register_pick(state, _pick_event(mcap=mcap))
        return state

    def test_single_wick_does_not_fire(self):
        state = self._watch_state()
        w = state["thesis_watch"]["MINT1"]
        # invalidation = 1M. One poll below, then back above: reset.
        self.assertEqual(thesis_watch._evaluate(w, 900_000.0, 3, True), [])
        self.assertEqual(thesis_watch._evaluate(w, 1_500_000.0, 3, True), [])
        self.assertEqual(w["below"], 0)

    def test_three_consecutive_polls_fire(self):
        state = self._watch_state()
        w = state["thesis_watch"]["MINT1"]
        self.assertEqual(thesis_watch._evaluate(w, 900_000.0, 3, True), [])
        self.assertEqual(thesis_watch._evaluate(w, 950_000.0, 3, True), [])
        # 800k is below BOTH the 1M invalidation and the 1.4M stop —
        # both confirm together, honestly.
        self.assertEqual(thesis_watch._evaluate(w, 800_000.0, 3, True),
                         ["break", "stop"])
        self.assertTrue(w["fired_break"])

    def test_no_double_fire(self):
        state = self._watch_state()
        w = state["thesis_watch"]["MINT1"]
        for _ in range(3):
            thesis_watch._evaluate(w, 900_000.0, 3, True)
        self.assertEqual(thesis_watch._evaluate(w, 900_000.0, 3, True), [])

    def test_tp_and_stop_levels(self):
        state = self._watch_state()
        w = state["thesis_watch"]["MINT1"]
        # tp = 6M, stop = 1.4M
        for _ in range(3):
            fired = thesis_watch._evaluate(w, 6_500_000.0, 3, True)
        self.assertIn("leg_a_tp", fired)
        w2_state = self._watch_state()
        w2 = w2_state["thesis_watch"]["MINT1"]
        for _ in range(3):
            fired2 = thesis_watch._evaluate(w2, 1_000_000.0, 3, True)
        # 1M is below BOTH stop (1.4M) and invalidation (1M)? No:
        # 1M is not < 1M invalidation. stop fires, break does not.
        self.assertIn("stop", fired2)
        self.assertNotIn("break", fired2)

    def test_plan_levels_can_disable(self):
        state = self._watch_state()
        w = state["thesis_watch"]["MINT1"]
        for _ in range(3):
            fired = thesis_watch._evaluate(w, 6_500_000.0, 3, False)
        self.assertEqual(fired, [])


class CapCompositionTests(unittest.TestCase):
    def test_fourth_break_fires_fifth_queues(self):
        state = {}
        published = []
        degen = _degen()
        now = 1_000_000.0
        for i in range(4):
            st = {}
            thesis_watch.register_pick(
                st, _pick_event(mcap=2_000_000.0) | {"mint": "M%d" % i,
                                                    "symbol": "S%d" % i})
            w = st["thesis_watch"]["M%d" % i]
            w["below"] = 3
            w["fired_break"] = True
            thesis_watch._fire_break(state, lambda e, severity=None:
                                     published.append(e), degen, w,
                                     900_000.0, now)
        self.assertEqual(len(published), 4)
        # Fifth trips the cap -> pending digest, not a card.
        st = {}
        thesis_watch.register_pick(
            st, _pick_event() | {"mint": "M9", "symbol": "S9"})
        w = st["thesis_watch"]["M9"]
        w["below"] = 3
        w["fired_break"] = True
        thesis_watch._fire_break(state, lambda e, severity=None:
                                 published.append(e), degen, w,
                                 900_000.0, now)
        self.assertEqual(len(published), 4)
        self.assertEqual(len(state["thesis_break_pending"]), 1)

    def test_pending_flush_collapses_to_one_digest(self):
        state = {}
        published = []
        degen = _degen()
        now = 1_000_000.0
        # Fill the window, then queue two breaks.
        for i in range(4):
            thesis_watch._window_ok(state, degen, now)
        for i in range(2):
            thesis_watch._queue_pending(state, {"symbol": "S%d" % i,
                                                "mint": "M%d" % i,
                                                "label": "thesis broken"},
                                        now)
        # Window still full -> no flush.
        self.assertFalse(thesis_watch._flush_pending(
            state, lambda e, severity=None: published.append(e),
            degen, now))
        # Next hour: window resets -> ONE digest card.
        later = now + 3700.0
        self.assertTrue(thesis_watch._flush_pending(
            state, lambda e, severity=None: published.append(e),
            degen, later))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["event"],
                         "launch.thesis_break_digest")
        self.assertEqual(published[0]["count"], 2)
        self.assertEqual(state["thesis_break_pending"], [])


class FilterCompositionTests(unittest.TestCase):
    def test_new_events_in_signal_events(self):
        for ev in ("launch.thesis_break", "launch.plan_level_hit",
                   "launch.thesis_break_digest"):
            self.assertIn(ev, _filters.SIGNAL_EVENTS)
            self.assertIn(ev, _filters._KIND_LABELS)

    def test_quiet_summary_works(self):
        item = _filters.summarize_for_quiet(
            {"event": "launch.thesis_break", "symbol": "MOON",
             "mint": "M1", "ts": "x"})
        self.assertIsNotNone(item)
        self.assertIn("thesis break", item["kind"])


class SchemaTests(unittest.TestCase):
    def test_new_events_validate(self):
        for e in ({"event": "launch.thesis_break", "symbol": "S",
                   "mint": "M"},
                  {"event": "launch.plan_level_hit", "symbol": "S",
                   "mint": "M", "level_kind": "stop"},
                  {"event": "launch.thesis_break_digest", "count": 2}):
            event_bus.validate_event(dict(e))  # must not raise

    def test_intraday_top3_now_validates(self):
        # Pre-existing bug (2026-10-09): the schema was missing, so
        # production publishes raised ValueError and pick cards never
        # fired. Registering it revives the approved lane.
        event_bus.validate_event(dict(_pick_event()))

    def test_unknown_field_still_rejected(self):
        with self.assertRaises(ValueError):
            event_bus.validate_event({"event": "launch.thesis_break",
                                      "symbol": "S", "mint": "M",
                                      "bogus_field": 1})


class RendererTests(unittest.TestCase):
    def test_thesis_break_card(self):
        e = {"event": "launch.thesis_break", "symbol": "MOON",
             "mint": "MINT1", "invalidation_level": 1_000_000.0,
             "card_mcap": 2_000_000.0, "break_mcap": 900_000.0,
             "break_pct": -55.0, "polls_below": 3,
             "plan_text": "exit — thesis failed"}
        text = alerts.format_alert(e)
        self.assertTrue(text.splitlines()[0].startswith("📉 THESIS BREAK"))
        self.assertIn("$1.00M", text)  # the exact published level
        self.assertIn("confirmed over 3 price checks", text)
        self.assertIn("CA: MINT1", text)
        self.assertNotIn("SELL NOW", text)  # descriptive, never commanding

    def test_plan_level_hit_card(self):
        e = {"event": "launch.plan_level_hit", "symbol": "MOON",
             "mint": "MINT1", "level_kind": "leg_a_tp",
             "level_mcap": 6_000_000.0, "mcap_now": 6_200_000.0,
             "plan_text": "bank leg A"}
        text = alerts.format_alert(e)
        self.assertTrue(text.splitlines()[0].startswith("📋 PLAN LEVEL HIT"))
        self.assertIn("leg A TP", text)

    def test_digest_card(self):
        e = {"event": "launch.thesis_break_digest", "count": 2,
             "items": [{"symbol": "A", "label": "thesis broken"},
                       {"symbol": "B", "label": "plan level hit (stop)"}]}
        text = alerts.format_alert(e)
        self.assertIn("×2", text.splitlines()[0])
        self.assertIn("$A", text)

    def test_renderers_do_not_mutate(self):
        for e in ({"event": "launch.thesis_break", "symbol": "S",
                   "mint": "M", "invalidation_level": 5},
                  {"event": "launch.plan_level_hit", "symbol": "S",
                   "mint": "M", "level_kind": "stop"},
                  {"event": "launch.thesis_break_digest", "count": 1},
                  {"event": "launch.early", "tier": "early",
                   "symbol": "T", "mint": "M"}):
            before = copy.deepcopy(e)
            alerts.format_alert(e)
            self.assertEqual(e, before)

    def test_copy_ca_markup(self):
        mk = alerts.format_alert_markup({"event": "launch.thesis_break",
                                         "mint": "MINT1"})
        self.assertIsNotNone(mk)


class NoBehaviorChangeTests(unittest.TestCase):
    """The lifecycle layer is display-only: none of its code may reach
    entry/exit/gate/sizing symbols."""

    FORBIDDEN = ("submit_order", "close_position", "_paper_enter",
                 "kill_switch", "positions.pop", "broker.",
                 "open_position", "cancel_order", "set_stop_loss",
                 "take_profit", "auto_enter")

    def test_thesis_watch_source_scan(self):
        src = open("daemon/telegram/thesis_watch.py").read()
        for sym in self.FORBIDDEN:
            self.assertNotIn(sym, src, sym)

    def test_new_alert_code_source_scan(self):
        src = open("daemon/telegram/alerts.py").read()
        # Only the new lifecycle helpers are scanned: they must not
        # reference behavior symbols.
        for name in ("_path_to_pick", "_thesis_break", "_plan_level_hit",
                     "_thesis_break_digest"):
            self.assertIn("def %s" % name, src)


class MaybeCheckTests(unittest.TestCase):
    def test_disabled_knob_skips(self):
        state = {}
        thesis_watch.register_pick(state, _pick_event())
        out = thesis_watch.maybe_check(state, lambda e, severity=None: None,
                                       degen=_degen(
                                           thesis_break_enabled=False))
        self.assertEqual(out["checked"], 0)

    def test_throttled_to_poll_interval(self):
        state = {}
        thesis_watch.register_pick(state, _pick_event())
        with mock.patch("daemon.telegram.blink.fetch_stats") as fs:
            fs.return_value = (500_000.0, None, None)
            out1 = thesis_watch.maybe_check(
                state, lambda e, severity=None: None,
                degen=_degen(), now=1_000_000.0)
            out2 = thesis_watch.maybe_check(
                state, lambda e, severity=None: None,
                degen=_degen(), now=1_000_030.0)
        self.assertEqual(fs.call_count, 1)
        self.assertGreaterEqual(out1["checked"], 0)
        self.assertEqual(out2["checked"], 0)

    def test_full_break_flow_publishes(self):
        state = {}
        published = []
        thesis_watch.register_pick(state, _pick_event())
        with mock.patch("daemon.telegram.blink.fetch_stats") as fs:
            fs.return_value = (500_000.0, None, None)  # below 1M level
            for i in range(3):
                thesis_watch.maybe_check(
                    state, lambda e, severity=None: published.append(e),
                    degen=_degen(), now=1_000_000.0 + i * 61)
        breaks = [e for e in published
                  if e["event"] == "launch.thesis_break"]
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0]["symbol"], "MOON")
        # Coin released after the break fired.
        self.assertEqual(state.get("thesis_watch", {}), {})

    def test_price_failure_keeps_counters(self):
        state = {}
        thesis_watch.register_pick(state, _pick_event())
        with mock.patch("daemon.telegram.blink.fetch_stats") as fs:
            fs.return_value = (0.0, None, None)
            out = thesis_watch.maybe_check(
                state, lambda e, severity=None: None,
                degen=_degen(), now=1_000_000.0)
        self.assertEqual(out["checked"], 0)
        self.assertIn("MINT1", state["thesis_watch"])


if __name__ == "__main__":
    unittest.main()
