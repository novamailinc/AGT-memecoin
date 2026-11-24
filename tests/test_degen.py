"""Tests for the degen lane evaluator (real-money manual trading alerts)."""
import unittest

from daemon.launch_scanner import degen


class HardBlockTests(unittest.TestCase):
    def test_honeypot_blocks(self):
        report = {"passed": False, "fails": ["honeypot (sell simulation)"]}
        self.assertIsNotNone(degen.hard_block_reason(report))

    def test_cannot_sell_blocks(self):
        report = {"passed": False,
                  "fails": ["cannot sell all (sell simulation)"]}
        self.assertIsNotNone(degen.hard_block_reason(report))

    def test_insider_does_not_block(self):
        # Insiders are a LABEL in degen mode, not a wall.
        report = {"passed": False, "fails": ["insider cluster > 20%"]}
        self.assertIsNone(degen.hard_block_reason(report))

    def test_unknown_report_does_not_block(self):
        self.assertIsNone(degen.hard_block_reason(None))
        self.assertIsNone(degen.hard_block_reason({"unknown": True}))

    def test_low_score_does_not_block(self):
        report = {"passed": True, "score": 1, "fails": []}
        self.assertIsNone(degen.hard_block_reason(report))


class RiskLabelTests(unittest.TestCase):
    def test_unvetted_label(self):
        labels = degen.risk_labels(None)
        self.assertTrue(any("UNVETTED" in l for l in labels))

    def test_insider_becomes_label(self):
        report = {"passed": False, "score": 10,
                  "fails": ["insider cluster > 20%"]}
        labels = degen.risk_labels(report)
        self.assertTrue(any("insider" in l for l in labels))

    def test_hard_block_not_duplicated_as_label(self):
        report = {"passed": False, "fails": ["honeypot (sell simulation)"]}
        labels = degen.risk_labels(report)
        self.assertFalse(any("honeypot" in l.lower() for l in labels))


class EvaluateTests(unittest.TestCase):
    def _market(self, **kw):
        # Evidence-based: mcap $62k (sweet spot), vol/mcap 0.28 (hot).
        m = {"mcap_usd": 62_000.0, "vol_usd": 17_000.0,
             "launched_ts": 0.0}
        m.update(kw)
        return m

    def test_strong_runner_qualifies(self):
        buyers = [{"wallet": "abc123"}, {"wallet": "def456"}]
        weights = {"abc123": 1.5, "def456": 1.2}
        report = {"passed": True, "score": 45, "fails": []}
        ok, score, reasons, labels, names = degen.evaluate(
            "mint", "solana", self._market(), report, buyers, weights,
            deployer_status="ok", now=1_000_000.0)
        self.assertTrue(ok)
        # mcap 40 + turnover 22 + smart 0 (watchlist-only 2026-10-20)
        # + fresh 5 = ~67
        self.assertGreaterEqual(score, 60.0)
        self.assertEqual(len(names), 2)

    def test_honeypot_never_qualifies(self):
        buyers = [{"wallet": "abc123"}]
        report = {"passed": False, "fails": ["honeypot (sell simulation)"]}
        ok, score, reasons, labels, names = degen.evaluate(
            "mint", "solana", self._market(), report, buyers, {"abc123": 1.5},
            now=1_000_000.0)
        self.assertFalse(ok)
        self.assertEqual(score, 0.0)
        self.assertTrue(any("hard-block" in r for r in reasons))

    def test_dead_mcap_zone_rejected(self):
        # $600k mcap: dead zone (2% precision) — no footprint, no card.
        report = {"passed": True, "score": 50, "fails": []}
        ok, score, *_ = degen.evaluate(
            "mint", "solana",
            self._market(mcap_usd=600_000.0, vol_usd=15_000.0),
            report, [], {}, now=1_000_000.0)
        self.assertFalse(ok)

    def test_no_turnover_rejected(self):
        # No volume = no footprint = no signal, even in sweet-spot mcap.
        report = {"passed": True, "score": 50, "fails": []}
        ok, score, *_ = degen.evaluate(
            "mint", "solana",
            self._market(mcap_usd=62_000.0, vol_usd=100.0),
            report, [], {}, now=1_000_000.0)
        self.assertFalse(ok)

    def test_dust_mcap_no_headroom(self):
        self.assertEqual(degen._mcap_sweet_spot_score(5_000.0), 0.0)
        self.assertEqual(degen._mcap_sweet_spot_score(62_000.0), 40.0)
        self.assertEqual(degen._mcap_sweet_spot_score(600_000.0), 0.0)

    def test_turnover_scoring(self):
        # Winners median 0.28, losers median 0.02.
        self.assertEqual(degen._turnover_score(17_000.0, 62_000.0), 22.0)
        self.assertEqual(degen._turnover_score(1_000.0, 62_000.0), 0.0)
        self.assertEqual(degen._turnover_score(0.0, 62_000.0), 0.0)

    def test_unvetted_can_qualify_with_labels(self):
        buyers = [{"wallet": "abc123"}, {"wallet": "def456"},
                  {"wallet": "ghi789"}]
        weights = {b["wallet"]: 1.3 for b in buyers}
        ok, score, reasons, labels, names = degen.evaluate(
            "mint", "solana", self._market(), None, buyers, weights,
            now=1_000_000.0)
        self.assertTrue(ok)
        self.assertTrue(any("UNVETTED" in l for l in labels))


