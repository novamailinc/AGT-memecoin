#!/usr/bin/env python3
"""Tests for the 429 early warning + auto-shed (2026-11-18).

- The pacer records timestamped 429 events; more than 5 in 10
  minutes is a spike (scripts/helius-429-watch writes the alert
  state file, the self-audit surfaces it, Telegram pings once on
  the transition into active).
- Background Helius consumers (deployers, smartmoney, the nightly
  grader) auto-shed while a bucket is in backoff: zero HTTP
  attempts, logged once per episode. Entry-critical calls keep
  priority tokens and are untouched by the shed.

No network, no live state: temp home dir + fake urlopen.
Run: python3 tests/test_helius_429_watch.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import helius_ratelimit as rl
from daemon.launch_scanner import helius_alerts as alerts

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class _Ctx:
    """Temp FOREX_AGENT_HOME + fresh pacer/guard singletons."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="429-watch-")
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


def _record_429s(bucket, n, now=None):
    """Drive n 429s into the pacer for one bucket (real code path)."""
    t = time.time() if now is None else now
    for _ in range(n):
        rl.pacer_report(bucket, False)


def test_429_events_recorded_and_windowed():
    with _Ctx():
        bucket = "bkt-test-1"
        _record_429s(bucket, 3)
        check("429: 3 events counted in window",
              rl.recent_429_count(bucket, window_s=600) == 3)
        check("429: events are sorted timestamps",
              rl.recent_429_events(bucket, window_s=600)
              == sorted(rl.recent_429_events(bucket, window_s=600)))
        check("429: count is 0 outside the window",
              rl.recent_429_count(bucket, window_s=0) == 0)
        check("429: other bucket unaffected",
              rl.recent_429_count("bkt-other", window_s=600) == 0)


def test_spike_threshold():
    with _Ctx():
        bucket = "bkt-test-2"
        _record_429s(bucket, 5)
        check("spike: 5 in 10m is NOT a spike",
              not rl.pacer_429_spike())
        _record_429s(bucket, 1)
        check("spike: 6 in 10m IS a spike", rl.pacer_429_spike())
        check("spike: count is 6",
              rl.recent_429_count(window_s=600) == 6)


def test_background_call_allowed():
    with _Ctx():
        bucket = "bkt-test-3"
        check("shed: allowed when clear",
              rl.background_call_allowed(bucket))
        rl.shared_guard().report_ratelimited(bucket)
        check("shed: blocked while guard in backoff",
              not rl.background_call_allowed(bucket))
        rl.shared_guard().report_success(bucket)
        check("shed: allowed again after success clears",
              rl.background_call_allowed(bucket))


class _FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


def test_shed_logged_once_per_episode():
    with _Ctx():
        bucket = "bkt-test-4"
        log = _FakeLogger()
        rl.shared_guard().report_ratelimited(bucket)
        rl.log_backoff_episode_once(bucket, "deployers", log)
        rl.log_backoff_episode_once(bucket, "deployers", log)
        rl.log_backoff_episode_once(bucket, "deployers", log)
        check("shed: same episode logs exactly once",
              len(log.warnings) == 1)
        rl.shared_guard().report_ratelimited(bucket)
        rl.log_backoff_episode_once(bucket, "deployers", log)
        check("shed: extended episode logs again",
              len(log.warnings) == 2)
        rl.shared_guard().report_success(bucket)
        rl.log_backoff_episode_once(bucket, "deployers", log)
        check("shed: clear does not log",
              len(log.warnings) == 2)


def test_deployers_shed_makes_zero_http_attempts():
    with _Ctx():
        from daemon.launch_scanner import deployers
        bucket = rl.bucket_id_for_key("KEY-SHED-TEST")
        rl.shared_guard().report_ratelimited(bucket)
        called = []
        real_open = urllib.request.urlopen
        real_key = deployers.api_key
        try:
            deployers.api_key = lambda: "KEY-SHED-TEST"
            urllib.request.urlopen = lambda *a, **k: called.append(
                1) or (_ for _ in ()).throw(AssertionError(
                    "urlopen must not be called while shed"))
            check("shed: deployers returns None in backoff",
                  deployers._rpc("getSignaturesForAddress", []) is None)
            check("shed: deployers made zero HTTP attempts",
                  called == [])
        finally:
            urllib.request.urlopen = real_open
            deployers.api_key = real_key


def test_smartmoney_shed_makes_zero_http_attempts():
    with _Ctx():
        from daemon.launch_scanner import smartmoney
        bucket = rl.bucket_id_for_key("KEY-SHED-TEST2")
        rl.shared_guard().report_ratelimited(bucket)
        called = []
        real_open = urllib.request.urlopen
        real_key = smartmoney.api_key
        try:
            smartmoney.api_key = lambda: "KEY-SHED-TEST2"
            urllib.request.urlopen = lambda *a, **k: called.append(
                1) or (_ for _ in ()).throw(AssertionError(
                    "urlopen must not be called while shed"))
            check("shed: smartmoney returns None in backoff",
                  smartmoney._rpc("getSignaturesForAddress", []) is None)
            check("shed: smartmoney made zero HTTP attempts",
                  called == [])
        finally:
            urllib.request.urlopen = real_open
            smartmoney.api_key = real_key


def test_alert_file_active_and_quiet():
    with _Ctx() as ctx:
        bucket = "bkt-test-5"
        _record_429s(bucket, 6)
        state = alerts.check_and_record(home=ctx.home)
        check("alert: spike state is active",
              state.get("active") is True)
        check("alert: count is 6", state.get("count_10m") == 6)
        path = os.path.join(ctx.home, "helius_429_alert.json")
        with open(path) as fh:
            on_disk = json.load(fh)
        check("alert: state file matches",
              on_disk.get("active") is True
              and on_disk.get("count_10m") == 6)
        check("alert: read_alert returns the state",
              (alerts.read_alert(home=ctx.home) or {}).get(
                  "active") is True)


