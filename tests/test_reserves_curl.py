#!/usr/bin/env python3
"""Tests for the curl transport in reserves._rpc_attempt (2026-11-20).

_rpc_attempt used urllib.request.urlopen, which flaps on this VM
(HTTP 308, IncompleteRead, RemoteDisconnected) while curl
succeeds — the flake that starved the entry path during the
Helius quiet period (candidates rejected fail-closed as
unknown_curve / unknown_canonical). The transport is now a curl
subprocess; the return contract is unchanged: (result, kind,
elapsed_s) with kind in {"ok", "429", "fail"}; JSON-RPC
-32005/-32029 map to "429"; any curl failure is "fail"
(fail-closed); never raises.

No network: every test mocks subprocess.run. Run:
python3 tests/test_reserves_curl.py

The URL travels in a per-call 0600 curl config file (-K), never on
argv: the Helius URL carries the api key in its query string, and
argv is visible in ps / /proc/<pid>/cmdline.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import reserves

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _body(result=None, error=None):
    d = {"jsonrpc": "2.0", "id": 1}
    if result is not None:
        d["result"] = result
    if error is not None:
        d["error"] = error
    return (d,)


def _out(payload, status=200):
    import json
    return json.dumps(payload).encode() + (
        b"\n__HTTP_STATUS__:%d" % status)


class _FakeRun:
    """Records the curl argv; returns a scripted _FakeProc or raises.

    Also records the content of the -K config file, read live during
    the call (the file is deleted after _rpc_attempt returns)."""

    def __init__(self, proc=None, exc=None):
        self.proc = proc
        self.exc = exc
        self.calls = []
        self.config_contents = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if "-K" in argv:
            cfg = argv[argv.index("-K") + 1]
            try:
                with open(cfg, "rb") as fh:
                    self.config_contents.append(fh.read())
            except OSError:
                self.config_contents.append(None)
        if self.exc is not None:
            raise self.exc
        return self.proc


class _Ctx:
    """Patches reserves.subprocess.run; restores on exit."""

    def __init__(self, fake):
        self.fake = fake
        self._real = None

    def __enter__(self):
        self._real = reserves.subprocess
        fake = self.fake

        class _Sub:
            run = staticmethod(fake)

        reserves.subprocess = _Sub
        return self

    def __exit__(self, *exc):
        reserves.subprocess = self._real
        return False


BODY = b'{"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress"}'
URL = "https://api.mainnet-beta.solana.com"


def _attempt(fake, timeout=12):
    with _Ctx(fake):
        return reserves._rpc_attempt(URL, BODY, "getSignaturesForAddress",
                                     "api.mainnet-beta.solana.com",
                                     timeout)


def test_ok():
    proc = _FakeProc(0, _out({"result": {"v": 1}}))
    fake = _FakeRun(proc)
    result, kind, elapsed = _attempt(fake)
    check("curl: ok returns result", result == {"v": 1})
    check("curl: ok kind", kind == "ok")
    check("curl: elapsed is a float", isinstance(elapsed, float))


def test_http_429():
    proc = _FakeProc(0, _out({"error": "slow down"}, status=429))
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: HTTP 429 kind", kind == "429" and result is None)


def test_curl_exit_nonzero():
    proc = _FakeProc(7, b"", b"Failed to connect to host")
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: nonzero exit is fail", kind == "fail"
          and result is None)


def test_http_500():
    proc = _FakeProc(0, _out({"error": "boom"}, status=500))
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: HTTP 500 is fail", kind == "fail" and result is None)


def test_http_308_no_redirect():
    proc = _FakeProc(0, _out({}, status=308))
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: HTTP 308 is fail (no redirect follow)",
          kind == "fail" and result is None)


def test_jsonrpc_32005():
    proc = _FakeProc(0, _out({"error": {"code": -32005,
                                        "message": "rate limited"}}))
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: JSON-RPC -32005 maps to 429",
          kind == "429" and result is None)


def test_jsonrpc_32029():
    proc = _FakeProc(0, _out({"error": {"code": -32029,
                                        "message": "rate limited"}}))
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: JSON-RPC -32029 maps to 429",
          kind == "429" and result is None)


def test_jsonrpc_other_error():
    proc = _FakeProc(0, _out({"error": {"code": -32601,
                                        "message": "not found"}}))
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: other JSON-RPC error is fail",
          kind == "fail" and result is None)


def test_empty_result_dict():
    proc = _FakeProc(0, _out({}))
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: empty result dict is fail",
          kind == "fail" and result is None)


def test_bad_json():
    proc = _FakeProc(0, b"not json\n__HTTP_STATUS__:200")
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: bad JSON body is fail, never raises",
          kind == "fail" and result is None)


def test_unparseable_output():
    proc = _FakeProc(0, b"no status marker at all")
    result, kind, _e = _attempt(_FakeRun(proc))
    check("curl: output without status marker is fail",
          kind == "fail" and result is None)


def test_timeout_never_raises():
    fake = _FakeRun(exc=subprocess.TimeoutExpired(cmd="curl",
                                                  timeout=12))
    result, kind, _e = _attempt(fake)
    check("curl: subprocess timeout is fail, never raises",
          kind == "fail" and result is None)


def test_curl_argv():
    proc = _FakeProc(0, _out({"result": 1}))
    fake = _FakeRun(proc)
    _attempt(fake, timeout=12)
    argv, kw = fake.calls[0]
    check("curl: argv starts with curl", argv[0] == "curl")
    check("curl: POST", "-X" in argv and "POST" in argv)
    check("curl: url is NOT on the argv (keyed URLs stay out of ps)",
          URL not in argv and not any(URL in a for a in argv))
    check("curl: url travels via a -K config file",
          "-K" in argv and fake.config_contents
          and fake.config_contents[0] is not None
          and URL.encode() in fake.config_contents[0])
    check("curl: JSON content-type header",
          "Content-Type: application/json" in argv)
    check("curl: body via stdin", "--data-binary" in argv
          and "@-" in argv and kw.get("input") == BODY)
    check("curl: max-time matches timeout",
          "--max-time" in argv
          and argv[argv.index("--max-time") + 1] == "12")
    check("curl: no redirect follow (-L absent)", "-L" not in argv
          and "--location" not in argv)
    check("curl: subprocess timeout is bounded",
          kw.get("timeout") == 17)


def test_keyed_url_not_in_argv():
    """A Helius URL (key in the query string) must never appear in
    argv; the key lives only in the -K config file, which is deleted
    after the call."""
    key = "FAKEKEY" + "0" * 28
    keyed_url = "https://mainnet.helius-rpc.com/?api-key=" + key
    proc = _FakeProc(0, _out({"result": {"slot": 1}}))
    fake = _FakeRun(proc)
    with _Ctx(fake):
        result, kind, _e = reserves._rpc_attempt(
            keyed_url, BODY, "getSlot", "mainnet.helius-rpc.com", 12)
    check("curl: keyed url returns ok",
          kind == "ok" and result == {"slot": 1})
    argv, _kw = fake.calls[0]
    joined = " ".join(argv)
    check("curl: fake key never appears in argv", key not in joined)
    check("curl: keyed url never appears in argv",
          keyed_url not in joined and "api-key" not in joined)
    check("curl: keyed url is in the -K config file",
          fake.config_contents and fake.config_contents[0] is not None
          and keyed_url.encode() in fake.config_contents[0])
    check("curl: -K config file is deleted after the call",
          not os.path.exists(argv[argv.index("-K") + 1]))


def main():
    test_ok()
    test_http_429()
    test_curl_exit_nonzero()
    test_http_500()
    test_http_308_no_redirect()
    test_jsonrpc_32005()
    test_jsonrpc_32029()
    test_jsonrpc_other_error()
    test_empty_result_dict()
    test_bad_json()
    test_unparseable_output()
    test_timeout_never_raises()
    test_curl_argv()
    test_keyed_url_not_in_argv()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL RESERVES-CURL TESTS PASSED")


if __name__ == "__main__":
    main()


if __name__ != "__main__":
    from tests.recorded_checks import recorded_check_guard
