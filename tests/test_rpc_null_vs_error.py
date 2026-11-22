#!/usr/bin/env python3
"""Tests for the null-vs-error repair (2026-11-22).

Two defects shared one root cause: a JSON-RPC {"result": null} body
is a completed call whose answer is EMPTY, but the reserves
transport classified it as a failure and every layer above
conflated it with an outage:

  * _rpc_attempt returned kind "fail" for a null result, so a
    getTransaction for a not-yet-indexed signature burned the
    second Helius key AND the public-RPC budget on failover for a
    definitive empty answer.
  * The Helius adapter reported every null payload as kind "error",
    while the Alchemy leg reports the same body as kind "ok" with a
    null result — the same provider answer counted as healthy on
    one leg and as an outage on the other.

The fix adds typed plumbing (kind "null") end to end without
changing rpc()'s result-or-None contract for the fleet, and the
adapter now reports null answers as kind "ok" with a null payload.

No network, no live state: every transport seam is faked.
Run: python3 tests/test_rpc_null_vs_error.py
"""
import json as _json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_tmp_home = tempfile.mkdtemp(prefix="nullvserror-")
os.environ["FOREX_AGENT_HOME"] = _tmp_home

import daemon.launch_scanner.reserves as res

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---- 1. _rpc_attempt classifies a null result as "null" -----------
class _FakeCurlProc:
    def __init__(self, stdout):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = b""


def _fake_curl(payload):
    out = _json.dumps(payload).encode() + b"\n__HTTP_STATUS__:200"

    def _run(argv, **kw):
        return _FakeCurlProc(out)

    return _run


class _FakeCurlModule:
    def __init__(self, run):
        self.run = staticmethod(run)


_real_subprocess = res.subprocess

res.subprocess = _FakeCurlModule(
    _fake_curl({"jsonrpc": "2.0", "id": 1, "result": None}))
_r, _k, _e = res._rpc_attempt("https://mainnet.helius-rpc.com/?api-key=k",
                              b"{}", "getTransaction",
                              "mainnet.helius-rpc.com", 5)
check("attempt: null result -> kind null", _r is None and _k == "null")

res.subprocess = _FakeCurlModule(
    _fake_curl({"jsonrpc": "2.0", "id": 1, "result": {"slot": 1}}))
_r, _k, _e = res._rpc_attempt("https://mainnet.helius-rpc.com/?api-key=k",
                              b"{}", "getTransaction",
                              "mainnet.helius-rpc.com", 5)
check("attempt: payload result -> kind ok",
      _k == "ok" and _r == {"slot": 1})

res.subprocess = _FakeCurlModule(
    _fake_curl({"jsonrpc": "2.0", "id": 1, "result": None,
                "error": {"code": -32602, "message": "bad params"}}))
_r, _k, _e = res._rpc_attempt("https://mainnet.helius-rpc.com/?api-key=k",
                              b"{}", "getTransaction",
                              "mainnet.helius-rpc.com", 5)
check("attempt: error body with result null -> fail (error wins)",
      _k == "fail")
res.subprocess = _real_subprocess

# ---- 2. _rpc_impl_typed: a null answer never fails over -----------
class _GrantPacer:
    def __init__(self):
        self.reported = []

    def acquire(self, *a, **k):
        return True

    def report(self, bucket, ok):
        self.reported.append((bucket, ok))


calls = {"helius": [], "public": []}


def _null_attempt(url, body, method, host, timeout):
    if "helius-rpc.com" in url:
        calls["helius"].append(url)
        return None, "null", 0.0
    calls["public"].append(url)
    return {"v": 9}, "ok", 0.0


_real_pacer_fn = res._pacer
_real_pairs = res.rpc_url_pairs
_real_attempt = res._rpc_attempt

grant = _GrantPacer()
res._pacer = lambda: grant
res.rpc_url_pairs = lambda: [
    ("https://mainnet.helius-rpc.com/?api-key=k1", "b1"),
    ("https://mainnet.helius-rpc.com/?api-key=k2", "b2"),
    ("https://api.mainnet-beta.solana.com", None)]
res._rpc_attempt = _null_attempt
out, kind = res._rpc_impl_typed("getTransaction", [["sig"]])
check("impl: null answer -> (None, null), one key, no failover",
      out is None and kind == "null"
      and calls["helius"] == ["https://mainnet.helius-rpc.com/?api-key=k1"]
      and calls["public"] == [])
