"""Failover contract on the real router (no network): an UNKNOWN post-I/O
outcome is terminal; an ANSWERED 429 fails over to the next candidate
under its own reservation. Companion to the fixtures that used to assert
failover after an unknown outcome (stale since v3)."""
import io
import json
import urllib.error

from daemon.intelligence.acquisition.provider_router import ProviderRouter


class _Ok:
    status = 200
    def read(self):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": "FALLBACK"}}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _router(tmp_path, monkeypatch, alchemy_exc):
    monkeypatch.setenv("ALCHEMY_API_KEY", "fixture-not-real")
    monkeypatch.delenv("CHAINSTACK_ENDPOINT", raising=False)
    calls = []
    def transport(req, timeout=None):
        calls.append(req.full_url.split("?")[0])
        if "alchemy.com" in req.full_url:
            raise alchemy_exc(req)
        return _Ok()
    return ProviderRouter(home=str(tmp_path), helius_enabled=False, transport=transport), calls


def _answered_429(req):
    return urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b'{"error":{"code":429,"message":"rate limit"}}'))


def _unknown(req):
    return urllib.error.URLError("fixture outage")


def test_unknown_outcome_is_terminal_no_failover(tmp_path, monkeypatch):
    r, calls = _router(tmp_path, monkeypatch, _unknown)
    res = r.call("getAccountInfo", ["11111111111111111111111111111111"], max_attempts=3)
    assert len(calls) == 1 and "alchemy.com" in calls[0]       # nothing else was sent
    assert res.get("outcome_unknown") is True
    assert res.get("kind") != "ok"


def test_answered_429_fails_over_to_next_candidate(tmp_path, monkeypatch):
    r, calls = _router(tmp_path, monkeypatch, _answered_429)
    res = r.call("getAccountInfo", ["11111111111111111111111111111111"], max_attempts=3)
    assert len(calls) == 2 and "alchemy.com" in calls[0] and "alchemy.com" not in calls[1]
    assert res.get("kind") == "ok" and res.get("fallback") is True
    assert not res.get("outcome_unknown")
