"""Tests for the intraday proof-gated Top 3 (Phase 4, judgment mode).

Covers gate logic (holders FAIL blocks / unknown allowed with honest
labeling / hard fails and below-floor block / unvetted allowed but
labeled UNVETTED), daily cap enforcement, per-coin cooldown, zero-pick
days dropping nothing, 4/hour cap composition (a trip never burns the
cooldown), alert-only behavior (no positions opened, ever), WAT day
rollover, and config knob declarations.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from daemon.launch_scanner import intraday_top3


def _degen(**kw):
    base = dict(
        intraday_top3_enabled=True,
        intraday_top3_max_per_day=5,
        intraday_top3_cooldown_hours=6.0,
        early_min_score=1.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _cfg(**kw):
    return SimpleNamespace(degen=_degen(**kw))


def _report(score=50.0, fails=(), unknown=False):
    return {"score": score, "fails": list(fails), "unknown": unknown}


def _proof(holders="pass", lp="unknown", dist="pass"):
    return {"holders": {"verdict": holders, "detail": ""},
            "lp": {"verdict": lp, "detail": ""},
            "distribution": {"verdict": dist, "detail": ""}}


def _snap(mint="MINT1", **kw):
    s = dict(
        mint=mint, symbol="SYM", chain="solana",
        sniper=80.0, winner=70.0, holder=60.0, smart_buys=2, fp="HOT",
        mcap=500000.0,
        report=_report(), unvetted=False,
        proof_rec=_proof(),
    )
    s.update(kw)
    return s


def _pick(cfg, snap=None, state=None, cap=True, now=1_000_000.0):
    st = state if state is not None else {}
    with mock.patch("agent.events.publish") as pub:
        pick = intraday_top3.maybe_pick(
            lambda: cap, cfg, st, _snap() if snap is None else snap,
            now=now)
    return pick, pub, st


class GateTests(unittest.TestCase):
    def test_disabled_blocks(self):
        pick, pub, _ = _pick(_cfg(intraday_top3_enabled=False))
        self.assertIsNone(pick)
        pub.assert_not_called()

    def test_holders_fail_blocks(self):
        s = _snap(proof_rec=_proof(holders="fail"))
        pick, pub, _ = _pick(_cfg(), s)
        self.assertIsNone(pick)
        pub.assert_not_called()

    def test_holders_unknown_allowed_with_honest_label(self):
        s = _snap(proof_rec=_proof(holders="unknown"))
        pick, pub, _ = _pick(_cfg(), s)
        self.assertIsNotNone(pick)
        self.assertIn("?", pick["proof"])
        pub.assert_called_once()

    def test_lp_fail_ignored(self):
        s = _snap(proof_rec=_proof(lp="fail"))
        pick, pub, _ = _pick(_cfg(), s)
        self.assertIsNotNone(pick)

    def test_distribution_fail_ignored(self):
        s = _snap(proof_rec=_proof(dist="fail"))
        pick, pub, _ = _pick(_cfg(), s)
        self.assertIsNotNone(pick)

    def test_hard_fail_report_blocks(self):
        s = _snap(report=_report(fails=["honeypot"]))
        pick, pub, _ = _pick(_cfg(), s)
        self.assertIsNone(pick)
        pub.assert_not_called()

    def test_below_floor_blocks(self):
        s = _snap(report=_report(score=0.0))
        pick, pub, _ = _pick(_cfg(), s)
        self.assertIsNone(pick)
        pub.assert_not_called()

    def test_unvetted_allowed_but_labeled(self):
        s = _snap(report=None, unvetted=True)
        pick, pub, _ = _pick(_cfg(), s)
        self.assertIsNotNone(pick)
        self.assertEqual(pick["safety"], "UNVETTED")
        pub.assert_called_once()

    def test_known_score_shown(self):
        s = _snap(report=_report(score=42.0))
        pick, pub, _ = _pick(_cfg(), s)
        self.assertIsNotNone(pick)
        self.assertEqual(pick["safety"], "42/100")


class CapTests(unittest.TestCase):
    def test_daily_cap_enforced(self):
        st = {}
        for _ in range(5):
            with mock.patch("agent.events.publish"):
                p = intraday_top3.maybe_pick(
                    lambda: True, _cfg(), st,
                    _snap(mint="M%d" % _), now=1_000_000.0)
            self.assertIsNotNone(p)
        with mock.patch("agent.events.publish") as pub:
            p = intraday_top3.maybe_pick(
                lambda: True, _cfg(), st, _snap(mint="M99"),
                now=1_000_000.0)
        self.assertIsNone(p)
        pub.assert_not_called()

    def test_zero_pick_day_drops_nothing(self):
        # A day where nothing qualifies: no cards, no state pollution.
        st = {}
        s = _snap(proof_rec=_proof(holders="fail"))
        with mock.patch("agent.events.publish") as pub:
            p = intraday_top3.maybe_pick(
                lambda: True, _cfg(), st, s, now=1_000_000.0)
        self.assertIsNone(p)
        pub.assert_not_called()
        self.assertEqual(st.get(intraday_top3.COUNT_KEY) or 0, 0)

    def test_per_coin_cooldown(self):
        st = {}
        with mock.patch("agent.events.publish"):
            p1 = intraday_top3.maybe_pick(
                lambda: True, _cfg(), st, _snap(mint="M1"),
                now=1_000_000.0)
        self.assertIsNotNone(p1)
        with mock.patch("agent.events.publish") as pub:
            p2 = intraday_top3.maybe_pick(
                lambda: True, _cfg(), st, _snap(mint="M1"),
                now=1_000_000.0 + 3600.0)
        self.assertIsNone(p2)
        pub.assert_not_called()
        # After 6h the coin is eligible again.
        with mock.patch("agent.events.publish") as pub3:
            p3 = intraday_top3.maybe_pick(
                lambda: True, _cfg(), st, _snap(mint="M1"),
                now=1_000_000.0 + 6 * 3600.0 + 1.0)
        self.assertIsNotNone(p3)
        pub3.assert_called_once()

    def test_cap_trip_does_not_burn_cooldown(self):
        st = {}
        with mock.patch("agent.events.publish") as pub:
            p = intraday_top3.maybe_pick(
                lambda: False, _cfg(), st, _snap(mint="M1"),
                now=1_000_000.0)
        self.assertIsNone(p)
        pub.assert_not_called()
        # Cooldown was NOT stamped: the next qualifying card picks it up.
        with mock.patch("agent.events.publish") as pub2:
            p2 = intraday_top3.maybe_pick(
                lambda: True, _cfg(), st, _snap(mint="M1"),
                now=1_000_000.0 + 10.0)
        self.assertIsNotNone(p2)
        pub2.assert_called_once()

    def test_wat_day_rollover_resets_count(self):
        st = {}
        for i in range(5):
            with mock.patch("agent.events.publish"):
                intraday_top3.maybe_pick(
                    lambda: True, _cfg(), st, _snap(mint="D%d" % i),
                    now=1_000_000.0)
        self.assertEqual(st.get(intraday_top3.COUNT_KEY), 5)
        # Next WAT day: the count rolls over.
        with mock.patch("agent.events.publish") as pub:
            p = intraday_top3.maybe_pick(
                lambda: True, _cfg(), st, _snap(mint="D9"),
                now=1_000_000.0 + 86400.0)
        self.assertIsNotNone(p)
        pub.assert_called_once()


class AlertOnlyTests(unittest.TestCase):
    def test_pick_opens_no_positions(self):
        pick, _, st = _pick(_cfg())
        self.assertIsNotNone(pick)
        self.assertNotIn("degen_positions", st)
        # The published event carries no entry/order fields.
        with mock.patch("agent.events.publish") as pub:
            intraday_top3.maybe_pick(
                lambda: True, _cfg(), {}, _snap(mint="ZZZ"),
                now=2_000_000.0)
        evt = pub.call_args[0][0]
        self.assertEqual(evt["event"], "launch.intraday_top3")
        for banned in ("volume_sol", "size_sol", "order", "ticket",
                       "entry", "tag"):
            self.assertNotIn(banned, evt)

    def test_pick_carries_score_why_and_proof(self):
        with mock.patch("agent.events.publish") as pub:
            pick = intraday_top3.maybe_pick(
                lambda: True, _cfg(), {}, _snap(mint="Q1"),
                now=3_000_000.0)
        self.assertIsNotNone(pick)
        self.assertGreater(pick["score"], 0)
        self.assertTrue(pick["why"])
        self.assertIn("HOLDERS", pick["proof"])
        self.assertIn("never", pick["note"].lower())
        # The Telegram card renders the CA, score, safety and proof.
        from daemon.telegram.alerts import _intraday_top3 as render
        text = render(pub.call_args[0][0])
        self.assertIn("CA: Q1", text)
        self.assertIn("🎯 PICK", text)
        self.assertIn("HOLDERS", text)


class ConfigTests(unittest.TestCase):
    def test_knobs_declared(self):
        from config.config import DegenConfig
        import dataclasses
        names = {f.name for f in dataclasses.fields(DegenConfig)}
        for k in ("intraday_top3_enabled", "intraday_top3_max_per_day",
                  "intraday_top3_cooldown_hours"):
            self.assertIn(k, names)

    def test_knob_defaults(self):
        import yaml
        with open("config/defaults.yaml", "r",
                  encoding="utf-8") as fh:
            y = yaml.safe_load(fh)
        d = (y or {}).get("degen") or {}
        self.assertTrue(d.get("intraday_top3_enabled"))
        self.assertEqual(d.get("intraday_top3_max_per_day"), 5)
        self.assertEqual(d.get("intraday_top3_cooldown_hours"), 6.0)


class QualifyTests(unittest.TestCase):
    def test_qualify_returns_safety_label(self):
        ok, reason, label = intraday_top3.qualify(
            {}, _degen(), "M1", _report(score=7.0), False,
            _proof(), now=1_000_000.0)
        self.assertTrue(ok)
        self.assertEqual(label, "7/100")

    def test_qualify_never_raises(self):
        ok, reason, label = intraday_top3.qualify(
            None, None, None, None, False, None, now=None)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
