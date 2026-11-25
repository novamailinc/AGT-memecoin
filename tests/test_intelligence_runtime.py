"""Tests for daemon.intelligence.runtime (pytest mode, builtin fixtures).

Every provider answer is an injected fake: no network, no keys. Checkpoint
ordering, restart dedup, persistent gaps and fail-closed status are each
tested by observable store/state, not by mocks of the code under test,
except where a crash is simulated by making one durable step raise.
"""
import json
import os
import subprocess
import sys

import pytest

from daemon.intelligence import runtime as rt
from daemon.intelligence.events.event_store import EventStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "tests", "data", "intelligence")
ADDR = "ProgramAddr1111111111111111111111111111111"
KEY = "pump"


def _tx(name):
    with open(os.path.join(DATA, "atx_%s.json" % name)) as fh:
        wrap = json.load(fh)
    return wrap.get("transaction") or wrap


def _row(tx):
    return {"signature": tx["transaction"]["signatures"][0],
            "slot": tx["slot"], "blockTime": tx["blockTime"]}


class Chain(object):
    """Fake RPC over a list of tx dicts (oldest first)."""

    def __init__(self, txs):
        self.txs = list(txs)
        self.fail_tx = False
        self.fail_sigs = None
        self.calls = []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getSignaturesForAddress":
            if self.fail_sigs:
                return {"kind": self.fail_sigs}
            opts = params[1]
            rows = [_row(t) for t in reversed(self.txs)]
            if opts.get("until"):
                sigs = [r["signature"] for r in rows]
                if opts["until"] in sigs:
                    rows = rows[:sigs.index(opts["until"])]
            if opts.get("before"):
                sigs = [r["signature"] for r in rows]
                if opts["before"] in sigs:
                    rows = rows[sigs.index(opts["before"]) + 1:]
            return {"kind": "ok", "result": rows[:opts["limit"]],
                    "provider": "fake"}
        if method == "getTransaction":
            if self.fail_tx:
                return {"kind": "timeout"}
            for t in self.txs:
                if t["transaction"]["signatures"][0] == params[0]:
                    return {"kind": "ok", "result": t, "provider": "fake"}
            return {"kind": "no_result"}
        return {"kind": "no_result"}


def _make(home, chain, **kw):
    store = EventStore(path=os.path.join(str(home), "intelligence",
                                         "events.db"))
    store.open()
    streams = [rt.StreamSpec(KEY, ADDR)]
    return rt.IntelligenceRuntime(str(home), store, chain, streams, **kw)


def _applied(runtime):
    n = 0
    for ev in runtime.store.list_events(limit=1000):
        if runtime.store.is_applied(ev.event_id):
            n += 1
    return n


def _trades(runtime):
    return sum(s["trade_count"]
               for s in runtime.processor.registry.tokens.values())


def test_checkpoint_after_store_and_apply(tmp_path):
    chain = Chain([_tx("create"), _tx("buy")])
    r = _make(tmp_path, chain)
    st = r.run_cycle()
    cp = r.store.get_checkpoint(KEY)
    assert cp["last_signature"] == _row(_tx("buy"))["signature"]
    assert _applied(r) == 2
    assert st["state"] == rt.STATE_OK
    r.store.close()


def test_crash_between_apply_and_checkpoint_replays_cleanly(tmp_path):
    chain = Chain([_tx("create"), _tx("buy")])
    r = _make(tmp_path, chain)

    def boom(*a, **k):
        raise RuntimeError("crash before checkpoint")
    r.store.set_checkpoint = boom
    st = r.run_cycle()
    assert st["state"] == rt.STATE_DEGRADED
    assert "cycle_exception" in st["streams"][KEY]["reasons"]
    # Applied rows are durable, the checkpoint is not.
    assert r.store.get_checkpoint(KEY) is None
    assert _applied(r) >= 1
    applied_before = _applied(r)
    r.store.close()
    r2 = _make(tmp_path, chain)
    r2.run_cycle()
    assert r2.store.get_checkpoint(KEY)["last_signature"] == \
        _row(_tx("buy"))["signature"]
    assert _applied(r2) == 2 and applied_before <= 2
    assert _trades(r2) == 1          # one buy, never double counted
    r2.store.close()


def test_restart_rebuilds_registry_without_double_count(tmp_path):
    chain = Chain([_tx("create"), _tx("buy")])
    r = _make(tmp_path, chain)
    r.run_cycle()
    tokens = json.dumps(r.processor.registry.tokens, sort_keys=True)
    r.store.close()
    r2 = _make(tmp_path, chain)
    r2.startup()
    assert r2.rebuilt == 2
    assert json.dumps(r2.processor.registry.tokens, sort_keys=True) == tokens
    r2.run_cycle()
    assert json.dumps(r2.processor.registry.tokens, sort_keys=True) == tokens
    assert _trades(r2) == 1
    r2.store.close()


