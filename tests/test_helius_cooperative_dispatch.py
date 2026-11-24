"""Cooperative Helius multi-key dispatch (providers/helius.revised_call).

Design contract under test (offline; transport, pacer, account gate and
parse layer stubbed):

- COOPERATION: a stable method/params shard gives every call a home key;
  distinct calls spread across keys; the same call repeats its key.
- FAILURE ISOLATION: a key-level failure (unconfigured source or
  transport error) fails over to the next key; each attempt acquires
  its own aggregate-bucket token.
- NO THROTTLE/BUDGET BYPASS: an aggregate refusal (account hold or
  pacer cooldown) hops no keys; a vendor 429 is returned, not evaded;
  an RPC-level error (the key answered) fails over nowhere.
- PERSISTENCE: per-key counters (requests, method-weighted credits,
  outcomes) survive in helius_key_stats.json; no key material is stored.
"""
import json
import os
import pathlib
import sys

import pytest

TREE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TREE))

from daemon.intelligence.acquisition.providers import helius as H  # noqa: E402
from daemon.intelligence.acquisition.providers import base as B  # noqa: E402
from daemon.intelligence.acquisition import helius_account as HA  # noqa: E402


class FakePacer:
    def __init__(self, acquire_results):
        self._results = list(acquire_results)
        self.acquire_calls = 0
        self.reports = []

    def acquire(self, bucket, *a, **kw):
        self.acquire_calls += 1
        return self._results.pop(0) if self._results else True

    def report(self, bucket, ok):
        self.reports.append(ok)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setattr(H, "numbered_keys", lambda: ["K1", "K2", "K3"])
    monkeypatch.setattr(HA, "allowed", lambda home=None: True)
    monkeypatch.setattr(HA, "throttled", lambda *a, **kw: None)
    state = {"pacer": FakePacer([True] * 20)}
    import daemon.launch_scanner.helius_ratelimit as HR
    monkeypatch.setattr(HR, "pacer", lambda: state["pacer"])
    monkeypatch.setattr(HR, "bucket_id_for_key", lambda k: "acct")
    adapter = H.HeliusAdapter(home=home, transport=None)
    adapter.revised = True
    return {"home": home, "adapter": adapter, "state": state}


def script_transport(monkeypatch, responses):
    """responses: list of (status, body, err, retry) per http call."""
    calls = []
    def fake(url, method, params, timeout=None, _opener=None):
        calls.append(url)
        return responses.pop(0)
    monkeypatch.setattr(B, "http_json_rpc", fake)
    return calls


def map_parse(monkeypatch):
    def fake(name, status, body, err, retry_after=None):
        if err is not None:
            return None, "error", None, None
        if status == 429:
            return None, "rate_limited", 5.0, "per_second"
        return (body or {}).get("result"), "ok", None, None
    monkeypatch.setattr(B, "_parse_rpc_body", fake)


def test_stable_shard_spreads_and_repeats(env, monkeypatch):
    map_parse(monkeypatch)
    script_transport(monkeypatch, [(200, {"result": 1}, None, None)] * 40)
    seen = {}
    for i in range(12):
        out = env["adapter"].call("getBalance", ["acct%d" % i])
        assert out["kind"] == "ok"
        seen[i] = out["credential_source"]
        again = env["adapter"].call("getBalance", ["acct%d" % i])
        assert again["credential_source"] == seen[i]  # stable home key
    assert len(set(seen.values())) > 1  # distinct calls spread


def test_failover_on_transport_error_consumes_tokens(env, monkeypatch):
    map_parse(monkeypatch)
    calls = script_transport(monkeypatch, [
        (None, None, "timeout", None),          # home key transport error
        (200, {"result": 42}, None, None),       # next key answers
    ])
    out = env["adapter"].call("getBalance", ["acct-x"])
    assert out["kind"] == "ok"
    assert len(calls) == 2
    assert env["state"]["pacer"].acquire_calls == 2  # each attempt pays
    stats = json.load(open(os.path.join(env["home"],
                                        "helius_key_stats.json")))
    assert sum(s.get("transport_error", 0) for s in stats.values()) == 1
    assert sum(s.get("ok", 0) for s in stats.values()) == 1


