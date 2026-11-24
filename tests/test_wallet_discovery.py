"""Hermetic tests for daemon/launch_scanner/wallet_discovery.py.

No network, no real HOME reads: the RPC layer is faked and
FOREX_AGENT_HOME points at a tmp dir.
"""

import os
import time

import pytest

from daemon.launch_scanner import wallet_discovery as wd


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _tx(owner_buys, block_time=1_700_000_000, mint="MINT1", signature="PURE-PARSER"):
    """Build a fake jsonParsed getTransaction result.

    owner_buys: {owner: (pre, post)} for the mint under test.
    """
    pre, post = [], []
    for owner, (a, b) in owner_buys.items():
        pre.append({"owner": owner, "mint": mint,
                    "uiTokenAmount": {"uiAmount": a}})
        post.append({"owner": owner, "mint": mint,
                     "uiTokenAmount": {"uiAmount": b}})
    return {"transaction": {"signatures": [signature]}, "blockTime": block_time,
            "meta": {"err": None, "preTokenBalances": pre,
                     "postTokenBalances": post}}


class FakeRPC:
    """Canned Helius: sigs newest-first with blockTime, txs by signature."""

    def __init__(self, sigs, txs):
        # sigs: {mint: [(signature, block_time)]}; txs: {mint: {sig: tx}}
        self.sigs = sigs
        self.txs = txs
        self.calls = []
        self._mint = None

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getSignaturesForAddress":
            self._mint = params[0]
            return [{"signature": s, "blockTime": bt}
                    for s, bt in self.sigs.get(params[0], [])]
        if method == "getTransaction":
            return self.txs.get(self._mint, {}).get(params[0], {})
        raise AssertionError("unexpected rpc %s" % method)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FOREX_AGENT_HOME", str(tmp_path))
    # module cached _HOME at import; proposals_path() reads env live
    return tmp_path


# ---------------------------------------------------------------------------
# Step 1: early buyers
# ---------------------------------------------------------------------------

def test_tx_buyers_extracts_balance_increases():
    tx = _tx({"alice": (0, 100.0), "bob": (50.0, 50.0),
              "carol": (200.0, 10.0)})
    buyers = wd._tx_buyers(tx, "MINT1")
    assert set(buyers) == {"alice"}
    assert buyers["alice"] == pytest.approx(100.0)


def test_tx_buyers_ignores_other_mints():
    tx = {"meta": {"preTokenBalances": [],
                   "postTokenBalances": [
                       {"owner": "dave", "mint": "OTHER",
                        "uiTokenAmount": {"uiAmount": 999.0}}]}}
    assert wd._tx_buyers(tx, "MINT1") == {}


def test_early_buyers_respects_window():
    t0 = 1_700_000_000.0
    sigs = [("sig_new", t0 + 7200),   # 2h after first-seen: too late
            ("sig_mid", t0 + 600),    # inside window
            ("sig_old", t0 - 300)]    # slightly before: allowed
    txs = {
        "sig_new": _tx({"late": (0, 50.0)}, t0+7200, signature="sig_new"),
        "sig_mid": _tx({"mid": (0, 50.0)}, t0+600, signature="sig_mid"),
        "sig_old": _tx({"early": (0, 50.0)}, t0-300, signature="sig_old"),
    }
    rpc = FakeRPC({"MINT1": sigs}, {"MINT1": txs})
    buyers = wd.early_buyers_for_mint(rpc, "MINT1", t0,
                                      early_window_s=3600.0,
                                      sleep_s=0)
    assert set(buyers) == {"mid", "early"}
    assert "late" not in buyers


def test_early_buyers_never_raises_on_rpc_failure():
    def boom(method, params):
        raise RuntimeError("down")
    assert wd.early_buyers_for_mint(boom, "MINT1", time.time(),
                                    sleep_s=0) == {}


# ---------------------------------------------------------------------------
# Step 2: scoring
# ---------------------------------------------------------------------------

def _runner_rpc():
    t0 = 1_700_000_000.0
    sigs = {"MINT1": [("s1", t0 + 100)], "MINT2": [("s2", t0 + 100)]}
    txs = {"MINT1": {"s1": _tx({"w_alice": (0, 10.0), "w_bob": (0, 5.0)}, t0+100, signature="s1")},
           "MINT2": {"s2": _tx({"w_alice": (0, 10.0), "w_bob": (0, 5.0)},
                               block_time=t0+100, mint="MINT2", signature="s2")}}
    return FakeRPC(sigs, txs), t0