def test_stored_but_unapplied_event_is_applied_once_on_restart(tmp_path):
    chain = Chain([_tx("create"), _tx("buy")])
    r = _make(tmp_path, chain)
    r.store.mark_applied = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("crash between store and apply"))
    st = r.run_cycle()
    assert st["state"] == rt.STATE_DEGRADED
    assert r.store.get_checkpoint(KEY) is None
    assert _applied(r) == 0
    assert r.store.count_events(verification_status="verified") >= 1
    r.store.close()
    r2 = _make(tmp_path, chain)
    r2.startup()
    assert r2.counters["reconciled"] >= 1
    first = _applied(r2)
    r2.run_cycle()
    assert _applied(r2) == 2 and first <= 2
    assert _trades(r2) == 1
    r2.store.close()


def test_page_budget_exhaustion_persists_gap_and_keeps_checkpoint(tmp_path):
    chain = Chain([_tx("create")])
    r = _make(tmp_path, chain)
    r.run_cycle()
    cp = r.store.get_checkpoint(KEY)
    # Three new transactions but only one single-row page allowed.
    chain.txs = [_tx("create"), _tx("buy"), _tx("migrate")]
    r.max_pages = 1
    r.page_limit = 1
    st = r.run_cycle()
    assert r.store.get_checkpoint(KEY) == cp
    assert st["state"] == rt.STATE_DEGRADED
    assert st["streams"][KEY]["gaps"]
    assert "catchup_failed" in st["streams"][KEY]["reasons"]
    r.store.close()
    # The gap survives a restart (persisted, not in memory only).
    r2 = _make(tmp_path, chain, max_pages=1, page_limit=1)
    assert r2.state.degraded(KEY)
    assert r2.state.stream(KEY)["gaps"]
    # With a sufficient budget it recovers and the degraded state clears.
    r2.max_pages = 8
    r2.page_limit = 10
    st2 = r2.run_cycle()
    assert st2["state"] == rt.STATE_OK
    assert st2["streams"][KEY]["gaps"] == []
    r2.store.close()


def test_fetch_failure_stops_stream_without_advancing(tmp_path):
    chain = Chain([_tx("create"), _tx("buy")])
    chain.fail_tx = True
    r = _make(tmp_path, chain)
    st = r.run_cycle()
    assert r.store.get_checkpoint(KEY) is None
    assert st["state"] == rt.STATE_DEGRADED
    assert "processing_stopped" in st["streams"][KEY]["reasons"]
    chain.fail_tx = False
    assert r.run_cycle()["state"] == rt.STATE_OK
    r.store.close()


def test_signature_page_failure_is_degraded(tmp_path):
    chain = Chain([_tx("create")])
    chain.fail_sigs = "timeout"
    r = _make(tmp_path, chain)
    st = r.run_cycle()
    assert st["state"] == rt.STATE_DEGRADED
    assert r.store.get_checkpoint(KEY) is None
    r.store.close()


def test_corrupt_state_file_is_quarantined_and_degrades(tmp_path):
    d = tmp_path / "intelligence"
    d.mkdir()
    (d / rt.STATE_FILE).write_text("{not json")
    chain = Chain([_tx("create")])
    r = _make(tmp_path, chain)
    assert r.state.data.get("state_file_corrupt") is True
    assert any(n.startswith(rt.STATE_FILE) and n != rt.STATE_FILE
               for n in os.listdir(str(d)))
    st = r.run_cycle()
    # No checkpoint yet: nothing proves the lost gaps are covered.
    assert st["state"] == rt.STATE_DEGRADED
    assert "state_file_corrupt" in st["streams"][KEY]["reasons"]
    chain.txs.append(_tx("buy"))
    st = r.run_cycle()
    # A checkpoint now exists and a clean catch-up from it covers the rest.
    assert st["state"] == rt.STATE_OK
    r.store.close()


def test_routed_rpc_maps_unusable_and_retention_limited():
    def mk(res):
        return rt.routed_rpc(call=lambda *a, **k: res)
    ok = mk({"kind": "ok", "result": [1], "provider": "alchemy"})
    assert ok("getBlockHeight", [])["kind"] == "ok"
    bad = mk({"kind": "ok", "result": [1], "decision_usable": False})
    assert bad("getBlockHeight", [])["kind"] != "ok"
    ret = mk({"kind": "ok", "result": [1],
              "completeness": "retention-limited"})
    assert ret("getBlockHeight", [])["kind"] != "ok"
    assert mk(None)("getBlockHeight", [])["kind"] != "ok"


def test_routed_rpc_marks_normal_scanning_background():
    seen = {}

    def call(method, params, **kw):
        seen.update(kw)
        return {"kind": "ok", "result": 1}
    rt.routed_rpc(call=call)("getSlot", [])
    assert seen["background"] is True
    assert seen["hints"] == {"purpose": "normal_scanning"}


def test_pipeline_failure_goes_to_retry_queue(tmp_path):
    chain = Chain([_tx("create")])
    r = _make(tmp_path, chain)

    def broken(*a, **k):
        return "failed", None
    r.processor.process_transaction = broken
    st = r.run_cycle()
    assert r.store.get_checkpoint(KEY) is None
    assert r.retry.pending_count() == 1
    assert st["state"] == rt.STATE_DEGRADED
    r.store.close()