def test_no_failover_on_rpc_level_error(env, monkeypatch):
    map_parse(monkeypatch)
    # status 200 + err None mapped to kind 'error' by forcing body=None
    monkeypatch.setattr(B, "_parse_rpc_body",
                        lambda *a, **kw: (None, "error", None, None))
    calls = script_transport(monkeypatch,
                             [(200, {"error": {"code": -32602}}, None, None)])
    out = env["adapter"].call("getBalance", ["acct-y"])
    assert out["kind"] == "error"
    assert len(calls) == 1  # the key answered; nothing to fail over from


def test_no_failover_on_vendor_429(env, monkeypatch):
    map_parse(monkeypatch)
    calls = script_transport(monkeypatch, [(429, None, None, "5")])
    out = env["adapter"].call("getBalance", ["acct-z"])
    assert out["kind"] == "rate_limited"
    assert len(calls) == 1               # a 429 is answered, not evaded
    assert env["state"]["pacer"].acquire_calls == 1


def test_aggregate_cooldown_hops_no_keys(env, monkeypatch):
    map_parse(monkeypatch)
    env["state"]["pacer"] = FakePacer([False])
    import daemon.launch_scanner.helius_ratelimit as HR
    monkeypatch.setattr(HR, "pacer", lambda: env["state"]["pacer"])
    calls = script_transport(monkeypatch, [])
    out = env["adapter"].call("getBalance", ["acct-w"])
    assert out["kind"] == "rate_limited"
    assert out.get("note") == "shared account cooldown"
    assert calls == []                   # aggregate refusal hops no keys


def test_account_hold_hops_no_keys(env, monkeypatch):
    monkeypatch.setattr(HA, "allowed", lambda home=None: False)
    calls = script_transport(monkeypatch, [])
    out = env["adapter"].call("getBalance", ["acct-v"])
    assert out["kind"] == "rate_limited"
    assert out.get("note") == "shared account hold"
    assert calls == []
    assert env["state"]["pacer"].acquire_calls == 0


def test_unconfigured_source_skips_to_next(env, monkeypatch):
    map_parse(monkeypatch)
    # Only one key configured; whichever shard is home, dispatch must
    # land on the configured one.
    monkeypatch.setattr(H, "numbered_keys", lambda: [None, "K2", None])
    calls = script_transport(monkeypatch, [(200, {"result": 7}, None, None)])
    out = env["adapter"].call("getBalance", ["acct-u"])
    assert out["kind"] == "ok"
    assert out["credential_source"] == "key2"
    assert len(calls) == 1 and "K2" in calls[0]


def test_stats_persist_without_key_material(env, monkeypatch):
    map_parse(monkeypatch)
    script_transport(monkeypatch, [(200, {"result": 1}, None, None)] * 4)
    env["adapter"].call("getBalance", ["acct-t"])
    # Fresh adapter, same home: counters accumulate across instances.
    again = H.HeliusAdapter(home=env["home"], transport=None)
    again.revised = True
    again.call("getBalance", ["acct-t"])
    raw = open(os.path.join(env["home"], "helius_key_stats.json")).read()
    stats = json.loads(raw)
    assert sum(s["requests"] for s in stats.values()) == 2
    assert "K1" not in raw and "K2" not in raw and "K3" not in raw
    assert all(isinstance(s["credits"], (int, float))
               for s in stats.values())


def test_all_sources_unconfigured(env, monkeypatch):
    monkeypatch.setattr(H, "numbered_keys", lambda: [None, None, None])
    out = env["adapter"].call("getBalance", ["acct-s"])
    assert out["kind"] == "unconfigured"
