"""Tests for daemon/launch_scanner/pumpportal.py.

All network is faked: a FakeWS stands in for the socket. The real
WebSocket endpoint is never touched by unit tests.
"""

import json
import time
import unittest

from daemon.launch_scanner import pumpportal
from daemon.launch_scanner.pumpportal import (
    FREE_METHODS, HANDSHAKE_BACKOFF_S, PAID_METHODS, PumpPortalFeed,
    _is_handshake_failure, parse_migration, parse_new_token,
)
from daemon.launch_scanner.logstream import WSError


def _new_token_payload(**kw):
    base = {
        "signature": "sigABC123",
        "mint": "SoLaNaMiNt1111111111111111111111111111111",
        "traderPublicKey": "Trader1111111111111111111111111111111111",
        "txType": "create",
        # initialBuy arrives in LAMPORTS (2026-11-16 units fix):
        # 420,000,000 lamports = 0.42 SOL.
        "initialBuy": 420_000_000,
        "bondingCurveKey": "Curve11111111111111111111111111111111111",
        "vTokensInBondingCurve": 1000000,
        "vSolInBondingCurve": 30,
        "marketCapSol": 12.5,
        "name": "Test Coin",
        "symbol": "TEST",
        "uri": "https://example.com/meta.json",
    }
    base.update(kw)
    return base


def _migration_payload(**kw):
    base = {
        "signature": "sigMIG456",
        "traderPublicKey": "Trader1111111111111111111111111111111111",
        "txType": "migrate",
        "mint": "MiGrAtEdMiNt111111111111111111111111111111",
        "marketCapSol": 85.0,
        "name": "Graduated Coin",
        "symbol": "GRAD",
        "uri": "https://example.com/meta.json",
        "pool": "pumpswap",
    }
    base.update(kw)
    return base


class ParseNewTokenTest(unittest.TestCase):
    def test_full_payload_normalizes(self):
        c = parse_new_token(_new_token_payload())
        self.assertEqual(c["mint"],
                         "SoLaNaMiNt1111111111111111111111111111111")
        self.assertEqual(c["symbol"], "TEST")
        self.assertEqual(c["name"], "Test Coin")
        self.assertEqual(c["chain"], "solana")
        self.assertEqual(c["source"], "pumpportal-new")
        self.assertEqual(c["extra"]["market_cap_sol"], 12.5)
        # Units (2026-11-16): initialBuy arrives in lamports and is
        # stored as SOL — 420,000,000 lamports == 0.42 SOL.
        self.assertAlmostEqual(c["extra"]["initial_buy_sol"], 0.42)
        self.assertEqual(c["extra"]["signature"], "sigABC123")
        self.assertTrue(c["url"].endswith(c["mint"]))

    def test_missing_mint_returns_none(self):
        self.assertIsNone(parse_new_token(_new_token_payload(mint="")))
        self.assertIsNone(parse_new_token(_new_token_payload(mint=None)))

    def test_wrong_txtype_returns_none(self):
        self.assertIsNone(
            parse_new_token(_new_token_payload(txType="migrate")))
        self.assertIsNone(
            parse_new_token(_new_token_payload(txType="buy")))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_new_token({}))
        self.assertIsNone(parse_new_token(None))
        self.assertIsNone(parse_new_token("not-a-dict"))
        self.assertIsNone(parse_new_token({"txType": "create",
                                           "mint": 12345}))

    def test_normalized_shape_matches_sources(self):
        # The same keys sources.py candidates carry so _consider works.
        c = parse_new_token(_new_token_payload())
        for key in ("mint", "symbol", "name", "chain", "source",
                    "created_ts", "usd_market_cap", "extra", "url"):
            self.assertIn(key, c)


class ParseMigrationTest(unittest.TestCase):
    def test_pumpswap_migration(self):
        c = parse_migration(_migration_payload())
        self.assertEqual(c["source"], "pumpportal-migration")
        self.assertEqual(c["extra"]["migration_pool"], "pumpswap")
        self.assertEqual(c["mint"],
                         "MiGrAtEdMiNt111111111111111111111111111111")

    def test_raydium_migration_with_market_id(self):
        c = parse_migration(_migration_payload(pool="raydium",
                                               marketId="MktId999"))
        self.assertEqual(c["extra"]["migration_pool"], "raydium")
        self.assertEqual(c["extra"]["market_id"], "MktId999")

    def test_wrong_txtype_returns_none(self):
        self.assertIsNone(
            parse_migration(_migration_payload(txType="create")))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_migration({}))
        self.assertIsNone(parse_migration(None))
        self.assertIsNone(parse_migration({"txType": "migrate"}))


