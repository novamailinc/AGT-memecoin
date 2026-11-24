"""429 evidence must belong to the request that produced it: a 429
captured by an earlier http_json_rpc call (e.g. the legacy Helius path,
which never consumes it) must not attach to a later rate-limited result."""
import io
import json
import urllib.error

from daemon.intelligence.acquisition.providers import base
from daemon.intelligence.acquisition.providers.alchemy import AlchemyAdapter

KEY = "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"


def _http_429(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 429, "x", {"Retry-After": "7"},
                                 io.BytesIO(b'{"error":{"code":429,"message":"monthly"}}'))


class _RpcErr429:
    status = 200
    def read(self):
        return json.dumps({"jsonrpc": "2.0", "id": 1,
                           "error": {"code": 429, "message": "rate limit"}}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_stale_429_meta_not_attached_to_later_result(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", KEY)
    # an earlier 429 whose meta is never consumed (legacy path)
    base.http_json_rpc("https://x/v2/k", "getSlot", [], _opener=_http_429)
    a = AlchemyAdapter(home="/nonexistent-home", transport=lambda req, timeout=None: _RpcErr429())
    r = a.call("getSignaturesForAddress", ["x"])
    assert r["kind"] == "rate_limited"
    assert "rate_limit_evidence" not in r      # nothing was captured for THIS request


def test_own_429_meta_still_attached(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", KEY)
    a = AlchemyAdapter(home="/nonexistent-home", transport=_http_429)
    ev = a.call("getSignaturesForAddress", ["x"])["rate_limit_evidence"]
    assert ev["numeric_headers"]["retry-after"] == "7"
