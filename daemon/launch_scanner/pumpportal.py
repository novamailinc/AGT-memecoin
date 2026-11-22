"""PumpPortal WebSocket discovery: pump.fun launches + migrations.

Subscribes to the FREE streams at ``wss://pumpportal.fun/api/data``:

* ``subscribeNewToken`` — every new pump.fun token creation
* ``subscribeMigration`` — pump.fun -> PumpSwap / Raydium migrations

The trade streams (``subscribeTokenTrade`` / ``subscribeAccountTrade``)
went PAID in May 2026 (API key + wallet required) and are NEVER
subscribed to: this module is a discovery feed only, and it performs
zero trading API calls. Paper-only, data only.

Fail-soft is mandatory: a background daemon thread holds the single
socket; any failure (connect refused, handshake rejected, socket drop)
is absorbed by exponential backoff and the scanner keeps working on the
existing discovery sources. A dead PumpPortal feed never raises into
the scan cycle.

PumpPortal enforces a strict one-connection rule — the feed holds ONE
connection per daemon life and backs off exponentially on reconnect
(never reconnect-spam; hourly bans are the penalty for hammering).

Stdlib only: reuses ``logstream.WSClient`` (minimal RFC 6455 client with
HTTP CONNECT proxy support), so no new dependency is needed.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from daemon.launch_scanner.logstream import WSClient, WSError

logger = logging.getLogger("forex_agent.daemon.launch_scanner.pumpportal")

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"

# Free streams only. Trade streams went paid in May 2026 — they are
# listed here as an explicit deny list so a future edit can't silently
# subscribe to a paid endpoint.
FREE_METHODS = ("subscribeNewToken", "subscribeMigration")
PAID_METHODS = ("subscribeTokenTrade", "subscribeAccountTrade")

# A server that REFUSES the handshake is not a flapping socket — it is
# a closed endpoint (ban / outage). Retrying it on the normal ladder
# (10s doubling to 600s) looks like reconnect spam and can extend the
# ban. A refused handshake waits one hour before the next attempt, and
# the long wait is a constant: it is never consumed or decayed, so a
# repeat refusal waits the full hour again.
HANDSHAKE_BACKOFF_S = 3600.0

# PumpPortal payload txType values we handle.
TXT_CREATE = "create"    # new token
TXT_MIGRATE = "migrate"  # graduation to PumpSwap / Raydium


def _is_handshake_failure(exc: BaseException) -> bool:
    """True when the server refused the connection during the WebSocket
    handshake — "connection closed during handshake" or a "websocket
    handshake rejected" — as opposed to a healthy connection that
    dropped mid-stream. A refusal is a possible ban; a drop is not."""
    return "handshake" in str(exc).lower()


def _s(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _mint(value: Any) -> str:
    """A mint must be a non-empty string — never stringify garbage."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def parse_new_token(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one ``subscribeNewToken`` event into a candidate dict.

    The same normalized shape as ``sources.py`` candidates so the event
    flows into the existing ``_consider`` / scoring pipeline as data —
    through the normal safety gates and scoring; nothing bypasses.
    Returns None for anything that isn't a well-formed creation event
    (never raises).
    """
    try:
        if not isinstance(payload, dict):
            return None
        if payload.get("txType") != TXT_CREATE:
            return None
        mint = _mint(payload.get("mint"))
        if not mint:
            return None
        # Event state (2026-11-17, zero-RPC entry): the create
        # payload already carries the opening virtual reserves, so
        # they are plumbed through instead of dropped at parse time.
        # Units are RAW as sent: vTokensInBondingCurve is raw token
        # units (6 decimals), vSolInBondingCurve is SOL. Consumers
        # must not mix the two scales. state_from_event is True
        # only when the event is complete (mint + curve + both
        # reserves present and positive) — an incomplete event
        # never claims trusted state.
        vtok = _f(payload.get("vTokensInBondingCurve"))
        vsol = _f(payload.get("vSolInBondingCurve"))
        curve = _s(payload.get("bondingCurveKey"))
        state_from_event = bool(mint and curve and vtok > 0
                                and vsol > 0)
        return {
            "mint": mint,
            "symbol": _s(payload.get("symbol")),
            "name": _s(payload.get("name")),
            "chain": "solana",
            "source": "pumpportal-new",
            "created_ts": time.time(),
            "state_from_event": state_from_event,
            "usd_market_cap": 0.0,  # WS carries no USD mcap; the judge
            "extra": {              # prices it from DexScreener stats.
                "signature": _s(payload.get("signature")),
                "market_cap_sol": _f(payload.get("marketCapSol")),
                # 2026-11-16 (units fix): PumpPortal sends initialBuy in
                # LAMPORTS, not SOL — median observed 40,000,000 (= 0.04
                # SOL); the field was stored raw, so every consumer that
                # compared it against a SOL-scale bar (the flash
                # pre-gate's 1.0 SOL, dossiers' MIN_INITIAL_BUY_SOL)
                # passed ~97% of births on this clause alone. Divide by
                # 1e9 so initial_buy_sol is actually SOL. marketCapSol
                # already arrives in SOL (median ~30) and is untouched.
                "initial_buy_sol": _f(payload.get("initialBuy")) / 1e9,
                "trader": _s(payload.get("traderPublicKey")),
                "bonding_curve": curve,
                "virtual_token_reserves": vtok,
                "virtual_sol_reserves": vsol,
                "uri": _s(payload.get("uri")),
            },
            "url": "https://pump.fun/coin/%s" % mint,
        }
    except Exception:
        logger.warning("pumpportal new-token parse failed", exc_info=True)
        return None


# Venue normalization (2026-10-23 repair): live PumpPortal migration
# payloads name the PumpSwap venue "pump-amm" (82 of 84 real migration
# items recorded that day) — NOT "pumpswap". The un-normalized value
# failed every downstream venue whitelist, so stream detection folded
# ZERO candidates while the GT backfill carried all entries. Unknown
# venues pass through lowercased so consumers still fail closed.
_VENUE_ALIASES = {"pump-amm": "pumpswap", "pump-fun-amm": "pumpswap",
                  "pumpswap": "pumpswap", "raydium": "raydium"}


def _venue(raw: Any) -> Optional[str]:
    v = _s(raw).lower()
    if not v:
        return None
    if v in _VENUE_ALIASES:
        return _VENUE_ALIASES[v]
    if v.startswith("raydium"):
        return "raydium"
    return v


def parse_migration(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one ``subscribeMigration`` event into a candidate dict.

    Migration = the coin graduated the pump.fun bonding curve and now
    trades on a real venue (PumpSwap or Raydium). The venue lands in
    ``extra["migration_pool"]`` so tracking can note it (see the
    PumpSwap graduation coverage: a coin that graduates keeps being
    tracked post-migration). Returns None for anything malformed —
    never raises.
    """
    try:
        if not isinstance(payload, dict):
            return None
        if payload.get("txType") != TXT_MIGRATE:
            return None
        mint = _mint(payload.get("mint"))
        if not mint:
            return None
        pool = _venue(payload.get("pool"))  # normalized: pumpswap|raydium
        # Event state (2026-11-17): the migration event is complete
        # when it names the mint and the venue. It does NOT claim
        # price completeness: real migration payloads carry
        # market_cap_sol == 0 (762 recorded, 2026-10-23), so no
        # price is ever derived from this event — only the venue,
        # the pool id, and the migration signature.
        state_from_event = bool(mint and pool)
        return {
            "mint": mint,
            "symbol": _s(payload.get("symbol")),
            "name": _s(payload.get("name")),
            "chain": "solana",
            "source": "pumpportal-migration",
            "created_ts": time.time(),
            "state_from_event": state_from_event,
            "usd_market_cap": 0.0,  # priced by the judge via DexScreener
            "extra": {
                "signature": _s(payload.get("signature")),
                "market_cap_sol": _f(payload.get("marketCapSol")),
                "trader": _s(payload.get("traderPublicKey")),
                "bonding_curve": _s(payload.get("bondingCurveKey")),
                "migration_pool": pool,
                "market_id": _s(payload.get("marketId")),
                "uri": _s(payload.get("uri")),
            },
            "url": "https://pump.fun/coin/%s" % mint,
        }
    except Exception:
        logger.warning("pumpportal migration parse failed", exc_info=True)
        return None


