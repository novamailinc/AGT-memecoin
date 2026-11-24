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
import random
import threading
import time

try:
    from daemon.launch_scanner import reserves as _res
    from daemon.launch_scanner import reserve_monitor as _resmon
    from daemon.launch_scanner.logstream import WSClient, WSError
except Exception:  # pragma: no cover - script-style import path
    import reserves as _res  # type: ignore
    import reserve_monitor as _resmon  # type: ignore
    from logstream import WSClient, WSError  # type: ignore

try:
    from daemon.launch_scanner import helius_ratelimit as _guard_mod
except Exception:  # pragma: no cover - script-style import path
    try:
        import helius_ratelimit as _guard_mod  # type: ignore
    except Exception:
        _guard_mod = None  # type: ignore

logger = logging.getLogger("forex_agent.daemon.launch_scanner.armed_watch")

ARMED_MULT = 1.5          # peak/entry at which a position arms
STALE_S = 30.0            # no message for this long -> STALE
MOVE_JOURNAL_PCT = 20.0   # reconnect moves worth journaling
_RECV_TICK_S = 5.0


def _record_reserve_snapshot(pool, mint, v, decimals=None):
    """Feed the 6.2 reserve monitor from a vault resolution the
    watcher ALREADY made - no new RPC, no added polling, cadence
    unchanged. Measurement only: the monitor never gates, scores, or
    exits, and a monitor failure must never affect the watch loop.
    Units are the raw vault amounts resolve_vaults returns; quote_mint
    and decimals are passed only when grounded by the resolution."""
    try:
        if not isinstance(v, dict):
            return
        _resmon.record_snapshot(pool, mint, time.time(),
                                v.get("base_amount"),
                                v.get("quote_amount"),
                                source="armed_watch",
                                quote_mint=v.get("quote_mint"),
                                decimals=decimals)
    except Exception:
        pass


def _record_push_snapshot(pool, mint, amt, quote_mint=None,
                          decimals=None):
    """Feed the monitor from a websocket accountNotification: pushed
    vault amounts stay RAW (same units as resolve_vaults), identity
    comes from our own subscription map, time is arrival time. The
    two sides can be observed at different times - per-side
    observation ts are recorded, never merged into a fake single
    reading. Records only when BOTH sides have been observed at
    least once; an unobserved side is unrecorded, never zero-filled."""
    try:
        if not (amt.get("base_ts") and amt.get("quote_ts")):
            return
        _resmon.record_snapshot(pool, mint, time.time(),
                                amt.get("base"), amt.get("quote"),
                                source="armed_watch_ws_push",
                                quote_mint=quote_mint,
                                decimals=decimals,
                                base_ts=amt.get("base_ts"),
                                quote_ts=amt.get("quote_ts"))
    except Exception:
        pass


def _shared_guard():
    """Fleet-wide Helius guard, or None when unavailable (fail-open:
    the watcher behaves as before — unguarded handshake attempts).
    Patchable in tests."""
    if _guard_mod is None:
        return None
    try:
        return _guard_mod.shared_guard()
    except Exception:
        return None


def _helius_quiet_active():
    """True while the fleet Helius quiet period is in force.
    Fail-open False when the guard module is unavailable (the
    watcher then behaves as before — guard-gated handshakes).
    Patchable in tests."""
    if _guard_mod is None:
        return False
    try:
        return bool(_guard_mod.quiet_active())
    except Exception:
        return False


def _quiet_probe_pending():
    """True while a post-quiet probe is unclaimed (quiet expired,
    single probe not yet taken). The watcher must not claim the
    probe with a WSS handshake — the probe has to be a cheap RPC
    read, not a handshake (a 429 handshake would re-enter 12h
    quiet on a handshake's evidence). Fail-open False when the
    guard module is unavailable. Patchable in tests."""
    if _guard_mod is None:
        return False
    try:
        return bool(_guard_mod.quiet_probe_pending())
    except Exception:
        return False