class EvaluateLaunchTests(unittest.TestCase):
    def test_fresh_launch_qualifies(self):
        # Brand new coin, no data yet — gambleable.
        ok, score, reasons, labels = degen.evaluate_launch(
            "mint", "solana",
            {"mcap_usd": 0.0, "launched_ts": 1_000_000.0},
            {"passed": False, "score": 0, "fails": ["top holder owns 100%"]},
            now=1_000_060.0)
        self.assertTrue(ok)
        self.assertTrue(any("top holder" in l for l in labels))

    def test_honeypot_blocked(self):
        ok, score, reasons, labels = degen.evaluate_launch(
            "mint", "solana", {"mcap_usd": 0.0, "launched_ts": 1_000_000.0},
            {"passed": False, "fails": ["honeypot (sell simulation)"]},
            now=1_000_060.0)
        self.assertFalse(ok)

    def test_dust_rejected(self):
        ok, *_ = degen.evaluate_launch(
            "mint", "solana",
            {"mcap_usd": 5_000.0, "launched_ts": 1_000_000.0},
            None, now=1_000_060.0)
        self.assertFalse(ok)

    def test_stale_rejected(self):
        ok, *_ = degen.evaluate_launch(
            "mint", "solana",
            {"mcap_usd": 50_000.0, "launched_ts": 1_000_000.0},
            None, now=1_000_000.0 + 3 * 3600)
        self.assertFalse(ok)

    def test_unvetted_never_instant(self):
        # 2026-10-29 SR fix: an unknown safety report never sends an
        # instant card — unvetted coins belong to the digest only.
        for report in (None, {"unknown": True}):
            ok, *_ = degen.evaluate_launch(
                "mint", "solana",
                {"mcap_usd": 60_000.0, "launched_ts": 1_000_000.0},
                report, now=1_000_060.0)
            self.assertFalse(ok)

    def test_sr_shape_blocked(self):
        # The exact SR shape: detection mcap 0, no safety report.
        # Before the fix the 0-mcap hole let this through to enrichment
        # and the NOMCAP branch fired it anyway.
        ok, *_ = degen.evaluate_launch(
            "mint", "solana",
            {"mcap_usd": 0.0, "launched_ts": 1_000_000.0},
            None, now=1_000_060.0)
        self.assertFalse(ok)

    def test_known_score_zero_still_qualifies(self):
        # Call-site-2 purpose preserved: a KNOWN score-0 report (no hard
        # block) still qualifies at detection stage; the enriched
        # runner gate decides at send time.
        ok, *_ = degen.evaluate_launch(
            "mint", "solana",
            {"mcap_usd": 60_000.0, "launched_ts": 1_000_000.0},
            {"passed": False, "score": 0, "fails": []},
            now=1_000_060.0)
        self.assertTrue(ok)

    def test_enriched_runner_gate(self):
        # SR enriched values ($2.9k mcap, no liq) must fail; only proven
        # runners pass.
        self.assertFalse(degen.enriched_runner_ok(2_900.0, 0.0))
        self.assertFalse(degen.enriched_runner_ok(0.0, 0.0))
        self.assertFalse(degen.enriched_runner_ok(60_000.0, 100.0))
        self.assertFalse(degen.enriched_runner_ok(20_000.0, 30_000.0))
        self.assertTrue(degen.enriched_runner_ok(60_000.0, 30_000.0))
        self.assertFalse(degen.enriched_runner_ok(None, None))


class PositionSizeNoteTests(unittest.TestCase):
    def test_downside_math(self):
        note = degen.position_size_note(0.01, 300_000.0)
        self.assertIn("0.010", note)
        self.assertIn("rugs", note)


if __name__ == "__main__":
    unittest.main()


class DegenCardTests(unittest.TestCase):
    def test_degen_card_renders(self):
        from daemon.telegram.alerts import format_alert, format_alert_markup
        evt = {
            "event": "degen.alert",
            "chain": "solana",
            "mint": "6KDpQM4BVDdtAkjtyFDiBCkbii8q8BieEyXXm6kNpump",
            "symbol": "TEST",
            "chg_24h_pct": 120.0,
            "chg_1h_pct": 15.0,
            "usd_market_cap": 300_000.0,
            "degen_score": 72.0,
            "degen_reasons": ["smart money 30/40 (2 buyers)"],
            "degen_risks": ["⚠️ UNVETTED — no safety report"],
            "degen_buyers": ["abc123×1.5"],
            "degen_size_note": "💸 If this rugs, you lose 0.010 SOL.",
        }
        text = format_alert(evt)
        self.assertIn("🎲 DEGEN", text)
        self.assertIn("HIGH RISK", text)
        self.assertIn("6KDpQM4BVDdtAkjtyFDiBCkbii8q8BieEyXXm6kNpump", text)
        self.assertIn("72/100", text)
        self.assertIn("UNVETTED", text)
        self.assertIn("0.010 SOL", text)
        # Copy-CA button present.
        markup = format_alert_markup(evt)
        self.assertIsNotNone(markup)
        btn = markup["inline_keyboard"][0][0]
        self.assertEqual(btn["copy_text"]["text"], evt["mint"])

    def test_degen_card_hard_block_never_formats(self):
        # Hard-blocked coins never reach the card; formatter is pure.
        from daemon.telegram.alerts import format_alert
        evt = {"event": "degen.alert", "symbol": "X"}
        self.assertIn("DEGEN", format_alert(evt))
