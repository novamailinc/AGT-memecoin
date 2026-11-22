"""Sniper re-score (rescore.py) — miss-catch rebuild 2026-10-09.

A quality-passed launch scoring ~0 at birth (no signals exist yet) gets a
fair trial at +5/+15 min on live signals. Tests cover registration,
routing (tier-1 promotion with safety re-gate / digest band / honest
drop), restart handling, and the measurement ledger.

No network: _fresh_mcap is stubbed; rank/verdict/stage are faked.
"""

import time
import unittest
import unittest.mock
from types import SimpleNamespace

from daemon.launch_scanner import rescore


def _degen(**kw):
    base = dict(
        sniper_rescore_enabled=True,
        sniper_rescore_at_s=[300, 900, 1800, 3600],
        sniper_digest_min_score=25.0,
        sniper_tier_threshold=50.0,
        sniper_enabled=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _cfg(**kw):
    return SimpleNamespace(degen=_degen(**kw))


def _payload(mint="MINT1", **kw):
    p = dict(
        mint=mint, symbol="TKN", name="Token", chain="solana",
        url="https://x", source="pumpfun", tier="early",
        reason="quality-pass score 90", safety_score=90,
        mcap_birth=20000.0, mcap_hist=[[1000.0, 20000.0]],
        first_ts=1000.0, narratives=["other"], narrative_heat=1.0,
        sentiment=None,
    )
    p.update(kw)
    return p


class _Scanner:
    """Minimal fake of the LaunchScanner surface rescore.py needs."""

    def __init__(self, rank_score=0.0, regate=(True, "ok", {"score": 90})):
        self._rank_score = rank_score
        self._regate = regate
        self.staged = []
        self.digested = []
        self.rank_calls = 0
        self._fp_tracker = None

    def _sniper_rank(self, config, state, c, entry, new_cand, tape_stats=None):
        self.rank_calls += 1
        return {"score": self._rank_score, "tier": "sniper",
                "threshold": 50.0, "reasons": [], "enabled": True,
                "signals_snapshot": {}}

    def _instant_verdict(self, degen, c):
        return self._regate

    def _sniper_stage(self, config, state, degen, c, new_cand, entry,
                      mcap, tier, reason, report, res, now, extra=None):
        self.staged.append({"mint": c.get("mint"), "mcap": mcap,
                            "reason": reason})

    def _enqueue_early_digest(self, config, state, degen, c, new_cand,
                              mcap, tier, reason, report, res, now):
        self.digested.append({"mint": c.get("mint"), "reason": reason})


class RescoreRegisterTest(unittest.TestCase):
    def test_register_and_idempotent(self):
        state = {}
        now = time.time()
        self.assertTrue(
            rescore.register(state, _degen(), now, "M1", _payload("M1")))
        # Second registration is a no-op, not a duplicate.
        self.assertTrue(
            rescore.register(state, _degen(), now + 1, "M1",
                             _payload("M1")))
        self.assertEqual(list(state["sniper_rescore"].keys()), ["M1"])

    def test_register_disabled(self):
        state = {}
        self.assertFalse(rescore.register(
            state, _degen(sniper_rescore_enabled=False),
            time.time(), "M1", _payload("M1")))
        self.assertEqual(state.get("sniper_rescore") or {}, {})

    def test_register_bounded(self):
        state = {}
        now = time.time()
        for i in range(rescore._MAX_REGISTRY + 10):
            rescore.register(state, _degen(), now + i, "M%d" % i,
                             _payload("M%d" % i))
        self.assertEqual(len(state["sniper_rescore"]),
                         rescore._MAX_REGISTRY)


class RescorePollTest(unittest.TestCase):
    def setUp(self):
        # The bus is PRODUCTION state: no test may ever publish to it.
        # Tests that assert on publishing patch again locally (theirs
        # shadows this one inside the with-block).
        self._bus_patch = unittest.mock.patch.object(
            rescore, "_publish_feed_signal",
            unittest.mock.Mock(return_value=True))
        self._bus_patch.start()
        self.addCleanup(self._bus_patch.stop)

    def _state_with(self, mint, first_ts, fired=None):
        return {"sniper_rescore": {
            mint: {"first_ts": first_ts, "fired": list(fired or []),
                   "payload": _payload(mint)}}}

    def test_not_due_yet(self):
        sc = _Scanner(rank_score=80.0)
        now = time.time()
        state = self._state_with("M1", now - 100)
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(30000.0, None)):
            rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(sc.rank_calls, 0)
        self.assertIn("M1", state["sniper_rescore"])

    def test_promotes_to_tier1_with_regate(self):
        sc = _Scanner(rank_score=80.0,
                      regate=(True, "quality-pass score 90",
                              {"score": 90, "passed": True}))
        now = time.time()
        state = self._state_with("M1", now - 300)
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(40000.0, None)):
            rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(sc.rank_calls, 1)
        self.assertEqual(len(sc.staged), 1)
        self.assertEqual(sc.staged[0]["mint"], "M1")
        self.assertEqual(sc.digested, [])
        led = state["sniper_rescore_ledger"]["M1"]
        self.assertEqual(led["outcome"], "promoted-tier1")
        self.assertEqual(led["score"], 80.0)

    def test_promotion_regate_fail_goes_digest_not_tier1(self):
        # Safety can only get worse with age: a failed re-gate must
        # NEVER stage a tier-1 card. Digest (WATCH) is the fallback.
        sc = _Scanner(rank_score=80.0,
                      regate=(False, "safety-fail: honeypot",
                              {"score": 1, "passed": False}))
        now = time.time()
        state = self._state_with("M1", now - 300)
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(40000.0, None)):
            rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(sc.staged, [])
        self.assertEqual(len(sc.digested), 1)
        self.assertIn("re-gate", sc.digested[0]["reason"])
        led = state["sniper_rescore_ledger"]["M1"]
        self.assertEqual(led["outcome"], "promoted-digest(regate-fail)")

    def test_digest_band(self):
        sc = _Scanner(rank_score=30.0)
        now = time.time()
        state = self._state_with("M1", now - 300)
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(25000.0, None)):
            rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(sc.staged, [])
        self.assertEqual(len(sc.digested), 1)
        led = state["sniper_rescore_ledger"]["M1"]
        self.assertEqual(led["outcome"], "digest")

    # --- mid-zone feed (2026-10-21) -------------------------------------
    _RUNNING_TAPE = {"usd_liquidity": 30000.0, "chg_m5": 12.0,
                     "buys_m5": 100, "sells_m5": 40,
                     "usd_market_cap": 80000.0}

    def _feed_poll(self, sc, tape, mcap=80000.0, cfg=None):
        now = time.time()
        state = self._state_with("M1", now - 300)
        pub = unittest.mock.Mock(return_value=True)
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(mcap, tape)), \
                unittest.mock.patch.object(
                rescore, "_publish_feed_signal", pub):
            rescore.poll(sc, cfg or _cfg(), state, now)
        return state, pub

    def test_feed_promotion_midzone_running(self):
        sc = _Scanner(rank_score=45.0)
        state, pub = self._feed_poll(sc, dict(self._RUNNING_TAPE))
        self.assertEqual(len(sc.staged), 1)
        self.assertEqual(sc.digested, [])
        self.assertEqual(pub.call_count, 1)
        self.assertEqual(pub.call_args[0][-1], "feed")
        led = state["sniper_rescore_ledger"]["M1"]
        self.assertEqual(led["outcome"], "promoted-feed")

    def test_feed_blocked_thin_liquidity(self):
        sc = _Scanner(rank_score=45.0)
        tape = dict(self._RUNNING_TAPE, usd_liquidity=10000.0)
        state, pub = self._feed_poll(sc, tape)
        self.assertEqual(sc.staged, [])
        self.assertEqual(len(sc.digested), 1)
        self.assertEqual(pub.call_count, 0)
        led = state["sniper_rescore_ledger"]["M1"]
        self.assertEqual(led["outcome"], "digest")

    def test_feed_blocked_falling_tape(self):
        sc = _Scanner(rank_score=45.0)
        tape = dict(self._RUNNING_TAPE, chg_m5=-5.0)
        state, pub = self._feed_poll(sc, tape)
        self.assertEqual(sc.staged, [])
        self.assertEqual(len(sc.digested), 1)
        self.assertEqual(pub.call_count, 0)

    def test_feed_blocked_low_mcap(self):
        sc = _Scanner(rank_score=45.0)
        state, pub = self._feed_poll(sc, dict(self._RUNNING_TAPE),
                                     mcap=30000.0)
        self.assertEqual(sc.staged, [])
        self.assertEqual(len(sc.digested), 1)
        self.assertEqual(pub.call_count, 0)

    def test_feed_regate_fail_falls_to_digest(self):
        sc = _Scanner(rank_score=45.0,
                      regate=(False, "safety-fail: honeypot",
                              {"score": 1, "passed": False}))
        state, pub = self._feed_poll(sc, dict(self._RUNNING_TAPE))
        self.assertEqual(sc.staged, [])
        self.assertEqual(len(sc.digested), 1)
        self.assertEqual(pub.call_count, 0)
        led = state["sniper_rescore_ledger"]["M1"]
        self.assertEqual(led["outcome"], "digest")

    def test_feed_disabled_by_config(self):
        sc = _Scanner(rank_score=45.0)
        state, pub = self._feed_poll(
            sc, dict(self._RUNNING_TAPE),
            cfg=_cfg(sniper_feed_enabled=False))
        self.assertEqual(sc.staged, [])
        self.assertEqual(len(sc.digested), 1)
        self.assertEqual(pub.call_count, 0)

    def test_tier1_promotion_also_publishes_bus(self):
        sc = _Scanner(rank_score=80.0,
                      regate=(True, "quality-pass score 90",
                              {"score": 90, "passed": True}))
        now = time.time()
        state = self._state_with("M1", now - 300)
        pub = unittest.mock.Mock(return_value=True)
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(40000.0, None)), \
                unittest.mock.patch.object(
                rescore, "_publish_feed_signal", pub):
            rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(len(sc.staged), 1)
        self.assertEqual(pub.call_count, 1)
        self.assertEqual(pub.call_args[0][-1], "tier1")

    def test_honest_drop_below_digest_bar(self):
        sc = _Scanner(rank_score=10.0)
        now = time.time()
        state = self._state_with("M1", now - 3700)
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(21000.0, None)):
            rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(sc.staged, [])
        self.assertEqual(sc.digested, [])
        led = state["sniper_rescore_ledger"]["M1"]
        self.assertEqual(led["outcome"], "dropped")
        # All four offsets fired -> registry entry retired.
        self.assertNotIn("M1", state.get("sniper_rescore", {}))

    def test_second_offset_fires_independently(self):
        sc = _Scanner(rank_score=30.0)
        now = time.time()
        state = self._state_with("M1", now - 900, fired=[300.0])
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(25000.0, None)):
            rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(sc.rank_calls, 1)
        self.assertEqual(len(sc.digested), 1)
        # +1800/+3600 offsets (2026-10-21) still pending -> entry stays.
        self.assertIn("M1", state.get("sniper_rescore", {}))
        self.assertIn(900.0, state["sniper_rescore"]["M1"]["fired"])

    def test_stale_registry_entry_pruned(self):
        sc = _Scanner(rank_score=80.0)
        now = time.time()
        state = self._state_with("M1", now - 4000)
        rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(sc.rank_calls, 0)
        self.assertNotIn("M1", state.get("sniper_rescore", {}))

    def test_disabled_poll_is_noop(self):
        sc = _Scanner(rank_score=80.0)
        now = time.time()
        state = self._state_with("M1", now - 900)
        rescore.poll(sc, _cfg(sniper_rescore_enabled=False), state, now)
        self.assertEqual(sc.rank_calls, 0)
        self.assertIn("M1", state["sniper_rescore"])

    def test_no_mcap_still_ranks_but_never_stages(self):
        # Rank needs no mcap; card/anchor emission waits for a number.
        sc = _Scanner(rank_score=80.0)
        now = time.time()
        state = self._state_with("M1", now - 300)
        with unittest.mock.patch.object(
                rescore, "_fresh_stats", return_value=(0.0, None)):
            rescore.poll(sc, _cfg(), state, now)
        self.assertEqual(sc.rank_calls, 1)
        self.assertEqual(sc.staged, [])
        self.assertEqual(sc.digested, [])
        led = state["sniper_rescore_ledger"]["M1"]
        self.assertEqual(led["outcome"], "held-no-mcap")


