#!/usr/bin/env python3
"""Tests for the Helius quiet period (2026-11-20).

While quiet is active the pacer issues NO Helius token anywhere
in the fleet, so every caller falls through to the public-RPC
fallback (reserves) or skips (background) — zero Helius attempts
of any kind, including the armed-watch WSS handshake. After
`until`, exactly one caller claims the single probe: a success
clears quiet, a 429 re-enters quiet for 12h.

Reserves checks target retained _rpc_impl compatibility, not central routine
allocation. Watcher guard is tested on an explicit fake Helius URL.

No network, no live state: temp home dir, fake _rpc_attempt.
Run: python3 tests/test_helius_quiet.py
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import helius_ratelimit as rl
from daemon.launch_scanner import reserves
from daemon.launch_scanner import armed_watch

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class _Ctx:
    """Temp FOREX_AGENT_HOME + fresh pacer/guard singletons."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="helius-quiet-")
        self._old_env = os.environ.get("FOREX_AGENT_HOME")

    def __enter__(self):
        os.environ["FOREX_AGENT_HOME"] = self.home
        rl._PACER = None
        rl._SHARED_GUARD = None
        rl.reset_shed_log_for_tests()
        return self

    def __exit__(self, *exc):
        rl._PACER = None
        rl._SHARED_GUARD = None
        rl.reset_shed_log_for_tests()
        if self._old_env is None:
            os.environ.pop("FOREX_AGENT_HOME", None)
        else:
            os.environ["FOREX_AGENT_HOME"] = self._old_env
        return False


def _quiet_path(home):
    return os.path.join(home, "helius_quiet.json")


def test_quiet_state_roundtrip():
    with _Ctx() as ctx:
        st = rl.quiet_status()
        check("quiet: empty home reports inactive",
              st.get("active") is False)
        check("quiet: empty home has no probe result",
              st.get("probe_result") is None)
        check("quiet: inactive is not active",
              rl.quiet_active(now=1000000.0) is False)
        st = rl.quiet_set(12, "test reason", now=1000000.0)
        check("quiet: set returns the state",
              isinstance(st, dict) and st.get("active") is True)
        check("quiet: set until is now+12h",
              st.get("until") == 1000000.0 + 12 * 3600)
        check("quiet: set records reason + set_ts",
              st.get("reason") == "test reason"
              and st.get("set_ts") == 1000000.0)
        check("quiet: active inside the window",
              rl.quiet_active(now=1000100.0) is True)
        check("quiet: inactive past until",
              rl.quiet_active(now=1000000.0 + 12 * 3600 + 1) is False)
        check("quiet: probe pending once expired",
              rl.quiet_probe_pending(
                  now=1000000.0 + 12 * 3600 + 1) is True)
        check("quiet: no probe pending inside the window",
              rl.quiet_probe_pending(now=1000100.0) is False)
        st = rl.quiet_clear("done", now=1000200.0)
        check("quiet: clear returns inactive",
              isinstance(st, dict) and st.get("active") is False)
        check("quiet: clear records reason",
              st.get("reason") == "done")
        check("quiet: set with 0 hours refuses",
              rl.quiet_set(0, "x") is None)
        check("quiet: set with negative hours refuses",
              rl.quiet_set(-1, "x") is None)
        # Corrupt state file: fail-open to inactive, never raises.
        with open(_quiet_path(ctx.home), "w") as fh:
            fh.write("{not json")
        check("quiet: corrupt file reports inactive",
              rl.quiet_status().get("active") is False)
        check("quiet: corrupt file fails open (not active)",
              rl.quiet_active() is False)
        check("quiet: corrupt file admits (open)",
              rl.quiet_admit() == "open")


def test_quiet_admit_states():
    with _Ctx():
        check("admit: inactive admits open",
              rl.quiet_admit(now=1000000.0) == "open")
        rl.quiet_set(12, "test", now=1000000.0)
        check("admit: inside window refuses",
              rl.quiet_admit(now=1000100.0) == "refuse")
        t_exp = 1000000.0 + 12 * 3600 + 10
        check("admit: expired grants the probe once",
              rl.quiet_admit(now=t_exp) == "probe")
        check("admit: second caller refused while claimed",
              rl.quiet_admit(now=t_exp + 1) == "refuse")
        check("admit: no probe pending while claimed",
              rl.quiet_probe_pending(now=t_exp + 1) is False)
        check("admit: claim lapses after the deadline",
              rl.quiet_admit(now=t_exp + 61) == "probe")
        check("admit: clear re-opens",
              rl.quiet_clear("done") is not None
              and rl.quiet_admit() == "open")


