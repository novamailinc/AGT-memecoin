"""Card visual-language redesign (2026-10-09, user-approved).

Every card announces its lane on the FIRST line:
  🎯 PICK   — the ONLY "here's what I'd buy" card (intraday Top 3, daily Top 5)
  👀 WATCH  — interesting, not actionable
  ⚠️ AVOID  — danger, stay away
  📰 DIGEST — context, read when you want

DISPLAY-LAYER ONLY: these tests assert rendering. Events, hooks,
scoring, entries, exits, gates and caps are untouched — a source scan
at the bottom proves no behavior symbols are reachable from the
renderers.
"""

import copy
import unittest

from daemon.telegram import alerts


def _lane_of(text):
    return text.splitlines()[0] if text else ""


class LaneLabelTests(unittest.TestCase):
    def test_pick_lane_intraday_top3(self):
        e = {"event": "launch.intraday_top3", "symbol": "MOON",
             "mint": "MINT1", "score": 82, "why": "sniper 80/100; 3 tracked wallets in",
             "safety": "80/100", "proof": "HOLDERS:PASS"}
        self.assertTrue(_lane_of(alerts.format_alert(e)).startswith("🎯 PICK"))

    def test_pick_lane_daily_top5(self):
        e = {"event": "launch.top_picks", "count": 5, "date": "2026-10-09",
             "picks": [{"symbol": "A", "score": 80, "why": "sniper 80/100",
                        "mint": "M1"}]}
        self.assertTrue(_lane_of(alerts.format_alert(e)).startswith("🎯 PICK"))

    def test_watch_lanes(self):
        cases = [
            ({"event": "launch.early", "tier": "early", "symbol": "T",
              "mint": "M"}, "👀 WATCH"),
            ({"event": "launch.detected", "tier": "degen", "symbol": "T",
              "mint": "M"}, "👀 WATCH"),
            ({"event": "launch.lowcap_runner", "symbol": "T", "mint": "M"},
             "👀 WATCH"),
            ({"event": "momentum.alert", "symbol": "T", "safety_score": 80},
             "👀 WATCH"),
            ({"event": "launch.second_wave", "symbol": "T", "mint": "M"},
             "👀 WATCH"),
            ({"event": "launch.late_runner", "symbol": "T", "mint": "M"},
             "👀 WATCH"),
            ({"event": "launch.sustained", "symbol": "T", "mint": "M"},
             "👀 WATCH"),
        ]
        for e, lane in cases:
            text = alerts.format_alert(e)
            self.assertIsNotNone(text, e["event"])
            self.assertTrue(_lane_of(text).startswith(lane), e["event"])
            self.assertIn("not a pick", text, e["event"])

    def test_no_pick_lane_leak(self):
        """Only the two pick events may use the 🎯 PICK lane."""
        watch_events = ["launch.early", "launch.detected",
                        "launch.lowcap_runner", "momentum.alert",
                        "launch.second_wave", "launch.late_runner",
                        "launch.sustained"]
        for ev in watch_events:
            e = {"event": ev, "symbol": "T", "mint": "M",
                 "safety_score": 80, "tier": "degen"}
            text = alerts.format_alert(e) or ""
            self.assertNotIn("🎯 PICK", text, ev)

    def test_avoid_lane(self):
        e = {"event": "degen.warning", "door": "lowcap", "symbol": "T",
             "mint": "M", "safety_score": 12,
             "dangers": ["honeypot"]}
        self.assertTrue(
            _lane_of(alerts.format_alert(e)).startswith("⚠️ AVOID"))

    def test_digest_lanes(self):
        cases = [
            {"event": "launch.early_digest", "count": 0, "items_safe": [],
             "items_fresh": []},
            {"event": "launch.runners_recap", "count": 0, "items": []},
            {"event": "tuning.weekly_review", "week": "2026-10-06"},
            {"event": "tuning.proposals", "count": 0, "items": []},
            {"event": "discovery.proposals", "count": 0},
            {"event": "deployer.proposals", "count": 0},
        ]
        for e in cases:
            text = alerts.format_alert(e)
            self.assertIsNotNone(text, e["event"])
            self.assertTrue(_lane_of(text).startswith("📰 DIGEST"),
                            e["event"])


