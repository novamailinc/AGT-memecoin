#!/usr/bin/env python3
"""Moon-bag graduation lane (2026-10-26, user-ordered "Build it").

The experiment isolates ONE variable against grad_raw: the final-leg
exit (the moon bag). Everything else is grad_raw, copied exactly:

- Buys EVERY pump.fun graduation — no safety check, no veto, no
  runner rule, no score. Detection is the LIVE PumpPortal migration
  stream + GeckoTerminal 60s backfill over a 30-min window, with
  grad_raw's FIRE-FIRST migration-tx pricing, dedupe-ever, and
  $100 paper bankroll as the only volume limit (NO daily cap).
- Cost model copied EXACTLY from grad_raw: honest (adverse) fills,
  venue fees, cost gate — so the comparison is clean.
- THE ONLY DIFFERENCES (the experiment):
  (a) final-leg trail 30% -> 50%: the last 25% rides on a 50%
      trailing stop from its peak instead of 30%;
  (b) NO 24h time stop: TIME_STOP_H is float("inf") on this
      instance only, so the hygiene backstop never fires. (None
      would crash grad.py's `TIME_STOP_H * 3600` arithmetic, so
      infinity is the clean disable — same semantics, no crash.)
  Legs A (50%@2x) and B (25%@3x) and the -50% hard stop are
  EXACTLY grad_raw's. Leg-C close records still carry grad.py's
  hardcoded reason label "leg_c_trail_30pct" — label only; the
  executed trail is 0.50 (see tests/test_moon_lane.py).
- STAKE: $1 per coin (new-lane standard). grad_raw's $25 CEO
  override is deliberately NOT applied — raw dollars are not
  comparable across lanes, so the verdict compares expectancy per
  close and win rate (stake-independent metrics).
- PRE-REGISTERED VERDICT (stamped into the ledger header): at 100
  closes, mean pnl per close <= 0 halts the lane permanently
  (killswitch lane "moon", human-only re-enable); > 0 survives.
  Head-to-head vs grad_raw is reported on expectancy/close and
  win rate, never raw dollars. grad_raw's closes list is capped
  at the latest 500, so same-window comparisons must respect it.
- NO cards in v1 — measurement only, like grad_raw v1.

Implementation: grad.py's engine loaded as an ISOLATED module
instance via importlib, with lane policy overridden on that
instance only (LEG_C_TRAIL, TIME_STOP_H, ledger, lane, stake).
The gated lane, grad_raw, and grad_select module instances are
untouched — lanes run in separate processes with separate state.

Paper only. Nothing here touches real money, keys, or wallets.
Free-only: PumpPortal public stream + GeckoTerminal/DexScreener.
"""
import importlib.util
import logging
import os
import threading
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.moon")

try:
    import rt_sync as _rt_journal  # P7 reconciliation journal (fail-soft)
except Exception:  # pragma: no cover
    _rt_journal = None

# P7: stream-consumer freshness for the migration feed.
_FEED_STATE = {"healthy": None, "down_since": None}


def _note_feed_health(feed, now):
    """Track the PumpPortal feed's LIVE/STALE transitions; on
    recovery, journal the gap (positions' >20% moves during the gap
    are covered by the armed watcher / tape reconciliation)."""
    if feed is None:
        return
    try:
        healthy = bool(getattr(feed, "healthy", True))
    except Exception:
        return
    prev = _FEED_STATE["healthy"]
    if prev is None:
        _FEED_STATE["healthy"] = healthy
        if not healthy:
            _FEED_STATE["down_since"] = now
        return
    if healthy == prev:
        return
    _FEED_STATE["healthy"] = healthy
    if not healthy:
        _FEED_STATE["down_since"] = now
        logger.warning("moon feed STALE (stream down)")
        return
    down = _FEED_STATE["down_since"]
    _FEED_STATE["down_since"] = None
    logger.warning("moon feed LIVE again")
    if down is not None and _rt_journal is not None:
        try:
            _rt_journal.journal_reconciliation({
                "ts": now, "source": "moon-feed",
                "gap_s": round(now - down, 1),
                "note": "migration stream reconnected; GT backfill "
                        "covers the gap window"})
        except Exception:
            pass

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "moon_ledger.json")
LANE = "moon"
CHAIN = "solana"