def _seed_bucket(home, bucket, tokens, age_s=0.0):
    """Write one pacer bucket's state directly (test-only seeding):
    `tokens` as of `age_s` seconds ago, so the next transact
    refills from there. Top-level shape is {"buckets": {...}}."""
    path = os.path.join(home, "helius_pacer.json")
    try:
        with open(path) as fh:
            state = json.load(fh)
    except Exception:
        state = {}
    buckets = state.get("buckets") or {}
    buckets[bucket] = {
        "tokens": tokens,
        "updated_ts": time.time() - age_s,
        "cooldown_until": 0.0,
        "probe_claimed_until": 0.0,
        "recent_429": [],
    }
    with open(path, "w") as fh:
        json.dump({"buckets": buckets}, fh)


def test_pacer_rechecks_quiet_while_waiting():
    """2026-11-22: quiet engaging while a caller waits for a token
    must refuse the token — a single check at entry is not enough.
    Seed a bucket at 0.0 tokens aged 18s (refills to ~0.9, so the
    first acquire iteration sleeps ~2s for the last token), start
    acquire(wait_s=5) in a thread, engage quiet 0.5s in, expect
    False. Control run with no quiet expects True after ~2s."""
    with _Ctx() as ctx:
        bucket = "bkt-midwait-1"
        _seed_bucket(ctx.home, bucket, 0.0, age_s=18.0)
        got = []
        t = threading.Thread(
            target=lambda: got.append(
                rl.pacer_acquire(bucket, wait_s=5)))
        t.start()
        time.sleep(0.5)
        rl.quiet_set(1, "mid-wait test")
        t.join(timeout=8)
        check("mid-wait: acquire returns False when quiet engages "
              "mid-wait", got == [False])
        check("mid-wait: thread finished", not t.is_alive())
        # Control: same setup, no quiet -> the token arrives.
        rl.quiet_clear("control")
        rl.reset_pacer_for_tests()
        _seed_bucket(ctx.home, bucket, 0.0, age_s=18.0)
        got2 = []
        t2 = threading.Thread(
            target=lambda: got2.append(
                rl.pacer_acquire(bucket, wait_s=5)))
        t2.start()
        t2.join(timeout=8)
        check("mid-wait: acquire returns True with no quiet",
              got2 == [True])
        check("mid-wait: control thread finished", not t2.is_alive())


def test_pacer_refuses_while_quiet():
    with _Ctx():
        bucket = "bkt-quiet-1"
        check("pacer: acquire works before quiet",
              rl.pacer_acquire(bucket, wait_s=0) is True)
        check("pacer: background allowed before quiet",
              rl.background_call_allowed(bucket) is True)
        rl.quiet_set(12, "test")
        check("pacer: acquire refused while quiet",
              rl.pacer_acquire(bucket, wait_s=0) is False)
        check("pacer: priority acquire refused while quiet",
              rl.pacer_acquire(bucket, priority=True, wait_s=0)
              is False)
        check("pacer: background refused while quiet",
              rl.background_call_allowed(bucket) is False)
        rl.quiet_clear("done")
        check("pacer: acquire works after clear",
              rl.pacer_acquire(bucket, wait_s=0) is True)
        check("pacer: background allowed after clear",
              rl.background_call_allowed(bucket) is True)


def _expired_quiet():
    """Set a quiet period whose `until` is already past (real time)."""
    now = time.time()
    rl.quiet_set(12, "test", now=now - 13 * 3600)
    return now


