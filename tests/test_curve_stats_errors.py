#!/usr/bin/env python3
"""Curve-stat failure reporting (2026-10-31).

The 2026-10-30 unknown_curve burst journaled curve_stats_error "None"
for 58/58 failures: reserves.rpc swallowed every failure class into a
bare None, and _rpc_err_text rendered None as the string "None". The
fail-closed decision is untouched by these tests; only the diagnostic
path is exercised: failures must now carry their class, and a bare
None must never again masquerade as an error string. No network.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import helius_ratelimit as rl
from daemon.launch_scanner import reserves as R

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


# 1. bare None renders distinctly, never as the string "None"
check("err: None -> rpc_returned_none",
      GS._rpc_err_text(None) == "rpc_returned_none")
check("err: dict error keeps code",
      "-32602" in GS._rpc_err_text({"error": {"code": -32602,
                                               "message": "Invalid params"}}))
check("err: plain string unchanged",
      GS._rpc_err_text("foo") == "foo")

# 2. reserves.rpc logs failures (host + type) and never the api key,
#    while keeping its None-on-failure contract
records = []


class _Cap(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


_h = _Cap()
R.logger.addHandler(_h)
_old_level = R.logger.level
R.logger.setLevel(logging.DEBUG)
try:
    with patch.object(R, "rpc_url_pairs",
                      return_value=[("https://example.invalid/"
                                     "?api-key=SECRET123", None)]):
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("boom")):
            res = R.rpc("getSignaturesForAddress", ["addr", {}])
finally:
    R.logger.removeHandler(_h)
    R.logger.setLevel(_old_level)
check("rpc: failure still returns None", res is None)
check("rpc: failure logged with host + type",
      any("example.invalid" in m and "URLError" in m for m in records))
check("rpc: no api key in any log record",
      all("SECRET123" not in m for m in records))


class _FakeReserves:
    def __init__(self, behavior):
        self._behavior = behavior

    def rpc(self, method, params):
        if self._behavior == "raise":
            raise RuntimeError("boom")
        return None


# 3. _curve_stats exception path propagates the exception type
_orig = GS._reserves
try:
    GS._reserves = _FakeReserves("raise")
    out = GS._curve_stats("SomeCurveAddr111111111111111111111111111")
finally:
    GS._reserves = _orig
check("curve: exception path keeps reason",
      out.get("reason") == "exception")
check("curve: exception path carries type",
      out.get("rpc_error") == "RuntimeError")
check("curve: exception path trades unknown",
      out.get("trades") is None)

# 4. _curve_stats None reply journals the distinct marker
try:
    GS._reserves = _FakeReserves("none")
    out2 = GS._curve_stats("SomeCurveAddr111111111111111111111111111")
finally:
    GS._reserves = _orig
check("curve: None reply -> non_list_result",
      out2.get("reason") == "non_list_result")
check("curve: None reply -> rpc_returned_none marker",
      out2.get("rpc_error") == "rpc_returned_none")

# 5. Guarded transport (2026-11-17, pacing fix).
# rpc() now routes through rpc_url_pairs() + the cross-process
# pacer (helius_ratelimit.SharedPacer): at most ONE Helius
# attempt per logical call, one token per 20s per key, no token
# -> no HTTP attempt. A 429 engages progressive pacer backoff
# (30s, doubling to 300s) with a single probe at expiry, and is
# mirrored into the shared guard file. Exit pricing (exempt)
# takes a PRIORITY token but NEVER bypasses the pacer — during
# an active cooldown it waits like everyone else (the old
# exempt-bypasses-the-guard leak is closed by design).
# These tests patch rpc_url_pairs (returning (url, bucket) tuples)
# and _shared_guard (a real SharedGuard on a temp home — never
# the live guard file). Guard + pacer share one temp home per
# file so the mirror lands where the assertions read it. Each
# test uses its own bucket tag so cooldowns from one test cannot
# leak into the next.
_TMP = tempfile.TemporaryDirectory()
os.environ["FOREX_AGENT_HOME"] = _TMP.name
rl._PACER = None
rl._SHARED_GUARD = None


def _guard():
    return rl.SharedGuard(home=_TMP.name)


def _pacer_backoff(bucket):
    st = rl.pacer()._read_state().get(bucket) or {}
    return st.get("backoff_s")


def _expire_pacer_cooldown(bucket):
    path = os.path.join(_TMP.name, "helius_pacer.json")
    with open(path) as fh:
        data = json.load(fh)
    data["buckets"][bucket]["cooldown_until"] = time.time() - 1.0
    with open(path, "w") as fh:
        json.dump(data, fh)


def _pairs(tag):
    return [("https://mainnet.helius-rpc.com/?api-key=K1", tag + "k1"),
            ("https://mainnet.helius-rpc.com/?api-key=K2", tag + "k2"),
            ("https://api.mainnet-beta.solana.com", None)]


def _http429(url):
    return urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)


# Helius 429 on the first key -> stop at once: no same-URL retry,
# no failover to the other key, no public endpoint. The pacer
# backoff for that key engages (mirrored into the guard file);
# the other key stays clear.
_calls = []
_g1 = _guard()
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t1")), \
        patch.object(R, "_shared_guard", return_value=_g1):
    def _once429(req, timeout=None):
        _calls.append(req.full_url)
        raise _http429(req.full_url)
    with patch("urllib.request.urlopen", side_effect=_once429):
        res429 = R.rpc("getSignaturesForAddress", ["addr", {}])
check("rpc: Helius 429 stops at once, no retry/failover/public",
      res429 is None and len(_calls) == 1
      and "api-key=K1" in _calls[0])
check("rpc: 429 engages the pacer for that key only",
      _g1.cooldown_remaining("t1k1") > 20.0
      and _g1.cooldown_remaining("t1k2") == 0.0
      and _pacer_backoff("t1k1") == 30.0)

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _ok_resp(result):
    return _Resp(json.dumps({"jsonrpc": "2.0", "id": 1,
                             "result": result}).encode())


# Pacer hot for every key at call time -> zero HTTP attempts.
_calls2 = []
_g2 = _guard()
rl.pacer_report("t2k1", False)
rl.pacer_report("t2k2", False)
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t2")), \
        patch.object(R, "_shared_guard", return_value=_g2):
    def _never(req, timeout=None):
        _calls2.append(req.full_url)
        return _ok_resp({"ok": 1})
    with patch("urllib.request.urlopen", side_effect=_never):
        res429b = R.rpc("getSignaturesForAddress", ["addr", {}])
check("rpc: pacer hot for all keys -> zero HTTP attempts",
      res429b is None and len(_calls2) == 0)

# Exempt (exit pricing) respects the pacer too: during an active
# cooldown it does NOT attempt (the old bypass leak is closed),
# but once the cooldown lapses the priority probe lets it through
# and a success clears that key's cooldown (the other key stays
# hot).
_calls3 = []
_g3 = _guard()
rl.pacer_report("t3k1", False)
rl.pacer_report("t3k2", False)
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t3")), \
        patch.object(R, "_shared_guard", return_value=_g3):
    def _exempt_ok(req, timeout=None):
        _calls3.append(req.full_url)
        return _ok_resp({"price": 7})
    with patch("urllib.request.urlopen", side_effect=_exempt_ok):
        res_hot = R.rpc("getAccountInfo", ["mint", {}], exempt=True)
        n_hot = len(_calls3)  # calls made while the cooldown was active
    _expire_pacer_cooldown("t3k1")
    with patch("urllib.request.urlopen", side_effect=_exempt_ok):
        res429c = R.rpc("getAccountInfo", ["mint", {}], exempt=True)
check("rpc: exempt call waits out the active cooldown",
      res_hot is None and n_hot == 0)
check("rpc: exempt call served once the cooldown lapses",
      res429c == {"price": 7} and len(_calls3) == 1)
check("rpc: exempt success clears that key's cooldown",
      _g3.cooldown_remaining("t3k1") == 0.0
      and _g3.cooldown_remaining("t3k2") > 20.0
      and _pacer_backoff("t3k1") == 30.0)

# Non-429 failure -> fail over to the next key, then public (a real
# outage, not a quota problem); the guard stays clear for both keys.
_calls4 = []
_g4 = _guard()
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t4")), \
        patch.object(R, "_shared_guard", return_value=_g4):
    def _boom(req, timeout=None):
        _calls4.append(req.full_url)
        if "mainnet-beta" in req.full_url:
            return _ok_resp({"ok": 1})
        raise urllib.error.URLError("boom")
    with patch("urllib.request.urlopen", side_effect=_boom):
        res429d = R.rpc("getSignaturesForAddress", ["addr", {}])
check("rpc: non-429 failure fails over to next key then public",
      res429d == {"ok": 1} and len(_calls4) == 3
      and "api-key=K1" in _calls4[0]
      and "api-key=K2" in _calls4[1]
      and "mainnet-beta" in _calls4[2])
check("rpc: non-429 failures do not engage the guard",
      _g4.cooldown_remaining("t4k1") == 0.0
      and _g4.cooldown_remaining("t4k2") == 0.0)

# Public leg keeps its one fast-429 retry (Helius legs do not).
_calls5 = []
_g5 = _guard()
with patch.object(R, "_RETRY_BACKOFF_S", 0), \
        patch.object(R, "_RETRY_JITTER_S", 0), \
        patch.object(R, "rpc_url_pairs", return_value=_pairs("t5")), \
        patch.object(R, "_shared_guard", return_value=_g5):
    def _pub_flaky(req, timeout=None):
        _calls5.append(req.full_url)
        if "mainnet-beta" not in req.full_url:
            raise urllib.error.URLError("boom")
        if len([u for u in _calls5 if "mainnet-beta" in u]) == 1:
            raise _http429(req.full_url)
        return _ok_resp({"ok": 2})
    with patch("urllib.request.urlopen", side_effect=_pub_flaky):
        res429e = R.rpc("getSignaturesForAddress", ["addr", {}])
check("rpc: public fast 429 retried once, then succeeds",
      res429e == {"ok": 2} and len(_calls5) == 4
      and [u for u in _calls5 if "mainnet-beta" in u] ==
      ["https://api.mainnet-beta.solana.com"] * 2)

# key rotation: consecutive rpc_urls() calls alternate the first key
with patch.object(R, "_helius_keys", return_value=["KEY_A", "KEY_B"]):
    u1 = R.rpc_urls()
    u2 = R.rpc_urls()
    u3 = R.rpc_urls()
check("rpc: two Helius keys rotate as first URL",
      u1[0] != u2[0] and u1[0] == u3[0] and len(u1) == 3
      and {u1[0], u2[0]} ==
      {"https://mainnet.helius-rpc.com/?api-key=KEY_A",
       "https://mainnet.helius-rpc.com/?api-key=KEY_B"})

print()
if FAILURES:
    print("FAILURES:", FAILURES)
    sys.exit(1)
print("ALL CURVE STATS ERROR TESTS PASSED")
