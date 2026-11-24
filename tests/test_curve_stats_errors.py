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
        # Transport is a curl subprocess (2026-11-20); inject the
        # failure at that seam, not the retired urllib path.
        with patch("subprocess.run",
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

# 5. Guarded transport (2026-11-17 pacing fix, 2026-11-18 public
# fallback, 2026-11-20 curl transport).
# rpc() routes through rpc_url_pairs() + the cross-process pacer:
# at most ONE Helius attempt per logical call, one token per 20s
# per key, no token -> no Helius attempt and the call falls back
# to ONE budgeted public attempt (the 2026-11-18 resilience build
# made the public leg a real fallback; a public 429 is a hard
# stop, a transport failure gets one retry). A Helius 429 engages
# progressive pacer backoff (30s, doubling to 300s) mirrored into
# the shared guard file, then also falls back to public once.
# Exit pricing (exempt) takes a PRIORITY token but NEVER bypasses
# the pacer — during an active cooldown it makes no Helius
# attempt (the old exempt-bypasses-the-guard leak stays closed).
# Mock seam: every HTTP attempt goes through
# _rpc_attempt(url, body, method, host, timeout) ->
# (result, kind, elapsed) with kind in {"ok", "429", "fail"};
# these tests patch that seam so no network is touched. The
# public budget is stubbed to always allow so each scenario
# exercises routing, not the 2s budget spacing. Cooldown
# bookkeeping uses the real pacer + guard on a temp home. Each
# test uses its own bucket tag so cooldowns cannot leak across.
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


class _AllowBudget:
    """Public-fallback budget stub: always admits (routing tests
    must not depend on the 2s budget spacing)."""

    def acquire(self):
        return True


_K1 = "https://mainnet.helius-rpc.com/?api-key=K1"
_K2 = "https://mainnet.helius-rpc.com/?api-key=K2"
_PUB = "https://api.mainnet-beta.solana.com"


# Helius 429 on the first key: no same-URL retry, no failover to
# the other key — Helius has failed closed and the call makes ONE
# budgeted public attempt (2026-11-18 resilience build); a public
# 429 is a hard stop. The pacer backoff for the 429ing key
# engages (mirrored into the guard file); the other key stays
# clear.
_calls = []
_g1 = _guard()
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t1")),         patch.object(R, "_public_budget", return_value=_AllowBudget()):
    def _t1(url, body, method, host, timeout):
        _calls.append(url)
        return None, "429", 0.01
    with patch.object(R, "_rpc_attempt", side_effect=_t1):
        res429 = R.rpc("getSignaturesForAddress", ["addr", {}])
check("rpc: Helius 429 -> no Helius retry/failover, one public attempt",
      res429 is None and _calls == [_K1, _PUB])
check("rpc: 429 engages the pacer for that key only",
      _g1.cooldown_remaining("t1k1") > 20.0
      and _g1.cooldown_remaining("t1k2") == 0.0
      and _pacer_backoff("t1k1") == 30.0)

# Pacer hot for every key at call time -> zero Helius attempts;
# the call falls back to one budgeted public attempt, which can
# serve it.
_calls2 = []
rl.pacer_report("t2k1", False)
rl.pacer_report("t2k2", False)
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t2")),         patch.object(R, "_public_budget", return_value=_AllowBudget()):
    def _t2(url, body, method, host, timeout):
        _calls2.append(url)
        return {"ok": 1}, "ok", 0.01
    with patch.object(R, "_rpc_attempt", side_effect=_t2):
        res429b = R.rpc("getSignaturesForAddress", ["addr", {}])
check("rpc: pacer hot for all keys -> zero Helius attempts, "
      "public fallback serves",
      res429b == {"ok": 1} and _calls2 == [_PUB])

# Exempt (exit pricing) respects the pacer too: during an active
# cooldown it makes NO Helius attempt (the old bypass leak is
# closed) and falls back to the public leg; once the cooldown
# lapses the priority probe lets it through and a success clears
# that key's cooldown (the other key stays hot).
_calls3 = []
_g3 = _guard()
rl.pacer_report("t3k1", False)
rl.pacer_report("t3k2", False)
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t3")),         patch.object(R, "_public_budget", return_value=_AllowBudget()):
    def _t3(url, body, method, host, timeout):
        _calls3.append(url)
        if "mainnet-beta" in url:
            return None, "429", 0.01
        return {"price": 7}, "ok", 0.01
    with patch.object(R, "_rpc_attempt", side_effect=_t3):
        res_hot = R.rpc("getAccountInfo", ["mint", {}], exempt=True)
        n_hot = len([u for u in _calls3 if "helius" in u])
    _expire_pacer_cooldown("t3k1")
    with patch.object(R, "_rpc_attempt", side_effect=_t3):
        res429c = R.rpc("getAccountInfo", ["mint", {}], exempt=True)
check("rpc: exempt call makes no Helius attempt during a cooldown",
      res_hot is None and n_hot == 0)
check("rpc: exempt call served once the cooldown lapses",
      res429c == {"price": 7}
      and [u for u in _calls3 if "helius" in u] == [_K1])
check("rpc: exempt success clears that key's cooldown",
      _g3.cooldown_remaining("t3k1") == 0.0
      and _g3.cooldown_remaining("t3k2") > 20.0
      and _pacer_backoff("t3k1") == 30.0)

# Non-429 failure -> fail over to the next key, then public (a real
# outage, not a quota problem); the guard stays clear for both keys.
_calls4 = []
_g4 = _guard()
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t4")),         patch.object(R, "_public_budget", return_value=_AllowBudget()):
    def _t4(url, body, method, host, timeout):
        _calls4.append(url)
        if "mainnet-beta" in url:
            return {"ok": 1}, "ok", 0.01
        return None, "fail", 0.01
    with patch.object(R, "_rpc_attempt", side_effect=_t4):
        res429d = R.rpc("getSignaturesForAddress", ["addr", {}])
check("rpc: non-429 failure fails over to next key then public",
      res429d == {"ok": 1} and _calls4 == [_K1, _K2, _PUB])
check("rpc: non-429 failures do not engage the guard",
      _g4.cooldown_remaining("t4k1") == 0.0
      and _g4.cooldown_remaining("t4k2") == 0.0)

# Public leg: a transport failure gets ONE retry on the same URL
# in the same budget slot (a 429 is never retried — covered above
# by the first scenario, where the public 429 ended the call).
_calls5 = []
with patch.object(R, "rpc_url_pairs", return_value=_pairs("t5")),         patch.object(R, "_public_budget", return_value=_AllowBudget()):
    def _t5(url, body, method, host, timeout):
        _calls5.append(url)
        if "mainnet-beta" not in url:
            return None, "fail", 0.01
        if len([u for u in _calls5 if "mainnet-beta" in u]) == 1:
            return None, "fail", 0.01
        return {"ok": 2}, "ok", 0.01
    with patch.object(R, "_rpc_attempt", side_effect=_t5):
        res429e = R.rpc("getSignaturesForAddress", ["addr", {}])
check("rpc: public transport failure retried once, then succeeds",
      res429e == {"ok": 2} and _calls5 == [_K1, _K2, _PUB, _PUB])

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