check("impl: null answer reports SUCCESS to pacer",
      grant.reported == [("b1", True)])
check("impl: null answer stamps last_rpc_source",
      res.last_rpc_source() == "helius")

# ---- 3. rpc() / rpc_typed() contracts -----------------------------
check("rpc(): null answer still returns bare None (fleet contract)",
      res.rpc("getTransaction", [["sig"]]) is None)
check("rpc_typed(): null answer -> (None, null)",
      res.rpc_typed("getTransaction", [["sig"]]) == (None, "null"))

calls["helius"] = []


def _ok_attempt(url, body, method, host, timeout):
    calls["helius"].append(url)
    return {"v": 1}, "ok", 0.0


res._rpc_attempt = _ok_attempt
check("rpc(): ok result unchanged",
      res.rpc("getSlot", []) == {"v": 1})
check("rpc_typed(): ok -> (result, ok)",
      res.rpc_typed("getSlot", []) == ({"v": 1}, "ok"))

res._pacer = _real_pacer_fn
res.rpc_url_pairs = _real_pairs
res._rpc_attempt = _real_attempt

# ---- 4. Helius adapter: null is an answer, not an outage ----------
from daemon.intelligence.acquisition.providers import helius as hadapt

adapter = hadapt.HeliusAdapter()

_real_rpc_typed = res.rpc_typed
res.rpc_typed = lambda method, params, timeout=12, exempt=False: (
    None, "null")
r = adapter.call("getTransaction", [["sig"]])
check("adapter: null answer -> kind ok, null payload, noted",
      r["kind"] == "ok" and r["result"] is None
      and "null" in (r.get("note") or ""))
res.rpc_typed = lambda method, params, timeout=12, exempt=False: (
    {"x": 1}, "ok")
r = adapter.call("getSlot", [])
check("adapter: ok passthrough",
      r["kind"] == "ok" and r["result"] == {"x": 1})
res.rpc_typed = lambda method, params, timeout=12, exempt=False: (
    None, "error")
r = adapter.call("getSlot", [])
check("adapter: error stays error",
      r["kind"] == "error" and r["result"] is None)
res.rpc_typed = _real_rpc_typed

# ---- 5. Safety-report contract: unknown never carries pass --------
from daemon.launch_scanner import instant_safety, safety

rep = instant_safety.check("")
check("instant_safety: empty mint -> unknown, passed=False",
      rep.get("unknown") is True and rep.get("passed") is False)

_real_inner = instant_safety._check_inner


def _boom(mint, timeout):
    raise RuntimeError("rpc down")


instant_safety._check_inner = _boom
instant_safety._cache.clear()
rep = instant_safety.check("So11111111111111111111111111111111111111112")
check("instant_safety: crash -> unknown, passed=False",
      rep.get("unknown") is True and rep.get("passed") is False)
instant_safety._check_inner = _real_inner

rep = safety.check_token("solana", "")
check("safety.check_token: empty mint -> unknown, passed=False",
      rep.get("unknown") is True and rep.get("passed") is False)
rep = safety.check_token("notachain", "SomeMint111111111111111111111111111")
check("safety.check_token: unknown chain -> unknown, passed=False",
      rep.get("unknown") is True and rep.get("passed") is False)

_real_solana = safety._check_solana


def _boom_solana(mint, timeout, limits=None):
    raise RuntimeError("goplus down")


safety._check_solana = _boom_solana
rep = safety.check_token("solana", "SomeMint111111111111111111111111111")
check("safety.check_token: crash -> unknown, passed=False",
      rep.get("unknown") is True and rep.get("passed") is False)
safety._check_solana = _real_solana

# ---- 6. Consumers still fail closed on the hardened report --------
from daemon.launch_scanner import grad

_real_check_token = grad._safety.check_token
grad._safety.check_token = lambda chain, mint, timeout=10: {
    "passed": False, "score": 0, "flags": [], "fails": [],
    "unknown": True}
ok, why, rep, transient = grad._safety_ok("SomeMint111111111111111111111111111")
check("grad._safety_ok: unknown report blocks entry (transient)",
      ok is False and why == "safety unknown" and transient is True)
grad._safety.check_token = _real_check_token

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL NULL-VS-ERROR TESTS PASSED")