BANKROLL_START = 100.00
NOTIONAL_USD = 1.00       # new-lane standard; grad_raw's $25 CEO
                           # override is deliberately NOT applied —
                           # cross-lane comparison uses expectancy/
                           # close and win rate, never raw dollars
MAX_OPEN = 150            # headroom only — the bankroll binds first
DAILY_ENTRY_CAP = 100000  # NO daily cap by design; bankroll is the limit
MAX_GRAD_AGE_S = 1800.0   # 30-min window (backfill must survive GT lag)
VERDICT_SAMPLE_CLOSES = 100

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")


# ---- engine (isolated grad.py instance with moon policy) -----------------

def _no_safety_gate(mint):
    """The defining policy of this lane: there is NO safety gate.
    Signature matches grad._safety_ok. Returns a pass with no report
    — the entry snapshot therefore honestly shows no safety data."""
    return True, "", None, False


def _no_veto(mint, symbol, safety_report):
    """No veto engine on this lane. Signature matches grad._veto_ok."""
    return True, ""


def _preregistration():
    """Verdict terms for the MOON lane, stamped into its ledger header
    at creation (same anti-quiet-change rule as grad.py)."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'moon'); "
                 "if > 0 the lane survived its sample. Re-enable is "
                 "human-only." % VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": None,  # none — bankroll-bound by design
            "max_grad_age_s": MAX_GRAD_AGE_S,
            "runner_rule_applies": False,
            "safety_gate": "NONE (grad_raw's no-gate graduation game, "
                           "copied exactly)",
            "veto": "NONE",
            "detection": "PumpPortal migration stream (live) + "
                         "GeckoTerminal backfill (30-min window)",
            "ladder": "50%@2x / 25%@3x / 25% trail 50% / -50% stop / "
                      "NO time stop (moon-bag experiment: the ONLY "
                      "differences vs grad_raw)",
            "experiment_vs_grad_raw": "final-leg trail 0.30 -> 0.50; "
                                     "TIME_STOP_H 24 -> inf (disabled)",
        },
        "registered_ts": time.time(),
    }


def make_engine():
    """Load grad.py as a fresh, isolated module and override its lane
    policy for the MOON lane. The gated lane's own module instance is
    never touched. Never raises on import shape — grad.py's deps are
    resolved by its own import block."""
    spec = importlib.util.spec_from_file_location("moon_engine",
                                                  _GRAD_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    eng.LEDGER_PATH = LEDGER_PATH
    eng.LANE = LANE
    eng.NOTIONAL_USD = NOTIONAL_USD
    eng.MAX_OPEN = MAX_OPEN
    eng.DAILY_ENTRY_CAP = DAILY_ENTRY_CAP
    eng.MAX_GRAD_AGE_S = MAX_GRAD_AGE_S
    # THE EXPERIMENT (the only exit differences vs grad_raw):
    # final-leg trail 0.30 -> 0.50, and the 24h time stop disabled.
    # TIME_STOP_H is compared as `TIME_STOP_H * 3600` in grad.py, so
    # None would crash; float("inf") is the clean disable (an age
    # can never exceed infinity, so neither time-stop branch fires).
    eng.LEG_C_TRAIL = 0.50
    eng.TIME_STOP_H = float("inf")
    eng._safety_ok = _no_safety_gate
    eng._veto_ok = _no_veto
    eng._preregistration = _preregistration
    _ff_install(eng)
    return eng


# ---- detection ------------------------------------------------------------

def migration_to_candidate(item, now=None):
    """Convert one PumpPortalFeed migration dict into a grad-engine
    candidate row. None for anything that is not a pump.fun
    graduation to PumpSwap/Raydium. Never raises."""
    try:
        if not isinstance(item, dict):
            return None
        if item.get("source") != "pumpportal-migration":
            return None
        mint = str(item.get("mint") or "")
        if not mint.endswith("pump"):
            return None
        extra = item.get("extra") or {}
        dex = str(extra.get("migration_pool") or "").lower()
        # Venue aliases (2026-10-23 repair): live PumpPortal payloads
        # say "pump-amm" for PumpSwap — normalize here too so a raw
        # (unparsed-by-pumpportal.py) item still folds. Fail closed on
        # anything genuinely unknown.
        if dex in ("pump-amm", "pump-fun-amm"):
            dex = "pumpswap"
        elif dex.startswith("raydium"):
            dex = "raydium"
        if dex not in ("pumpswap", "raydium"):
            return None
        now = time.time() if now is None else now
        created = float(item.get("created_ts") or 0) or now
        return {
            "mint": mint,
            "symbol": str(item.get("symbol") or "?")[:32],
            "created_ts": created,
            "dex": dex,
            "pool_address": str(extra.get("market_id") or ""),
            # FIRE-FIRST: real PumpPortal payloads carry NO market_id
            # and NO price fields (762 recorded migrations, 2026-10-23:
            # market_cap_sol always 0, market_id always empty) — but
            # the payload's signature identifies the migration
            # transaction, whose own balances price the entry.
            "migration_sig": str(extra.get("signature") or ""),
            "reserve_usd": 0.0,
            "fdv_usd": 0.0,
            "source": "pumpportal-migration",
        }
    except Exception:
        return None


def _stamp_detection(engine, mint, source, ff=None):
    """Record HOW this entry was detected (stream vs backfill) and
    that no gate applied, on the open position — so the two entry
    classes can be scored separately at verdict time. `ff` (optional)
    stamps the FIRE-FIRST price provenance: entry_price_source plus
    the payload price fields, or the fallback reason when the
    instant path was attempted but the live quote priced the entry."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = d["positions"].get(mint)
            if pos is not None:
                pos["source"] = "graduation-moon"
                ef = pos.setdefault("entry_features", {})
                ef["detection"] = source
                ef["gate"] = "none"
                if ff:
                    ef["entry_price_source"] = \
                        str(ff.get("source") or "live_quote")
                    info = ff.get("info")
                    if info:
                        ef["payload_price_usd"] = \
                            float(info["price_usd"])
                        ef["payload_mcap_usd"] = \
                            float(info["mcap_usd"])
                        ef["payload_pool"] = str(info.get("pool") or "")
                        ef["payload_sol_usd"] = \
                            float(info.get("sol_usd") or 0)
                    if ff.get("fallback"):
                        ef["ff_fallback"] = str(ff["fallback"])[:160]
                engine._write(d)
    except Exception:
        pass


