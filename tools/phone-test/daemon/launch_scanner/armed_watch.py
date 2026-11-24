#!/usr/bin/env python3
"""Armed-position push watcher (2026-10-23, P1 + P7 + P9a).

Positions whose peak is >= 1.5x entry ("armed" — they have profit to
lose) get PER-SLOT push pricing instead of polled quotes: one Helius
standard-WSS connection (free tier; subscriptions verified live
2026-10-23) holds accountSubscribe on the two PumpSwap VAULT token
accounts of every armed Solana position. Every swap moves a vault
balance, so every swap pushes a notification; the price is recomputed
from the two vault amounts the moment either side moves and handed
to the caller's on_price callback — exits then evaluate on the event,
not on a clock.

Freshness state machine (P7): the watcher is LIVE while connected,
STALE when the socket is down / silent past STALE_S, and RECONCILING
on reconnect — during which it re-reads every armed position's
reserves directly (account reads), re-emits prices, and reports via
on_reconcile(moved, gap_s) any position whose price moved more than
MOVE_JOURNAL_PCT while the stream was dark, so the caller can
journal the gap before the state returns to LIVE.

Scope: Solana PumpSwap positions (pair = pool address). Anything
else (Raydium pairs, EVM, HL) is simply not watched here — the
existing tape/daemon paths keep pricing those, unchanged.

One connection, consolidated subscriptions (free-tier WSS allows
~5 connections; this uses exactly one). Never raises.
"""
import base64
import json
import logging
import threading
import time

try:
    from daemon.launch_scanner import reserves as _res
    from daemon.launch_scanner.logstream import WSClient, WSError
except Exception:  # pragma: no cover - script-style import path
    import reserves as _res  # type: ignore
    from logstream import WSClient, WSError  # type: ignore

logger = logging.getLogger("forex_agent.daemon.launch_scanner.armed_watch")

ARMED_MULT = 1.5          # peak/entry at which a position arms
STALE_S = 30.0            # no message for this long -> STALE
MOVE_JOURNAL_PCT = 20.0   # reconnect moves worth journaling
_RECV_TICK_S = 5.0


def armed_positions(positions):
    """{mint: pos} subset of open positions that are armed:
    recorded peak >= ARMED_MULT x entry. Pure; never raises."""
    out = {}
    try:
        for mint, pos in (positions or {}).items():
            entry = float((pos or {}).get("entry_price") or 0)
            peak = float((pos or {}).get("peak_price") or 0)
            if entry > 0 and peak >= ARMED_MULT * entry:
                out[mint] = pos
    except Exception:
        pass
    return out