class PumpPortalFeed:
    """Background PumpPortal WebSocket subscriber.

    ``drain()`` returns normalized candidate dicts (``pumpportal-new``
    / ``pumpportal-migration``) detected since the last drain. Never
    raises out of its thread; when the socket is down the scanner
    simply keeps its existing discovery sources.

    Exactly one connection is ever held: ``_run`` loops ``_serve``
    (one connect + subscribe + recv loop) with exponential backoff
    between attempts — base doubling to the cap, with jitter, so a
    flapping endpoint can never look like reconnect spam.
    """

    def __init__(self,
                 ws_url: str = PUMPPORTAL_WS_URL,
                 backoff_base_s: float = 10.0,
                 backoff_max_s: float = 600.0,
                 connect_timeout: float = 20.0,
                 max_queue: int = 2000,
                 ws_factory: Optional[Callable[[str, float], Any]] = None):
        self._ws_url = ws_url or PUMPPORTAL_WS_URL
        self._base = max(1.0, float(backoff_base_s or 10.0))
        self._max = max(self._base, float(backoff_max_s or 600.0))
        self._timeout = float(connect_timeout or 20.0)
        self._ws_factory = ws_factory or (
            lambda url, timeout: WSClient(url, timeout=timeout))
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(
            maxsize=max(100, int(max_queue or 2000)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._healthy = False
        self._connects = 0
        self._dropped = 0
        self._last_error: Optional[str] = None
        self._backoff = self._base
        self._seen_sigs: set = set()
        self._subscribed: List[str] = []

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="degen-pumpportal",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy

    @property
    def connects(self) -> int:
        with self._lock:
            return self._connects

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    def drain(self) -> List[Dict[str, Any]]:
        """Normalized candidates since the last drain. Never raises."""
        out: List[Dict[str, Any]] = []
        try:
            while True:
                try:
                    out.append(self._q.get_nowait())
                except queue.Empty:
                    return out
        except Exception:
            logger.warning("pumpportal drain failed", exc_info=True)
            return out

    # -- internals ------------------------------------------------------
    def _set_healthy(self, value: bool) -> None:
        with self._lock:
            self._healthy = value

    def _note_error(self, exc: BaseException) -> None:
        with self._lock:
            self._last_error = "%s: %s" % (type(exc).__name__, exc)

    def _next_backoff(self) -> float:
        """Current backoff, then double it (capped), with jitter."""
        wait = self._backoff
        self._backoff = min(self._max, self._backoff * 2.0)
        return wait * (0.8 + 0.4 * random.random())

    def _reset_backoff(self) -> None:
        self._backoff = self._base

    def _wait_after_failure(self, exc: BaseException) -> float:
        """Wait before the next reconnect attempt after a failure.

        A refused handshake (possible server ban) waits the long
        backoff and does NOT consume the normal ladder — a repeat
        refusal waits the full hour again. A mid-stream drop uses the
        normal exponential backoff.
        """
        if _is_handshake_failure(exc):
            logger.warning(
                "pumpportal server refused the handshake (possible "
                "ban) — reconnecting in %.0fs", HANDSHAKE_BACKOFF_S)
            return HANDSHAKE_BACKOFF_S
        logger.warning("pumpportal connection failed: %s", exc)
        return self._next_backoff()

    def _run(self) -> None:
        """Reconnect loop: one connection at a time, exponential backoff."""
        while not self._stop.is_set():
            wait: Optional[float] = None
            try:
                self._serve()
            except Exception as exc:
                self._note_error(exc)
                wait = self._wait_after_failure(exc)
            self._set_healthy(False)
            if self._stop.is_set():
                break
            if wait is None:
                # _serve returned cleanly without the stop flag (the
                # recv loop only exits on stop, so this should not
                # happen) — treat it as a plain drop.
                wait = self._next_backoff()
            logger.warning("pumpportal reconnecting in %.0fs "
                           "(attempt backoff)", wait)
            if self._stop.wait(wait):
                break

    def _subscribe(self, ws: Any) -> None:
        """Subscribe the FREE streams only — never a trade stream."""
        self._subscribed = []
        for method in FREE_METHODS:
            if method in PAID_METHODS:
                # Belt and suspenders: a paid method can never be sent.
                raise ValueError("refusing paid stream %s" % method)
            ws.send_text(json.dumps({"method": method}))
            self._subscribed.append(method)

    def _serve(self) -> None:
        ws = self._ws_factory(self._ws_url, self._timeout)
        try:
            ws.connect()
            self._subscribe(ws)
            with self._lock:
                self._connects += 1
            self._reset_backoff()
            self._set_healthy(True)
            logger.warning("pumpportal subscribed: %s",
                           ", ".join(self._subscribed))
            while not self._stop.is_set():
                msg = ws.recv_text()
                if msg is None:
                    raise WSError("server closed the stream")
                self._handle_message(msg)
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _handle_message(self, msg: str) -> None:
        try:
            data = json.loads(msg)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        tx_type = data.get("txType")
        if tx_type == TXT_CREATE:
            item = parse_new_token(data)
        elif tx_type == TXT_MIGRATE:
            item = parse_migration(data)
        else:
            return  # acks, trade payloads (never subscribed), noise
        if item is None:
            return
        sig = (item.get("extra") or {}).get("signature") or ""
        if sig:
            if sig in self._seen_sigs:
                return
            self._seen_sigs.add(sig)
            if len(self._seen_sigs) > 5000:
                self._seen_sigs = set(list(self._seen_sigs)[-2500:])
        try:
            self._q.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                logger.warning("pumpportal queue full — dropped %d events",
                               self._dropped)
        else:
            logger.warning("pumpportal %s %s %s",
                           item.get("source"),
                           item.get("symbol") or item.get("mint"),
                           item.get("mint"))
