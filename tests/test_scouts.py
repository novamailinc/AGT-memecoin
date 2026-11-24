"""Tests for the scout engine (Phase 3, judgment mode).

Covers qualification (holders FAIL blocks / unknown allows / LP and
distribution ignored), safety re-checks, caps/cooldowns/budget,
tagging + proof-snapshot recording, kill-switch respect, the
_paper_enter volume override, and config knob declarations.
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

# --- Test isolation (repair 2026-10-23) -----------------------------------
# LaunchScanner._paper_enter gates every paper entry with a bare
# ``import killswitch`` and fails CLOSED on any gate error. Two live-state
# leaks broke the PaperEnterScoutTests when this file is run standalone
# (``PYTHONPATH=. python tests/test_scouts.py``):
#   1. daemon/launch_scanner/ is not on sys.path in that invocation, so
#      the bare import raised ModuleNotFoundError and every entry was
#      refused with "killswitch gate error".
#   2. Once importable, killswitch reads the LIVE state file under
#      ~/.forex-agent/killswitch/ at import time, where the "main" lane
#      is halted — entries were refused for a production reason.
# Fix (established suite style, cf. test_killswitch.py / test_steady.py):
# put daemon/launch_scanner/ on sys.path and redirect FOREX_AGENT_HOME
# to a fresh empty temp dir BEFORE any scanner/killswitch import, so the
# gate sees an empty (unhalted) state. FOREX_AGENT_TESTING=1 keeps the
# graduation filter local (no live Dex sentinel lookup, no live journal
# writes). No assertion below was changed.
_TEST_HOME = tempfile.mkdtemp(prefix="forex-test-scouts-")
os.environ["FOREX_AGENT_HOME"] = _TEST_HOME
os.environ["FOREX_AGENT_TESTING"] = "1"

# Import-order guard: if an earlier test module already imported
# killswitch in this pytest process, its import-time HOME snapshot
# points at the default home (unreadable under the test env, and the
# gate fails CLOSED = halted). Realign its paths to this file's
# redirected home; the dir is fresh and empty either way.
from daemon.launch_scanner import killswitch as _killswitch_mod
if _killswitch_mod.HOME != _TEST_HOME:
    _killswitch_mod.HOME = _TEST_HOME
    _killswitch_mod.STATE_DIR = os.path.join(_TEST_HOME, "killswitch")
    _killswitch_mod.STATE_FILE = os.path.join(
        _killswitch_mod.STATE_DIR, "state.json")
    _killswitch_mod.SHADOW_FILE = os.path.join(
        _killswitch_mod.STATE_DIR, "state.json.bak")
    _killswitch_mod.LOCK_FILE = os.path.join(
        _killswitch_mod.STATE_DIR, "state.lock")
    os.makedirs(_killswitch_mod.STATE_DIR, exist_ok=True)
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "daemon", "launch_scanner"))

from daemon.launch_scanner import scouts
from daemon.launch_scanner.__init__ import LaunchScanner


def _degen(**kw):
    base = dict(
        scout_enabled=True,
        scout_size_sol=0.01,
        scout_max_open=10,
        scout_daily_loss_budget_sol=0.05,
        scout_cooldown_hours=6.0,
        early_min_score=1.0,
        entry_sol=0.05,
        max_positions=5,
        auto_enter=True,
        stop_loss_pct=30.0,
        take_profit_pct=200.0,
        max_hold_hours=4.0,
        two_tier_enabled=True,
        tier_b_max_hold_hours=24.0,
        daily_max_loss_sol=1.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _report(score=50.0, fails=(), unknown=False):
    return {"score": score, "fails": list(fails), "unknown": unknown}


def _proof(holders="pass", lp="unknown", dist="pass"):
    return {"holders": {"verdict": holders, "detail": ""},
            "lp": {"verdict": lp, "detail": ""},
            "distribution": {"verdict": dist, "detail": ""}}


def _q(degen, mint="MINT1", report=None, proof_rec=None, state=None,
       now=1_000_000.0):
    st = state if state is not None else {}
    return scouts.qualify(st, degen, mint, "solana",
                          report=_report() if report is None else report,
                          now=now, _proof_rec=proof_rec)


class QualifyTests(unittest.TestCase):
    def test_disabled_blocks(self):
        ok, reason, _ = _q(_degen(scout_enabled=False))
        self.assertFalse(ok)
        self.assertIn("disabled", reason)

    def test_holders_fail_blocks(self):
        ok, reason, _ = _q(_degen(), proof_rec=_proof(holders="fail"))
        self.assertFalse(ok)
        self.assertIn("holders FAIL", reason)

    def test_holders_unknown_allows(self):
        ok, reason, _ = _q(_degen(), proof_rec=_proof(holders="unknown"))
        self.assertTrue(ok, reason)

    def test_holders_pass_allows(self):
        ok, reason, _ = _q(_degen(), proof_rec=_proof(holders="pass"))
        self.assertTrue(ok, reason)

    def test_lp_fail_ignored(self):
        # LP check is dead (0% coverage) — never gates a scout.
        ok, reason, _ = _q(_degen(), proof_rec=_proof(lp="fail"))
        self.assertTrue(ok, reason)

    def test_distribution_fail_ignored(self):
        # Distribution is WATCH-only — never gates a scout.
        ok, reason, _ = _q(_degen(), proof_rec=_proof(dist="fail"))
        self.assertTrue(ok, reason)

    def test_hard_fail_report_blocks(self):
        ok, reason, _ = _q(
            _degen(), report=_report(fails=["insider cluster 30%"]))
        self.assertFalse(ok)
        self.assertIn("hard-fail", reason)

    def test_unknown_report_blocks(self):
        ok, _, _ = _q(_degen(), report=_report(unknown=True))
        self.assertFalse(ok)

    def test_below_floor_blocks(self):
        ok, _, _ = _q(_degen(), report=_report(score=0.5))
        self.assertFalse(ok)

    def test_cooldown_blocks_then_expires(self):
        st = {}
        d = _degen()
        ok, _, _ = scouts.qualify(st, d, "M", "solana", report=_report(),
                                  now=1000.0, _proof_rec=_proof())
        self.assertTrue(ok)
        scouts.record_scout(st, "M", _proof(), 0.01, now=1000.0)
        ok, reason, _ = scouts.qualify(
            st, d, "M", "solana", report=_report(), now=1000.0 + 3600,
            _proof_rec=_proof())
        self.assertFalse(ok)
        self.assertIn("cooldown", reason)
        ok, _, _ = scouts.qualify(
            st, d, "M", "solana", report=_report(),
            now=1000.0 + 7 * 3600, _proof_rec=_proof())
        self.assertTrue(ok)

    def test_max_open_blocks(self):
        st = {"degen_positions": {}}
        for i in range(10):
            st["degen_positions"]["scout:M%d" % i] = {
                "label": "scout", "mint": "M%d" % i}
        ok, reason, _ = _q(_degen(), mint="NEW", state=st,
                           proof_rec=_proof())
        self.assertFalse(ok)
        self.assertIn("max open", reason)

    def test_non_scout_positions_do_not_count(self):
        st = {"degen_positions": {}}
        for i in range(10):
            st["degen_positions"]["M%d" % i] = {
                "label": "launch", "mint": "M%d" % i}
        ok, _, _ = _q(_degen(), mint="NEW", state=st, proof_rec=_proof())
        self.assertTrue(ok)

    def test_daily_budget_halts_new_scouts(self):
        # Simulate a day already past the loss budget via the journal.
        import sqlite3, tempfile, os
        from datetime import datetime, timezone
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE journal (ts TEXT, kind TEXT, symbol TEXT,"
                       " direction TEXT, outcome TEXT, payload TEXT)")
            now = datetime.now(timezone.utc).isoformat()
            import json as _json
            db.execute(
                "INSERT INTO journal VALUES (?, ?, ?, ?, ?, ?)",
                (now, "event", "X", None, None,
                 _json.dumps({"kind": "event", "symbol": "X", "payload": {
                     "event": "position.closed",
                     "ticket": 1, "symbol": "X",
                     "reason": "degen_stop_loss",
                     "label": "scout", "profit": -0.06}})))
            db.commit()
            db.close()
            pnl = scouts.daily_scout_pnl_sol(path)
            self.assertAlmostEqual(pnl, -0.06)
            # Budget is 0.05 -> -0.06 trips it.
            import unittest.mock as mock
            with mock.patch("storage.store.DEFAULT_DB_PATH", path):
                ok, reason, _ = _q(_degen(), proof_rec=_proof())
            self.assertFalse(ok)
            self.assertIn("budget", reason)
        finally:
            os.unlink(path)


class CountingTests(unittest.TestCase):
    def test_leg_pair_counts_once(self):
        pos = {"scout:M1": {"label": "scout", "mint": "M1"},
               "scout:M1|B": {"label": "scout", "mint": "M1"},
               "M2": {"label": "launch", "mint": "M2"}}
        self.assertEqual(scouts.open_scout_count(pos), 1)

    def test_empty_counts_zero(self):
        self.assertEqual(scouts.open_scout_count(None), 0)
        self.assertEqual(scouts.open_scout_count({}), 0)


class SnapshotTests(unittest.TestCase):
    def test_proof_snapshot_compact(self):
        self.assertEqual(
            scouts.proof_snapshot(_proof(holders="fail")),
            {"lp": "unknown", "holders": "fail",
             "distribution": "pass"})

    def test_proof_snapshot_none(self):
        self.assertEqual(scouts.proof_snapshot(None),
                         {"lp": "?", "holders": "?", "distribution": "?"})

    def test_record_log_bounded(self):
        st = {}
        for i in range(250):
            scouts.record_scout(st, "M%d" % i, _proof(), 0.01,
                                now=float(i))
        self.assertLessEqual(len(st["scout_log"]), 200)
        self.assertIn("M1", st["scout_cooldown"])


class WeeklySummaryTests(unittest.TestCase):
    def test_empty_journal(self):
        import sqlite3, tempfile, os
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE journal (ts TEXT, kind TEXT, symbol TEXT,"
                       " direction TEXT, outcome TEXT, payload TEXT)")
            db.commit()
            db.close()
            s = scouts.weekly_summary(path, "/nonexistent/fills.jsonl")
            self.assertEqual(s["closes"], 0)
            self.assertIsNone(s["avg_winner_mult"])
            self.assertEqual(s["kill_bar_mult"], 14.5)
        finally:
            os.unlink(path)

    def test_summary_joins_fills(self):
        import sqlite3, tempfile, os, json as _json
        from datetime import datetime, timezone
        fd, dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, fpath = tempfile.mkstemp(suffix=".jsonl")
        try:
            now = datetime.now(timezone.utc).isoformat()
            db = sqlite3.connect(dbpath)
            db.execute("CREATE TABLE journal (ts TEXT, kind TEXT, symbol TEXT,"
                       " direction TEXT, outcome TEXT, payload TEXT)")

            def _row(evt):
                return (now, "event", evt.get("symbol"), None, None,
                        _json.dumps({"kind": "event",
                                     "symbol": evt.get("symbol"),
                                     "payload": evt}))
            db.execute("INSERT INTO journal VALUES (?, ?, ?, ?, ?, ?)",
                       _row({"event": "position.closed",
                             "ticket": 7, "symbol": "WIN",
                             "reason": "degen_take_profit",
                             "label": "scout", "profit": 0.09}))
            db.execute("INSERT INTO journal VALUES (?, ?, ?, ?, ?, ?)",
                       _row({"event": "position.closed",
                             "ticket": 8, "symbol": "LOSE",
                             "reason": "degen_stop_loss",
                             "label": "scout", "profit": -0.003}))
            # Non-scout close must not leak in.
            db.execute("INSERT INTO journal VALUES (?, ?, ?, ?, ?, ?)",
                       _row({"event": "position.closed",
                             "ticket": 9, "symbol": "BIG",
                             "reason": "degen_take_profit",
                             "label": "launch", "profit": 5.0}))
            db.commit()
            db.close()
            with open(fpath, "w") as fh:
                fh.write(_json.dumps({"ticket": 7, "kind": "BUY",
                                      "price_sol": 0.001}) + "\n")
                fh.write(_json.dumps({"ticket": 7, "kind": "CLOSE",
                                      "price_sol": 0.02}) + "\n")
                fh.write(_json.dumps({"ticket": 8, "kind": "BUY",
                                      "price_sol": 0.001}) + "\n")
                fh.write(_json.dumps({"ticket": 8, "kind": "CLOSE",
                                      "price_sol": 0.0007}) + "\n")
            s = scouts.weekly_summary(dbpath, fpath)
            self.assertEqual(s["closes"], 2)
            self.assertEqual(s["wins"], 1)
            self.assertEqual(s["losses"], 1)
            self.assertAlmostEqual(s["avg_winner_mult"], 20.0)
        finally:
            os.unlink(dbpath)
            os.unlink(fpath)


class PaperEnterScoutTests(unittest.TestCase):
    def setUp(self):
        self.sc = LaunchScanner.__new__(LaunchScanner)

    def _adapter(self, price=0.001, fail=False):
        def _result():
            return SimpleNamespace(ticket=4242, price=price)
        ad = SimpleNamespace()
        ad._price_sol_quiet = lambda symbol: price
        if fail:
            def _boom(req):
                raise Exception("kill-switch engaged")
            ad.submit_order = _boom
        else:
            ad.submit_order = lambda req: _result()
        ad.close_position = lambda ticket: price * 2
        return ad

    def test_scout_entry_uses_tiny_size_and_tag(self):
        d = _degen()
        state = {"degen_positions": {}}
        card = {"entry": None, "note": "",
                "proof_snapshot": {"holders": "pass"}}
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "MINT1", "TKN", tag="scout",
                                  volume_sol=0.01)
        self.assertTrue(ok)
        pos = state["degen_positions"]["scout:MINT1"]
        self.assertEqual(pos["label"], "scout")
        # Two-tier split of the 0.01 scout size.
        self.assertAlmostEqual(pos["volume_sol"], 0.005)
        self.assertEqual(card["entry"]["volume_sol"], 0.01)
        self.assertEqual(pos["proof_snapshot"], {"holders": "pass"})
        # Leg B also tagged scout, no fixed TP.
        pos_b = state["degen_positions"]["scout:MINT1|B"]
        self.assertEqual(pos_b["label"], "scout")
        self.assertIsNone(pos_b["tp"])

    def test_kill_switch_refuses_scout(self):
        d = _degen()
        state = {"degen_positions": {}}
        card = {"entry": None, "note": ""}
        ok = self.sc._paper_enter(None, self._adapter(fail=True), state,
                                  card, d, "MINT1", "TKN", tag="scout",
                                  volume_sol=0.01)
        self.assertFalse(ok)
        self.assertEqual(state["degen_positions"], {})

    def test_default_volume_unchanged(self):
        # Non-scout callers without the override keep entry_sol.
        # (Graduation evidence on the card so the Phase 5 filter lets
        # the entry through — this test is about the volume default.)
        d = _degen()
        state = {"degen_positions": {}}
        card = {"entry": None, "note": "", "extra": {"complete": True}}
        ok = self.sc._paper_enter(None, self._adapter(), state, card, d,
                                  "MINT1", "TKN")
        self.assertTrue(ok)
        self.assertAlmostEqual(
            state["degen_positions"]["MINT1"]["volume_sol"], 0.025)


class ConfigKnobTests(unittest.TestCase):
    def test_degen_config_declares_scout_knobs(self):
        from config.config import DegenConfig
        import dataclasses
        names = {f.name for f in dataclasses.fields(DegenConfig)}
        for k in ("scout_enabled", "scout_size_sol", "scout_max_open",
                  "scout_daily_loss_budget_sol", "scout_cooldown_hours"):
            self.assertIn(k, names)

    def test_yaml_declares_scout_knobs(self):
        import yaml
        with open("config/defaults.yaml") as fh:
            y = yaml.safe_load(fh)
        degen = y.get("degen") or {}
        for k in ("scout_enabled", "scout_size_sol", "scout_max_open",
                  "scout_daily_loss_budget_sol", "scout_cooldown_hours"):
            self.assertIn(k, degen, "defaults.yaml missing degen.%s" % k)
        self.assertEqual(degen["scout_size_sol"], 0.01)
        self.assertEqual(degen["scout_max_open"], 10)


if __name__ == "__main__":
    unittest.main()