class FakeWS:
    """In-memory stand-in for logstream.WSClient."""

    def __init__(self, messages=(), fail_on="never"):
        self.messages = list(messages)
        # "connect" | "recv" | "handshake" | "handshake-rejected" | "never"
        self.fail_on = fail_on
        self.sent = []
        self.closed = False

    def connect(self):
        if self.fail_on == "connect":
            raise WSError("connection refused (fake)")
        if self.fail_on == "handshake":
            raise WSError("connection closed during handshake")
        if self.fail_on == "handshake-rejected":
            raise WSError("websocket handshake rejected: 403")

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def recv_text(self):
        if self.fail_on == "recv":
            raise WSError("socket dropped (fake)")
        if self.messages:
            return self.messages.pop(0)
        time.sleep(0.01)
        return json.dumps({"txType": "noise"})

    def close(self):
        self.closed = True


def _feed_with(messages=(), fail_on="never", **kw):
    fakes = []

    def factory(url, timeout):
        fake = FakeWS(messages=messages, fail_on=fail_on)
        fakes.append(fake)
        return fake

    feed = PumpPortalFeed(ws_factory=factory, **kw)
    return feed, fakes


class TradeStreamGuardTest(unittest.TestCase):
    def test_never_subscribes_trade_streams(self):
        feed, fakes = _feed_with()
        fake = FakeWS()
        feed._subscribe(fake)
        methods = [m.get("method") for m in fake.sent]
        self.assertEqual(set(methods), set(FREE_METHODS))
        for m in methods:
            self.assertNotIn("Trade", m)
            self.assertNotIn(m, PAID_METHODS)

    def test_paid_methods_are_deny_listed(self):
        self.assertIn("subscribeTokenTrade", PAID_METHODS)
        self.assertIn("subscribeAccountTrade", PAID_METHODS)
        for m in PAID_METHODS:
            self.assertNotIn(m, FREE_METHODS)

    def test_subscribe_refuses_paid_method(self):
        feed, _ = _feed_with()
        fake = FakeWS()
        # Simulate a tampered FREE_METHODS (defense in depth): a paid
        # method must raise instead of being sent.
        orig = pumpportal.FREE_METHODS
        pumpportal.FREE_METHODS = ("subscribeNewToken",
                                   "subscribeTokenTrade")
        try:
            with self.assertRaises(ValueError):
                feed._subscribe(fake)
        finally:
            pumpportal.FREE_METHODS = orig

    def test_trade_payloads_ignored_even_if_received(self):
        feed, _ = _feed_with()
        fake = FakeWS()
        feed._handle_message(json.dumps(
            {"txType": "buy", "mint": "SHOULDNOTAPPEAR"}))
        feed._handle_message(json.dumps({"method": "subscribe-ack"}))
        self.assertEqual(feed.drain(), [])


class RoutingTest(unittest.TestCase):
    def test_create_and_migrate_routed(self):
        feed, _ = _feed_with()
        feed._handle_message(json.dumps(_new_token_payload()))
        feed._handle_message(json.dumps(_migration_payload()))
        out = feed.drain()
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["source"], "pumpportal-new")
        self.assertEqual(out[1]["source"], "pumpportal-migration")

    def test_duplicate_signature_deduped(self):
        feed, _ = _feed_with()
        msg = json.dumps(_new_token_payload())
        feed._handle_message(msg)
        feed._handle_message(msg)  # same signature -> dedupe
        out = feed.drain()
        self.assertEqual(len(out), 1)

    def test_drain_never_raises(self):
        feed, _ = _feed_with()
        feed._handle_message("not json at all {{{")
        feed._handle_message(json.dumps([1, 2, 3]))
        self.assertEqual(feed.drain(), [])