class ArmedWatcher:
    """Background accountSubscribe watcher over armed positions.

    on_price(mint, price_usd, ts) — called from the watcher thread
    on every vault move (and once per position after reconciliation).
    on_state(state, info) — state transitions; info carries gap_s
    and moved=[{mint, from_px, to_px, pct}] on RECONCILING->LIVE.
    Thread-safe set_positions(); never raises.
    """

    def __init__(self, on_price, on_state=None, ws_url=None,
                 sol_usd_fn=None):
        self._on_price = on_price
        self._on_state = on_state or (lambda s, i: None)
        self._ws_url = ws_url  # None -> Helius WSS from reserves keys
        self._sol_usd_fn = sol_usd_fn or _res.sol_usd_price
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._want = {}        # mint -> {"pool","decimals"}
        self._vault_cache = {}  # pool -> vaults (resolution is stable)
        self._subs = {}        # subid -> (mint, role)
        self._pending = {}     # request id -> (mint, role)
        self._amounts = {}     # mint -> {"base": int, "quote": int}
        self._last_px = {}     # mint -> last emitted price
        self._state = "STALE"
        self._last_msg_ts = 0.0
        self._down_since = time.time()
        self._dirty = threading.Event()  # subscription set changed

    # -- public -------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="armed-watch", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def state(self):
        with self._lock:
            return self._state

    def price(self, mint, max_age_s=15.0, now=None):
        """Last pushed price for a mint when fresh, else 0.0 — the
        price_fn contract lanes already understand."""
        now = time.time() if now is None else now
        with self._lock:
            px = float(self._last_px.get(mint) or 0)
            ts = float(self._amounts.get(mint, {}).get("ts") or 0)
        if px > 0 and ts and now - ts <= max_age_s:
            return px
        return 0.0

    def set_positions(self, positions):
        """Declare the armed set: {mint: pos}. Resolves each pool's
        vaults once (direct reads) and diffs against the current
        set. Vault resolutions are cached on the pos dict itself
        (pos['pool_vaults']) when the caller passes live dicts."""
        want = {}
        for mint, pos in (positions or {}).items():
            try:
                pool = str(pos.get("pair") or "")
                if not pool:
                    continue
                vaults = pos.get("pool_vaults") \
                    or self._vault_cache.get(pool)
                if not (isinstance(vaults, dict)
                        and vaults.get("base_vault")
                        and vaults.get("quote_vault")):
                    v = _res.resolve_vaults(pool, mint)
                    if not v:
                        continue
                    vaults = {"base_vault": v["base_vault"],
                              "quote_vault": v["quote_vault"]}
                    self._vault_cache[pool] = vaults
                    try:
                        pos["pool_vaults"] = vaults
                    except Exception:
                        pass
                    with self._lock:
                        self._amounts[mint] = {
                            "base": v["base_amount"],
                            "quote": v["quote_amount"],
                            "ts": time.time()}
                want[mint] = {"pool": pool,
                              "decimals": _res.mint_decimals(mint),
                              "base_vault": vaults["base_vault"],
                              "quote_vault": vaults["quote_vault"]}
            except Exception:
                continue
        with self._lock:
            if want != self._want:
                self._want = want
                self._dirty.set()

    # -- internals ------------------------------------------------------

    def _set_state(self, state, info=None):
        with self._lock:
            old = self._state
            self._state = state
        if old != state:
            try:
                self._on_state(state, info or {})
            except Exception:
                pass

    def _url(self):
        if self._ws_url:
            return self._ws_url
        k = _res._helius_key()
        if k:
            return "wss://mainnet.helius-rpc.com/?api-key=" + k
        return "wss://api.mainnet-beta.solana.com"

    def _run(self):
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._serve()
            except Exception as e:
                logger.warning("armed_watch connection failed: %s", e)
            self._set_state("STALE")
            with self._lock:
                self._down_since = time.time()
                self._subs = {}
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)

    def _serve(self):
        ws = WSClient(self._url(), timeout=_RECV_TICK_S)
        ws.connect()
        try:
            self._subscribe_all(ws)
            gap_s = time.time() - self._down_since \
                if self._down_since else 0.0
            self._reconcile(gap_s)
            backoff_reset = True
            while not self._stop.is_set():
                if self._dirty.is_set():
                    self._dirty.clear()
                    self._subscribe_all(ws)
                try:
                    msg = ws.recv_text()
                except Exception:
                    msg = ""  # recv tick timeout: loop, check state
                now = time.time()
                if msg:
                    self._last_msg_ts = now
                    self._handle(ws, msg)
                if self._state == "LIVE" and self._last_msg_ts and \
                        now - self._last_msg_ts > STALE_S:
                    self._set_state("STALE", {"reason": "silent"})
                if msg is None:
                    raise WSError("server closed the stream")
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _subscribe_all(self, ws):
        with self._lock:
            want = dict(self._want)
            self._subs = {}
        rid = int(time.time() * 1000) % 100000
        for mint, w in want.items():
            for role, vault in (("base", w["base_vault"]),
                                ("quote", w["quote_vault"])):
                rid += 1
                self._pending[rid] = (mint, role)
                ws.send_text(json.dumps({
                    "jsonrpc": "2.0", "id": rid,
                    "method": "accountSubscribe",
                    "params": [vault, {"encoding": "base64",
                                       "commitment": "processed"}]}))
        logger.warning("armed_watch subscribed: %d vault(s) for %d "
                       "position(s)", len(self._pending), len(want))

    def _handle(self, ws, msg):
        try:
            d = json.loads(msg)
        except Exception:
            return
        if "id" in d and "result" in d:
            pend = getattr(self, "_pending", {}) or {}
            key = pend.pop(d["id"], None)
            if key:
                with self._lock:
                    self._subs[d["result"]] = key
            return
        if d.get("method") != "accountNotification":
            return
        try:
            sub = (d.get("params") or {}).get("subscription")
            res = (d.get("params") or {}).get("result") or {}
            data = (res.get("value") or {}).get("data") or []
            raw = base64.b64decode(data[0]) if data else b""
            ta = _res.parse_token_account(raw)
            if ta is None:
                return
            with self._lock:
                key = self._subs.get(sub)
                if not key:
                    return
                mint, role = key
                amt = self._amounts.setdefault(
                    mint, {"base": 0, "quote": 0, "ts": 0.0})
                amt[role] = ta["amount"]
                amt["ts"] = time.time()
                dec = (self._want.get(mint) or {}).get("decimals")
                base, quote = amt.get("base"), amt.get("quote")
            px = _res.amounts_price(base, quote, dec,
                                    sol_usd=self._sol_usd_fn()) \
                if base and quote else None
            if px:
                with self._lock:
                    self._last_px[mint] = px
                try:
                    self._on_price(mint, px, time.time())
                except Exception:
                    pass
        except Exception:
            pass

    def _reconcile(self, gap_s):
        """On (re)connect: re-read every armed position directly,
        emit fresh prices, and report >20% moves during the gap."""
        self._set_state("RECONCILING", {"gap_s": round(gap_s, 1)})
        moved = []
        with self._lock:
            want = dict(self._want)
        for mint, w in want.items():
            try:
                v = _res.resolve_vaults(w["pool"], mint)
                if not v:
                    continue
                with self._lock:
                    self._amounts[mint] = {
                        "base": v["base_amount"],
                        "quote": v["quote_amount"], "ts": time.time()}
                    old = float(self._last_px.get(mint) or 0)
                px = _res.amounts_price(v["base_amount"],
                                        v["quote_amount"],
                                        w["decimals"],
                                        sol_usd=self._sol_usd_fn())
                if px:
                    with self._lock:
                        self._last_px[mint] = px
                    try:
                        self._on_price(mint, px, time.time())
                    except Exception:
                        pass
                    if old > 0:
                        pct = (px - old) / old * 100.0
                        if abs(pct) > MOVE_JOURNAL_PCT:
                            moved.append({"mint": mint,
                                          "from_px": old, "to_px": px,
                                          "pct": round(pct, 1)})
            except Exception:
                continue
        self._set_state("LIVE", {"gap_s": round(gap_s, 1),
                                 "moved": moved})
        try:
            self._on_state("RECONCILED",
                           {"gap_s": round(gap_s, 1), "moved": moved})
        except Exception:
            pass