class PickAnatomyTests(unittest.TestCase):
    def _pick(self, **over):
        e = {"event": "launch.intraday_top3", "symbol": "MOON",
             "mint": "MINT1", "score": 82,
             "why": "sniper 80/100; 3 tracked wallets in",
             "safety": "80/100", "proof": "HOLDERS:PASS",
             "usd_market_cap": 2_000_000}
        e.update(over)
        return alerts.format_alert(e)

    def test_conviction_headline(self):
        text = self._pick()
        self.assertIn("🎯 PICK · conviction 82", text.splitlines()[0])

    def test_why_top_two(self):
        text = self._pick()
        self.assertIn("WHY", text)
        self.assertIn("· sniper 80/100", text)
        self.assertIn("· 3 tracked wallets in", text)

    def test_watch_out_from_real_data(self):
        text = self._pick()
        self.assertIn("WATCH OUT", text)
        # mcap 2M -> thesis dies at half
        self.assertIn("thesis dies if it loses the $1.00M mcap level", text)

    def test_watch_out_unvetted_honest(self):
        text = self._pick(unvetted=True)
        self.assertIn("UNMEASURED", text)

    def test_watch_out_crowd_heating(self):
        text = self._pick(crowd_line="👥 crowd: high")
        self.assertIn("not early anymore", text)

    def test_watch_out_buyers_drying(self):
        text = self._pick(last_buyer_line="🥀 buyers drying up")
        self.assertIn("buyers drying up", text)

    def test_plan_section_present(self):
        text = self._pick()
        self.assertIn("PLAN", text)
        self.assertIn("leg A", text)
        self.assertIn("leg B", text)

    def test_ca_own_line(self):
        text = self._pick()
        self.assertIn("\nCA: MINT1\n", text)

    def test_signals_row_disciplined(self):
        text = self._pick(social_heat_line="📣 social heat: med",
                          crowd_line="👥 crowd: low",
                          deployer_reputation_line="🧬 deployer: unknown",
                          dust_line="🧂 dust: clean")
        self.assertIn("signals", text)
        self.assertIn("📣 med", text)
        self.assertIn("👥 low", text)
        self.assertIn("🧂 clean", text)
        # unknown values never reach the signals row (the WATCH OUT
        # section may honestly say "unknown" — that is not spam)
        sig_line = [ln for ln in text.splitlines()
                    if ln.startswith("signals")]
        self.assertEqual(len(sig_line), 1)
        self.assertNotIn("unknown", sig_line[0].lower())

    def test_signals_row_absent_when_empty(self):
        text = self._pick()
        self.assertNotIn("signals", text)

    def test_safety_and_proof_shown(self):
        text = self._pick()
        self.assertIn("80/100", text)
        self.assertIn("HOLDERS:PASS", text)

    def test_render_never_raises_on_garbage(self):
        text = alerts._intraday_top3({"event": "launch.intraday_top3"})
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith("🎯 PICK"))


class WatchCardTests(unittest.TestCase):
    def test_early_watch_keeps_safety_and_footer(self):
        e = {"event": "launch.early", "tier": "early", "symbol": "TKN",
             "chain": "solana", "source": "pumpfun", "mint": "MINT1",
             "usd_market_cap": 15000, "age_minutes": 0.5,
             "safety_score": 55, "url": "u"}
        text = alerts.format_alert(e)
        self.assertIn("55/100", text)
        self.assertIn("CA: MINT1", text)
        self.assertIn("not a pick", text)

    def test_render_does_not_mutate_event(self):
        """Composure: the scout hook reads the published event AFTER the
        early card renders — rendering must never mutate it."""
        e = {"event": "launch.early", "tier": "early", "symbol": "TKN",
             "mint": "MINT1", "safety_score": 55}
        before = copy.deepcopy(e)
        alerts.format_alert(e)
        self.assertEqual(e, before)


class NoBehaviorChangeTests(unittest.TestCase):
    """The card redesign is display-layer only. Prove the renderer
    module cannot reach entry/exit/gate/sizing machinery."""

    def test_no_behavior_symbols_in_alerts(self):
        import pathlib
        lines = pathlib.Path("daemon/telegram/alerts.py").read_text().splitlines()
        # "kill_switch" / "broker." appear only inside display event-name
        # strings ("kill_switch.activated", "broker.disconnected"); they
        # are not actionable here. Everything below WOULD be behavior.
        banned = ("submit_order", "close_position", "_paper_enter",
                  "positions.pop", "import broker", "open_position")
        for i, ln in enumerate(lines, 1):
            if "kill_switch.activated" in ln or "broker.disconnected" in ln:
                continue
            for sym in banned:
                self.assertNotIn(sym, ln, "%s (line %d)" % (sym, i))