# ---- FIRE-FIRST: migration-transaction instant entry -----------------
# Baseline measured 2026-10-23 from this lane's own ledger (228
# stream-stamped entries, P5 stamps): entry - migration median
# 101.0s (p90 265.7s). Decomposition: (a) drain wait median 83.3s —
# the daemon's single loop is saturated by sequential exit-quote
# polling, so stream items sit in the feed queue; (b) decide queue
# median 4.4s (p90 49.1s) — candidates are considered serially and
# each pays (c) tape + quote round-trips, median 3.3s (p90 5.9s,
# max 13.5s: DexScreener pairs fetch, tape fallback, a 2.0s retry
# sleep). FIRE-FIRST removes (c) for stream candidates and the
# daemon's stream-wake thread removes most of (a)/(b).
#
# Why the migration TRANSACTION is the price source: the normalized
# PumpPortal payload carries no usable price data (762 recorded
# migrations: market_cap_sol always 0, market_id always empty), and
# the PumpSwap pool address cannot be derived from the mint (the
# pool creator varies per pool — verified against 35 real pairs).
# But the payload's signature names the migration transaction, and
# that transaction's post-token-balances ARE the new pool's initial
# reserves: the account that ends the tx holding both the mint and
# WSOL is the pool, and its balance ratio is the exact price at the
# migration instant. One RPC round-trip replaces the whole quote
# chain, and it also yields the real pool address (which the
# phantom-entry guard requires and the payload never carries).
# Everything downstream is untouched: the same honest-fill adverse
# adjustment and venue fee inside grad._consider_candidate, the
# same $1 size, dedupe-ever, bankroll limit, ladder, killswitch.
#
# Post-entry verification: shortly after a payload-priced entry, a
# live quote is fetched through the ORIGINAL quote path; the
# deviation is journaled into the position's entry_features
# (payload_verify). Deviation > FF_DEVIATION_FLAG_PCT, or no live
# quote obtainable inside the verification window, sets entry_dq —
# a data-quality flag ONLY: history is never rewritten and exits
# keep working off live quotes exactly as before.

