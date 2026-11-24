"""Telegram flood backoff (2026-10-10).

The daemon retried failed sends with no backoff — 3,306 hammering
attempts that kept extending Telegram's flood ban. Now a 429 parks ALL
mutating calls until Telegram's own retry_after elapses.

Hermetic: no network; HTTPError is faked.
"""

import io
import json
import time
import urllib.error

import pytest

from daemon.telegram.client import (
    FloodGuard,
    TelegramClient,
    TelegramError,
    TelegramFloodError,
    _parse_retry_after,
)


def _429_error(retry_after=123):
    body = json.dumps({
        "ok": False, "error_code": 429,
        "description": "Too Many Requests: retry after %d" % retry_after,
        "parameters": {"retry_after": retry_after},
    }).encode()
    return urllib.error.HTTPError(
        "https://api.telegram.org/botT/sendMessage", 429,
        "Too Many Requests", {}, io.BytesIO(body))


def test_parse_retry_after_from_parameters():
    raw = json.dumps({"parameters": {"retry_after": 9360}})
    assert _parse_retry_after(raw) == 9360.0


def test_parse_retry_after_from_description():
    assert _parse_retry_after("Too Many Requests: retry after 42") == 42.0


def test_parse_retry_after_none_when_absent():
    assert _parse_retry_after('{"ok": false}') is None


def test_guard_blocks_during_cooldown():
    g = FloodGuard()
    g.note_429(100)
    assert g.cooling() > 0
    with pytest.raises(TelegramFloodError):
        g.check()


def test_guard_clear_when_no_429():
    g = FloodGuard()
    assert g.cooling() == 0
    g.check()  # no raise


def test_429_parks_mutating_calls(monkeypatch):
    client = TelegramClient("TOKEN")
    real_urlopen = __import__("urllib.request", fromlist=["urlopen"]).urlopen

    def fake_urlopen(req, timeout=None):
        raise _429_error(120)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(TelegramFloodError):
        client.send_message(1, "hi")
    # Cooldown is set: the NEXT call raises without touching the network.
    calls = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: calls.append(req))
    with pytest.raises(TelegramFloodError):
        client.send_message(1, "hi again")
    assert calls == []  # no HTTP call made during cooldown
    assert client.flood.cooling() > 0


def test_429_body_ok_false_also_parks(monkeypatch):
    client = TelegramClient("TOKEN")

    class FakeResp:
        def read(self):
            return json.dumps({"ok": False, "error_code": 429,
                               "parameters": {"retry_after": 60}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: FakeResp())
    with pytest.raises(TelegramFloodError):
        client.send_message(1, "hi")
    assert client.flood.cooling() > 0


def test_non_429_errors_still_raise_telegram_error(monkeypatch):
    client = TelegramClient("TOKEN")
    body = b'{"ok": false, "error_code": 400, "description": "Bad Request"}'
    err = urllib.error.HTTPError("https://x", 400, "Bad Request", {},
                                 io.BytesIO(body))
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(err))
    with pytest.raises(TelegramError) as ei:
        client.send_message(1, "hi")
    assert not isinstance(ei.value, TelegramFloodError)
    assert client.flood.cooling() == 0  # no cooldown for non-429


def test_reads_not_gated_but_writes_are(monkeypatch):
    client = TelegramClient("TOKEN")
    client.flood.note_429(300)
    # getMe is a read: still attempted (may fail on network, that's fine).
    calls = []

    class FakeResp:
        def read(self):
            return b'{"ok": true, "result": {"id": 1}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: (calls.append(1), FakeResp())[1])
    assert client.get_me() == {"id": 1}
    assert calls == [1]
    # ...but a send is blocked without a network call.
    calls.clear()
    with pytest.raises(TelegramFloodError):
        client.send_message(1, "hi")
    assert calls == []


def test_blink_skips_during_cooldown():
    from daemon.telegram import blink

    class FloodedClient:
        class flood:
            @staticmethod
            def cooling(now=None):
                return 120.0

        def edit_message_text(self, *a):
            raise AssertionError("must not be called during cooldown")

    state = {"blink": {"k": {"chat_id": 1, "message_id": 2, "mint": "M",
                             "chain": "solana", "symbol": "T",
                             "ts_card": time.time(), "card_usd": 100.0,
                             "peak_usd": 100.0, "lines": ["a", "b"]}}}
    out = blink.poll(state, FloodedClient())
    assert out.get("skipped_flood") == 1
    # Card NOT dropped: still registered for after the cooldown.
    assert "k" in state["blink"]


# ---------------------------------------------------------------------------
# State-wipe guard (2026-10-10): get_daemon_state returned {} on read
# errors; the daemon then persisted the empty dict and wiped pairing.
# ---------------------------------------------------------------------------

def test_state_load_ok_true_for_plain_dict():
    from daemon.common import get_daemon_state, state_load_ok

    class Store:
        def journal_query(self, **kw):
            return [{"detail": {"paired_chat_id": 123}}]

    state = get_daemon_state(Store(), "telegram")
    assert state == {"paired_chat_id": 123}
    assert state_load_ok(state) is True


def test_state_load_ok_true_for_fresh_daemon():
    from daemon.common import get_daemon_state, state_load_ok

    class Store:
        def journal_query(self, **kw):
            return []

    state = get_daemon_state(Store(), "telegram")
    assert state == {}
    assert state_load_ok(state) is True  # fresh: safe to save


def test_state_load_ok_false_on_read_error():
    from daemon.common import get_daemon_state, state_load_ok

    class Store:
        def journal_query(self, **kw):
            raise RuntimeError("database is locked")

    state = get_daemon_state(Store(), "telegram")
    assert state == {}  # behaves empty...
    assert state_load_ok(state) is False  # ...but marked: do not persist


def test_run_once_skips_save_on_failed_read():
    import daemon.telegram as tg_mod
    from daemon.telegram import TelegramDaemon

    saved = []

    class FailStore:
        def journal_query(self, **kw):
            raise RuntimeError("database is locked")

    real_get_store = tg_mod.get_store
    real_save = tg_mod.save_daemon_state
    real_bus_configure = tg_mod.event_bus.configure
    real_bus_poll = tg_mod.event_bus.poll
    tg_mod.get_store = lambda: FailStore()
    tg_mod.save_daemon_state = lambda s, name, st: saved.append((name, st))
    tg_mod.event_bus.configure = lambda store=None: None
    tg_mod.event_bus.poll = lambda since=None, limit=100: []
    try:
        daemon = TelegramDaemon(
            client_factory=lambda t: None, tools=object())
        # no token -> early return path; must not save either
        import os
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        daemon.run_once()
    finally:
        tg_mod.get_store = real_get_store
        tg_mod.save_daemon_state = real_save
        tg_mod.event_bus.configure = real_bus_configure
        tg_mod.event_bus.poll = real_bus_poll
    assert saved == []