class FailSoftTest(unittest.TestCase):
    def test_connect_failure_never_raises(self):
        feed, _ = _feed_with(fail_on="connect")
        feed.start()
        try:
            time.sleep(0.3)
            self.assertFalse(feed.healthy)
            self.assertEqual(feed.drain(), [])  # no raise
            self.assertTrue(feed._thread.is_alive())  # still retrying
            self.assertIsNotNone(feed.last_error)
        finally:
            feed.stop()

    def test_recv_failure_recovers_with_backoff(self):
        # The base backoff clamps at 1.0s minimum (the one-connection
        # rule: never reconnect faster). Two attempts land within ~2.5s.
        feed, fakes = _feed_with(fail_on="recv", backoff_base_s=1.0,
                                 backoff_max_s=1.0)
        feed.start()
        try:
            time.sleep(2.6)
            # Several attempts happened (backoff engaged, no spam crash),
            # the feed is still alive and drain stays safe.
            self.assertGreaterEqual(len(fakes), 2)
            self.assertEqual(feed.drain(), [])
            self.assertTrue(feed._thread.is_alive())
        finally:
            feed.stop()

    def test_backoff_clamps_at_one_second_minimum(self):
        # Even a 0.05s knob can't reconnect-spam: the feed enforces a
        # 1.0s floor so PumpPortal's one-connection rule is respected.
        feed, _ = _feed_with(backoff_base_s=0.05)
        self.assertEqual(feed._base, 1.0)

    def test_stop_is_clean(self):
        feed, _ = _feed_with(fail_on="connect")
        feed.start()
        time.sleep(0.1)
        feed.stop()
        feed._thread.join(timeout=5)
        self.assertFalse(feed._thread.is_alive())


class BackoffTest(unittest.TestCase):
    def test_exponential_backoff_progression(self):
        feed, _ = _feed_with(backoff_base_s=10.0, backoff_max_s=600.0)
        waits = [feed._next_backoff() for _ in range(8)]
        # jitter is x0.8-1.2 of the nominal ladder 10,20,40,80,160,320,600,600
        nominal = [10, 20, 40, 80, 160, 320, 600, 600]
        for w, n in zip(waits, nominal):
            self.assertGreaterEqual(w, n * 0.8)
            self.assertLessEqual(w, n * 1.2)

    def test_backoff_resets_on_successful_subscribe(self):
        feed, _ = _feed_with()
        feed._next_backoff()
        feed._next_backoff()
        self.assertGreater(feed._backoff, 10.0)
        feed._reset_backoff()
        self.assertEqual(feed._backoff, 10.0)

    def test_backoff_never_below_base_or_above_max(self):
        feed, _ = _feed_with(backoff_base_s=10.0, backoff_max_s=600.0)
        for _ in range(20):
            w = feed._next_backoff()
            self.assertGreaterEqual(w, 8.0)   # 10 * 0.8 jitter floor
            self.assertLessEqual(w, 720.0)    # 600 * 1.2 jitter ceiling


class ThreadModelTest(unittest.TestCase):
    def test_start_is_idempotent(self):
        feed, _ = _feed_with(fail_on="connect")
        feed.start()
        first = feed._thread
        feed.start()
        self.assertIs(feed._thread, first)
        feed.stop()

    def test_one_connection_at_a_time(self):
        # Messages flow through a single serve loop; the feed never
        # opens a second socket while one is alive.
        msgs = [json.dumps(_new_token_payload(signature="s%d" % i,
                                              mint="MINT%03d" % i))
                for i in range(3)]
        feed, fakes = _feed_with(messages=msgs)
        feed.start()
        try:
            deadline = time.time() + 3
            out = []
            while time.time() < deadline and len(out) < 3:
                out += feed.drain()
                time.sleep(0.05)
            self.assertEqual(len(out), 3)
            self.assertEqual(len(fakes), 1)  # exactly one connection
        finally:
            feed.stop()


class EventStateTest(unittest.TestCase):
    """Zero-RPC event state (2026-11-17): the create payload's
    opening virtual reserves ride through the parser, and the
    state_from_event flag says whether the event is complete.
    The repo's bug lesson, locked in: the LISTENER must delegate
    to the parser — inlining instruction parsing in the listener
    silently drops the flag and every buy re-reads the curve."""

    def test_new_token_carries_virtual_reserves_and_flag(self):
        c = parse_new_token(_new_token_payload())
        self.assertEqual(c["extra"]["virtual_token_reserves"],
                         1000000)
        self.assertEqual(c["extra"]["virtual_sol_reserves"], 30)
        self.assertTrue(c["state_from_event"])

    def test_new_token_incomplete_event_flag_false(self):
        # No bonding curve key: still normalizes, but never
        # claims trusted event state.
        c = parse_new_token(_new_token_payload(bondingCurveKey=""))
        self.assertIsNotNone(c)
        self.assertFalse(c["state_from_event"])
        c2 = parse_new_token(
            _new_token_payload(vTokensInBondingCurve=0))
        self.assertFalse(c2["state_from_event"])

    def test_migration_flag(self):
        c = parse_migration(_migration_payload())
        self.assertTrue(c["state_from_event"])
        self.assertEqual(c["extra"]["bonding_curve"], "")
        c2 = parse_migration(_migration_payload(pool=""))
        self.assertFalse(c2["state_from_event"])

    def test_feed_delegates_to_parser(self):
        # A raw message through _handle_message must come out
        # with the parser's flag + reserves — if the listener
        # ever inlines its own parsing, this fails.
        feed, _ = _feed_with()
        feed._handle_message(json.dumps(_new_token_payload()))
        out = feed.drain()
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["state_from_event"])
        self.assertEqual(out[0]["extra"]["virtual_token_reserves"],
                         1000000)
        self.assertEqual(out[0]["extra"]["virtual_sol_reserves"], 30)
        feed._handle_message(json.dumps(_migration_payload()))
        out = feed.drain()
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["state_from_event"])


