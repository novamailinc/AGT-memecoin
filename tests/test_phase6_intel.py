"""Phase 6 intelligence layer: reputation tiers, serial-launcher flag,
funder clustering, blacklist proposals (proposal-only), wallet-proposal
decay, and the private-data-never-in-repo guarantee.

No network — all RPC is faked. Nothing here touches the real
~/.forex-agent runtime files: every path constant is patched to /tmp.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daemon.launch_scanner import deployers, wallet_discovery

DEP1 = "DeplAAAA11111111111111111111111111111111111"
DEP2 = "DeplBBBB22222222222222222222222222222222222"
DEP3 = "DeplCCCC33333333333333333333333333333333333"
FUND1 = "FundAAAA11111111111111111111111111111111111"


def _degen(**kw):
    base = dict(deployer_min_launches=3, deployer_hot_hit_rate=0.30,
                deployer_rug_blacklist_bar=0.5,
                deployer_blacklist_min_launches=3,
                deployer_serial_window_s=86400.0,
                deployer_serial_min_launches=5,
                deployer_reputation_line_enabled=True,
                deployer_intel_enabled=True,
                deployer_intel_max_funders_per_day=5,
                discovery_proposal_ttl_days=30.0)
    base.update(kw)
    return SimpleNamespace(**base)


class _DeployerPaths:
    """Patch every deployer runtime file into a tmp dir."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.patches = []

    def start(self):
        for name, fname in (
                ("_REGISTRY_FILE", "deployer_history.json"),
                ("_FUNDERS_FILE", "deployer_funders.json"),
                ("_BLACKLIST_FILE", "deployer_blacklist.json"),
                ("_BL_PROPOSALS_FILE",
                 "deployer_blacklist_proposals.json")):
            p = patch.object(deployers, name, self.tmp / fname)
            p.start()
            self.patches.append(p)
        return self

    def stop(self):
        for p in self.patches:
            p.stop()


class TierMathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/phase6-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.paths = _DeployerPaths(self.tmp).start()
        self.addCleanup(self.paths.stop)

    def _entry(self, launches, hits=0, rugs=0, dead=0):
        return {"launches_seen": launches, "hits_2x": hits, "rugs": rugs,
                "dead": dead, "first_seen": 1.0, "last_seen": 2.0,
                "launch_ts": []}

    def test_unknown_and_new_are_neutral(self):
        d = _degen()
        self.assertEqual(deployers.tier_of(None, d), "unknown")
        self.assertEqual(deployers.tier_of({}, d), "unknown")
        self.assertEqual(deployers.tier_of(self._entry(1), d), "new")
        self.assertEqual(deployers.tier_of(self._entry(2, hits=2), d),
                         "new")  # 100% hit but too few launches

    def test_hot_risky_cold(self):
        d = _degen()
        self.assertEqual(
            deployers.tier_of(self._entry(10, hits=4), d), "hot")
        # safety-first: hot AND rug-heavy shows risky
        self.assertEqual(
            deployers.tier_of(self._entry(10, hits=4, rugs=6), d),
            "risky")
        self.assertEqual(
            deployers.tier_of(self._entry(10, hits=1, rugs=1), d),
            "cold")

    def test_rug_rate_math(self):
        self.assertAlmostEqual(
            deployers.rug_rate(self._entry(10, rugs=3)), 0.3)
        self.assertEqual(deployers.rug_rate(None), 0.0)
        self.assertEqual(deployers.rug_rate({}), 0.0)

    def test_reputation_line_unknown_safe(self):
        d = _degen()
        self.assertEqual(deployers.reputation_line(None, d),
                         "🧬 deployer: unknown")
        self.assertEqual(deployers.reputation_line("", d),
                         "🧬 deployer: unknown")
        self.assertEqual(deployers.reputation_line(DEP1, d),
                         "🧬 deployer: unknown")  # no registry entry
        # never raises, even on garbage
        self.assertTrue(
            deployers.reputation_line(DEP1, None).startswith("🧬"))

    def test_reputation_line_shows_facts(self):
        d = _degen()
        for _ in range(10):
            deployers.record_launch(DEP1)
        for _ in range(4):
            deployers.record_outcome("m%d" % _, DEP1, "hit_2x")
        line = deployers.reputation_line(DEP1, d)
        self.assertIn("HOT", line)
        self.assertIn("10 launches", line)
        self.assertIn("0% rug", line)

    def test_reputation_line_risky(self):
        d = _degen()
        for _ in range(4):
            deployers.record_launch(DEP2)
        for _ in range(3):
            deployers.record_outcome("r%d" % _, DEP2, "rug")
        line = deployers.reputation_line(DEP2, d)
        self.assertIn("RISKY", line)
        self.assertIn("75% rug", line)