FF_TOTAL_SUPPLY = 1_000_000_000.0   # pump.fun fixed total supply
FF_MCAP_MIN_USD = 10_000.0          # graduation sanity bounds: a
FF_MCAP_MAX_USD = 2_000_000.0       # payload price outside = garbage
FF_DEVIATION_FLAG_PCT = 25.0
FF_VERIFY_DELAY_S = 15.0            # let DexScreener index the pool
FF_VERIFY_WINDOW_S = 120.0
FF_VERIFY_TRIES = 3
FF_VERIFY_PER_SCAN = 2              # bound the work per scan cycle
_FF_VERIFY_LOCK = threading.Lock()
_WSOL = "So11111111111111111111111111111111111111112"


def _ff_sol_usd(engine):
    """SOL/USD for payload pricing (reserves' 30s-cached read).
    Engine attr `_ff_sol_usd_override` (tests) wins. 0.0 = unknown
    (caller falls back to the live quote path). Never raises."""
    fn = getattr(engine, "_ff_sol_usd_override", None)
    if fn is not None:
        try:
            return float(fn() or 0)
        except Exception:
            return 0.0
    try:
        import reserves as _res
        return float(_res.sol_usd_price() or 0)
    except Exception:
        return 0.0


def _ff_parse_tx_price(res, mint, sol_usd=0.0):
    """Pure parse of a getTransaction result into a payload price:
    (info|None, why). info carries price_usd, liquidity_usd (both
    pool sides at the payload price), mcap_usd (price x fixed
    supply), pool (the vault owner = the pool account), quote_sol,
    sol_usd. Sanity: the implied mcap must sit inside graduation
    bounds, else the payload is garbage and the caller falls back.
    Never raises."""
    try:
        meta = (res or {}).get("meta") or {}
        bals = meta.get("postTokenBalances") or []
        by_owner = {}
        for b in bals:
            try:
                amt = b.get("uiTokenAmount") or {}
                raw = float(amt.get("amount") or 0)
                dec = int(amt.get("decimals") or 0)
            except Exception:
                continue
            if raw <= 0:
                continue
            by_owner.setdefault(str(b.get("owner") or ""), {})[
                str(b.get("mint") or "")] = (raw, dec)
        best = None
        for owner, toks in by_owner.items():
            if not owner or mint not in toks or _WSOL not in toks:
                continue
            b_raw, b_dec = toks[mint]
            q_raw, q_dec = toks[_WSOL]
            if b_raw <= 0 or q_raw <= 0:
                continue
            if best is None or q_raw > best[0]:
                best = (q_raw, owner, b_raw, b_dec, q_dec)
        if best is None:
            return None, "no paired pool vaults in migration tx"
        q_raw, pool, b_raw, b_dec, q_dec = best
        price_sol = (q_raw / (10.0 ** q_dec)) / \
            (b_raw / (10.0 ** b_dec))
        if price_sol <= 0:
            return None, "migration tx price not positive"
        sol = float(sol_usd or 0)
        if sol <= 0:
            return None, "no SOL/USD for payload price"
        price_usd = price_sol * sol
        info = {
            "price_usd": price_usd,
            "liquidity_usd": 2.0 * (q_raw / (10.0 ** q_dec)) * sol,
            "mcap_usd": price_usd * FF_TOTAL_SUPPLY,
            "pool": pool,
            "quote_sol": q_raw / (10.0 ** q_dec),
            "sol_usd": sol,
        }
        if not (FF_MCAP_MIN_USD <= info["mcap_usd"]
                <= FF_MCAP_MAX_USD):
            return None, ("payload mcap $%.0f outside graduation "
                          "bounds" % info["mcap_usd"])
        return info, ""
    except Exception as e:
        return None, "migration tx parse failed: %s" % e


