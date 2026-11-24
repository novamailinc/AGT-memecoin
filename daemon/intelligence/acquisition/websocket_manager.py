"""WebSocket subscription manager (Phase 2; Phase 5 Alchemy extension).

Subscriptions: accountSubscribe, programSubscribe, logsSubscribe,
signatureSubscribe, slotSubscribe. Reconnect with exponential
backoff (2s base, doubling, 60s cap). One persistent connection per
provider; subscriptions are re-issued after a reconnect.

The audit finding this exists for: WSS handshake 429s are invisible
to the RPC spike alert. This module counts them SEPARATELY from RPC
429s (``wss_handshake_429s`` vs ``rpc_429s``) and reports them to the
health monitor through a callback, so a WSS storm can never again
look like a quiet RPC path.

Transport: ``websocket-client`` (already in the venv), imported
lazily so a missing library degrades to "unavailable" instead of
breaking the import. A ``ws_factory`` callable can be injected for
tests (fake sockets), so no test ever opens a real socket.

PHASE 5 — AlchemySubscriptions (spec section 4B): narrow Alchemy
WSS subscriptions ONLY. Allowed targets are the Pump.fun program,
the PumpSwap program, the SPL token program, and explicitly named
bonding-curve / pool / token accounts — never the whole network.
Every subscription method below enforces that narrowness and
rejects an unfiltered attempt (ValueError) instead of sending it.
Subscription lifecycle (subscribe/unsubscribe, reconnect with
backoff, handshake-429 counted SEPARATELY from RPC 429s) reuses
WebSocketManager; bookkeeping (which subscriptions are live, when
they last delivered) reuses SubscriptionManager.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from daemon import solana_ids as _solana_ids

logger = logging.getLogger("forex_agent.daemon.intelligence.acquisition.websocket_manager")

SUBSCRIPTION_METHODS = {
    "accountSubscribe", "programSubscribe", "logsSubscribe",
    "signatureSubscribe", "slotSubscribe",
}
RECONNECT_BASE_S = 2.0
RECONNECT_MAX_S = 60.0
RECV_TIMEOUT_S = 30.0


def wss_url_for(provider, home=None):
    """WebSocket endpoint for a provider, resolved through the
    standardized adapters (``rpc_client.wss_url``). Returns None
    when this layer cannot build one — Helius, whose keys live in
    the live transport (armed_watch owns its WSS URL)."""
    from . import rpc_client
    return rpc_client.wss_url(provider, home=home)


class WebSocketManager:
    """One WS connection per provider with subscription bookkeeping.

    ``ws_factory(url, on_message, on_error, on_close, on_open)`` builds the
    transport; the default uses websocket-client's WebSocketApp in a
    daemon thread. ``on_handshake_429`` / ``on_handshake_ok`` are
    optional callbacks (the health monitor wires them).

    Lifecycle (D-02 fix, Phase 2): ``connected`` is set ONLY in the
    ``on_open`` callback, and subscriptions are sent (and re-sent on
    reconnect) ONLY from ``on_open``. ``connect()`` merely constructs
    the transport; the handshake completes asynchronously when the
    run loop drives it. The old code set ``connected = True`` right
    after the factory returned and sent subscriptions on a socket
    whose handshake had never run — the sends failed silently and
    the client reported healthy with nothing subscribed.
    """

    def __init__(self, provider, url, ws_factory=None,
                 on_handshake_429=None, on_handshake_ok=None,
                 on_event=None, on_subscribed=None, now=None):
        self.provider = provider
        self.url = url  # never logged (carries the key)
        self._factory = ws_factory or self._default_factory
        self._on_handshake_429 = on_handshake_429
        self._on_handshake_ok = on_handshake_ok
        self._on_event = on_event
        self._on_subscribed = on_subscribed
        self._now = now
        self._subs = {}        # sub_id -> {method, params}
        self._wanted = {}      # key (method, params-json) -> {method, params}
        self._next_local_id = 1
        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.connected = False
        self.connect_attempts = 0
        self.wss_handshake_429s = 0
        self.reconnects = 0
        self.last_connect_ts = None
        self.last_disconnect_ts = None
        self.last_message_ts = None
        self._backoff_s = RECONNECT_BASE_S

    def _time(self):
        return self._now() if self._now else time.time()

    # -- subscription bookkeeping --------------------------------------
    def subscribe(self, method, params):
        """Register a wanted subscription (dedupe by method+params).
        Returns a local subscription key. The live subscribe request
        is sent when connected; after a reconnect all wanted subs are
        re-issued automatically."""
        if method not in SUBSCRIPTION_METHODS:
            raise ValueError("not a subscription method: %r" % method)
        key = (method, json.dumps(params, sort_keys=True, default=str))
        with self._lock:
            if key not in self._wanted:
                self._wanted[key] = {"method": method, "params": params,
                                     "local_id": self._next_local_id}
                self._next_local_id += 1
            entry = self._wanted[key]
        if self.connected:
            self._send_subscribe(entry)
        return entry["local_id"]

    def unsubscribe(self, method, params):
        key = (method, json.dumps(params, sort_keys=True, default=str))
        with self._lock:
            entry = self._wanted.pop(key, None)
        if entry is not None and self.connected:
            self._send_unsubscribe(entry)

    def wanted_subscriptions(self):
        with self._lock:
            return [dict(v) for v in self._wanted.values()]

    # -- transport ------------------------------------------------------
    def _default_factory(self, url, on_message, on_error, on_close,
                         on_open):
        import websocket  # lazy: websocket-client, already in the venv
        app = websocket.WebSocketApp(
            url, on_message=on_message, on_error=on_error,
            on_close=on_close, on_open=on_open)
        return app

    def _send_subscribe(self, entry):
        if not self._ws:
            return
        try:
            self._ws.send(json.dumps({
                "jsonrpc": "2.0", "id": entry["local_id"],
                "method": entry["method"], "params": entry["params"]}))
        except Exception:
            logger.warning("%s: subscribe send failed", self.provider)

    def _send_unsubscribe(self, entry):
        if not self._ws:
            return
        method = entry["method"].replace("Subscribe", "Unsubscribe")
        try:
            self._ws.send(json.dumps({
                "jsonrpc": "2.0", "id": entry["local_id"],
                "method": method,
                "params": [entry.get("server_id", entry["local_id"])]}))
        except Exception:
            pass

    def _on_message(self, _ws, message):
        self.last_message_ts = self._time()
        try:
            data = json.loads(message)
        except ValueError:
            return
        if "id" in data and "result" in data:
            # subscribe acknowledgement: record the server-side id.
            acked = None
            with self._lock:
                for entry in self._wanted.values():
                    if entry["local_id"] == data["id"]:
                        entry["server_id"] = data["result"]
                        acked = dict(entry)
            if acked is not None and self._on_subscribed:
                self._on_subscribed(self.provider, acked)
            return
        if self._on_event:
            self._on_event(self.provider, data)

    def _on_open(self, _ws):
        """Handshake completed: the connection is established NOW.
        Only here does ``connected`` become True, and only here are
        the wanted subscriptions sent (re-sent on every reconnect,
        exactly once per open). A stop that landed before the open
        leaves the manager disconnected."""
        if self._stop.is_set():
            return
        self.connected = True
        self.last_connect_ts = self._time()
        self._backoff_s = RECONNECT_BASE_S
        if self._on_handshake_ok:
            self._on_handshake_ok(self.provider)
        for entry in self.wanted_subscriptions():
            self._send_subscribe(entry)

    def _on_error(self, _ws, error):
        text = str(error)
        if "429" in text or "Too Many Requests" in text:
            self._note_handshake_429()
        else:
            logger.warning("%s WS error: %s", self.provider, text[:200])

    def _on_close(self, _ws, *_args):
        self.connected = False
        self.last_disconnect_ts = self._time()
        if not self._stop.is_set():
            self._schedule_reconnect()

    def _note_handshake_429(self):
        """Handshake refused with 429 — counted SEPARATELY from RPC
        429s, and reported to the health monitor."""
        self.wss_handshake_429s += 1
        if self._on_handshake_429:
            self._on_handshake_429(self.provider)

    def connect(self):
        """Construct the transport (one attempt). Returns True when
        the transport was constructed — NOT when the connection is
        open. The handshake completes asynchronously: ``connected``
        stays False until ``_on_open`` fires (driven by the
        transport's run loop — for the default factory that is
        ``WebSocketApp.run_forever``, called by ``run_forever``).
        Subscriptions are sent from ``_on_open`` only, never from
        here: sending on a socket whose handshake has not run drops
        the subscribe silently (D-02). The run loop owns retries."""
        self.connect_attempts += 1
        try:
            self._ws = self._factory(
                self.url, self._on_message, self._on_error,
                self._on_close, self._on_open)
        except Exception as exc:
            if "429" in str(exc):
                self._note_handshake_429()
            return False
        return True

    def _schedule_reconnect(self):
        self.reconnects += 1
        wait = self._backoff_s
        self._backoff_s = min(RECONNECT_MAX_S, self._backoff_s * 2)
        logger.warning("%s WS disconnected; reconnect in %.0fs "
                       "(attempt %d)", self.provider, wait,
                       self.reconnects)
        return wait

    def next_reconnect_wait_s(self):
        """Current backoff (testable without sleeping)."""
        return self._backoff_s

    def run_forever(self, sleep=time.sleep):
        """Connect, then reconnect with backoff until stop() is
        called. Intended for a daemon thread."""
        while not self._stop.is_set():
            if self.connect():
                # Block on the transport until it closes.
                try:
                    self._ws.run_forever()
                except Exception:
                    pass
                self.connected = False
                self.last_disconnect_ts = self._time()
            if self._stop.is_set():
                break
            wait = self._schedule_reconnect()
            sleep(wait)

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self.connected = False

    def stats(self):
        return {"provider": self.provider, "connected": self.connected,
                "connect_attempts": self.connect_attempts,
                "reconnects": self.reconnects,
                "wss_handshake_429s": self.wss_handshake_429s,
                "wanted_subscriptions": len(self._wanted),
                "last_connect_ts": self.last_connect_ts,
                "last_disconnect_ts": self.last_disconnect_ts,
                "last_message_ts": self.last_message_ts}


# ---------------------------------------------------------------------------
# Phase 5 — narrow Alchemy WSS subscriptions (spec section 4B)
# ---------------------------------------------------------------------------
# Program ids (canonical values live in daemon/solana_ids.py —
# never redefine the literals here; Phase 2, D-01/D-04/D-05).
# Only these three programs may ever be the target of an Alchemy
# subscription in this module.
PUMP_PROGRAM_ID = _solana_ids.PUMP_PROGRAM_ID
PUMPSWAP_PROGRAM_ID = _solana_ids.PUMPSWAP_PROGRAM_ID
TOKEN_PROGRAM_ID = _solana_ids.TOKEN_PROGRAM_ID
ALLOWED_PROGRAM_IDS = frozenset(
    (PUMP_PROGRAM_ID, PUMPSWAP_PROGRAM_ID, TOKEN_PROGRAM_ID))

# accountSubscribe is only for explicitly named accounts, and only
# of these kinds — a bonding-curve account, a pool account, or a
# token account. There is no "watch every account" mode.
ACCOUNT_KINDS = frozenset(("bonding_curve", "pool", "token_account"))

# Expected delivery intervals (seconds) used for staleness
# bookkeeping. slotSubscribe delivers roughly every 400ms; a
# signature subscription resolves or expires within ~2 minutes;
# logs/account/program subs are quiet until the watched program or
# account moves, so a long interval is expected, not a fault.
EXPECTED_INTERVAL_S = {
    "slotSubscribe": 10.0,
    "signatureSubscribe": 120.0,
    "logsSubscribe": 300.0,
    "accountSubscribe": 300.0,
    "programSubscribe": 300.0,
}


def _notification_slot(data):
    """Best-effort slot extraction from a subscription notification,
    for last-delivery bookkeeping. Returns None when the message
    carries no slot."""
    try:
        params = data.get("params") or {}
        result = params.get("result") or {}
        if isinstance(result, dict):
            if "slot" in result and isinstance(result["slot"], int):
                return result["slot"]
            ctx = result.get("context") or {}
            if isinstance(ctx, dict) and isinstance(ctx.get("slot"), int):
                return ctx["slot"]
    except Exception:
        pass
    return None


class AlchemySubscriptions:
    """Narrow Alchemy WSS subscriptions, lifecycle + bookkeeping.

    Wraps one WebSocketManager ("alchemy") and one
    SubscriptionManager (the book). Every subscription method
    enforces the narrowness rule and raises ValueError on an
    unfiltered or out-of-scope attempt instead of sending it:

    - logsSubscribe: ``mentions`` REQUIRED, non-empty, and every
      mentioned pubkey must be one of ALLOWED_PROGRAM_IDS.
    - programSubscribe: program id must be in ALLOWED_PROGRAM_IDS.
    - accountSubscribe: one explicit pubkey + a kind in
      ACCOUNT_KINDS (bonding_curve / pool / token_account).
    - signatureSubscribe: one explicit signature.
    - slotSubscribe: the chain slot feed (no filter possible or
      needed — it is a single global counter, not a firehose).

    There is deliberately no generic ``subscribe(method, params)``
    passthrough: a caller cannot smuggle an unfiltered subscription
    past the guards.

    Connection reuse: one manager = one persistent connection; all
    subscriptions share it and are re-issued after a reconnect.
    Payload minimization: commitments default to "confirmed",
    encodings to "jsonParsed" only where a parsed payload is needed
    (accountSubscribe); logsSubscribe carries only the mentions
    filter, no extra filters.

    Handshake 429s are counted on the manager
    (``wss_handshake_429s``) SEPARATELY from RPC 429s — this class
    exposes no rpc_429s counter at all, and ``stats()`` says so
    explicitly. An optional ``on_handshake_429`` callback is
    forwarded to the health monitor by the caller.
    """

    def __init__(self, url=None, home=None, ws_factory=None,
                 manager=None, subscription_manager=None,
                 on_handshake_429=None, on_handshake_ok=None,
                 on_event=None, now=None):
        from .subscription_manager import SubscriptionManager
        self._now = now
        self._on_event = on_event
        self._on_handshake_429 = on_handshake_429
        self._book = (subscription_manager if subscription_manager
                      is not None else SubscriptionManager(now=now))
        self._labels = {}  # subscription key -> account kind label
        if manager is not None:
            if manager.provider != "alchemy":
                raise ValueError("AlchemySubscriptions wraps an "
                                 "'alchemy' manager, not %r"
                                 % manager.provider)
            self.manager = manager
            # Wire bookkeeping onto an externally supplied manager.
            self.manager._on_subscribed = self._on_subscribed
            self.manager._on_event = self._dispatch
        else:
            if url is None:
                url = wss_url_for("alchemy", home=home)
            if not url:
                raise RuntimeError(
                    "no Alchemy WSS URL available (no key configured); "
                    "AlchemySubscriptions cannot connect")
            self.manager = WebSocketManager(
                "alchemy", url, ws_factory=ws_factory,
                on_handshake_429=self._note_handshake_429,
                on_handshake_ok=on_handshake_ok,
                on_event=self._dispatch,
                on_subscribed=self._on_subscribed,
                now=now)
        self._handshake_429_callbacks = (
            [on_handshake_429] if on_handshake_429 else [])

    def _time(self):
        return self._now() if self._now else time.time()

    # -- internal wiring ------------------------------------------------
    def _note_handshake_429(self, provider):
        for cb in self._handshake_429_callbacks:
            try:
                cb(provider)
            except Exception:
                pass

    def _on_subscribed(self, provider, entry):
        self._book.mark_subscribed(entry["method"], entry["params"],
                                   entry.get("server_id"))

    def _dispatch(self, provider, data):
        """Route a notification to the book (last-delivery tracking)
        and to the caller's on_event callback."""
        if isinstance(data, dict) and data.get("method", "").endswith(
                "Notification"):
            params = data.get("params") or {}
            server_id = params.get("subscription")
            slot = _notification_slot(data)
            if server_id is not None:
                for entry in self.manager.wanted_subscriptions():
                    if entry.get("server_id") == server_id:
                        self._book.mark_message(
                            entry["method"], entry["params"], slot=slot)
                        break
        if self._on_event:
            self._on_event(provider, data)

    def _register(self, method, params, label=None):
        from .subscription_manager import subscription_key
        rec = self._book.register(
            "alchemy", method, params,
            expected_interval_s=EXPECTED_INTERVAL_S[method])
        if label is not None:
            self._labels[subscription_key(method, params)] = label
        return rec

    # -- narrow subscriptions -------------------------------------------
    def subscribe_logs(self, mentions, commitment="confirmed"):
        """logsSubscribe with a REQUIRED mentions filter. Every
        mention must be an allowed program id. Unfiltered (empty /
        missing mentions) or out-of-scope mentions are refused."""
        if not mentions:
            raise ValueError("logsSubscribe requires a non-empty "
                             "mentions filter — unfiltered log "
                             "subscriptions are not allowed")
        for m in mentions:
            if m not in ALLOWED_PROGRAM_IDS:
                raise ValueError("logsSubscribe mention %r is not an "
                                 "allowed program (pump / pumpswap / "
                                 "token program only)" % m)
        params = [{"mentions": list(mentions)},
                  {"commitment": commitment}]
        self._register("logsSubscribe", params)
        return self.manager.subscribe("logsSubscribe", params)

    def subscribe_pump_logs(self, commitment="confirmed"):
        return self.subscribe_logs([PUMP_PROGRAM_ID],
                                   commitment=commitment)

    def subscribe_pumpswap_logs(self, commitment="confirmed"):
        return self.subscribe_logs([PUMPSWAP_PROGRAM_ID],
                                   commitment=commitment)

    def subscribe_token_program_logs(self, commitment="confirmed"):
        return self.subscribe_logs([TOKEN_PROGRAM_ID],
                                   commitment=commitment)

    def subscribe_program(self, program_id, commitment="confirmed",
                          encoding="jsonParsed"):
        """programSubscribe for one allowed program only."""
        if program_id not in ALLOWED_PROGRAM_IDS:
            raise ValueError("programSubscribe refused for %r — only "
                             "pump / pumpswap / token program "
                             "subscriptions are allowed" % program_id)
        params = [program_id, {"encoding": encoding,
                               "commitment": commitment}]
        self._register("programSubscribe", params)
        return self.manager.subscribe("programSubscribe", params)

    def subscribe_account(self, pubkey, kind, commitment="confirmed",
                          encoding="jsonParsed"):
        """accountSubscribe for ONE explicitly named account, of a
        declared kind (bonding_curve / pool / token_account)."""
        if kind not in ACCOUNT_KINDS:
            raise ValueError("accountSubscribe kind must be one of %s, "
                             "got %r" % (sorted(ACCOUNT_KINDS), kind))
        if not pubkey or not isinstance(pubkey, str):
            raise ValueError("accountSubscribe requires one explicit "
                             "account pubkey")
        params = [pubkey, {"encoding": encoding,
                           "commitment": commitment}]
        self._register("accountSubscribe", params, label=kind)
        return self.manager.subscribe("accountSubscribe", params)

    def subscribe_signature(self, signature, commitment="confirmed"):
        if not signature or not isinstance(signature, str):
            raise ValueError("signatureSubscribe requires one explicit "
                             "signature")
        params = [signature, {"commitment": commitment}]
        self._register("signatureSubscribe", params)
        return self.manager.subscribe("signatureSubscribe", params)

    def subscribe_slot(self):
        params = []
        self._register("slotSubscribe", params)
        return self.manager.subscribe("slotSubscribe", params)

    # -- unsubscribe ------------------------------------------------------
    def unsubscribe_logs(self, mentions, commitment="confirmed"):
        params = [{"mentions": list(mentions)},
                  {"commitment": commitment}]
        self.manager.unsubscribe("logsSubscribe", params)

    def unsubscribe_program(self, program_id, commitment="confirmed",
                            encoding="jsonParsed"):
        params = [program_id, {"encoding": encoding,
                               "commitment": commitment}]
        self.manager.unsubscribe("programSubscribe", params)

    def unsubscribe_account(self, pubkey, commitment="confirmed",
                            encoding="jsonParsed"):
        params = [pubkey, {"encoding": encoding,
                           "commitment": commitment}]
        self.manager.unsubscribe("accountSubscribe", params)

    def unsubscribe_signature(self, signature, commitment="confirmed"):
        params = [signature, {"commitment": commitment}]
        self.manager.unsubscribe("signatureSubscribe", params)

    def unsubscribe_slot(self):
        self.manager.unsubscribe("slotSubscribe", [])

    # -- lifecycle (delegates to the manager) ------------------------------
    def connect(self):
        return self.manager.connect()

    def run_forever(self, sleep=time.sleep):
        return self.manager.run_forever(sleep=sleep)

    def stop(self):
        return self.manager.stop()

    @property
    def wss_handshake_429s(self):
        """Handshake 429s — counted SEPARATELY from RPC 429s."""
        return self.manager.wss_handshake_429s

    @property
    def reconnects(self):
        return self.manager.reconnects

    def next_reconnect_wait_s(self):
        return self.manager.next_reconnect_wait_s()

    # -- bookkeeping --------------------------------------------------------
    def live_subscriptions(self):
        """Which subscriptions are live, with last-delivery info.
        Each record carries method/params/status/server_id/messages/
        last_msg_ts/last_msg_slot and, for accounts, the kind label."""
        from .subscription_manager import subscription_key
        out = []
        for rec in self._book.all():
            rec = dict(rec)
            label = self._labels.get(rec["key"])
            if label is not None:
                rec["account_kind"] = label
            out.append(rec)
        return out

    def stale_subscriptions(self, now=None):
        return self._book.stale(
            now=now if now is not None else self._time())

    def stats(self):
        base = self.manager.stats()
        base.update({
            "wss_handshake_429s": self.manager.wss_handshake_429s,
            # There is deliberately no rpc_429s counter here: WSS
            # handshake 429s are counted separately from RPC 429s
            # (audit finding); RPC 429s live in the health monitor.
            "rpc_429s": None,
            "live_subscriptions": len(self._book.all()),
            "subscribed_programs": sorted({
                p for rec in self._book.all()
                for p in rec["params"][:1]
                if rec["method"] == "programSubscribe"
                and isinstance(p, str)}),
        })
        return base
