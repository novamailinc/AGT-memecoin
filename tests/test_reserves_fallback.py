#!/usr/bin/env python3
"""Compatibility tests for the public-RPC fallback in reserves._rpc_impl (2026-11-18).

When Helius fails closed (429, or no pacer token on any key), the
entry-critical reads try api.mainnet-beta.solana.com ONCE under its
own cross-process budget (1 call / 2s fleet-wide), hard stop on
429. A transport failure (IncompleteRead / disconnect / timeout)
gets ONE retry on the same URL in the same budget slot — the
2026-11-18 evening probe measured first-try 6/8, with one retry
7/8 (fleet-wide first-try ~60%). A 429 is never retried. Every
fallback attempt is logged with its source, and last_rpc_source()
reports which provider answered.

Live probe 2026-11-18: getSignaturesForAddress on the pump
program, 3 calls 7s apart, 3x HTTP 200 in ~0.7s — the leg is
viable at low volume.

Modern reserves.rpc uses central routine allocation; these tests prove only
the retained compatibility transport, not current caller routing.

No network, no live state: every test uses a temp home dir and
fake _rpc_attempt / pacer. Run: python3 tests/test_reserves_fallback.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import reserves

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class _FakePacer:
    def __init__(self, acquire_results=True):
        self._acquire = acquire_results
        self.reports = []
        self.acquires = []

    def acquire(self, bucket, priority=False, wait_s=None):
        self.acquires.append(bucket)
        if isinstance(self._acquire, list):
            return bool(self._acquire.pop(0)) if self._acquire \
                else False
        return bool(self._acquire)

    def report(self, bucket, ok):
        self.reports.append((bucket, ok))


class _FakeAttempt:
    """Scripted _rpc_attempt. host_kind: {"helius": [...], "public": [...]}
    each a list of (result, kind) returned in order; calls recorded."""

    def __init__(self, host_kind):
        self._script = {k: list(v) for k, v in host_kind.items()}
        self.calls = []

    def __call__(self, url, body, method, host, timeout):
        side = "public" if "mainnet-beta" in url else "helius"
        self.calls.append(side)
        script = self._script.get(side) or []
        if script:
            result, kind = script.pop(0)
            return result, kind, 0.1
        return None, "fail", 0.1


class _Ctx:
    """Temp home + patched reserves; restores on exit."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="reserves-fb-")
        self._real = {}

    def __enter__(self):
        self._real["HOME"] = reserves.HOME
        self._real["BUDGET"] = reserves._PUBLIC_BUDGET
        self._real["pacer"] = reserves._pacer
        self._real["attempt"] = reserves._rpc_attempt
        self._real["keys"] = reserves._helius_keys
        self._real["vault_url"] = reserves._vault_url
        self._real["alchemy_key"] = reserves._alchemy_key
        reserves._alchemy_key = lambda: ""
        reserves._vault_url = lambda: None
        reserves.HOME = self.home
        reserves._PUBLIC_BUDGET = None
        return self

    def __exit__(self, *exc):
        reserves._alchemy_key = self._real["alchemy_key"]
        reserves.HOME = self._real["HOME"]
        reserves._PUBLIC_BUDGET = self._real["BUDGET"]
        reserves._pacer = self._real["pacer"]
        reserves._rpc_attempt = self._real["attempt"]
        reserves._helius_keys = self._real["keys"]
        reserves._vault_url = self._real["vault_url"]
        return False

    def patch(self, pacer, attempt, keys=("KEY-A", "KEY-B")):
        reserves._pacer = lambda: pacer
        reserves._rpc_attempt = attempt
        reserves._helius_keys = lambda: list(keys)


def test_helius_ok_never_touches_public():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({"helius": [({"ok": 1}, "ok")]})
        c.patch(pacer, att)
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: Helius ok returns result", got == {"ok": 1})
        check("fallback: Helius ok never calls public",
              att.calls == ["helius"])
        check("fallback: Helius ok stamps source helius",
              reserves.last_rpc_source() == "helius")


def test_helius_429_falls_back_to_public_once():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "helius": [(None, "429")],
            "public": [({"ok": 2}, "ok")]})
        c.patch(pacer, att)
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: Helius 429 -> public returns result",
              got == {"ok": 2})
        check("fallback: Helius 429 tried public exactly once",
              att.calls == ["helius", "public"])
        check("fallback: public success stamps source public-rpc",
              reserves.last_rpc_source() == "public-rpc")
        check("fallback: Helius 429 reported False",
              pacer.reports and pacer.reports[0][1] is False)


def test_public_429_hard_stop_no_retry():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "helius": [(None, "429")],
            "public": [(None, "429"), ({"ok": 9}, "ok")]})
        c.patch(pacer, att)
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: public 429 returns None", got is None)
        check("fallback: public 429 is NOT retried",
              att.calls == ["helius", "public"])
        check("fallback: failed call stamps source None",
              reserves.last_rpc_source() is None)


def test_public_transport_failure_retried_once():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "helius": [(None, "429")],
            "public": [(None, "fail"), ({"ok": 7}, "ok")]})
        c.patch(pacer, att)
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: public transport failure retried once",
              got == {"ok": 7})
        check("fallback: retry used exactly two public attempts",
              att.calls == ["helius", "public", "public"])
        check("fallback: retry success stamps source public-rpc",
              reserves.last_rpc_source() == "public-rpc")


def test_public_transport_failure_twice_returns_none():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "helius": [(None, "429")],
            "public": [(None, "fail"), (None, "fail")]})
        c.patch(pacer, att)
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: two public transport failures return None",
              got is None)
        check("fallback: no third public attempt after retry fails",
              att.calls == ["helius", "public", "public"])
        check("fallback: double failure stamps source None",
              reserves.last_rpc_source() is None)