class HandshakeBackoffTest(unittest.TestCase):
    """2026-11-20: a refused handshake is a possible server ban, not
    a flapping socket. It waits one hour; a mid-stream drop keeps the
    normal exponential ladder. Retrying a ban on the ~10 min ladder
    is reconnect spam that can extend the ban."""

    def test_classifier_spots_both_handshake_forms(self):
        self.assertTrue(_is_handshake_failure(
            WSError("connection closed during handshake")))
        self.assertTrue(_is_handshake_failure(
            WSError("websocket handshake rejected: 403")))

    def test_classifier_rejects_mid_stream_drops(self):
        self.assertFalse(_is_handshake_failure(
            WSError("server closed the stream")))
        self.assertFalse(_is_handshake_failure(
            WSError("connection refused (fake)")))
        self.assertFalse(_is_handshake_failure(
            WSError("socket dropped (fake)")))

    def test_handshake_failure_waits_the_long_backoff(self):
        feed, _ = _feed_with(backoff_base_s=10.0, backoff_max_s=600.0)
        wait = feed._wait_after_failure(
            WSError("connection closed during handshake"))
        self.assertEqual(wait, HANDSHAKE_BACKOFF_S)
        self.assertEqual(wait, 3600.0)

    def test_handshake_rejection_waits_the_long_backoff(self):
        feed, _ = _feed_with(backoff_base_s=10.0, backoff_max_s=600.0)
        wait = feed._wait_after_failure(
            WSError("websocket handshake rejected: 403"))
        self.assertEqual(wait, HANDSHAKE_BACKOFF_S)

    def test_long_backoff_survives_repeat_refusals(self):
        # The long wait is a constant, never consumed: every repeat
        # refusal waits the full hour again, and the normal ladder
        # is untouched (a later mid-stream drop still starts at base).
        feed, _ = _feed_with(backoff_base_s=10.0, backoff_max_s=600.0)
        for _ in range(3):
            self.assertEqual(feed._wait_after_failure(
                WSError("connection closed during handshake")),
                HANDSHAKE_BACKOFF_S)
        self.assertEqual(feed._backoff, 10.0)
        wait = feed._next_backoff()
        self.assertGreaterEqual(wait, 8.0)   # 10 * 0.8 jitter floor
        self.assertLessEqual(wait, 12.0)     # 10 * 1.2 jitter ceiling

    def test_mid_stream_drop_keeps_exponential_backoff(self):
        feed, _ = _feed_with(backoff_base_s=10.0, backoff_max_s=600.0)
        wait = feed._wait_after_failure(WSError("server closed the stream"))
        self.assertGreaterEqual(wait, 8.0)   # 10 * 0.8 jitter floor
        self.assertLessEqual(wait, 12.0)     # 10 * 1.2 jitter ceiling
        # ... and the ladder keeps doubling on repeat drops.
        wait2 = feed._wait_after_failure(WSError("server closed the stream"))
        self.assertGreaterEqual(wait2, 16.0)
        self.assertLessEqual(wait2, 24.0)

    def test_handshake_refusal_makes_one_attempt_only(self):
        # Thread level: a refused handshake means the next attempt is
        # an hour out, so no second connect happens in the window —
        # while a mid-stream drop retries on the short ladder.
        feed, fakes = _feed_with(fail_on="handshake")
        feed.start()
        try:
            time.sleep(2.0)
            self.assertEqual(len(fakes), 1)
            self.assertTrue(feed._thread.is_alive())
        finally:
            feed.stop()
            feed._thread.join(timeout=5)
            self.assertFalse(feed._thread.is_alive())

    def test_mid_stream_drop_retries_in_the_same_window(self):
        feed, fakes = _feed_with(fail_on="recv", backoff_base_s=1.0,
                                 backoff_max_s=1.0)
        feed.start()
        try:
            time.sleep(2.6)
            self.assertGreaterEqual(len(fakes), 2)
            self.assertTrue(feed._thread.is_alive())
        finally:
            feed.stop()


if __name__ == "__main__":
    unittest.main()
