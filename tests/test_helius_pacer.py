#!/usr/bin/env python3
"""Tests for the cross-process Helius pacer (2026-11-17, pacing fix).

The pacer is the ONE admission path for every Helius call: a
token bucket per key (1 token / 20s, capacity 2) + progressive
backoff (30s, doubling to 300s) with a SINGLE probe call to test
recovery after a cooldown. A fresh key would NOT fix the 429
flapping (measured 2026-11-17: even 1 req/5s/key succeeds only
~17%/8% under live load); only pacing the fleet does.

No network, no live state file: every test uses a temp home dir.
Run: python3 tests/test_helius_pacer.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import helius_ratelimit as rl

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def fresh_home():
    """Temp FOREX_AGENT_HOME + fresh pacer/guard singletons."""
    home = tempfile.mkdtemp(prefix="pacer-test-")
    os.environ["FOREX_AGENT_HOME"] = home
    rl._PACER = None
    rl._SHARED_GUARD = None
    return home


def pacer_state(home):
    with open(os.path.join(home, "helius_pacer.json")) as fh:
        return json.load(fh)["buckets"]


def guard_state(home):
    with open(os.path.join(home, "helius_guard.json")) as fh:
        return json.load(fh)["buckets"]


def set_bucket(home, bucket, **over):
    """Rewrite one pacer bucket's state (simulates time passing)."""
    path = os.path.join(home, "helius_pacer.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        data = {"buckets": {}}
    b = data["buckets"].setdefault(bucket, {
        "tokens": rl._PACER_CAPACITY, "updated_ts": time.time(),
        "cooldown_until": 0.0, "backoff_s": rl._PACER_BACKOFF_BASE_S,
        "probe_claimed_until": 0.0})
    b.update(over)
    with open(path, "w") as fh:
        json.dump(data, fh)


BKT = "testbucket000001"


# 1. Fresh bucket: capacity 2, third acquire fails fast (wait 0).
home = fresh_home()
p = rl.pacer()
check("fresh: first token", p.acquire(BKT, wait_s=0))
check("fresh: second token", p.acquire(BKT, wait_s=0))
check("fresh: third token denied", not p.acquire(BKT, wait_s=0))

# 2. Refill: tokens come back at 1 per _PACER_REFILL_S.
set_bucket(home, BKT, updated_ts=time.time() - 25.0)
check("refill: one token after 25s", p.acquire(BKT, wait_s=0))
check("refill: empty again", not p.acquire(BKT, wait_s=0))

# 3. Priority may borrow one token of debt, no more.
check("priority: borrow one", p.acquire(BKT, priority=True, wait_s=0))
check("priority: no second borrow",
      not p.acquire(BKT, priority=True, wait_s=0))
check("normal: no token while in debt",
      not p.acquire(BKT, wait_s=0))

# 4. Cooldown: a 429 blocks acquires; the probe claim is single.
set_bucket(home, BKT, tokens=2.0, updated_ts=time.time(),
           cooldown_until=0.0, backoff_s=rl._PACER_BACKOFF_BASE_S,
           probe_claimed_until=0.0)
p.report(BKT, False)
st = pacer_state(home)[BKT]
check("429: cooldown ~30s", 25.0 < st["cooldown_until"] - time.time()
      < 35.0)
check("429: backoff is base", st["backoff_s"] == 30.0)
check("429: acquire blocked during cooldown",
      not p.acquire(BKT, wait_s=0))
# Cooldown lapses -> exactly one probe claim.
set_bucket(home, BKT, cooldown_until=time.time() - 1.0)
check("probe: first caller claims it", p.acquire(BKT, wait_s=0))
check("probe: second caller waits (no token)",
      not p.acquire(BKT, wait_s=0))

# 5. Probe success clears the cooldown and resets the ladder.
p.report(BKT, True)
st = pacer_state(home)[BKT]
check("probe ok: cooldown cleared", st["cooldown_until"] == 0.0)
check("probe ok: backoff reset", st["backoff_s"] == 30.0)
check("probe ok: probe claim cleared",
      st["probe_claimed_until"] == 0.0)

# 6. Probe 429 doubles the backoff (30 -> 60).
p.report(BKT, False)
set_bucket(home, BKT, cooldown_until=time.time() - 1.0)
check("probe2: claim again", p.acquire(BKT, wait_s=0))
p.report(BKT, False)
st = pacer_state(home)[BKT]
check("probe 429: backoff doubled to 60", st["backoff_s"] == 60.0)
check("probe 429: cooldown ~60s",
      55.0 < st["cooldown_until"] - time.time() < 65.0)

# 7. Mirror: the pacer outcome lands in the SharedGuard file.
g = guard_state(home)[BKT]
check("mirror: guard cooldown matches pacer",
      abs(g["until"] - st["cooldown_until"]) < 2.0)
check("mirror: guard backoff matches pacer", g["backoff"] == 60.0)
p.report(BKT, True)
g = guard_state(home)[BKT]
check("mirror: success clears guard cooldown", g["until"] == 0.0)

# 8. Cross-process: two pacer instances share one home; tokens
#    taken by one are gone for the other.
home2 = fresh_home()
pa = rl.SharedPacer(home=home2)
pb = rl.SharedPacer(home=home2)
check("xproc: A takes token 1", pa.acquire(BKT, wait_s=0))
check("xproc: A takes token 2", pa.acquire(BKT, wait_s=0))
check("xproc: B sees empty bucket", not pb.acquire(BKT, wait_s=0))
pa.report(BKT, True)
check("xproc: B still empty after A success (no refill yet)",
      not pb.acquire(BKT, wait_s=0))

# 9. reserves.rpc gates on the pacer: deny -> no HTTP at all.
import daemon.launch_scanner.reserves as res
_real_pacer_fn = res._pacer
_real_pairs = res.rpc_url_pairs
_real_attempt = res._rpc_attempt


class _DenyPacer:
    def acquire(self, *a, **k):
        return False

    def report(self, *a, **k):
        pass


class _GrantPacer:
    def __init__(self):
        self.reported = []

    def acquire(self, *a, **k):
        return True

    def report(self, bucket, ok):
        self.reported.append((bucket, ok))


calls = {"helius": [], "public": []}


def _fake_attempt(url, body, method, host, timeout):
    if "helius-rpc.com" in url:
        calls["helius"].append(url)
        return {"v": 1}, "ok", 0.0
    calls["public"].append(url)
    return None, "fail", 0.0


res._pacer = lambda: _DenyPacer()
res.rpc_url_pairs = lambda: [
    ("https://mainnet.helius-rpc.com/?api-key=k1", "b1"),
    ("https://mainnet.helius-rpc.com/?api-key=k2", "b2"),
    ("https://api.mainnet-beta.solana.com", None)]
res._rpc_attempt = _fake_attempt
check("reserves: denied pacer -> None, zero Helius HTTP",
      res.rpc("getSlot", []) is None and calls["helius"] == [])
grant = _GrantPacer()
res._pacer = lambda: grant
check("reserves: granted pacer -> result",
      res.rpc("getSlot", []) == {"v": 1})
check("reserves: success reported to pacer",
      grant.reported == [("b1", True)])


def _fake_429_attempt(url, body, method, host, timeout):
    if "helius-rpc.com" in url:
        return None, "429", 0.1
    return None, "fail", 0.1


calls["helius"] = []
calls["public"] = []
grant2 = _GrantPacer()
res._pacer = lambda: grant2
res._rpc_attempt = _fake_429_attempt
check("reserves: 429 -> None (no public fallback)",
      res.rpc("getSlot", []) is None and calls["public"] == [])
check("reserves: 429 reported to pacer",
      grant2.reported == [("b1", False)])
res._pacer = _real_pacer_fn
res.rpc_url_pairs = _real_pairs
res._rpc_attempt = _real_attempt

# 10. reserves._rpc_attempt: HTTP 200 + error -32005 counts as 429.
# (2026-11-22: the transport moved to a curl subprocess on 2026-11-20,
# so the old urllib.urlopen mock never ran — mock subprocess.run the
# way test_reserves_curl.py does. The URL travels in a -K config file,
# never on the argv, so the fake only needs to return the scripted
# stdout; _rpc_attempt still writes + deletes a real temp config.)
import json as _json


class _FakeCurlProc:
    def __init__(self, stdout):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = b""


def _fake_curl_run(code):
    payload = {"jsonrpc": "2.0", "id": 1, "result": None,
               "error": {"code": code, "message": "rate limited"}}
    out = _json.dumps(payload).encode() + b"\n__HTTP_STATUS__:200"

    def _run(argv, **kw):
        return _FakeCurlProc(out)

    return _run


class _FakeCurlModule:
    def __init__(self, run):
        self.run = staticmethod(run)


_real_subprocess = res.subprocess
for _code in (-32005, -32029):
    res.subprocess = _FakeCurlModule(_fake_curl_run(_code))
    _result, _kind, _el = res._rpc_attempt(
        "https://mainnet.helius-rpc.com/?api-key=k", b"{}",
        "getSlot", "mainnet.helius-rpc.com", 5)
    check("reserves: %d JSON-RPC error is a 429" % _code, _kind == "429")
res.subprocess = _real_subprocess

# 11. deployers + smartmoney share one pacer bucket (main key):
#     a 429 via smartmoney blocks deployers with zero HTTP.
import daemon.launch_scanner.smartmoney as sm
import daemon.launch_scanner.deployers as dp
import urllib.error as _urlerr

home3 = fresh_home()
_real_sm_key = sm.api_key
_real_dp_key = dp.api_key
sm.api_key = lambda: "k"
dp.api_key = lambda: "k"


def _raise_429(*a, **k):
    raise _urlerr.HTTPError("http://x", 429, "Too Many Requests", {}, None)


_real_sm_open = sm.urllib.request.urlopen
_real_dp_open = dp.urllib.request.urlopen
sm.urllib.request.urlopen = _raise_429
check("shared bucket: smartmoney 429 -> None",
      sm._rpc("getSignaturesForAddress", []) is None)
dp_calls = []
dp.urllib.request.urlopen = lambda *a, **k: dp_calls.append(1)
check("shared bucket: deployers blocked after smartmoney 429",
      dp._rpc("getSignaturesForAddress", []) is None
      and dp_calls == [])
sm.urllib.request.urlopen = _real_sm_open
dp.urllib.request.urlopen = _real_dp_open
sm.api_key = _real_sm_key
dp.api_key = _real_dp_key

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL PACER TESTS PASSED")