def _wss_bucket():
    """Shared-guard bucket for the key the WSS handshake uses, or ""
    when the watcher falls back to the public endpoint (no Helius
    quota, nothing to guard) or the key cannot be read."""
    try:
        key = _res._helius_key()
    except Exception:
        return ""
    if not key or _guard_mod is None:
        return ""
    try:
        return _guard_mod.bucket_id_for_key(key)
    except Exception:
        return ""


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
        self._subs = {}        # subid -> (mint, role, pool, generation)
        self._pending = {}     # request id -> (mint, role, pool, gen)
        self._amounts = {}     # mint -> {"base": int, "quote": int}
        self._monitor_amounts = {}  # isolated measurement capture state
        self._generation = 0   # armed-identity generation; a
                               # notification captured under an older
                               # generation is discarded, never
                               # stored against the new pool
        self._stale_sub_drops = 0
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
        fresh_amounts = {}
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
                              "quote_vault": v["quote_vault"],
                              "quote_mint": v.get("quote_mint"),
                              # signed virtual quote reserves at resolve
                              # time; pushed vault amounts stay RAW, so
                              # adding this can drift across fee sweeps
                              # — the exit probe (price_for_position)
                              # re-reads the pool for the exact value.
                              "quote_virtual": v.get("quote_virtual", 0)}
                    self._vault_cache[pool] = vaults
                    try:
                        pos["pool_vaults"] = vaults
                    except Exception:
                        pass
                    now_cap = time.time()
                    fresh_amounts[mint] = {
                        "base": v["base_amount"],
                        "quote": v["quote_amount"],
                        "ts": now_cap,
                        "base_ts": now_cap,
                        "quote_ts": now_cap}
                    # decimals unrecorded here: mint_decimals runs
                    # below when the armed entry is built; the
                    # snapshot never blocks on it.
                    _record_reserve_snapshot(pool, mint, v)
                want[mint] = {"pool": pool,
                              "decimals": _res.mint_decimals(mint),
                              "base_vault": vaults["base_vault"],
                              "quote_vault": vaults["quote_vault"]}
            except Exception:
                continue
        with self._lock:
            if want != self._want:
                old = self._want
                self._want = want
                # A new armed identity is a new generation: amounts
                # cached against a pool/vault identity that changed
                # (or vanished) are purged, never paired with the
                # new pool; old-generation notifications are dropped
                # by the generation check in _handle.
                self._generation += 1
                for m in list(self._amounts):
                    if m in fresh_amounts:
                        continue
                    w_old, w_new = old.get(m), want.get(m)
                    if (not w_old or not w_new
                            or w_new.get("pool") != w_old.get("pool")
                            or w_new.get("base_vault")
                            != w_old.get("base_vault")
                            or w_new.get("quote_vault")
                            != w_old.get("quote_vault")):
                        self._amounts.pop(m, None)
                        self._monitor_amounts.pop(m, None)
                self._dirty.set()
            for m, a in fresh_amounts.items():
                self._amounts[m] = a
                self._monitor_amounts[m] = dict(a)

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
        guard_hot_logged = False
        quiet_logged = False
        probe_logged = False
        pacer_logged = False
        while not self._stop.is_set():
            # Helius quiet period (2026-11-20): pause WSS handshake
            # attempts entirely — the handshake itself 429s while
            # the flag is active, and quiet exists to give the flag
            # a full window without a single Helius attempt of any
            # kind. Exit pricing is unaffected: while STALE the
            # lanes fall back to polled quotes
            # (reserves.price_for_position, public fallback
            # during quiet).
            if _helius_quiet_active():
                if not quiet_logged:
                    logger.warning(
                        "armed_watch: Helius quiet active — pausing "
                        "WSS handshake, exit pricing on polled quotes")
                    quiet_logged = True
                self._set_state("STALE")
                # Jittered re-check (2026-11-22 herd fix): every
                # watcher sleeps a fixed 5s here, so they all wake
                # in lockstep the moment quiet clears and contend
                # for one pacer token. A 0-2s jitter spreads them
                # (kept short so a watcher still re-checks well
                # inside the 8s resume window the quiet tests
                # allow).
                if self._stop.wait(5.0 + random.uniform(0.0, 2.0)):
                    break
                continue
            quiet_logged = False
            # Post-quiet probe pending (2026-11-22): quiet expired
            # and the single probe is unclaimed — let an RPC caller
            # claim and resolve it first. The probe must be a cheap
            # RPC read, not a WSS handshake.
            if _quiet_probe_pending():
                if not probe_logged:
                    logger.warning(
                        "armed_watch: Helius quiet probe pending — "
                        "pausing WSS handshake until an RPC caller "
                        "resolves the probe")
                    probe_logged = True
                self._set_state("STALE")
                if self._stop.wait(5.0 + random.uniform(0.0, 2.0)):
                    break
                continue
            probe_logged = False
            guard = _shared_guard()
            bucket = _wss_bucket() if guard is not None else ""
            if bucket:
                try:
                    remaining = guard.cooldown_remaining(bucket)
                except Exception:
                    remaining = 0.0
                if remaining > 0:
                    # Helius is rate-limited fleet-wide (HTTP 429s, or
                    # a 429 handshake from this or another watcher):
                    # do NOT attempt the handshake — it would 429 and
                    # add nothing. Before this gate each trader retried
                    # the handshake every 60s forever (13,303 failures
                    # on 2026-11-16). Wait out the cooldown in small
                    # slices so a stop request still exits promptly.
                    # Exit pricing is unaffected: while the watcher is
                    # STALE the lanes fall back to the exempt polled
                    # path (reserves.price_for_position).
                    if not guard_hot_logged:
                        logger.warning(
                            "armed_watch: Helius guard hot (%.0fs left) "
                            "— skipping WSS handshake, exit pricing on "
                            "polled quotes", remaining)
                        guard_hot_logged = True
                    self._set_state("STALE")
                    if self._stop.wait(min(5.0, remaining)
                                      + random.uniform(0.0, 2.0)):
                        break
                    continue
            guard_hot_logged = False
            # Pacer gate (2026-11-22): the handshake is a Helius
            # call on the same key, so it takes a pacer token like
            # every other Helius call — this serialises reconnect
            # herds across the fleet (before this gate the
            # handshakes bypassed the pacer entirely: 1,026
            # handshake 429s in 48h, invisible to the spike
            # monitor, while the guard alone let every watcher
            # reconnect at once when it cleared). No token -> wait
            # and retry the loop; the guard/quiet checks above run
            # again first. Fail-open: without the pacer module the
            # watcher behaves as before (guard-gated handshakes).
            if bucket and _guard_mod is not None:
                try:
                    _paced = bool(_guard_mod.pacer_acquire(
                        bucket, wait_s=0))
                except Exception:
                    _paced = True
                if not _paced:
                    if not pacer_logged:
                        logger.warning(
                            "armed_watch: Helius pacer throttled — "
                            "skipping WSS handshake, exit pricing on "
                            "polled quotes")
                        pacer_logged = True
                    self._set_state("STALE")
                    if self._stop.wait(5.0 + random.uniform(0.0, 2.0)):
                        break
                    continue
            pacer_logged = False
            try:
                self._serve()
            except Exception as e:
                if "429" in str(e) and bucket:
                    # The handshake itself was rate-limited: engage
                    # the fleet-wide cooldown so this and every other
                    # process stop attempting until it clears. Only
                    # 429s engage the guard; other failures keep the
                    # local exponential backoff below. A WSS success
                    # deliberately does NOT clear the bucket — the
                    # watcher makes no HTTP calls, so it has no
                    # evidence about HTTP health.
                    try:
                        guard.report_ratelimited(bucket)
                    except Exception:
                        pass
                    # 2026-11-22: also engage the pacer — before
                    # this a handshake 429 never reached the pacer,
                    # so the spike monitor, the auto-quiet and the
                    # RPC backoff were all blind to the largest
                    # single source of Helius 429 responses.
                    try:
                        if _guard_mod is not None:
                            _guard_mod.pacer_report(bucket, False)
                    except Exception:
                        pass
                logger.warning("armed_watch connection failed: %s", e)
            self._set_state("STALE")
            with self._lock:
                self._down_since = time.time()
                self._subs = {}
            # Jittered backoff (2026-11-22 herd fix): watchers that
            # fail together share the same backoff ladder and would
            # otherwise retry in lockstep.
            if self._stop.wait(backoff * (1.0 + random.uniform(0.0, 0.5))):
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
        rid = int(time.time() * 1000) % 100000
        with self._lock:
            want = dict(self._want)
            gen = self._generation
            self._subs = {}
        for mint, w in want.items():
            for role, vault in (("base", w["base_vault"]),
                                ("quote", w["quote_vault"])):
                rid += 1
                # Identity bound at capture time: pool and generation
                # travel with the subscription, so a delayed
                # notification can never be stored against a newer
                # armed identity.
                self._pending[rid] = (mint, role, w["pool"], gen)
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
                mint, role, pool, gen = key
                if (gen != self._generation
                        or pool != (self._want.get(mint) or {}).get("pool")):
                    # Delayed notification from an older armed
                    # identity: discarded, never stored against the
                    # new pool or paired with a newer other side.
                    self._stale_sub_drops += 1
                    return
                now = time.time()
                amt = self._amounts.setdefault(
                    mint, {"base": 0, "quote": 0, "ts": 0.0,
                           "base_ts": 0.0, "quote_ts": 0.0})
                amt[role] = ta["amount"]
                amt[role + "_ts"] = now
                amt["ts"] = now
                capture = self._monitor_amounts.setdefault(mint, {})
                capture[role] = ta["amount"]
                capture[role + "_ts"] = now
                amt_snap = dict(capture)
                dec = (self._want.get(mint) or {}).get("decimals")
                base, quote = amt.get("base"), amt.get("quote")
            _record_push_snapshot(
                pool, mint, amt_snap,
                quote_mint=(self._vault_cache.get(pool) or {})
                .get("quote_mint"),
                decimals=dec)
            quote_mint_px = ((self._vault_cache.get(
                (self._want.get(mint) or {}).get("pool") or "")
                or {}).get("quote_mint"))
            _cv = (self._vault_cache.get(
                (self._want.get(mint) or {}).get("pool") or "") or {})
            px = _res.amounts_price(
                base, (quote or 0) + (_cv.get("quote_virtual") or 0),
                dec, sol_usd=self._sol_usd_fn(),
                quote_mint=quote_mint_px) if base and quote else None
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
            gen = self._generation
        for mint, w in want.items():
            try:
                v = _res.resolve_vaults(w["pool"], mint)
                if not v:
                    continue
                with self._lock:
                    # The resolve ran outside the lock: a position
                    # change during it makes the result OLD-identity
                    # data. Re-check generation AND pool/vault
                    # identity before publishing anything from it -
                    # never seed old-pool amounts against the new
                    # armed set.
                    cur = self._want.get(mint)
                    if (self._generation != gen or not cur
                            or cur.get("pool") != w.get("pool")
                            or cur.get("base_vault")
                            != w.get("base_vault")
                            or cur.get("quote_vault")
                            != w.get("quote_vault")):
                        self._stale_sub_drops += 1
                        continue
                    now_rec = time.time()
                    self._amounts[mint] = {
                        "base": v["base_amount"],
                        "quote": v["quote_amount"], "ts": now_rec,
                        "base_ts": now_rec, "quote_ts": now_rec}
                    self._monitor_amounts[mint] = dict(self._amounts[mint])
                    old = float(self._last_px.get(mint) or 0)
                _record_reserve_snapshot(w["pool"], mint, v,
                                         decimals=w.get("decimals"))
                px = _res.amounts_price(v["base_amount"],
                                        v["quote_effective"],
                                        w["decimals"],
                                        sol_usd=self._sol_usd_fn(),
                                        quote_mint=v.get("quote_mint"))
                if px:
                    with self._lock:
                        if self._generation != gen or self._want.get(mint) != w:
                            self._stale_sub_drops += 1
                            continue
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
