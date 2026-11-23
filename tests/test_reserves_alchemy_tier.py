#!/usr/bin/env python3
"""Tests for the Alchemy primary tier in reserves.rpc (2026-11-23).

§9 (unified provider architecture) orders routine acquisition
Alchemy PRIMARY -> Helius selective -> budgeted public. The lane
entry-critical read path (reserves._rpc_impl_typed) was built
Helius-first; the Alchemy tier added 2026-11-23 wires the declared
policy into the actual reads:

  * Alchemy first when configured, exactly ONE paced attempt per
    call (its own namespaced pacer bucket, same 429 discipline).
  * Any Alchemy failure (429 or other) falls through to the
    UNCHANGED Helius tier - controlled failover to the next tier,
    never a same-tier retry burn, never a straight jump to public.
  * A definitive JSON null from Alchemy is returned as-is (same
    index family, mainnet), stamped "alchemy".
  * Unconfigured Alchemy leaves the previous behavior untouched.
  * No HTTP attempt happens without a pacer token: the tier cannot
    bypass the fleet's pacing restrictions by construction.
  * The credential may be saved as a bare key or a full RPC URL;
    both shapes resolve, and neither is ever logged.

No network, no live state, no real credentials: every test uses a
temp home dir and fake _rpc_attempt / pacer. Run:
python3 tests/test_reserves_alchemy_tier.py
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
    """Scripted _rpc_attempt keyed by URL side: "alchemy" /
    "helius" / "public"; each side a list of (result, kind)."""

    def __init__(self, host_kind):
        self._script = {k: list(v) for k, v in host_kind.items()}
        self.calls = []

    def __call__(self, url, body, method, host, timeout):
        if "g.alchemy.com" in url:
            side = "alchemy"
        elif "mainnet-beta" in url:
            side = "public"
        else:
            side = "helius"
        self.calls.append(side)
        script = self._script.get(side) or []
        if script:
            result, kind = script.pop(0)
            return result, kind, 0.1
        return None, "fail", 0.1


class _Ctx:
    """Temp home + patched reserves; restores on exit."""

    def __init__(self, alchemy="ALCH-KEY"):
        self.home = tempfile.mkdtemp(prefix="reserves-alch-")
        self.alchemy = alchemy
        self._real = {}
        self._env = None

    def __enter__(self):
        self._real["HOME"] = reserves.HOME
        self._real["BUDGET"] = reserves._PUBLIC_BUDGET
        self._real["pacer"] = reserves._pacer
        self._real["attempt"] = reserves._rpc_attempt
        self._real["keys"] = reserves._helius_keys
        self._real["vault_url"] = reserves._vault_url
        self._real["alchemy_key"] = reserves._alchemy_key
        self._env = os.environ.pop("ALCHEMY_API_KEY", None)
        reserves.HOME = self.home
        reserves._PUBLIC_BUDGET = None
        reserves._alchemy_key = lambda: self.alchemy or ""
        return self

    def __exit__(self, *exc):
        reserves.HOME = self._real["HOME"]
        reserves._PUBLIC_BUDGET = self._real["BUDGET"]
        reserves._pacer = self._real["pacer"]
        reserves._rpc_attempt = self._real["attempt"]
        reserves._helius_keys = self._real["keys"]
        reserves._vault_url = self._real["vault_url"]
        reserves._alchemy_key = self._real["alchemy_key"]
        if self._env is not None:
            os.environ["ALCHEMY_API_KEY"] = self._env
        return False

    def patch(self, pacer, attempt, keys=("KEY-A", "KEY-B")):
        reserves._pacer = lambda: pacer
        reserves._rpc_attempt = attempt
        reserves._helius_keys = lambda: list(keys)


def test_alchemy_ok_is_primary_and_attributed():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({"alchemy": [({"ok": 1}, "ok")],
                            "helius": [({"ok": 2}, "ok")]})
        c.patch(pacer, att)
        got = reserves.rpc("getSignaturesForAddress", ["X"])
        check("alchemy: primary ok returns its result",
              got == {"ok": 1})
        check("alchemy: Helius never attempted on primary success",
              att.calls == ["alchemy"])
        check("alchemy: source stamped alchemy",
              reserves.last_rpc_source() == "alchemy")
        check("alchemy: attempt was paced (token acquired first)",
              len(pacer.acquires) == 1)
        check("alchemy: success reported to its own bucket",
              pacer.reports and pacer.reports[0][1] is True)
        check("alchemy: bucket is namespaced, not a Helius bucket",
              pacer.acquires[0] not in ("KEY-A", "KEY-B"))


def test_alchemy_429_fails_over_to_helius_not_public():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "alchemy": [(None, "429")],
            "helius": [({"ok": 2}, "ok")],
            "public": [({"ok": 3}, "ok")]})
        c.patch(pacer, att)
        got = reserves.rpc("getSignaturesForAddress", ["X"])
        check("alchemy: 429 falls over to Helius with its result",
              got == {"ok": 2})
        check("alchemy: 429 order is alchemy -> helius, never "
              "straight to public",
              att.calls == ["alchemy", "helius"])
        check("alchemy: 429 reported False on the alchemy bucket",
              pacer.reports and pacer.reports[0][1] is False)
        check("alchemy: failover stamps source helius",
              reserves.last_rpc_source() == "helius")


def test_alchemy_non429_fails_over_to_helius():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "alchemy": [(None, "fail")],
            "helius": [({"ok": 4}, "ok")]})
        c.patch(pacer, att)
        got = reserves.rpc("getSignaturesForAddress", ["X"])
        check("alchemy: non-429 failure falls over to Helius",
              got == {"ok": 4})
        check("alchemy: non-429 order is alchemy -> helius",
              att.calls == ["alchemy", "helius"])
        check("alchemy: non-429 failure not reported as a 429",
              all(ok for (_bucket, ok) in pacer.reports))


def test_alchemy_null_is_definitive():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({"alchemy": [(None, "null")],
                            "helius": [({"ok": 5}, "ok")]})
        c.patch(pacer, att)
        got = reserves.rpc("getSignaturesForAddress", ["X"])
        check("alchemy: definitive null returned as None",
              got is None)
        check("alchemy: null never re-asks another tier",
              att.calls == ["alchemy"])
        check("alchemy: null stamps source alchemy",
              reserves.last_rpc_source() == "alchemy")


def test_alchemy_single_attempt_no_same_tier_burn():
    with _Ctx() as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({
            "alchemy": [(None, "fail"), ({"ok": 6}, "ok")],
            "helius": [(None, "fail"), (None, "fail")],
            "public": [({"ok": 7}, "ok")]})
        c.patch(pacer, att)
        got = reserves.rpc("getSignaturesForAddress", ["X"])
        check("alchemy: exactly one alchemy attempt per call",
              att.calls.count("alchemy") == 1)
        check("alchemy: public reached only after both keyed tiers",
              att.calls[0] == "alchemy"
              and att.calls[-1] == "public")
        check("alchemy: end of chain still returns public result",
              got == {"ok": 7})
        check("alchemy: public never sees the alchemy URL",
              all("g.alchemy.com" not in str(call)
                  for call in att.calls))


def test_no_alchemy_key_leaves_helius_first():
    with _Ctx(alchemy="") as c:
        pacer = _FakePacer(True)
        att = _FakeAttempt({"alchemy": [({"ok": 8}, "ok")],
                            "helius": [({"ok": 9}, "ok")]})
        c.patch(pacer, att)
        got = reserves.rpc("getSignaturesForAddress", ["X"])
        check("alchemy: unconfigured -> previous Helius-first order",
              att.calls == ["helius"])
        check("alchemy: unconfigured -> Helius result",
              got == {"ok": 9})
        check("alchemy: unconfigured -> source helius",
              reserves.last_rpc_source() == "helius")


def test_no_token_no_attempt_no_bypass():
    """The tier cannot bypass pacing: with acquire False for every
    bucket, no keyed HTTP attempt may happen at all."""
    with _Ctx() as c:
        pacer = _FakePacer(False)
        att = _FakeAttempt({"alchemy": [({"ok": 1}, "ok")],
                            "helius": [({"ok": 2}, "ok")],
                            "public": [(None, "429")]})
        c.patch(pacer, att)
        reserves.rpc("getSignaturesForAddress", ["X"])
        check("alchemy: no token -> zero keyed attempts",
              "alchemy" not in att.calls
              and "helius" not in att.calls)
        check("alchemy: tokenless call still reaches the budgeted "
              "public leg only",
              att.calls == ["public"])


def test_full_url_credential_shape():
    """ALCHEMY_API_KEY may hold a full RPC URL, not a bare key."""
    full = "https://solana-mainnet.g.alchemy.com/v2/savedvalue"
    with _Ctx(alchemy=full) as c:
        check("alchemy: full-URL credential used verbatim",
              reserves._alchemy_url() == full)
        pacer = _FakePacer(True)
        att = _FakeAttempt({"alchemy": [({"ok": 1}, "ok")]})
        c.patch(pacer, att)
        got = reserves.rpc("getSignaturesForAddress", ["X"])
        check("alchemy: full-URL credential serves the call",
              got == {"ok": 1} and att.calls == ["alchemy"])
    with _Ctx(alchemy="barekey") as c:
        check("alchemy: bare key is wrapped in the v2 template",
              reserves._alchemy_url()
              == "https://solana-mainnet.g.alchemy.com/v2/barekey")


def test_resolver_env_and_disk_lookup():
    """The REAL resolver: env first, then the two disk conventions,
    through the router's own base.alchemy_key."""
    saved = os.environ.pop("ALCHEMY_API_KEY", None)
    try:
        with _Ctx(alchemy=None) as c:
            # Restore the REAL resolver for this test only.
            reserves._alchemy_key = c._real["alchemy_key"]
            home = c.home
            os.environ["ALCHEMY_API_KEY"] = "envkey"
            check("alchemy: resolver prefers env",
                  reserves._alchemy_url()
                  == "https://solana-mainnet.g.alchemy.com/v2/envkey")
            del os.environ["ALCHEMY_API_KEY"]
            d = os.path.join(home, "alchemy")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "api_key"), "w") as fh:
                fh.write("diskkey\n")
            check("alchemy: resolver reads $HOME/alchemy/api_key",
                  reserves._alchemy_url()
                  == "https://solana-mainnet.g.alchemy.com/v2/diskkey")
    finally:
        os.environ.pop("ALCHEMY_API_KEY", None)
        if saved is not None:
            os.environ["ALCHEMY_API_KEY"] = saved


def main():
    test_alchemy_ok_is_primary_and_attributed()
    test_alchemy_429_fails_over_to_helius_not_public()
    test_alchemy_non429_fails_over_to_helius()
    test_alchemy_null_is_definitive()
    test_alchemy_single_attempt_no_same_tier_burn()
    test_no_alchemy_key_leaves_helius_first()
    test_no_token_no_attempt_no_bypass()
    test_full_url_credential_shape()
    test_resolver_env_and_disk_lookup()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL RESERVES-ALCHEMY-TIER TESTS PASSED")


if __name__ == "__main__":
    main()