def test_score_wallets_counts_hits_and_smooths():
    rpc, t0 = _runner_rpc()
    runners = [
        {"mint": "MINT1", "symbol": "R1", "peak_mult": 12.0,
         "first_seen_ts": t0},
        {"mint": "MINT2", "symbol": "R2", "peak_mult": 8.0,
         "first_seen_ts": t0},
    ]
    scores = wd.score_wallets(runners, rpc, sleep_s=0)
    assert scores["w_alice"]["hits"] == 2
    assert scores["w_bob"]["hits"] == 2
    # Laplace smoothing: 2 hits -> (2+0.2)/(2+2) = 0.55
    assert scores["w_alice"]["hit_rate"] == pytest.approx(0.55)
    assert scores["w_alice"]["runners"] == ["R1", "R2"]


def test_score_wallets_excludes_deployers():
    rpc, t0 = _runner_rpc()
    runners = [{"mint": "MINT1", "symbol": "R1", "peak_mult": 12.0,
                "first_seen_ts": t0}]
    scores = wd.score_wallets(runners, rpc, deployers={"w_alice"},
                              sleep_s=0)
    assert "w_alice" not in scores
    assert "w_bob" in scores


# ---------------------------------------------------------------------------
# Step 3: proposals
# ---------------------------------------------------------------------------

def test_discovery_proposals_filters_and_sorts():
    scores = {
        "w1": {"hits": 3, "seen": 3, "hit_rate": 0.8, "runners": ["A"]},
        "w2": {"hits": 1, "seen": 1, "hit_rate": 0.9, "runners": ["B"]},
        "w3": {"hits": 2, "seen": 2, "hit_rate": 0.4, "runners": ["C"]},
        "w4": {"hits": 2, "seen": 2, "hit_rate": 0.7, "runners": ["D"]},
    }
    now = time.time()
    props = wd.discovery_proposals(scores, registry={"w4": {}},
                                   now=now, day="2026-10-01",
                                   min_hits=2, min_hit_rate=0.5)
    wallets = [p["wallet"] for p in props]
    assert wallets == ["w1"]          # w2 too few hits, w3 rate too low,
                                      # w4 already tracked
    p = props[0]
    assert p["kind"] == "smart_wallet_add"
    assert p["status"] == "open"
    assert p["id"].startswith("W-")