def test_unconfigured_is_not_ok(tmp_path):
    store = EventStore(path=str(tmp_path / "intelligence" / "events.db"))
    store.open()
    r = rt.IntelligenceRuntime(str(tmp_path), store, Chain([]), [])
    st = r.run_cycle()
    assert st["state"] == rt.STATE_UNCONFIGURED
    ok, reason = rt.intelligence_usable(str(tmp_path))
    assert ok is False
    store.close()


def test_intelligence_usable_fails_closed(tmp_path):
    home = str(tmp_path)
    assert rt.intelligence_usable(home) == (False, "no_status")
    d = tmp_path / "intelligence"
    d.mkdir()
    path = d / rt.STATUS_FILE

    def put(state, ts):
        path.write_text(json.dumps({"state": state, "ts": ts}))
    put("ok", 1000.0)
    assert rt.intelligence_usable(home, 300, now=1100.0) == (True, "ok")
    assert rt.intelligence_usable(home, 300, now=2000.0) == \
        (False, "stale_status")
    put("degraded", 1000.0)
    assert rt.intelligence_usable(home, 300, now=1100.0)[0] is False
    path.write_text("garbage")
    assert rt.intelligence_usable(home, 300, now=1100.0)[0] is False


class FakeFusion(object):
    def __init__(self, raises=False):
        self.mints = []
        self.raises = raises

    def run(self, mint, lane=None, signal_provider=None, **kw):
        self.mints.append((mint, lane))
        if self.raises:
            raise ValueError("fusion boom")


def test_shadow_fusion_only_for_token_created_once(tmp_path):
    chain = Chain([_tx("create"), _tx("buy")])
    fusion = FakeFusion()
    r = _make(tmp_path, chain, fusion_runner=fusion)
    r.run_cycle()
    r.run_cycle()
    assert len(fusion.mints) == 1
    assert fusion.mints[0][1] == "shadow"
    r.store.close()


def test_shadow_fusion_exception_is_ignored(tmp_path):
    chain = Chain([_tx("create"), _tx("buy")])
    r = _make(tmp_path, chain, fusion_runner=FakeFusion(raises=True))
    st = r.run_cycle()
    assert st["state"] == rt.STATE_OK
    assert r.counters["fusion_errors"] == 1
    r.store.close()


def test_parse_streams():
    streams, errors = rt.parse_streams("a=Addr1,b=Addr2")
    assert [s.key for s in streams] == ["a", "b"] and not errors
    streams, errors = rt.parse_streams("")
    assert streams == []


def test_import_opens_no_network_and_no_launch_scanner():
    code = (
        "import socket, urllib.request\n"
        "def boom(*a, **k): raise AssertionError('network at import')\n"
        "socket.socket.connect = boom\n"
        "urllib.request.urlopen = boom\n"
        "import sys\n"
        "import daemon.intelligence.runtime, daemon.intelligence.__main__\n"
        "assert not any(m.startswith('launch_scanner') for m in sys.modules)\n"
        "print('clean')\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout


def test_hard_kill_before_checkpoint_recovers(tmp_path):
    chain_txs = ["create", "buy"]
    script = (
        "import os, sys, json\n"
        "sys.path.insert(0, %r)\n"
        "from tests.test_intelligence_runtime import _make, Chain, _tx, KEY\n"
        "from pathlib import Path\n"
        "r = _make(Path(%r), Chain([_tx(n) for n in %r]))\n"
        "r.store.set_checkpoint = lambda *a, **k: os._exit(7)\n"
        "r.run_cycle()\n" % (ROOT, str(tmp_path), chain_txs))
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 7, out.stderr
    chain = Chain([_tx(n) for n in chain_txs])
    r = _make(tmp_path, chain)
    assert r.store.get_checkpoint(KEY) is None
    r.run_cycle()
    assert _applied(r) == 2
    assert _trades(r) == 1
    r.store.close()


def test_held_market_event_does_not_advance_checkpoint(tmp_path):
    chain = Chain([_tx("create")])
    r = _make(tmp_path, chain)
    # Force the processor to see a processed-only (unverifiable) tx.
    real = r.processor.process_transaction

    def processed(provider, tx, commitment=None, **kw):
        return real(provider, tx, commitment="processed", **kw)
    r.processor.process_transaction = processed
    st = r.run_cycle()
    assert r.store.get_checkpoint(KEY) is None
    assert st["state"] == rt.STATE_DEGRADED
    assert "processing_stopped" in st["streams"][KEY]["reasons"]
    r.store.close()


def test_corrupt_checkpoint_store_error_is_a_store_error():
    try:
        from daemon.intelligence.acquisition.event_backfill import \
            CorruptCheckpointStoreError
    except ImportError:
        pytest.skip("needs the shared checkpoint delta")
    assert rt._is_store_error(CorruptCheckpointStoreError("x"))