class SerialLauncherTest(unittest.TestCase):
    def test_serial_flag(self):
        now = time.time()
        entry = {"launch_ts": [now - 100, now - 200, now - 300,
                               now - 400, now - 500, now - 90000]}
        self.assertTrue(deployers.is_serial_launcher(
            entry, now=now, window_s=86400.0, min_launches=5))
        self.assertFalse(deployers.is_serial_launcher(
            entry, now=now, window_s=60.0, min_launches=5))
        self.assertFalse(deployers.is_serial_launcher(None, now=now))
        self.assertFalse(deployers.is_serial_launcher({}, now=now))

    def test_record_launch_keeps_bounded_ts(self):
        tmp = Path("/tmp/phase6s-%d" % int(time.time() * 1e6))
        tmp.mkdir(parents=True)
        paths = _DeployerPaths(tmp).start()
        self.addCleanup(paths.stop)
        for _ in range(35):
            deployers.record_launch(DEP3)
        entry = deployers.load_registry()[DEP3]
        self.assertEqual(len(entry["launch_ts"]),
                         deployers._MAX_LAUNCH_TS)
        self.assertEqual(entry["launches_seen"], 35)

    def test_serial_surfaces_on_line(self):
        tmp = Path("/tmp/phase6s2-%d" % int(time.time() * 1e6))
        tmp.mkdir(parents=True)
        paths = _DeployerPaths(tmp).start()
        self.addCleanup(paths.stop)
        for _ in range(6):
            deployers.record_launch(DEP3)
        line = deployers.reputation_line(DEP3, _degen())
        self.assertIn("⚡serial", line)


class FunderClusterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/phase6f-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.paths = _DeployerPaths(self.tmp).start()
        self.addCleanup(self.paths.stop)

    def _fake_rpc(self, oldest_sig="sig-oldest", payer="PAYER1"):
        calls = []

        def rpc(method, params):
            calls.append(method)
            if method == "getSignaturesForAddress":
                return [{"signature": oldest_sig}]
            if method == "getTransaction":
                return {"meta": {"err": None},
                        "transaction": {"signatures": [oldest_sig], "message": {
                            "accountKeys": [payer, DEP1]}}}
            return None

        rpc.calls = calls
        return rpc

    def test_funder_for_caches(self):
        rpc = self._fake_rpc()
        f1 = deployers.funder_for(DEP1, rpc)
        self.assertEqual(f1, "PAYER1")
        n_calls = len(rpc.calls)
        f2 = deployers.funder_for(DEP1, rpc)  # cached: no new calls
        self.assertEqual(f2, "PAYER1")
        self.assertEqual(len(rpc.calls), n_calls)
        # persisted with 0600
        mode = os.stat(self.tmp / "deployer_funders.json").st_mode
        self.assertEqual(oct(mode & 0o777), "0o600")

    def test_funder_for_never_raises(self):
        self.assertIsNone(deployers.funder_for("", None))
        self.assertIsNone(deployers.funder_for(DEP1, None))

        def boom(method, params):
            raise RuntimeError("nope")

        self.assertIsNone(deployers.funder_for(DEP1, boom))

    def test_funder_clusters(self):
        funders = {DEP1: {"funder": FUND1, "ts": 1.0},
                   DEP2: {"funder": FUND1, "ts": 1.0},
                   DEP3: {"funder": "OTHER", "ts": 1.0}}
        hist = {DEP1: {}, DEP2: {}, DEP3: {}}
        clusters = deployers.funder_clusters(hist, funders)
        self.assertEqual(set(clusters), {FUND1})
        self.assertEqual(sorted(clusters[FUND1]), sorted([DEP1, DEP2]))

    def test_funder_for_transfer_heuristic(self):
        # fee payer IS the deployer -> fall back to incoming transfer
        def rpc(method, params):
            if method == "getSignaturesForAddress":
                return [{"signature": "s1"}]
            return {"meta": {"err": None},
                    "transaction": {"signatures": ["s1"], "message": {
                        "accountKeys": [DEP1],
                        "instructions": [{
                            "program": "system",
                            "parsed": {"type": "transfer",
                                       "info": {"source": "BACKER9",
                                                "destination": DEP1}}}]}}}
        self.assertEqual(deployers.funder_for(DEP1, rpc), "BACKER9")


class BlacklistProposalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/phase6b-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.paths = _DeployerPaths(self.tmp).start()
        self.addCleanup(self.paths.stop)

    def _seed(self):
        for _ in range(4):
            deployers.record_launch(DEP1)
        for _ in range(3):
            deployers.record_outcome("x", DEP1, "rug")
        for _ in range(4):
            deployers.record_launch(DEP2)
        for _ in range(3):
            deployers.record_outcome("y", DEP2, "hit_2x")

    def test_propose_only_rug_heavy(self):
        self._seed()
        props = deployers.propose_blacklist(deployers.load_registry(),
                                            now=1000.0, degen=_degen())
        deps = [p["deployer"] for p in props]
        self.assertIn(DEP1, deps)      # 75% rug rate
        self.assertNotIn(DEP2, deps)   # 0% rug rate
        self.assertEqual(props[0]["status"], "open")
        self.assertIn("nothing auto-blocked", props[0]["rationale"])

    def test_no_blacklist_by_default_and_no_gating(self):
        self.assertEqual(deployers.load_blacklist(), [])
        self.assertFalse(deployers.is_blacklisted(DEP1))
        self.assertFalse(deployers.is_blacklisted(None))

    def test_save_dedupe_and_expiry(self):
        self._seed()
        now = 2000000.0
        props = deployers.propose_blacklist(deployers.load_registry(),
                                            now=now, degen=_degen())
        added = deployers.save_bl_proposals(props, now=now)
        self.assertEqual(len(added), 1)
        # second save dedupes the open proposal
        added2 = deployers.save_bl_proposals(
            deployers.propose_blacklist(deployers.load_registry(),
                                        now=now + 10, degen=_degen()),
            now=now + 10)
        self.assertEqual(added2, [])
        # stale opens expire after the TTL
        added3 = deployers.save_bl_proposals([], now=now + 31 * 86400.0)
        self.assertEqual(added3, [])
        statuses = [p["status"]
                    for p in deployers.load_bl_proposals()]
        self.assertIn("expired", statuses)


class WalletProposalDecayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/phase6w-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.pfile = self.tmp / "wallet_discovery_proposals.json"
        self.p = patch.object(wallet_discovery, "proposals_path",
                              lambda *a, **k: self.pfile)
        self.p.start()
        self.addCleanup(self.p.stop)

    def _write(self, props):
        self.pfile.write_text(json.dumps({"version": 1,
                                          "proposals": props}))
        os.chmod(self.pfile, 0o600)

    def test_stale_opens_expire(self):
        now = 3000000.0
        self._write([
            {"wallet": "W1", "status": "open",
             "created_ts": now - 40 * 86400.0},
            {"wallet": "W2", "status": "open",
             "created_ts": now - 5 * 86400.0},
            {"wallet": "W3", "status": "accepted",
             "created_ts": now - 90 * 86400.0},
        ])
        props = wallet_discovery.load_proposals()
        n = wallet_discovery.prune_stale_proposals(props, now=now,
                                                   ttl_days=30.0)
        self.assertEqual(n, 1)
        by_w = {p["wallet"]: p["status"] for p in props}
        self.assertEqual(by_w["W1"], "expired")
        self.assertEqual(by_w["W2"], "open")
        self.assertEqual(by_w["W3"], "accepted")  # untouched

    def test_mark_accepted(self):
        self._write([{"wallet": "W9", "status": "open",
                      "created_ts": 1.0}])
        self.assertTrue(wallet_discovery.mark_proposal_accepted("W9"))
        props = wallet_discovery.load_proposals()
        self.assertEqual(props[0]["status"], "accepted")
        self.assertFalse(wallet_discovery.mark_proposal_accepted("NOPE"))
        self.assertFalse(wallet_discovery.mark_proposal_accepted(""))

    def test_discovery_never_writes_registry(self):
        # proposals are review-only: the registry dict must be untouched
        scores = {"W1": {"hits": 3, "seen": 3, "hit_rate": 0.8,
                         "runners": ["AAA"]}}
        registry = {"TRACKED1": {"label": "x"}}
        before = json.dumps(registry, sort_keys=True)
        props = wallet_discovery.discovery_proposals(
            scores, registry, 4000000.0, "2026-10-09",
            min_hits=2, min_hit_rate=0.5)
        self.assertEqual(len(props), 1)
        self.assertEqual(json.dumps(registry, sort_keys=True), before)
        self.assertEqual(props[0]["status"], "open")
        self.assertEqual(props[0]["wallet"], "W1")


class DailyIntelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("/tmp/phase6i-%d" % int(time.time() * 1e6))
        self.tmp.mkdir(parents=True)
        self.paths = _DeployerPaths(self.tmp).start()
        self.addCleanup(self.paths.stop)

    def test_daily_intel_throttled_and_measurement_only(self):
        now = 5000000.0
        deployers.record_launch(DEP1)
        state: dict = {}

        def rpc(method, params):
            return None  # all misses -> fail-soft

        out = deployers.daily_intel(state, _degen(), rpc=rpc, now=now)
        self.assertTrue(out["ran"])
        out2 = deployers.daily_intel(state, _degen(), rpc=rpc,
                                     now=now + 100)
        self.assertFalse(out2["ran"])
        self.assertEqual(out2["reason"], "throttled")
        # only its own ledger key touched
        self.assertIn("deployer_intel", state)

    def test_daily_intel_never_raises(self):
        out = deployers.daily_intel({}, None, rpc=None)
        self.assertIn("ran", out)
        out2 = deployers.daily_intel(None, None, rpc="garbage")
        self.assertIn("ran", out2)


class PrivateDataPathsTest(unittest.TestCase):
    def test_intel_files_live_under_runtime_home_not_repo(self):
        # Structural property, immune to other tests rebinding
        # FOREX_AGENT_HOME: every intel file lives under the module's
        # own _HOME (the runtime dir captured at import) and never
        # under the public repo tree.
        home = str(deployers._HOME)
        repo = str(Path(
            _REPO_ROOT
        ).resolve())
        self.assertTrue(home, "deployers._HOME is empty")
        self.assertFalse(home.startswith(repo))
        for p in (deployers._FUNDERS_FILE, deployers._BLACKLIST_FILE,
                  deployers._BL_PROPOSALS_FILE):
            s = str(p)
            self.assertTrue(s.startswith(home),
                            "intel file outside runtime home: %s" % s)
            self.assertFalse(s.startswith(repo),
                             "intel file inside public repo: %s" % s)
        # wallet_discovery resolves its path from the env each call;
        # pin the env to a fake home and re-check.
        with patch.dict(os.environ,
                        {"FOREX_AGENT_HOME": "/tmp/fake-home-xyz"}):
            s = str(wallet_discovery.proposals_path())
            self.assertTrue(s.startswith("/tmp/fake-home-xyz"),
                            "proposals path escapes runtime home: %s" % s)
            self.assertFalse(s.startswith(repo),
                             "proposals path inside public repo: %s" % s)


if __name__ == "__main__":
    unittest.main()