def test_probe_success_clears_quiet():
    with _Ctx():
        bucket = "bkt-quiet-2"
        _expired_quiet()
        check("probe-ok: expired quiet is probe-pending",
              rl.quiet_probe_pending() is True)
        check("probe-ok: first caller acquires (probe)",
              rl.pacer_acquire(bucket, wait_s=0) is True)
        check("probe-ok: second caller refused during probe",
              rl.pacer_acquire(bucket, wait_s=0) is False)
        rl.pacer_report(bucket, True)
        st = rl.quiet_status()
        check("probe-ok: success clears quiet",
              st.get("active") is False)
        check("probe-ok: success records probe_result ok",
              (st.get("probe_result") or {}).get("ok") is True)
        check("probe-ok: acquire works after the clear",
              rl.pacer_acquire(bucket, wait_s=0) is True)


def test_probe_429_reenters_quiet():
    with _Ctx():
        bucket = "bkt-quiet-3"
        _expired_quiet()
        check("probe-429: first caller acquires (probe)",
              rl.pacer_acquire(bucket, wait_s=0) is True)
        before = time.time()
        rl.pacer_report(bucket, False)
        after = time.time()
        st = rl.quiet_status()
        check("probe-429: 429 re-enters quiet",
              st.get("active") is True)
        check("probe-429: re-entry until is ~now+12h",
              before + 12 * 3600 - 5 <= st.get("until", 0)
              <= after + 12 * 3600 + 5)
        check("probe-429: 429 records probe_result not-ok",
              (st.get("probe_result") or {}).get("ok") is False)
        check("probe-429: acquire refused after re-entry",
              rl.pacer_acquire(bucket, wait_s=0) is False)
        check("probe-429: background refused after re-entry",
              rl.background_call_allowed(bucket) is False)


def test_probe_report_by_non_holder_ignored():
    with _Ctx():
        _expired_quiet()
        # Forge a probe claim held by a foreign pid.
        path = _quiet_path(os.environ["FOREX_AGENT_HOME"])
        with open(path) as fh:
            st = json.load(fh)
        st["probe_claimed_until"] = time.time() + 60
        st["probe_claimed_by"] = 999999
        with open(path, "w") as fh:
            json.dump(st, fh)
        check("probe-guard: foreign claim blocks admission",
              rl.quiet_admit() == "refuse")
        check("probe-guard: report by non-holder is ignored",
              rl.quiet_report_probe(True) is None)
        with open(path) as fh:
            st2 = json.load(fh)
        check("probe-guard: foreign claim untouched by report",
              st2.get("probe_claimed_by") == 999999
              and st2.get("active") is True)
        check("probe-guard: failed report by non-holder ignored",
              rl.quiet_report_probe(False) is None)


class _FakeAttempt:
    """Scripted _rpc_attempt; records which side was called."""

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


class _RCtx:
    """Temp home + patched reserves (real pacer, fake _rpc_attempt)."""

    def __init__(self, home):
        self.home = home
        self._real = {}

    def __enter__(self):
        self._real["HOME"] = reserves.HOME
        self._real["BUDGET"] = reserves._PUBLIC_BUDGET
        self._real["pacer"] = reserves._pacer
        self._real["attempt"] = reserves._rpc_attempt
        self._real["keys"] = reserves._helius_keys
        self._real["alchemy_key"] = reserves._alchemy_key
        self._real["vault_url"] = reserves._vault_url
        reserves._alchemy_key = lambda: ""
        reserves._vault_url = lambda: None
        reserves.HOME = self.home
        reserves._PUBLIC_BUDGET = None
        reserves._pacer = lambda: rl.pacer()
        reserves._helius_keys = lambda: ["KEY-A", "KEY-B"]
        return self

    def __exit__(self, *exc):
        reserves._alchemy_key = self._real["alchemy_key"]
        reserves._vault_url = self._real["vault_url"]
        reserves.HOME = self._real["HOME"]
        reserves._PUBLIC_BUDGET = self._real["BUDGET"]
        reserves._pacer = self._real["pacer"]
        reserves._rpc_attempt = self._real["attempt"]
        reserves._helius_keys = self._real["keys"]
        return False

    def patch_attempt(self, attempt):
        reserves._rpc_attempt = attempt


