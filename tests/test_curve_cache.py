#!/usr/bin/env python3
"""Tests for the shared cross-process curve cache (2026-11-18).

The same coin is evaluated by grad_select, grad_raw, tranche, and
tranche_protect within minutes, each spending one bounded
getSignaturesForAddress call on the same bonding curve. The cache
stores RAW DATA ONLY (count + oldest signature + fetch ts, TTL
300s) so later lanes reuse the first lane's read — up to 4x fewer
curve calls. Failures are never cached; cache errors fail open
to a live call; never a verdict, only raw data.

No network, no live state: temp home dir + fake _reserves.
Run: python3 tests/test_curve_cache.py
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


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def _sigs(n):
    """Newest-first signature page of n entries (oldest last):
    index 0 is the newest (largest blockTime), index n-1 the
    oldest (smallest blockTime) — as Helius returns them."""
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
        self.home = tempfile.mkdtemp(prefix="curve-cache-")
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


def test_cache_put_get_hit():
    with _Ctx():
        c = cc.CurveCache(home=os.environ["FOREX_AGENT_HOME"])
        check("cache: miss on empty", c.get("CURVE1") is None)
        check("cache: put ok",
              c.put("CURVE1", 5, {"signature": "sig004",
                                  "blockTime": 1700000004,
                                  "slot": 1004}))
        got = c.get("CURVE1")
        check("cache: hit returns count", got is not None
              and got["count"] == 5)
        check("cache: hit returns oldest sig",
              got is not None and (got.get("oldest") or {}).get(
                  "signature") == "sig004")


def test_cache_ttl_expiry():
    with _Ctx():
        c = cc.CurveCache(home=os.environ["FOREX_AGENT_HOME"])
        c.put("CURVE1", 5, {"signature": "s"}, now=1000000.0)
        check("cache: hit inside TTL",
              c.get("CURVE1", now=1000000.0 + 299.0) is not None)
        check("cache: miss after TTL",
              c.get("CURVE1", now=1000000.0 + 301.0) is None)


def test_cache_corrupt_file_fail_open():
    with _Ctx() as ctx:
        c = cc.CurveCache(home=ctx.home)
        c.put("CURVE1", 5, {"signature": "s"})
        with open(os.path.join(ctx.home, "curve_cache.json"),
                  "w") as fh:
            fh.write("{corrupt")
        check("cache: corrupt file reads as miss",
              c.get("CURVE1") is None)
        check("cache: put still works over corrupt file",
              c.put("CURVE2", 7, None))
        check("cache: new entry readable after corrupt file",
              (c.get("CURVE2") or {}).get("count") == 7)


def test_cache_cap_prunes_oldest():
    with _Ctx() as ctx:
        c = cc.CurveCache(home=ctx.home)
        for i in range(cc.MAX_ENTRIES + 5):
            c.put("C%04d" % i, 1, None, now=1000000.0 + i)
        with open(os.path.join(ctx.home, "curve_cache.json")) as fh:
            data = json.load(fh)
        check("cache: file capped at MAX_ENTRIES",
              len(data.get("entries", {})) <= cc.MAX_ENTRIES)
        check("cache: oldest entries pruned first",
              "C0000" not in data.get("entries", {}))


def test_curve_stats_uses_cache_second_time():
    with _Ctx():
        fake = _FakeReserves([_sigs(5), _sigs(5)])
        old_res, old_cc = gs._reserves, gs._curve_cache
        gs._reserves = fake
        gs._curve_cache = cc
        try:
            first = gs._curve_stats("CURVEADDR")
            check("stats: first call is a live miss",
                  first.get("cache") == "miss")
            check("stats: first call trades=5",
                  first.get("trades") == 5)
            check("stats: first call exhausted with creation data",
                  first.get("exhausted") is True
                  and first.get("created_ts") == 1700000000.0
                  and first.get("oldest_sig") == "sig004")
            check("stats: first call made one rpc",
                  len(fake.calls) == 1)
            second = gs._curve_stats("CURVEADDR")
            check("stats: second call is a cache hit",
                  second.get("cache") == "hit")
            check("stats: second call made no rpc",
                  len(fake.calls) == 1)
            check("stats: cached stats equal live stats",
                  second.get("trades") == first.get("trades")
                  and second.get("created_ts")
                  == first.get("created_ts")
                  and second.get("oldest_sig")
                  == first.get("oldest_sig"))
            check("stats: hit stamps source cache",
                  second.get("rpc_source") == "cache")
        finally:
            gs._reserves, gs._curve_cache = old_res, old_cc


def test_curve_stats_capped_cached():
    with _Ctx():
        fake = _FakeReserves([_sigs(251)])
        old_res, old_cc = gs._reserves, gs._curve_cache
        gs._reserves = fake
        gs._curve_cache = cc
        try:
            first = gs._curve_stats("CURVEADDR")
            check("stats: capped live trades=251",
                  first.get("trades") == 251
                  and first.get("capped") is True)
            check("stats: capped live hides creation data",
                  first.get("oldest_sig") is None)
            second = gs._curve_stats("CURVEADDR")
            check("stats: capped hit stays capped, no creation data",
                  second.get("capped") is True
                  and second.get("oldest_sig") is None
                  and len(fake.calls) == 1)
        finally:
            gs._reserves, gs._curve_cache = old_res, old_cc


def test_curve_stats_failure_not_cached():
    with _Ctx():
        fake = _FakeReserves([None, _sigs(3)])
        old_res, old_cc = gs._reserves, gs._curve_cache
        gs._reserves = fake
        gs._curve_cache = cc
        try:
            first = gs._curve_stats("CURVEADDR")
            check("stats: failed read is unknown",
                  first.get("trades") is None)
            second = gs._curve_stats("CURVEADDR")
            check("stats: failure not cached, rpc called again",
                  len(fake.calls) == 2
                  and second.get("trades") == 3)
        finally:
            gs._reserves, gs._curve_cache = old_res, old_cc


def test_curve_stats_cache_expired_refetches():
    with _Ctx():
        fake = _FakeReserves([_sigs(5), _sigs(6)])
        old_res, old_cc = gs._reserves, gs._curve_cache
        gs._reserves = fake
        gs._curve_cache = cc
        try:
            gs._curve_stats("CURVEADDR")
            # Age the stored entry past the TTL by hand.
            path = os.path.join(os.environ["FOREX_AGENT_HOME"],
                                "curve_cache.json")
            with open(path) as fh:
                data = json.load(fh)
            for entry in data["entries"].values():
                entry["fetched_ts"] = time.time() - 400
            with open(path, "w") as fh:
                json.dump(data, fh)
            second = gs._curve_stats("CURVEADDR")
            check("stats: expired entry refetches live",
                  second.get("cache") == "miss"
                  and second.get("trades") == 6
                  and len(fake.calls) == 2)
        finally:
            gs._reserves, gs._curve_cache = old_res, old_cc


class _FakeCtx:
    def __init__(self):
        self.mint = "MINTADDR"
        self.birth = {"bonding_curve": "CURVEADDR"}
        self.values = {}
        self.stats = None
        self.coin_created = None


def test_check_stamps_source_and_cache_in_journal():
    with _Ctx():
        fake = _FakeReserves([_sigs(5)])
        old_res, old_cc = gs._reserves, gs._curve_cache
        gs._reserves = fake
        gs._curve_cache = cc
        try:
            chk = gs.CurveStatsCheck()
            ctx1 = _FakeCtx()
            chk.check({}, ctx1)
            check("journal: first read stamps source helius",
                  ctx1.values.get("curve_trades_source") == "helius")
            check("journal: first read stamps cache miss",
                  ctx1.values.get("curve_cache") == "miss")
            ctx2 = _FakeCtx()
            chk.check({}, ctx2)
            check("journal: second read stamps source cache",
                  ctx2.values.get("curve_trades_source") == "cache")
            check("journal: second read stamps cache hit",
                  ctx2.values.get("curve_cache") == "hit")
            check("journal: cache hit made no rpc",
                  len(fake.calls) == 1)
        finally:
            gs._reserves, gs._curve_cache = old_res, old_cc


def main():
    test_cache_put_get_hit()
    test_cache_ttl_expiry()
    test_cache_corrupt_file_fail_open()
    test_cache_cap_prunes_oldest()
    test_curve_stats_uses_cache_second_time()
    test_curve_stats_capped_cached()
    test_curve_stats_failure_not_cached()
    test_curve_stats_cache_expired_refetches()
    test_check_stamps_source_and_cache_in_journal()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL CURVE-CACHE TESTS PASSED")


if __name__ == "__main__":
    main()