class RescorePayloadTest(unittest.TestCase):
    def test_compact_rebuild_roundtrip(self):
        c = {"mint": "M1", "symbol": "TKN", "name": "Token",
             "chain": "solana", "url": "https://x", "source": "pumpfun"}
        new_cand = {"narratives": ["ai"], "narrative_heat": 1.5,
                    "sentiment": {"verdict": "bullish"}}
        entry = {"first_ts": 1234.0, "mcap": [[1234.0, 20000.0]]}
        p = rescore._compact_payload(c, new_cand, entry, 20000.0,
                                     "early", "quality-pass",
                                     {"score": 90})
        rc, rnc, rent = rescore._rebuild(p)
        self.assertEqual(rc["mint"], "M1")
        self.assertEqual(rc["chain"], "solana")
        self.assertEqual(rnc["narratives"], ["ai"])
        self.assertEqual(rnc["narrative_heat"], 1.5)
        self.assertEqual(rent["first_ts"], 1234.0)
        self.assertEqual(rent["mcap"], [[1234.0, 20000.0]])


class RescoreLiveHistoryTest(unittest.TestCase):
    """Regression test (2026-10-10): the re-score ranked on a
    birth-frozen entry — _rebuild never merged the live mcap series,
    so velocity was structurally zero and every re-score died at 0.0.
    The fix merges state["seen"][mint]["mcap"] at fire time."""

    def _state_with_live_hist(self):
        now = time.time()
        return {
            "seen": {
                "M1": {"mcap": [[now - 300, 20000.0],
                                [now - 120, 35000.0],
                                [now - 30, 45000.0]]},
            },
            "sniper_rescore": {
                "M1": {"first_ts": now - 300, "fired": [],
                       "payload": _payload("M1")},
            },
        }

    def test_rescore_uses_live_mcap_history(self):
        captured = {}

        class CapScanner(_Scanner):
            def _sniper_rank(self, config, state, c, entry, new_cand, tape_stats=None):
                captured["mcap"] = list(entry.get("mcap") or [])
                return super()._sniper_rank(config, state, c, entry,
                                            new_cand)

        state = self._state_with_live_hist()
        now = time.time()
        rec = state["sniper_rescore"]["M1"]
        rescore._rescore_once(CapScanner(rank_score=0.0), _cfg(),
                              state, _degen(), "M1", rec, now, 300.0)
        hist = captured.get("mcap") or []
        # Live history (3 points, latest $45k) replaced the frozen
        # birth snapshot (1 point at $20k).
        self.assertEqual(len(hist), 3)
        self.assertEqual(hist[-1][1], 45000.0)

    def test_missing_seen_falls_back_to_payload(self):
        captured = {}

        class CapScanner(_Scanner):
            def _sniper_rank(self, config, state, c, entry, new_cand, tape_stats=None):
                captured["mcap"] = list(entry.get("mcap") or [])
                return super()._sniper_rank(config, state, c, entry,
                                            new_cand)

        state = {"seen": {},
                 "sniper_rescore": {
                     "M1": {"first_ts": time.time() - 300, "fired": [],
                            "payload": _payload("M1")},
                 }}
        rec = state["sniper_rescore"]["M1"]
        rescore._rescore_once(CapScanner(rank_score=0.0), _cfg(),
                              state, _degen(), "M1", rec,
                              time.time(), 300.0)
        # No live history: the birth payload's single point survives.
        self.assertEqual(captured.get("mcap"), [[1000.0, 20000.0]])


if __name__ == "__main__":
    unittest.main()
