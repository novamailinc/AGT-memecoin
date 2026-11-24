"""A 429 keeps redacted, capped evidence (status, allowlisted headers,
body) so throughput vs quota vs other causes are classified from facts,
not guessed. Keys/URLs/cookies never leak."""
import io
import urllib.error

from daemon.intelligence.acquisition.providers.alchemy import AlchemyAdapter

KEY = "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"


def _opener_429(headers):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://x/v2/" + KEY, 429, "Too Many", headers,
            io.BytesIO(('{"error":{"code":429,"message":"exceeded for key %s '
                        'see https://a.b/c"}}' % KEY).encode()))
    return opener


def _call(headers, monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", KEY)
    a = AlchemyAdapter(home="/nonexistent-home", transport=_opener_429(headers))
    return a.call("getSignaturesForAddress", ["x"])


def test_429_evidence_structured_only(monkeypatch):
    r = _call({"Retry-After": "2", "X-RateLimit-Remaining": "0",
               "Set-Cookie": "sessionsecret", "Content-Type": "application/json",
               "X-Request-Id": "abc", "X-RateLimit-Policy": "Bearer short-tok 10.0.0.1"},
              monkeypatch)
    assert r["kind"] == "rate_limited" and r["transport_attempts"] == 1
    ev = r["rate_limit_evidence"]
    assert ev["status"] == 429 and ev["error_code"] == 429
    assert ev["numeric_headers"]["retry-after"] == "2"
    assert ev["numeric_headers"]["x-ratelimit-remaining"] == "0"
    # non-numeric ratelimit header value is NOT stored
    assert ev["numeric_headers"]["x-ratelimit-policy"] == "<non-numeric>"
    assert ev["content_type"] == "application/json"
    assert "exceeded" in ev["message_vocab"]
    blob = repr(ev)
    for leak in (KEY, "https://", "short-tok", "10.0.0.1", "sessionsecret", "abc"):
        assert leak not in blob
    assert set(ev) == {"status", "numeric_headers", "content_type", "body_len",
                       "json_valid", "error_code", "message_vocab"}


def test_arbitrary_body_text_never_stored(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", KEY)
    def opener(req, timeout=None):
        raise urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(
            b'{"error":{"code":-1,"message":"Bearer tok123 from 192.168.1.5 monthly"}}'))
    a = AlchemyAdapter(home="/nonexistent-home", transport=opener)
    ev = a.call("getSignaturesForAddress", ["x"])["rate_limit_evidence"]
    assert ev["message_vocab"] == ["monthly"]
    assert "tok123" not in repr(ev) and "192.168" not in repr(ev)


def test_ok_result_has_no_evidence(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", KEY)
    class R:
        status = 200; headers = {}
        def read(self): return b'{"jsonrpc":"2.0","id":1,"result":[]}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    a = AlchemyAdapter(home="/nonexistent-home", transport=lambda req, timeout=None: R())
    assert "rate_limit_evidence" not in a.call("getSignaturesForAddress", ["x"])