def test_save_and_load_proposals_roundtrip(home):
    now = time.time()
    props = [{"id": "W-ABC123", "kind": "smart_wallet_add",
              "wallet": "w_new", "status": "open", "created_ts": now}]
    added = wd.save_proposals(props)
    assert len(added) == 1
    # dupe wallet not added twice
    assert wd.save_proposals(props) == []
    loaded = wd.load_proposals()
    assert [p["wallet"] for p in loaded] == ["w_new"]
    # file is 0600
    assert (wd.proposals_path().stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Degen:
    discovery_enabled = True
    discovery_runner_mult = 5.0
    discovery_early_window_s = 3600.0
    discovery_min_hits = 1
    discovery_min_hit_rate = 0.3
    discovery_max_runners = 10
    discovery_max_new = 5
    discovery_max_sigs = 30
    discovery_interval_s = 3600.0


def test_run_discovery_end_to_end(home):
    rpc, t0 = _runner_rpc()
    state: dict = {}
    runners = [{"mint": "MINT1", "symbol": "R1", "peak_mult": 12.0,
                "first_seen_ts": t0}]
    out = wd.run_discovery(state, Degen(), runners, rpc=rpc,
                           registry={}, now=t0 + 5000)
    assert out["ran"] is True
    assert out["runners_mined"] == 1
    assert out["wallets_scored"] == 2
    assert out["proposals"] == 2
    # registry untouched — proposals only
    assert wd.load_proposals() and len(wd.load_proposals()) == 2
    # second run inside the throttle window does nothing
    out2 = wd.run_discovery(state, Degen(), runners, rpc=rpc,
                            registry={}, now=t0 + 5001)
    assert out2["ran"] is False
    assert out2["reason"] == "throttled"


def test_run_discovery_skips_below_bar_and_mined(home):
    rpc, _t0 = _runner_rpc()
    state = {wd._DISCOVERY_KEY: {wd._EVAL_KEY: {"MINT1": {}},
                                 "last_run_ts": 0}}
    out = wd.run_discovery(
        state, Degen(),
        [{"mint": "MINT1", "symbol": "R1", "peak_mult": 50.0,
          "first_seen_ts": 1},
         {"mint": "MINT9", "symbol": "R9", "peak_mult": 2.0,  # below bar
          "first_seen_ts": 1}],
        rpc=rpc, registry={}, now=time.time())
    assert out["ran"] is True
    assert out["runners_mined"] == 0
    assert out["reason"] == "no-fresh-runners"


def test_run_discovery_disabled_and_no_rpc(home):
    class Off:
        discovery_enabled = False
    out = wd.run_discovery({}, Off(), [], rpc=None)
    assert out["reason"] == "disabled"
    out = wd.run_discovery({}, Degen(), [], rpc=None)
    assert out["reason"] == "no-rpc"


# ---------------------------------------------------------------------------
# Pagination regression (2026-10-10): early_buyers_for_mint used to fetch
# only the newest 100 signatures, so any runner with >100 txs since launch
# silently returned zero buyers. It must now walk back to the launch.
# ---------------------------------------------------------------------------

class PagingRPC:
    """Canned Helius that honors limit/before, newest-first."""

    def __init__(self, sigs, txs):
        # sigs: [(signature, block_time)] newest-first; txs: {sig: tx}
        self.sigs = list(sigs)
        self.txs = dict(txs)
        self.sig_calls = 0

    def __call__(self, method, params):
        if method == "getSignaturesForAddress":
            self.sig_calls += 1
            opts = params[1] if len(params) > 1 else {}
            limit = int(opts.get("limit") or 100)
            before = opts.get("before")
            start = 0
            if before:
                for i, (s, _bt) in enumerate(self.sigs):
                    if s == before:
                        start = i + 1
                        break
            return [{"signature": s, "blockTime": bt}
                    for s, bt in self.sigs[start:start + limit]]
        if method == "getTransaction":
            return self.txs.get(params[0], {})
        raise AssertionError("unexpected rpc %s" % method)


def _deep_history(t0, n=250):
    """250 sigs newest-first; launch window lives at indexes 128..212,
    deeper than any single page — the old code found nothing here."""
    sigs, txs = [], {}
    for i in range(n):
        sig = "sig%03d" % i
        bt = t0 + 10000 - i * 50
        sigs.append((sig, bt))
        if t0 - 600 <= bt <= t0 + 3600:
            txs[sig] = _tx({"w%03d" % i: (0, 10.0)}, bt, signature=sig)
    return sigs, txs


def test_pagination_reaches_launch_beyond_first_page():
    t0 = 1_700_000_000.0
    sigs, txs = _deep_history(t0)
    rpc = PagingRPC(sigs, txs)
    buyers = wd.early_buyers_for_mint(rpc, "MINT1", t0,
                                      early_window_s=3600.0, sleep_s=0)
    # 85 in-window sigs; max_sigs=30 earliest -> w212..w183 (smallest
    # blockTime first: w212 bought right at the launch edge).
    assert len(buyers) == 30
    assert "w212" in buyers and "w183" in buyers
    assert "w182" not in buyers  # beyond max_sigs
    assert rpc.sig_calls == 3  # walked 3 pages to pass the window


def test_pagination_oldest_first_ordering():
    t0 = 1_700_000_000.0
    sigs, txs = _deep_history(t0)
    rpc = PagingRPC(sigs, txs)
    buyers = wd.early_buyers_for_mint(rpc, "MINT1", t0,
                                      early_window_s=3600.0, sleep_s=0)
    # Earliest in-window buy has the smallest blockTime.
    assert buyers["w212"] < buyers["w183"]


def test_pagination_respects_max_pages():
    t0 = 1_700_000_000.0
    sigs, txs = _deep_history(t0)
    rpc = PagingRPC(sigs, txs)
    buyers = wd.early_buyers_for_mint(rpc, "MINT1", t0,
                                      early_window_s=3600.0, sleep_s=0,
                                      max_pages=1)
    assert rpc.sig_calls == 1
    # Page 1 (newest 100) holds no in-window sigs -> nothing found,
    # but it terminates instead of hanging.
    assert buyers == {}


def test_pagination_terminates_when_before_ignored():
    # FakeRPC (above) ignores limit/before and returns everything every
    # time: the walk must still terminate via the no-progress guard.
    t0 = 1_700_000_000.0
    sigs = [("sig_new", t0 + 7200), ("sig_mid", t0 + 600)]
    txs = {"MINT1": {"sig_mid": _tx({"mid": (0, 50.0)}, t0+600, signature="sig_mid"),
                     "sig_new": _tx({"late": (0, 50.0)}, t0+7200, signature="sig_new")}}
    rpc = FakeRPC({"MINT1": sigs}, txs)
    buyers = wd.early_buyers_for_mint(rpc, "MINT1", t0,
                                      early_window_s=3600.0, sleep_s=0)
    assert set(buyers) == {"mid"}


def test_pagination_dedupes_overlapping_pages():
    t0 = 1_700_000_000.0
    sigs, txs = _deep_history(t0)

    class OverlapRPC(PagingRPC):
        def __call__(self, method, params):
            if method == "getSignaturesForAddress":
                # return each page twice to simulate overlap
                out = super().__call__(method, params)
                return out + out
            return super().__call__(method, params)

    rpc = OverlapRPC(sigs, txs)
    buyers = wd.early_buyers_for_mint(rpc, "MINT1", t0,
                                      early_window_s=3600.0, sleep_s=0,
                                      max_sigs=200)
    assert len(buyers) == 85  # all in-window, counted once