def _ff_tx_price(engine, sig, mint):
    """(info|None, why): fetch the migration transaction by the
    payload's signature and price the entry from its own balances.
    ONE RPC round-trip. Engine attr `_ff_tx_price_override` (tests)
    replaces the fetch+parse wholesale. Never raises."""
    fn = getattr(engine, "_ff_tx_price_override", None)
    if fn is not None:
        try:
            return fn(sig, mint)
        except Exception as e:
            return None, "payload price override failed: %s" % e
    # SOL/USD is fetched CONCURRENTLY with the tx: through this
    # VM's egress proxy each round-trip costs seconds, and the SOL
    # conversion is only needed once the tx lands. (reserves caches
    # SOL/USD for 30s, so on a warm cache the thread is free.)
    holder = {}

    def _sol():
        holder["sol"] = _ff_sol_usd(engine)

    t = threading.Thread(target=_sol, daemon=True)
    t.start()
    try:
        import reserves as _res
        res = _res.rpc("getTransaction", [sig, {
            "encoding": "json", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0}])
    except Exception as e:
        return None, "migration tx fetch failed: %s" % e
    if not isinstance(res, dict):
        return None, "migration tx not available yet"
    t.join(timeout=6)
    return _ff_parse_tx_price(res, mint,
                              sol_usd=holder.get("sol", 0.0))


def _ff_install(engine):
    """Install the fire-first wrappers on ONE raw engine instance.

    The wrappers serve the migration-tx price ONLY while scan_once
    holds an entry context (_ff_ctx) for that exact mint — a window
    of milliseconds around that candidate's consider call. Every
    other caller (exit polling via the daemon's price_fn, retries
    through the main loop, the gated lane's separate module
    instance) delegates to the original functions, looked up on the
    engine at call time so tests can stub the originals. Never
    raises."""
    if getattr(engine, "_ff_installed", False):
        return
    engine._ff_orig_quote_price = engine.quote_price
    engine._ff_orig_fresh_tape = engine._fresh_tape

    def quote_price(chain, mint, spot_index=None):
        ctx = getattr(engine, "_ff_ctx", None)
        if ctx and ctx.get("mint") == mint and ctx.get("info"):
            ctx["consumed"] = True
            return float(ctx["info"]["price_usd"])
        return engine._ff_orig_quote_price(chain, mint, spot_index)

    def fresh_tape(mint):
        ctx = getattr(engine, "_ff_ctx", None)
        if ctx and ctx.get("mint") == mint and ctx.get("info"):
            info = ctx["info"]
            return {"price_usd": float(info["price_usd"]),
                    "usd_liquidity": float(info["liquidity_usd"]),
                    "usd_market_cap": float(info["mcap_usd"]),
                    "tape_source": "migration-tx"}
        return engine._ff_orig_fresh_tape(mint)

    engine.quote_price = quote_price
    engine._fresh_tape = fresh_tape
    engine._ff_ctx = None
    engine._ff_pending = {}
    engine._ff_installed = True


