#!/usr/bin/env python3
"""Tests for the vault-surrogate Helius auth bridge (2026-11-18).

The bridge obtains an hsurr:* surrogate for custom.helius from
authd and builds the Helius RPC URL with it as the api-key query
parameter. The live probe on 2026-11-18 showed the swap does NOT
produce a valid Helius key from this VM (HTTP 401 while the GitHub
bearer control returned 200), so the bridge is NOT wired into the
live path — these tests cover the module in isolation with a fake
helper, and never touch the real authd socket or any raw key.

Run: python3 tests/test_helius_vault.py
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import helius_vault as hv

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class _FakeHelper:
    """Stand-in for dynamic_credentials (no authd, no network)."""

    def __init__(self, surrogate="hsurr:test-surrogate-123",
                 placement=None, fail=None):
        self._surrogate = surrogate
        self._placement = placement if placement is not None else {
            "query_param": "api-key"}
        self._fail = fail

    def url_with_surrogate_query_param(self, url, credential_name,
                                       allowed_hosts=()):
        if self._fail is not None:
            raise self._fail
        host = urllib.parse.urlparse(url).hostname or ""
        if host not in allowed_hosts:
            raise RuntimeError("refusing request to %s" % host)
        if not (isinstance(self._placement, dict)
                and isinstance(self._placement.get("query_param"),
                               str)):
            raise RuntimeError("credential is not query_param")
        param = self._placement["query_param"]
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query,
                                       keep_blank_values=True)
        query.append((param, self._surrogate))
        return urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(query)))


def _inject(fake):
    hv._HELPER = fake
    hv._HELPER_TRIED = True
    hv._WARNED.clear()


def test_surrogate_url():
    _inject(_FakeHelper())
    url = hv.vault_surrogate_url()
    check("vault: surrogate URL issued", url is not None)
    check("vault: URL host is Helius RPC",
          url is not None and urllib.parse.urlparse(url).hostname
          == "mainnet.helius-rpc.com")
    check("vault: URL carries api-key=hsurr:...",
          url is not None
          and "api-key=hsurr" in url)
    check("vault: URL carries no raw key material",
          url is not None and "DISKKEY-SENTINEL" not in url)
    check("vault: vault_available True when issuable",
          hv.vault_available())


def test_issuance_failure_falls_back():
    _inject(_FakeHelper(fail=RuntimeError("authd down")))
    check("vault: issuance failure returns None",
          hv.vault_surrogate_url() is None)
    check("vault: vault_available False on failure",
          not hv.vault_available())


def test_helper_missing_falls_back():
    hv._HELPER = None
    hv._HELPER_TRIED = True
    hv._WARNED.clear()
    check("vault: missing helper returns None",
          hv.vault_surrogate_url() is None)
    check("vault: missing helper -> vault_available False",
          not hv.vault_available())


def test_wrong_placement_falls_back():
    _inject(_FakeHelper(placement={"bearer_header": True}))
    check("vault: non-query-param placement returns None",
          hv.vault_surrogate_url() is None)


def test_disallowed_host_falls_back():
    _inject(_FakeHelper())
    check("vault: non-Helius base URL refused",
          hv.vault_surrogate_url("https://example.invalid/") is None)


def test_reset_for_tests():
    _inject(_FakeHelper())
    hv.reset_for_tests()
    check("vault: reset clears helper cache",
          hv._HELPER is None and not hv._HELPER_TRIED)


def main():
    test_surrogate_url()
    test_issuance_failure_falls_back()
    test_helper_missing_falls_back()
    test_wrong_placement_falls_back()
    test_disallowed_host_falls_back()
    test_reset_for_tests()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL VAULT TESTS PASSED")


if __name__ == "__main__":
    main()