def _load_script(rel, name):
    from importlib.machinery import SourceFileLoader
    path = os.path.join(os.path.dirname(__file__), "..", rel)
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    """Minimal urlopen stand-in returning a canned JSON body."""

    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_notify_telegram_daemon_token_fallback():
    """notify_telegram: vault send.py first, daemon token fallback.

    The vault send.py path 404s in production (stale token or an
    unswapped path-segment surrogate); the fallback must deliver
    through the daemon's own token file, and stay silent (False)
    when both paths fail. No network: fake subprocess + urlopen.
    """
    with _Ctx() as ctx:
        import subprocess as _sp
        real_run = _sp.run
        real_open = urllib.request.urlopen
        try:
            calls = {}

            def fake_run_ok(*a, **k):
                calls["run"] = True
                return type("P", (), {"returncode": 0, "stdout": ""})()

            def fake_run_fail(*a, **k):
                calls["run"] = True
                return type("P", (), {
                    "returncode": 1,
                    "stdout": '{"ok": false, "error": "HTTP Error 404"}',
                })()

            def fake_open_ok(req, *a, **k):
                calls["url"] = req.full_url
                calls["data"] = req.data
                return _FakeResp({"ok": True, "result": {"message_id": 7}})

            def fake_open_boom(req, *a, **k):
                raise urllib.error.URLError("no route")

            # 1. send.py succeeds: True, fallback never attempted.
            _sp.run = fake_run_ok
            urllib.request.urlopen = lambda *a, **k: calls.update(
                url="SHOULD-NOT-BE-CALLED") or _FakeResp({"ok": True})
            check("notify: send.py success returns True",
                  alerts.notify_telegram("hi") is True)
            check("notify: send.py success skips fallback",
                  calls.get("url") != "SHOULD-NOT-BE-CALLED")

            # 2. send.py 404s + token file present + urlopen ok:
            #    True via fallback, token + chat_id in the request.
            os.makedirs(os.path.join(ctx.home, "telegram"))
            with open(os.path.join(ctx.home, "telegram",
                                   "bot_token"), "w") as fh:
                fh.write("FAKE-TOKEN-123\n")
            _sp.run = fake_run_fail
            urllib.request.urlopen = fake_open_ok
            calls.clear()
            check("notify: fallback delivers when send.py 404s",
                  alerts.notify_telegram("spike test") is True)
            check("notify: fallback posts to api.telegram.org",
                  (calls.get("url") or "").startswith(
                      "https://api.telegram.org/botFAKE-TOKEN-123/"))
            check("notify: fallback carries the chat id",
                  b"6883056760" in (calls.get("data") or b""))

            # 3. send.py fails + no token file: False.
            os.remove(os.path.join(ctx.home, "telegram", "bot_token"))
            calls.clear()
            check("notify: no token file returns False",
                  alerts.notify_telegram("spike test") is False)

            # 4. send.py fails + token file present + urlopen down:
            #    False (never raises).
            with open(os.path.join(ctx.home, "telegram",
                                   "bot_token"), "w") as fh:
                fh.write("FAKE-TOKEN-123\n")
            urllib.request.urlopen = fake_open_boom
            check("notify: fallback transport failure returns False",
                  alerts.notify_telegram("spike test") is False)
        finally:
            _sp.run = real_run
            urllib.request.urlopen = real_open


def test_watch_script_transitions_and_audit_reads():
    with _Ctx() as ctx:
        os.environ["FOREX_AGENT_HOME"] = ctx.home
        watch = _load_script("scripts/helius-429-watch",
                             "helius_429_watch")
        audit = _load_script("scripts/bot-self-audit", "bot_self_audit")
        pings = []
        watch.notify_telegram = lambda text: pings.append(text) or True
        # No 429s: exit 0, no ping, audit ok.
        check("watch: quiet run exits 0", watch.main() == 0)
        check("watch: quiet run sends no ping", pings == [])
        ok, problems = audit._check_helius_429_alert()
        check("audit: quiet alert file is an ok-note",
              not problems and any("quiet" in n for n in ok))
        # Spike: exit 1, exactly one ping on transition.
        _record_429s("bkt-test-6", 6)
        check("watch: spike run exits 1", watch.main() == 1)
        check("watch: spike pings exactly once", len(pings) == 1)
        ok, problems = audit._check_helius_429_alert()
        check("audit: active spike is a problem",
              len(problems) == 1 and "429 SPIKE" in problems[0])
        # Still spiking: exit 1, no second ping.
        check("watch: ongoing spike exits 1", watch.main() == 1)
        check("watch: ongoing spike does not re-ping",
              len(pings) == 1)
        # Stale file: audit flags the watch as possibly dead.
        path = os.path.join(ctx.home, "helius_429_alert.json")
        with open(path) as fh:
            state = json.load(fh)
        state["updated_ts"] = time.time() - 3600
        with open(path, "w") as fh:
            json.dump(state, fh)
        ok, problems = audit._check_helius_429_alert()
        check("audit: stale alert file is a problem",
              len(problems) == 1 and "stale" in problems[0])


def main():
    test_429_events_recorded_and_windowed()
    test_spike_threshold()
    test_background_call_allowed()
    test_shed_logged_once_per_episode()
    test_deployers_shed_makes_zero_http_attempts()
    test_smartmoney_shed_makes_zero_http_attempts()
    test_alert_file_active_and_quiet()
    test_notify_telegram_daemon_token_fallback()
    test_watch_script_transitions_and_audit_reads()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL 429-WATCH TESTS PASSED")


if __name__ == "__main__":
    main()