def _ff_build_ctx(engine, cand):
    """Entry context for one fresh stream candidate. Returns a ctx
    dict always (info=None + why when the instant path cannot price
    this candidate — the caller then runs the classic path and the
    why is journaled on any entry that results). On success the
    candidate's pool_address is filled from the migration tx when
    the payload left it empty. Never raises."""
    mint = str(cand.get("mint") or "")
    try:
        sig = str(cand.get("migration_sig") or "")
        if not sig:
            return {"mint": mint, "info": None,
                    "why": "no migration signature in payload"}
        info, why = _ff_tx_price(engine, sig, mint)
        if info is None:
            return {"mint": mint, "info": None, "why": why}
        if not str(cand.get("pool_address") or "") \
                and info.get("pool"):
            cand["pool_address"] = str(info["pool"])
        return {"mint": mint, "info": info, "why": ""}
    except Exception as e:
        return {"mint": mint, "info": None,
                "why": "payload price error: %s" % e}


def _ff_register_pending(engine, mint, info, entry_ts):
    try:
        pend = getattr(engine, "_ff_pending", None)
        if pend is None:
            pend = engine._ff_pending = {}
        pend[mint] = {"payload_price": float(info["price_usd"]),
                      "entry_ts": float(entry_ts), "tries": 0}
    except Exception:
        pass


def _ff_sweep_pending(engine, now):
    """Re-register verification for payload-priced positions that
    still lack a verdict — the pending set is in-memory, the ledger
    is durable, and this VM reboots: without the sweep a restart
    would silently drop verifications. Never raises."""
    try:
        pend = getattr(engine, "_ff_pending", None)
        if pend is None:
            pend = engine._ff_pending = {}
        with engine._ledger_lock():
            d = engine._read()
        for mint, pos in (d.get("positions") or {}).items():
            ef = pos.get("entry_features") or {}
            if ef.get("entry_price_source") == "migration_payload" \
                    and "payload_verify" not in ef \
                    and mint not in pend:
                pend[mint] = {
                    "payload_price": float(
                        ef.get("payload_price_usd")
                        or pos.get("quoted_price") or 0),
                    "entry_ts": float(pos.get("entry_ts") or now),
                    "tries": 0}
    except Exception:
        pass


def _ff_stamp_verify(engine, mint, verdict):
    """Write a payload_verify verdict into the open position (its
    entry_features ride into the close record at finalize), or into
    the close record when the position already exited. entry_dq is
    set on dq_* verdicts. Flag only — nothing is rewritten and no
    exit path reads it. Never raises."""
    try:
        dq = str(verdict.get("status") or "").startswith("dq")
        with engine._ledger_lock():
            d = engine._read()
            pos = (d.get("positions") or {}).get(mint)
            if pos is not None:
                ef = pos.setdefault("entry_features", {})
                ef["payload_verify"] = verdict
                if dq:
                    pos["entry_dq"] = True
                    ef["entry_dq"] = True
                engine._write(d)
                return
            for c in d.get("closes") or []:
                if c.get("mint") == mint:
                    ef = c.setdefault("entry_features", {})
                    if "payload_verify" not in ef:
                        ef["payload_verify"] = verdict
                        if dq:
                            c["entry_dq"] = True
                            ef["entry_dq"] = True
                        engine._write(d)
                    return
    except Exception:
        pass


