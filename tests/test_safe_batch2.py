"""Safe batch 2 (2026-10-09): six measurement/display-only modules.

second_wallet, dust_holders, liquidity_mirage, spoof_walls,
copy_copiers, narrative_cluster — plus wiring (card lines, weekly
review) and the CRITICAL no-behavior-change guarantees:

  - none of the six modules can reach entry/exit/gate/sizing code
    (source scan for forbidden symbols),
  - every module fails soft and records "unknown" honestly instead of
    guessing.

No network — all HTTP/RPC is faked. Nothing touches the real
~/.forex-agent runtime tree: explicit tmp paths are passed everywhere.
"""

import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["FOREX_AGENT_TESTING"] = "1"

from daemon.launch_scanner import (
    copy_copiers,
    dust_holders,
    liquidity_mirage,
    narrative_cluster,
    second_wallet,
    spoof_walls,
)

MINT = "So11111111111111111111111111111111111111112"
W1 = "WALLET1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
W2 = "WALLET2bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
W3 = "WALLET3cccccccccccccccccccccccccccccccccccc"
F1 = "FUNDER1dddddddddddddddddddddddddddddddddddd"


def _degen(**kw):
    base = dict(
        second_wallet_enabled=True,
        second_wallet_max_funders_per_day=5,
        dust_holders_enabled=True,
        dust_holder_pct=0.01,
        dust_holder_min_count=50,
        dust_holder_share=0.5,
        liquidity_mirage_enabled=True,
        liquidity_mirage_min_snaps=6,
        liquidity_mirage_spike_mult=3.0,
        liquidity_mirage_collapse_min=30.0,
        spoof_walls_enabled=True,
        spoof_walls_min_snaps=6,
        spoof_walls_wall_mult=4.0,
        spoof_walls_wall_min=15.0,
        copy_copiers_enabled=True,
        copy_copiers_min_buyers=30,
        narrative_cluster_enabled=True,
        narrative_cluster_window_h=24.0,
        narrative_cluster_min_coins=5,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _tmp(tmp: Path, name: str) -> str:
    return str(tmp / name)


def _obs(n, liq=1000.0, buys=5.0, vol=100.0):
    """Fingerprint-style observation tuples."""
    base = time.time() - n * 300
    return [(base + i * 300, 1e6, 0.01, vol, buys, 1.0, liq)
            for i in range(n)]


class SecondWalletTest(unittest.TestCase):
    def test_disabled_unknown(self):
        r = second_wallet.analyze(_degen(second_wallet_enabled=False),
                                  MINT, [W1, W2])
        self.assertEqual(r["verdict"], "unknown")

    def test_too_few_wallets_unknown(self):
        r = second_wallet.analyze(_degen(), MINT, [W1],
                                  path=_tmp(Path("/tmp"), "sw.json"))
        self.assertEqual(r["verdict"], "unknown")

    def test_cluster_detected(self):
        p = _tmp(Path("/tmp"), "sw_cluster_%d.json" % id(self))
        with patch("daemon.launch_scanner.deployers.load_funders",
                   return_value={}), \
             patch("daemon.launch_scanner.deployers.funder_for",
                   return_value=F1):
            r = second_wallet.analyze(_degen(), MINT, [W1, W2, W3],
                                      path=p)
        self.assertEqual(r["verdict"], "cluster")
        self.assertEqual(r["cluster_size"], 3)

    def test_clean_no_shared_funder(self):
        p = _tmp(Path("/tmp"), "sw_clean_%d.json" % id(self))
        funders = {W1: F1, W2: "OTHER" + "e" * 39, W3: None}
        with patch("daemon.launch_scanner.deployers.load_funders",
                   return_value={w: {"funder": f, "ts": 0}
                                 for w, f in funders.items()}):
            r = second_wallet.analyze(_degen(), MINT, [W1, W2, W3],
                                      path=p)
        self.assertEqual(r["verdict"], "clean")

    def test_budget_exhausted_unknown(self):
        p = _tmp(Path("/tmp"), "sw_budget_%d.json" % id(self))
        with patch("daemon.launch_scanner.deployers.load_funders",
                   return_value={}), \
             patch("daemon.launch_scanner.deployers.funder_for",
                   return_value=None):
            r = second_wallet.analyze(
                _degen(second_wallet_max_funders_per_day=0),
                MINT, [W1, W2], path=p)
        self.assertEqual(r["verdict"], "unknown")

    def test_card_line_only_on_cluster(self):
        p = _tmp(Path("/tmp"), "sw_line_%d.json" % id(self))
        with patch("daemon.launch_scanner.deployers.load_funders",
                   return_value={}), \
             patch("daemon.launch_scanner.deployers.funder_for",
                   return_value=F1):
            line = second_wallet.card_line(_degen(), MINT, [W1, W2],
                                           path=p)
        self.assertIsNotNone(line)
        self.assertIn("2nd-wallet", line)
        # unknown -> no line
        p2 = _tmp(Path("/tmp"), "sw_line2_%d.json" % id(self))
        with patch("daemon.launch_scanner.deployers.load_funders",
                   return_value={}), \
             patch("daemon.launch_scanner.deployers.funder_for",
                   return_value=None):
            line2 = second_wallet.card_line(_degen(), MINT, [W1, W2],
                                            path=p2)
        self.assertIsNone(line2)

    def test_garbage_safe(self):
        r = second_wallet.analyze(None, None, None)
        self.assertEqual(r["verdict"], "unknown")
        self.assertIsNone(second_wallet.card_line(None, None))
        self.assertIsNone(second_wallet.cached_verdict("nope",
                                                       path="/tmp/nope.json"))

    def test_weekly_section_zeros(self):
        lines = second_wallet.weekly_section(
            path=_tmp(Path("/tmp"), "sw_empty_%d.json" % id(self)))
        self.assertTrue(any("0" in l for l in lines))


class DustHoldersTest(unittest.TestCase):
    def _report(self, n_dust, n_big):
        holders = ([{"address": "d%d" % i, "pct": 0.001}
                    for i in range(n_dust)] +
                   [{"address": "b%d" % i, "pct": 2.0}
                    for i in range(n_big)])
        return {"topHolders": holders}

    def test_inflated(self):
        r = dust_holders.assess(_degen(), self._report(60, 5))
        self.assertEqual(r["verdict"], "inflated")
        self.assertEqual(r["dust_count"], 60)

    def test_clean(self):
        r = dust_holders.assess(_degen(), self._report(5, 20))
        self.assertEqual(r["verdict"], "clean")

    def test_unknown_no_data(self):
        for rep in (None, {}, {"topHolders": []}):
            r = dust_holders.assess(_degen(), rep)
            self.assertEqual(r["verdict"], "unknown")

    def test_disabled(self):
        r = dust_holders.assess(_degen(dust_holders_enabled=False),
                                self._report(60, 5))
        self.assertEqual(r["verdict"], "unknown")

    def test_card_line(self):
        line = dust_holders.card_line(_degen(), self._report(60, 5))
        self.assertIn("inflated", line)
        line2 = dust_holders.card_line(_degen(), None)
        self.assertIn("unknown", line2)

    def test_garbage_safe(self):
        r = dust_holders.assess(None, None)
        self.assertEqual(r["verdict"], "unknown")
        # batch-1 pattern: 'unknown' is rendered, not hidden
        self.assertIn("unknown", dust_holders.card_line(None, None))


class LiquidityMirageTest(unittest.TestCase):
    def _snaps(self, liqs, base=None, vol=100.0):
        base = time.time() - 3600 if base is None else base
        return [(base + i * 300, 1e6, 0.01, vol, 5.0, 1.0, liq)
                for i, liq in enumerate(liqs)]

    def test_mirage(self):
        # spike 5x then collapse within 10 min, volume flat
        base = time.time() - 3600
        liqs = [1000.0] * 8
        liqs[3] = 5000.0
        liqs[4] = 900.0
        r = liquidity_mirage.assess(_degen(), self._snaps(liqs, base))
        self.assertEqual(r["verdict"], "mirage")

    def test_stable(self):
        r = liquidity_mirage.assess(_degen(), self._snaps([1000.0] * 8))
        self.assertEqual(r["verdict"], "stable")

    def test_unknown_too_few(self):
        r = liquidity_mirage.assess(_degen(), self._snaps([1000.0] * 3))
        self.assertEqual(r["verdict"], "unknown")

    def test_card_line_only_on_mirage(self):
        base = time.time() - 3600
        liqs = [1000.0] * 8
        liqs[3] = 5000.0
        liqs[4] = 900.0
        line = liquidity_mirage.card_line(_degen(),
                                          self._snaps(liqs, base))
        self.assertIsNotNone(line)
        self.assertIn("mirage", line)
        self.assertIsNone(liquidity_mirage.card_line(
            _degen(), self._snaps([1000.0] * 8)))

    def test_garbage_safe(self):
        r = liquidity_mirage.assess(None, None)
        self.assertEqual(r["verdict"], "unknown")
        self.assertIsNone(liquidity_mirage.card_line(None, None))


class SpoofWallsTest(unittest.TestCase):
    def _snaps(self, liqs, buys=5.0, base=None):
        base = time.time() - 3600 if base is None else base
        return [(base + i * 300, 1e6, 0.01, 100.0, buys, 1.0, liq)
                for i, liq in enumerate(liqs)]

    def test_watch(self):
        liqs = [1000.0] * 8
        liqs[3] = 4500.0
        liqs[4] = 900.0
        r = spoof_walls.assess(_degen(), self._snaps(liqs, buys=6.0))
        self.assertEqual(r["verdict"], "watch")

    def test_none(self):
        r = spoof_walls.assess(_degen(), self._snaps([1000.0] * 8))
        self.assertEqual(r["verdict"], "none")

    def test_unknown(self):
        r = spoof_walls.assess(_degen(), self._snaps([1000.0] * 2))
        self.assertEqual(r["verdict"], "unknown")

    def test_card_line_only_on_watch(self):
        liqs = [1000.0] * 8
        liqs[3] = 4500.0
        liqs[4] = 900.0
        self.assertIsNotNone(spoof_walls.card_line(
            _degen(), self._snaps(liqs, buys=6.0)))
        self.assertIsNone(spoof_walls.card_line(
            _degen(), self._snaps([1000.0] * 8)))

    def test_garbage_safe(self):
        r = spoof_walls.assess(None, None)
        self.assertEqual(r["verdict"], "unknown")


class CopyCopiersTest(unittest.TestCase):
    def test_pilein(self):
        p = _tmp(Path("/tmp"), "cc_pilein_%d.json" % id(self))
        obs = _obs(8, buys=10.0)  # 80 buyers, no smart
        r = copy_copiers.signal(_degen(), MINT, obs, 0, path=p)
        self.assertEqual(r["verdict"], "pilein")
        self.assertEqual(r["buyers"], 80)

    def test_smartled(self):
        p = _tmp(Path("/tmp"), "cc_smart_%d.json" % id(self))
        r = copy_copiers.signal(_degen(), MINT, _obs(8, buys=10.0), 2,
                                path=p)
        self.assertEqual(r["verdict"], "smartled")

    def test_quiet(self):
        p = _tmp(Path("/tmp"), "cc_quiet_%d.json" % id(self))
        r = copy_copiers.signal(_degen(), MINT, _obs(8, buys=1.0), 0,
                                path=p)
        self.assertEqual(r["verdict"], "quiet")

    def test_unknown_no_data(self):
        p = _tmp(Path("/tmp"), "cc_unk_%d.json" % id(self))
        r = copy_copiers.signal(_degen(), MINT, [], 0, path=p)
        self.assertEqual(r["verdict"], "unknown")

    def test_card_line_only_on_pilein(self):
        p = _tmp(Path("/tmp"), "cc_line_%d.json" % id(self))
        line = copy_copiers.card_line(_degen(), MINT, _obs(8, buys=10.0),
                                      0, path=p)
        self.assertIsNotNone(line)
        self.assertIn("copier pile-in", line)
        p2 = _tmp(Path("/tmp"), "cc_line2_%d.json" % id(self))
        self.assertIsNone(copy_copiers.card_line(
            _degen(), MINT, _obs(8, buys=1.0), 0, path=p2))

    def test_garbage_safe(self):
        r = copy_copiers.signal(None, None, None, None)
        self.assertEqual(r["verdict"], "unknown")
        self.assertIsNone(copy_copiers.card_line(None, None, None, None))


class NarrativeClusterTest(unittest.TestCase):
    def test_note_and_heating(self):
        p = _tmp(Path("/tmp"), "nc_%d.json" % id(self))
        d = _degen(narrative_cluster_min_coins=3)
        for i in range(4):
            narrative_cluster.note_coin("MINT%d" % i, "DOGE%d" % i,
                                        "dog coin", path=p)
        hot = narrative_cluster.heating(d, path=p)
        tags = [r["tag"] for r in hot]
        self.assertIn("dog", tags)
        dog = [r for r in hot if r["tag"] == "dog"][0]
        self.assertEqual(dog["count"], 4)
        self.assertIsNone(dog["median_move"])  # no peaks passed

    def test_median_move_from_peaks(self):
        p = _tmp(Path("/tmp"), "nc2_%d.json" % id(self))
        d = _degen(narrative_cluster_min_coins=2)
        narrative_cluster.note_coin("A1", "AICOIN", "ai", path=p)
        narrative_cluster.note_coin("A2", "AIBOT", "ai", path=p)
        hot = narrative_cluster.heating(
            d, peaks={"A1": 2.0, "A2": 4.0}, path=p)
        ai = [r for r in hot if r["tag"] == "ai"][0]
        self.assertEqual(ai["median_move"], 3.0)

    def test_card_line_on_heating(self):
        p = _tmp(Path("/tmp"), "nc3_%d.json" % id(self))
        d = _degen(narrative_cluster_min_coins=2)
        narrative_cluster.note_coin("X1", "CATX", "cat", path=p)
        line = narrative_cluster.card_line(d, "X2", "CATY", "cat",
                                           path=p)
        self.assertIsNotNone(line)
        self.assertIn("cat", line)

    def test_card_line_quiet_when_cold(self):
        p = _tmp(Path("/tmp"), "nc4_%d.json" % id(self))
        d = _degen(narrative_cluster_min_coins=50)
        line = narrative_cluster.card_line(d, "Z1", "ZZZ", "zzz",
                                           path=p)
        self.assertIsNone(line)

    def test_garbage_safe(self):
        self.assertEqual(narrative_cluster.heating(None), [])
        self.assertIsNone(narrative_cluster.note_coin(None, None, None))
        self.assertIsNone(narrative_cluster.card_line(None, None,
                                                     None, None))

    def test_weekly_section(self):
        p = _tmp(Path("/tmp"), "nc5_%d.json" % id(self))
        d = _degen(narrative_cluster_min_coins=2)
        narrative_cluster.note_coin("W1", "DOGW", "dog", path=p)
        narrative_cluster.note_coin("W2", "DOGX", "dog", path=p)
        lines = narrative_cluster.weekly_section(d, path=p)
        self.assertTrue(any("dog" in l for l in lines))


class NoBehaviorChangeTest(unittest.TestCase):
    """CRITICAL: safe batch 2 modules must not reach entry/exit/gate/
    sizing code. Source scan over the six module files."""

    FORBIDDEN = [
        "submit_order", "close_position", "_paper_enter",
        "kill_switch", "killswitch", "positions.pop",
        "open_position", "cancel_order", "set_leverage",
        "position_size", "risk_engine", "apply_tune",
        "broker.", "place_order",
    ]

    MODULES = ["second_wallet", "dust_holders", "liquidity_mirage",
               "spoof_walls", "copy_copiers", "narrative_cluster"]

    def test_no_forbidden_symbols(self):
        base = Path(__file__).resolve().parent.parent / \
            "daemon" / "launch_scanner"
        for mod in self.MODULES:
            src = (base / (mod + ".py")).read_text(encoding="utf-8")
            low = src.lower()
            for sym in self.FORBIDDEN:
                self.assertNotIn(
                    sym.lower(), low,
                    "%s.py references forbidden symbol %r" % (mod, sym))

    def test_no_gate_verdicts(self):
        # None of the six modules may emit allow/block/hold/drop
        # decisions — only display/measurement verdicts.
        base = Path(__file__).resolve().parent.parent / \
            "daemon" / "launch_scanner"
        for mod in self.MODULES:
            src = (base / (mod + ".py")).read_text(encoding="utf-8")
            for kw in ("BLOCK", "ALLOW"):
                # quoted verdict strings only; docstring prose excluded
                hits = [l for l in src.splitlines()
                        if '"%s"' % kw in l or "'%s'" % kw in l]
                self.assertEqual(
                    hits, [],
                    "%s.py emits gate verdict %s: %s" % (mod, kw, hits))


class KnobsTest(unittest.TestCase):
    """All 19 batch-2 knobs present and default-on in both places."""

    KNOBS = {
        "second_wallet_enabled": True,
        "second_wallet_max_funders_per_day": 5,
        "dust_holders_enabled": True,
        "dust_holder_pct": 0.01,
        "dust_holder_min_count": 50,
        "dust_holder_share": 0.5,
        "liquidity_mirage_enabled": True,
        "liquidity_mirage_min_snaps": 6,
        "liquidity_mirage_spike_mult": 3.0,
        "liquidity_mirage_collapse_min": 30.0,
        "spoof_walls_enabled": True,
        "spoof_walls_min_snaps": 6,
        "spoof_walls_wall_mult": 4.0,
        "spoof_walls_wall_min": 15.0,
        "copy_copiers_enabled": True,
        "copy_copiers_min_buyers": 30,
        "narrative_cluster_enabled": True,
        "narrative_cluster_window_h": 24.0,
        "narrative_cluster_min_coins": 5,
    }

    def test_defaults_yaml(self):
        import re
        base = Path(__file__).resolve().parent.parent
        text = (base / "config" / "defaults.yaml").read_text(
            encoding="utf-8")
        for k, v in self.KNOBS.items():
            m = re.search(r"^\s*%s:\s*(\S+)" % re.escape(k), text,
                          re.M)
            self.assertIsNotNone(m, "knob %s missing in defaults.yaml"
                                 % k)
            if isinstance(v, bool):
                self.assertEqual(m.group(1).lower(), "true",
                                 "knob %s not default-on" % k)

    def test_degen_config(self):
        from config.config import DegenConfig
        import dataclasses
        fields = {f.name for f in dataclasses.fields(DegenConfig)}
        for k in self.KNOBS:
            self.assertIn(k, fields, "knob %s missing in DegenConfig"
                          % k)


if __name__ == "__main__":
    unittest.main()
