"""Tests for the graduation filter (Phase 5, judgment mode).

Covers: three-valued graduation detection (migration annotation,
pump.fun complete flag, DexScreener venue with exact "pump" match),
scout bypass, unknown-fails-closed for full-size entries, disabled
knob, scale-up path filtering, exits/safety untouched, and the
graduation.blocked event schema.

Test-repair note (2026-10-23): state isolation added at import time
(same pattern as tests/test_killswitch.py, tests/test_mirror.py and
tests/test_veto_log.py). _paper_enter's killswitch gate (added
2026-10-15, after this suite was written) does a bare
``import killswitch`` — resolvable only when daemon/launch_scanner is
on sys.path, which every production entry script arranges
(scripts/grad-trader, scripts/degen-feed, ...) but this suite never
did — and it reads $FOREX_AGENT_HOME/killswitch/state.json, where the
live main lane has been halted since 2026-10-15. Without isolation
every PaperEnterGraduationTests entry died at the gate with
"killswitch gate error" before the graduation filter was reached.
The PaperEnter tests also stub the veto engine (added 2026-10-16 in
front of the graduation check): veto_check's bundle scan makes live
Solana RPC calls, which made these tests network-dependent; the veto
has its own suites (test_veto_log, test_verdict_ledger), and stubbing
it to "passed" is the established style (test_grad, test_ceo,
test_mirror). No assertion was changed or removed.
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ["FOREX_AGENT_TESTING"] = "1"

# Isolation BEFORE any daemon import: temp agent home (killswitch and
# veto resolve their state/log paths from FOREX_AGENT_HOME, and
# storage resolves its DB from FOREX_AGENT_STORAGE, at import time)
# and the launch_scanner dir on sys.path for the bare sibling imports
# the production entry scripts rely on (`import killswitch`).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="forex_graduation_")
os.environ["FOREX_AGENT_HOME"] = _TMP
os.environ["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "test.db")
for _p in (os.path.join(_REPO, "daemon", "launch_scanner"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from daemon.launch_scanner import graduation
from daemon.launch_scanner.__init__ import LaunchScanner
from agent.events import bus as event_bus


def _degen(**kw):
    base = dict(
        graduation_filter_enabled=True,
        entry_sol=0.05,
        max_positions=5,
        auto_enter=True,
        stop_loss_pct=30.0,
        take_profit_pct=200.0,
        max_hold_hours=4.0,
        two_tier_enabled=True,
        tier_b_max_hold_hours=24.0,
        daily_max_loss_sol=1000.0,
        scout_enabled=True,
        scout_size_sol=0.01,
        scout_max_open=10,
        scout_daily_loss_budget_sol=0.05,
        scout_cooldown_hours=6.0,
        early_min_score=1.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _card(**kw):
    c = {"entry": None, "note": "", "symbol": "TKN"}
    c.update(kw)
    return c


class IsGraduatedTests(unittest.TestCase):
    def test_migration_annotation_means_graduated(self):
        state = {"seen": {"M1": {"extra": {"migration": {
            "pool": "POOL1", "market_id": "MKT", "ts": 1.0}}}}}
        self.assertTrue(graduation.is_graduated(state, "M1", _live_dex=None))

    def test_complete_true_means_graduated(self):
        card = _card(extra={"complete": True})
        self.assertTrue(graduation.is_graduated({}, "M1", card,
                                                _live_dex=None))

    def test_complete_false_means_bonding_curve(self):
        card = _card(extra={"complete": False})
        self.assertFalse(graduation.is_graduated({}, "M1", card,
                                                 _live_dex=None))

    def test_pumpswap_venue_means_graduated(self):
        card = _card(dex="pumpswap")
        self.assertTrue(graduation.is_graduated({}, "M1", card,
                                                _live_dex=None))

    def test_raydium_venue_means_graduated(self):
        card = _card(dex="raydium")
        self.assertTrue(graduation.is_graduated({}, "M1", card,
                                                _live_dex=None))

    def test_pump_venue_means_bonding_curve(self):
        # Exact match: "pump" is the bonding curve...
        card = _card(dex="pump")
        self.assertFalse(graduation.is_graduated({}, "M1", card,
                                                  _live_dex=None))

    def test_pumpswap_not_confused_with_pump(self):
        # ...but "pumpswap" merely contains the substring and is a DEX.
        card = _card(dex="PumpSwap")
        self.assertTrue(graduation.is_graduated({}, "M1", card,
                                                 _live_dex=None))

    def test_no_data_means_unknown(self):
        self.assertIsNone(graduation.is_graduated({}, "M1", _card(),
                                                   _live_dex=None))

    def test_fake_lookup_dict(self):
        self.assertTrue(graduation.is_graduated({}, "M1", _card(),
                                                _live_dex={"dex": "orca"}))
        self.assertFalse(graduation.is_graduated({}, "M1", _card(),
                                                 _live_dex={"dex": "pump"}))
        self.assertIsNone(graduation.is_graduated({}, "M1", _card(),
                                                  _live_dex={"dex": ""}))

    def test_empty_mint_is_unknown(self):
        self.assertIsNone(graduation.is_graduated({}, "", _card(),
                                                   _live_dex=None))

    def test_migration_beats_complete_false(self):
        # A recorded migration wins over a stale complete=False flag.
        state = {"seen": {"M1": {"extra": {
            "migration": {"pool": "P", "ts": 1.0}}}}}
        card = _card(extra={"complete": False})
        self.assertTrue(graduation.is_graduated(state, "M1", card,
                                                _live_dex=None))


class CheckTests(unittest.TestCase):
    def test_disabled_allows_everything(self):
        ok, reason, g = graduation.check(
            {}, _degen(graduation_filter_enabled=False),
            "M1", _card(), "launch", _live_dex=None)
        self.assertTrue(ok)
        self.assertIn("disabled", reason)

    def test_scout_bypasses_even_ungraduated(self):
        card = _card(extra={"complete": False})
        ok, reason, _ = graduation.check(
            {}, _degen(), "M1", card, "scout", _live_dex=None)
        self.assertTrue(ok)
        self.assertIn("scout", reason)

    def test_graduated_allows_full_size(self):
        card = _card(extra={"complete": True})
        ok, _, g = graduation.check({}, _degen(), "M1", card, "launch",
                                    _live_dex=None)
        self.assertTrue(ok)
        self.assertTrue(g)

    def test_bonding_curve_blocks_full_size(self):
        card = _card(dex="pump")
        ok, reason, g = graduation.check({}, _degen(), "M1", card,
                                         "launch", _live_dex=None)
        self.assertFalse(ok)
        self.assertFalse(g)
        self.assertIn("not graduated", reason)

    def test_unknown_blocks_full_size(self):
        # Fail-closed: not proven graduated = no full-size entry.
        ok, reason, g = graduation.check({}, _degen(), "M1", _card(),
                                         "launch", _live_dex=None)
        self.assertFalse(ok)
        self.assertIsNone(g)
        self.assertIn("unknown", reason)

    def test_scale_up_path_is_filtered(self):
        # A scouted coin chart-confirming later re-enters with a
        # non-scout tag — that IS the scale-up path, and it must be
        # filtered like any full-size entry.
        card = _card(dex="pump")
        ok, _, _ = graduation.check({}, _degen(), "M1", card, "launch",
                                    _live_dex=None)
        self.assertFalse(ok)

    def test_check_fail_open_on_code_error(self):
        # A bug inside the check must never halt entries: fail-open.
        orig = graduation.is_graduated
        try:
            def _boom(*a, **k):
                raise RuntimeError("boom")
            graduation.is_graduated = _boom
            ok, reason, _ = graduation.check({}, _degen(), "M1", _card(),
                                             "launch", _live_dex=None)
            self.assertTrue(ok)
            self.assertIn("fail-open", reason)
        finally:
            graduation.is_graduated = orig


class PaperEnterGraduationTests(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)
        # Isolate the unit under test (the graduation filter) from the
        # veto gate that _paper_enter runs first: veto_check's bundle
        # scan performs live Solana RPC, so unstubbed these tests pass
        # or fail (and take seconds-to-minutes) based on the network.
        # Established suite style: test_grad/test_ceo/test_mirror stub
        # their module's veto dependency to a passing result the same
        # way. The killswitch gate is NOT stubbed — it reads the temp
        # FOREX_AGENT_HOME set at import, where no lane is halted.
        from daemon.launch_scanner import veto as veto_mod
        self._veto_patch = patch.object(
            veto_mod, "veto_check",
            return_value={"passed": True, "vetoes": [], "flags": [],
                          "details": {}})
        self._veto_patch.start()

    def tearDown(self):
        self._veto_patch.stop()

    def _adapter(self, price=0.001):
        ad = SimpleNamespace()
        ad._price_sol_quiet = lambda symbol: price
        ad.submit_order = lambda req: SimpleNamespace(ticket=4242,
                                                      price=price)
        ad.close_position = lambda ticket: price * 2
        return ad

    def _state(self):
        return {"degen_positions": {}, "seen": {}}

    def test_ungraduated_full_size_blocked(self):
        d = _degen()
        state = self._state()
        card = _card(dex="pump")
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "M1", "TKN")
        self.assertFalse(ok)
        self.assertEqual(state["degen_positions"], {})
        self.assertIn("graduation filter", card["note"])
        self.assertIn("alert only", card["note"])

    def test_graduated_full_size_allowed(self):
        d = _degen()
        state = self._state()
        card = _card(extra={"complete": True})
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "M1", "TKN")
        self.assertTrue(ok)
        self.assertIn("M1", state["degen_positions"])

    def test_scout_bypasses_filter(self):
        d = _degen()
        state = self._state()
        card = _card(dex="pump")  # bonding curve — scout still allowed
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "M1", "TKN", tag="scout",
                                  volume_sol=0.01)
        self.assertTrue(ok)
        self.assertIn("scout:M1", state["degen_positions"])

    def test_unknown_graduation_blocks_full_size(self):
        d = _degen()
        state = self._state()
        card = _card()  # no venue info, no lookup under tests
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "M1", "TKN")
        self.assertFalse(ok)
        self.assertEqual(state["degen_positions"], {})

    def test_filter_disabled_allows(self):
        d = _degen(graduation_filter_enabled=False)
        state = self._state()
        card = _card(dex="pump")
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "M1", "TKN")
        self.assertTrue(ok)

    def test_second_wave_tag_is_filtered(self):
        d = _degen()
        state = self._state()
        card = _card(dex="pump")
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "M1", "TKN", wave=2)
        self.assertFalse(ok)
        self.assertEqual(state["degen_positions"], {})

    def test_exits_untouched_for_allowed_entry(self):
        # Two-tier machinery intact for entries the filter allows.
        d = _degen()
        state = self._state()
        card = _card(extra={"complete": True})
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "M1", "TKN")
        self.assertTrue(ok)
        self.assertIn("M1", state["degen_positions"])
        self.assertIn("M1|B", state["degen_positions"])


class EventSchemaTests(unittest.TestCase):
    def test_graduation_blocked_schema_validates(self):
        evt = event_bus.validate_event({
            "event": "graduation.blocked",
            "mint": "M1",
            "symbol": "TKN",
            "tag": "launch",
            "graduation_state": "unknown",
            "reason": "graduation unknown — not proven graduated",
        })
        self.assertEqual(evt["event"], "graduation.blocked")

    def test_record_block_skipped_under_tests(self):
        # Must not touch the live journal from the suite.
        graduation.record_block("M1", "TKN", "launch", None, "unknown")
        # No assertion needed — the call must simply not raise and not
        # write. (FOREX_AGENT_TESTING=1 is set at module import.)


class BlockSummaryTests(unittest.TestCase):
    def test_empty_db_yields_zeros(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db") as tf:
            import sqlite3
            db = sqlite3.connect(tf.name)
            db.execute("create table journal (ts text, payload text, "
                       "kind text)")
            db.commit()
            db.close()
            s = graduation.block_summary(tf.name, days=7.0)
        self.assertEqual(s["blocks"], 0)
        self.assertEqual(s["mints"], [])


if __name__ == "__main__":
    unittest.main()