def _ff_verify_pending(engine, now):
    """Post-entry verification for payload-priced entries: fetch a
    live quote through the ORIGINAL quote path and journal the
    deviation. Bounded per scan (FF_VERIFY_PER_SCAN) and serialized
    across the daemon's scan threads; a second caller skips.
    Never raises."""
    if not _FF_VERIFY_LOCK.acquire(blocking=False):
        return
    try:
        _ff_sweep_pending(engine, now)
        pend = getattr(engine, "_ff_pending", None) or {}
        done = 0
        for mint, rec in list(pend.items()):
            if done >= FF_VERIFY_PER_SCAN:
                break
            age = now - float(rec.get("entry_ts") or now)
            if age < FF_VERIFY_DELAY_S:
                continue
            live_fn = getattr(engine, "_ff_orig_quote_price", None) \
                or engine.quote_price
            try:
                live = float(live_fn("solana", mint) or 0)
            except Exception:
                live = 0.0
            payload = float(rec.get("payload_price") or 0)
            verdict = None
            if live > 0 and payload > 0:
                dev = abs(live - payload) / payload * 100.0
                verdict = {
                    "ts": now, "live_price": live,
                    "payload_price": payload,
                    "deviation_pct": round(dev, 2),
                    "status": "ok" if dev <= FF_DEVIATION_FLAG_PCT
                    else "dq_deviation"}
            else:
                rec["tries"] = int(rec.get("tries") or 0) + 1
                if rec["tries"] < FF_VERIFY_TRIES \
                        and age <= FF_VERIFY_WINDOW_S:
                    continue
                verdict = {
                    "ts": now, "live_price": live,
                    "payload_price": payload,
                    "deviation_pct": None,
                    "status": "dq_unverifiable"}
            pend.pop(mint, None)
            done += 1
            _ff_stamp_verify(engine, mint, verdict)
            logger.warning("moon payload verify %s: %s "
                           "(payload $%.8f, live $%.8f)",
                           (mint or "?")[:12], verdict["status"],
                           payload, live)
    except Exception:
        pass
    finally:
        _FF_VERIFY_LOCK.release()


def scan_once(engine, feed=None, gt_rows=None, now=None,
              on_items=None, fresh_only=False):
    """One detection cycle for the MOON lane: drain the migration
    stream, merge stream + GT-backfill candidates into the engine
    watchlist, and attempt a no-gate entry for everything still in
    its age window. `on_items` (P8) receives the raw drained stream
    items (births + migrations) for observers like the dossier
    builder. `fresh_only` (FIRE-FIRST: the daemon's stream-wake
    thread) restricts entry attempts to candidates drained in THIS
    call — watchlist retries stay the main loop's business, at the
    main loop's cadence. Returns counters. Never raises."""
    out = {"stream": 0, "pools": 0, "watching": 0, "opened": 0,
           "skipped": 0, "dead": 0}
    try:
        return _scan_once(engine, feed, gt_rows, out, now, on_items,
                          fresh_only)
    except Exception as e:
        logger.warning("moon scan_once failed: %s", e)
        return out


