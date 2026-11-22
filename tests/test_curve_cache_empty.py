#!/usr/bin/env python3
"""Phase 2 regression tests — curve-cache correctness (tx-cache audit).

Rules pinned here:
1. An EMPTY curve response (count 0) is never cached: a real
   bonding curve always carries at least its create transaction,
   so an empty page is a null/incomplete read, not confirmed data.
   Caching it would block every later lane from retrieving the
   real count for the whole TTL.
2. A legacy count-0 entry already on disk must not serve either.
3. Cache keys are the curve address only — adequate because every
   fetch of this data uses the same parameters (one bounded
   getSignaturesForAddress, limit CURVE_SIG_LIMIT = 251, transport
   default commitment). The fetch parameters are pinned here so a
   future caller that varies them breaks this test first, before
   it can silently share a key.

No network, no live state: temp home dir + fake _reserves.
Run: python3 tests/test_curve_cache_empty.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import curve_cache as cc
from daemon.launch_scanner import grad_select as gs

FAILS = []
TOTAL = [0]


def check(name, cond):
    TOTAL[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def _sigs(n):
    return [{"signature": "sig%03d" % i,
             "blockTime": 1700000000 + (n - 1 - i),
             "slot": 1000 + (n - 1 - i)} for i in range(n)]


class _FakeReserves:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def rpc(self, method, params):
        self.calls.append((method, params))
        return self._script.pop(0) if self._script else None

    def last_rpc_source(self):
        return "helius"


class _Ctx:
    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="curve-cache-empty-")
        self._old_env = os.environ.get("FOREX_AGENT_HOME")
        os.environ["FOREX_AGENT_HOME"] = self.home

    def __enter__(self):
        cc.reset_for_tests()
        return self

    def __exit__(self, *exc):
        cc.reset_for_tests()
        if self._old_env is None:
            os.environ.pop("FOREX_AGENT_HOME", None)
        else:
            os.environ["FOREX_AGENT_HOME"] = self._old_env
        return False


def test_empty_response_never_cached():
    with _Ctx():
        c = cc.CurveCache(home=os.environ["FOREX_AGENT_HOME"])
        check("cache: put(count=0) refused",
              c.put("CURVE1", 0, None) is False)
        check("cache: refused empty put leaves a miss",
              c.get("CURVE1") is None)
        check("cache: put(count=1) still stored",
              c.put("CURVE2", 1,
                    {"signature": "sig000", "blockTime": 1700000000,
                     "slot": 1000}) is True)
        check("cache: non-empty put still readable",
              (c.get("CURVE2") or {}).get("count") == 1)


def test_legacy_zero_entry_does_not_serve():
    with _Ctx() as ctx:
        path = os.path.join(ctx.home, "curve_cache.json")
        with open(path, "w") as fh:
            json.dump({"entries": {
                "CURVE1": {"count": 0, "oldest": None,
                           "fetched_ts": time.time()}}}, fh)
        c = cc.CurveCache(home=ctx.home)
        check("cache: legacy count-0 entry on disk reads as miss",
              c.get("CURVE1") is None)


def test_empty_curve_read_refetches_live():
    with _Ctx():
        fake = _FakeReserves([[], _sigs(3)])
        old_res, old_cc = gs._reserves, gs._curve_cache
        gs._reserves = fake
        gs._curve_cache = cc
        try:
            first = gs._curve_stats("CURVEADDR")
            check("stats: empty page reads trades=0, exhausted",
                  first.get("trades") == 0
                  and first.get("exhausted") is True)
            second = gs._curve_stats("CURVEADDR")
            check("stats: empty read not cached — live refetch finds "
                  "the real count",
                  second.get("trades") == 3
                  and second.get("cache") == "miss"
                  and len(fake.calls) == 2)
        finally:
            gs._reserves, gs._curve_cache = old_res, old_cc


def test_fetch_parameters_pinned():
    with _Ctx():
        fake = _FakeReserves([_sigs(5)])
        old_res, old_cc = gs._reserves, gs._curve_cache
        gs._reserves = fake
        gs._curve_cache = cc
        try:
            gs._curve_stats("CURVEADDR")
            check("stats: fetch is one getSignaturesForAddress at "
                  "limit CURVE_SIG_LIMIT (the key's only parameter)",
                  len(fake.calls) == 1
                  and fake.calls[0][0] == "getSignaturesForAddress"
                  and fake.calls[0][1]
                  == ["CURVEADDR", {"limit": gs.CURVE_SIG_LIMIT}]
                  and gs.CURVE_SIG_LIMIT == 251)
        finally:
            gs._reserves, gs._curve_cache = old_res, old_cc


def main():
    test_empty_response_never_cached()
    test_legacy_zero_entry_does_not_serve()
    test_empty_curve_read_refetches_live()
    test_fetch_parameters_pinned()
    print()
    print("%d checks, %d failed" % (TOTAL[0], len(FAILS)))
    if FAILS:
        print("FAILED: %s" % FAILS)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