def test_no_token_falls_back_to_public():
    with _Ctx() as c:
        pacer = _FakePacer(False)
        att = _FakeAttempt({"public": [({"ok": 3}, "ok")]})
        c.patch(pacer, att)
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: no Helius token -> public returns result",
              got == {"ok": 3})
        check("fallback: no token makes zero Helius attempts",
              att.calls == ["public"])
        check("fallback: no-token public stamps source public-rpc",
              reserves.last_rpc_source() == "public-rpc")


def test_public_budget_blocks_second_rapid_fallback():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "helius": [(None, "429"), (None, "429")],
            "public": [({"ok": 4}, "ok")]})
        c.patch(pacer, att)
        first = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        second = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: first fallback succeeds", first == {"ok": 4})
        check("fallback: second fallback inside 2s is skipped",
              second is None)
        check("fallback: second fallback made no public attempt",
              att.calls == ["helius", "public", "helius"])


def test_helius_non429_failure_tries_next_key():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "helius": [(None, "fail"), ({"ok": 5}, "ok")]})
        c.patch(pacer, att)
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: non-429 failure tries next key",
              got == {"ok": 5})
        check("fallback: next-key success never calls public",
              att.calls == ["helius", "helius"])
        check("fallback: next-key success stamps source helius",
              reserves.last_rpc_source() == "helius")


def test_keyless_still_tries_public():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({"public": [({"ok": 6}, "ok")]})
        c.patch(pacer, att, keys=())
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("fallback: no keys configured still tries public",
              got == {"ok": 6})
        check("fallback: keyless public stamps source public-rpc",
              reserves.last_rpc_source() == "public-rpc")


def test_public_budget_direct():
    with _Ctx() as c:
        b = reserves.PublicRpcBudget(home=c.home)
        check("budget: first acquire True", b.acquire())
        check("budget: second rapid acquire False", not b.acquire())
        # Corrupt state file: fail-soft to the in-process throttle,
        # never an unpaced call, never a raise.
        with open(os.path.join(c.home,
                               "public_rpc_budget.json"), "w") as fh:
            fh.write("{not json")
        b2 = reserves.PublicRpcBudget(home=c.home)
        check("budget: corrupt state file still acquires once",
              b2.acquire())
        check("budget: corrupt state file still throttles second",
              not b2.acquire())


def test_source_resets_on_failure():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({"helius": [({"ok": 1}, "ok")]})
        c.patch(pacer, att)
        reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("source: ok call stamps helius",
              reserves.last_rpc_source() == "helius")
        att2 = _FakeAttempt({
            "helius": [(None, "429")],
            "public": [(None, "fail")]})
        reserves._rpc_attempt = att2
        reserves._PUBLIC_BUDGET = None
        got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
        check("source: failing call returns None", got is None)
        check("source: failing call clears stale source",
              reserves.last_rpc_source() is None)


def test_rpc_url_pairs_vault_key():
    """rpc_url_pairs includes the vault URL when the surrogate is
    available, and is disk-keys-only when it is not (2026-11-20).

    Modern reserves.rpc uses central routine allocation; these tests prove only
the retained compatibility transport, not current caller routing.

No network, no live state: _vault_url is patched to a fake
    surrogate URL or None; the real helius_vault is never called."""
    with _Ctx() as c:
        c.patch(_FakePacer(True), _FakeAttempt({}),
                keys=("KEY-A", "KEY-B"))
        # Vault available: a third Helius pair, stable bucket.
        reserves._vault_url = lambda: (
            "https://mainnet.helius-rpc.com/?api-key=hsurr:AAA")
        pairs = reserves.rpc_url_pairs()
        helius = [p for p in pairs if "helius-rpc.com" in p[0]]
        check("vault: surrogate available adds a third Helius pair",
              len(helius) == 3)
        check("vault: vault pair URL is the surrogate URL",
              helius[-1][0] == "https://mainnet.helius-rpc.com/"
              "?api-key=hsurr:AAA")
        check("vault: vault pair carries the stable vault bucket",
              helius[-1][1] == reserves._VAULT_BUCKET)
        # A rotated surrogate value must map to the SAME bucket
        # (the bucket is keyed on the credential, not the surrogate,
        # or the vault key would never be paced).
        reserves._vault_url = lambda: (
            "https://mainnet.helius-rpc.com/?api-key=hsurr:BBB")
        pairs2 = reserves.rpc_url_pairs()
        helius2 = [p for p in pairs2 if "helius-rpc.com" in p[0]]
        check("vault: rotated surrogate keeps the same bucket",
              helius2[-1][1] == reserves._VAULT_BUCKET)
        # Vault unavailable: disk keys + public only, unchanged.
        reserves._vault_url = lambda: None
        pairs3 = reserves.rpc_url_pairs()
        helius3 = [p for p in pairs3 if "helius-rpc.com" in p[0]]
        check("vault: surrogate None gives disk keys only",
              len(helius3) == 2)
        check("vault: public endpoint still last",
              pairs3[-1] == ("https://api.mainnet-beta.solana.com",
                             None))


def main():
    test_rpc_url_pairs_vault_key()
    test_helius_ok_never_touches_public()
    test_helius_429_falls_back_to_public_once()
    test_public_429_hard_stop_no_retry()
    test_public_transport_failure_retried_once()
    test_public_transport_failure_twice_returns_none()
    test_no_token_falls_back_to_public()
    test_public_budget_blocks_second_rapid_fallback()
    test_helius_non429_failure_tries_next_key()
    test_keyless_still_tries_public()
    test_public_budget_direct()
    test_source_resets_on_failure()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL RESERVES-FALLBACK TESTS PASSED")


if __name__ == "__main__":
    main()


if __name__ != "__main__":
    from tests.recorded_checks import recorded_check_guard