def _scan_once(engine, feed, gt_rows, out, now, on_items=None,
               fresh_only=False):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    try:
        if engine._ks.is_halted(LANE):
            logger.warning("moon scan: lane halted by killswitch "
                           "— no entries (exits still run)")
            return out
    except Exception:
        pass

    # FIRE-FIRST: post-entry payload verification (bounded). Main-
    # loop scans only — the stream-wake thread's fresh_only scans
    # never pay verification latency ahead of a fresh entry.
    if not fresh_only:
        _ff_verify_pending(engine, now)

    _note_feed_health(feed, now)
    rows = []
    fresh_mints = set()
    if feed is not None:
        try:
            items = feed.drain() or []
            if on_items is not None:
                try:
                    on_items(items)
                except Exception:
                    pass
            for item in items:
                cand = migration_to_candidate(item, now)
                if cand is not None:
                    rows.append(cand)
                    fresh_mints.add(str(cand.get("mint") or ""))
                    out["stream"] += 1
        except Exception:
            pass
    if gt_rows:
        out["pools"] = len(gt_rows)
        for p in gt_rows:
            row = dict(p)
            row.setdefault("source", "geckoterminal-backfill")
            rows.append(row)

    with engine._ledger_lock():
        d = engine._read()
        for row in rows:
            mint = str(row.get("mint") or "")
            if not mint or mint in d["positions"] or \
                    mint in d["dead_mints"] or \
                    any(c.get("mint") == mint for c in d["closes"]):
                continue
            if mint in d["watchlist"]:
                # Pool-address merge (2026-10-23): an empty pool
                # address blocks entry (phantom guard). A later row
                # (GT backfill) may carry the real pool — merge it in
                # without disturbing the first-detection stamp, so a
                # stream row that arrived without market_id does not
                # cost the lane a real graduation.
                _exist = d["watchlist"][mint]
                if not str(_exist.get("pool_address") or "") and \
                        str(row.get("pool_address") or ""):
                    _exist["pool_address"] = str(row["pool_address"])
                continue  # first detection source wins the stamp
            if engine.is_graduation_candidate(row, now) is None:
                continue
            row = dict(row)
            row.setdefault("first_seen_ts", now)
            d["watchlist"][mint] = row
        for mint, row in list(d["watchlist"].items()):
            if engine.is_graduation_candidate(row, now) is None:
                d["dead_mints"][mint] = {
                    "ts": now, "reason": "window expired"}
                del d["watchlist"][mint]
                out["dead"] += 1
        cutoff = now - engine.DEAD_TTL_S
        d["dead_mints"] = {m: v for m, v in d["dead_mints"].items()
                           if float(v.get("ts") or 0) >= cutoff}
        watch = list(d["watchlist"].values())
        engine._write(d)
    out["watching"] = len(watch)

    ff_ready = getattr(engine, "_ff_installed", False)
    # FIRE-FIRST: entry contexts for this cycle's stream candidates
    # are built BEFORE the consider loop, in parallel when a drain
    # brings a burst — each build is one RPC round-trip and serial
    # builds would re-queue the burst behind its own fetches.
    ff_ctxs = {}
    if ff_ready and fresh_mints:
        todo = [c for c in watch
                if str(c.get("mint") or "") in fresh_mints]
        if len(todo) == 1:
            c = todo[0]
            ff_ctxs[str(c.get("mint") or "")] = \
                _ff_build_ctx(engine, c)
        elif len(todo) > 1:
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=4) as ex:
                    built = list(ex.map(
                        lambda c: _ff_build_ctx(engine, c), todo))
                for c, ctx in zip(todo, built):
                    ff_ctxs[str(c.get("mint") or "")] = ctx
            except Exception:
                ff_ctxs = {}
    for cand in watch:
        mint = str(cand.get("mint") or "")
        if fresh_only and mint not in fresh_mints:
            continue
        # A candidate drained from the stream in THIS cycle gets its
        # prebuilt entry context — the consider call below then
        # prices from the migration transaction (no tape call, no
        # DexScreener quote chain) and fills the pool address the
        # payload never carries. Any failure leaves the classic path
        # in charge (fail-operational) and journals its reason.
        ctx = None
        if ff_ready and mint in fresh_mints:
            ctx = ff_ctxs.get(mint) or _ff_build_ctx(engine, cand)
            if ctx is not None and not ctx.get("info"):
                logger.warning("moon fire-first fallback %s: %s",
                               mint[:12], ctx.get("why") or "?")
            engine._ff_ctx = ctx
        try:
            opened, _why, final = engine.consider_candidate(cand, now)
        finally:
            if ctx is not None:
                engine._ff_ctx = None
        if opened:
            out["opened"] += 1
            ff = {"source": "live_quote"}
            if ctx is not None and ctx.get("consumed") \
                    and ctx.get("info"):
                ff = {"source": "migration_payload",
                      "info": ctx["info"]}
                _ff_register_pending(engine, mint, ctx["info"],
                                     time.time())
            elif ctx is not None:
                ff = {"source": "live_quote",
                      "fallback": ctx.get("why")
                      or "payload price unused"}
            _stamp_detection(engine, mint,
                             str(cand.get("source") or "watchlist"),
                             ff=ff)
        else:
            out["skipped"] += 1
            if final:
                with engine._ledger_lock():
                    d = engine._read()
                    d["watchlist"].pop(mint, None)
                    engine._write(d)
    return out


def account_state(engine):
    """Read-only summary via the engine. Never raises."""
    try:
        return engine.account_state()
    except Exception:
        return {}