def test_reserves_public_only_while_quiet():
    with _Ctx() as ctx:
        with _RCtx(ctx.home) as rc:
            att = _FakeAttempt({"public": [({"ok": 1}, "ok")]})
            rc.patch_attempt(att)
            rl.quiet_set(12, "test")
            got = reserves._rpc_impl("getSignaturesForAddress", ["X"])
            check("reserves-quiet: quiet routes to public",
                  got == {"ok": 1})
            check("reserves-quiet: zero Helius attempts while quiet",
                  att.calls == ["public"])
            check("reserves-quiet: source stamps public-rpc",
                  reserves.last_rpc_source() == "public-rpc")
            # Same call with quiet cleared goes to Helius again —
            # the gate (not the fallback) is the only difference.
            rl.quiet_clear("done")
            att2 = _FakeAttempt({"helius": [({"ok": 2}, "ok")]})
            rc.patch_attempt(att2)
            got2 = reserves._rpc_impl("getSignaturesForAddress", ["X"])
            check("reserves-quiet: clear restores Helius",
                  got2 == {"ok": 2})
            check("reserves-quiet: clear makes a Helius attempt",
                  att2.calls == ["helius"])
            check("reserves-quiet: clear stamps source helius",
                  reserves.last_rpc_source() == "helius")


def test_armed_watch_paused_while_quiet():
    with _Ctx():
        check("watch-quiet: helper sees quiet when set",
              rl.quiet_set(12, "test") is not None
              and armed_watch._helius_quiet_active() is True)
        calls = []
        watcher = armed_watch.ArmedWatcher(
            on_price=lambda *a: None,
            ws_url="wss://mainnet.helius-rpc.com/?api-key=FAKE-QUIET-TEST")
        watcher._serve = lambda: calls.append(1)
        watcher.start()
        try:
            time.sleep(0.5)
            check("watch-quiet: no handshake while quiet",
                  calls == [])
            check("watch-quiet: state stays STALE while quiet",
                  watcher.state == "STALE")
            rl.quiet_clear("done")
            check("watch-quiet: helper sees clear",
                  armed_watch._helius_quiet_active() is False)
            deadline = time.time() + 8.0
            while not calls and time.time() < deadline:
                time.sleep(0.2)
            check("watch-quiet: handshake resumes after clear",
                  len(calls) >= 1)
        finally:
            watcher.stop()
            if watcher._thread is not None:
                watcher._thread.join(timeout=3)


def _load_cli():
    from importlib.machinery import SourceFileLoader
    path = os.path.join(os.path.dirname(__file__), "..",
                        "scripts", "helius-quiet")
    loader = SourceFileLoader("helius_quiet_cli", path)
    spec = importlib.util.spec_from_loader("helius_quiet_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_roundtrip():
    with _Ctx() as ctx:
        mod = _load_cli()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(["set", "--hours", "12",
                           "--reason", "cli test"])
        check("cli: set exits 0", rc == 0)
        st = rl.quiet_status()
        check("cli: set writes an active quiet",
              st.get("active") is True
              and st.get("reason") == "cli test")
        check("cli: set until is ~now+12h",
              time.time() + 12 * 3600 - 5 <= st.get("until", 0)
              <= time.time() + 12 * 3600 + 5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(["status"])
        check("cli: status exits 0", rc == 0)
        check("cli: status prints ACTIVE",
              "ACTIVE" in buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(["clear", "--reason", "cli done"])
        check("cli: clear exits 0", rc == 0)
        check("cli: clear writes inactive",
              rl.quiet_status().get("active") is False)
        check("cli: set with 0 hours exits nonzero",
              mod.main(["set", "--hours", "0"]) != 0)
        check("cli: status file exists in the home",
              os.path.exists(_quiet_path(ctx.home)))


def main():
    test_quiet_state_roundtrip()
    test_quiet_admit_states()
    test_pacer_refuses_while_quiet()
    test_pacer_rechecks_quiet_while_waiting()
    test_probe_success_clears_quiet()
    test_probe_429_reenters_quiet()
    test_probe_report_by_non_holder_ignored()
    test_reserves_public_only_while_quiet()
    test_armed_watch_paused_while_quiet()
    test_cli_roundtrip()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL HELIUS-QUIET TESTS PASSED")


if __name__ == "__main__":
    main()


if __name__ != "__main__":
    from tests.recorded_checks import recorded_check_guard
