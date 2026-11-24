#!/usr/bin/env python3
"""Graduation RAW lane (2026-10-23, user-ordered): the friend's exact
game, measured head-to-head against the gated grad lane.

The user's words: "Build the every-graduation lane now — his coins
can't be missed." His friend's bot buys at graduation with NO safety
gate and has produced the wins the user keeps bringing (KIMCHI,
KWH). The gated grad lane (grad.py) can never answer whether the
gate helps or hurts, because it never buys what the gate blocks.
This lane is the other half of the experiment:

- Buys every pump.fun graduation THAT PASSES grad_select's four
  evidence filters (serial deployer, fast curve / few trades,
  canonical pools only, copycat tickers) — the shared filter code
  in grad_select.py, imported, never copied. NO safety check, no
  veto, no runner rule, no score: the filters are SELECTION, not
  safety. Before 2026-11-10 this lane bought EVERY graduation
  unfiltered; the filter port (user-ordered, "Build the raw
  filter") gates ENTRIES ONLY — exits, stakes, dedupe, killswitch
  are untouched, so filtered raw is grad_select entries with raw's
  exits (an exits A/B, not a new edge).
- Detection is the LIVE PumpPortal migration stream (the same feed
  the main scanner receives graduations on in real time), NOT
  GeckoTerminal polling — GT indexed KWH's pool ~10 minutes after
  graduation, which is how the gated lane's window expired on it.
  A GT newest-pools backfill (30-min age window) covers stream gaps
  (reboots); dedupe-ever makes the overlap harmless, and every entry
  is stamped with its detection source so the ledger separates
  stream-fresh entries from backfilled ones.
- Everything else is the SAME machinery as the gated lane, so the
  two ledgers differ ONLY in the gate: $1 paper shots, honest
  (adverse) fills, the user's exit ladder, one entry per mint ever,
  $100 paper bankroll as the only volume limit (NO daily entry cap).
  Positions in unsellable traps close at 0 — that is the true price
  of his game, and measuring it honestly is the point.
- PRE-REGISTERED VERDICT (stamped into the ledger header): at 100
  closes, mean pnl per close <= 0 halts the lane permanently
  (killswitch lane "grad_raw", human-only re-enable); > 0 survives.
- FIRE-FIRST entries (2026-10-23, user order "make our bot fire
  1st too"): stream-detected candidates are priced from the
  migration transaction itself (see the FIRE-FIRST section below)
  and entered by a dedicated stream-wake thread in the daemon, so a
  graduation is entered at the migration instant instead of after
  the tape + DexScreener quote round-trips. The live-quote path
  remains as the fallback for any candidate the migration tx
  cannot price, and every payload-priced entry is verified against
  a live quote shortly after entry.

Implementation: grad.py's engine (ledger, fills, ladder, verdict)
loaded as an ISOLATED module instance via importlib, with lane
policy overridden on that instance only. The gated lane's module,
daemon, and ledger are untouched — the two lanes run in separate
processes with separate state. Exits run in this lane's own daemon
loop (~5s cycle); rt_sync is deliberately NOT shared, because
rt_sync hosts the gated engine instance.

Paper only. Nothing here touches real money, keys, or wallets.
Free-only: PumpPortal public stream + GeckoTerminal/DexScreener.
"""
import importlib.util
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

# Transaction-outcome guards (2026-11-17, Batch 3): meta.err
# gating (tx_guards.py). REQUIRED, not fail-soft: the migration
# tx price refuses reverted txs through it, so a missing module
# must fail loud at trader startup instead of silently pricing
# phantom entries from dead txs.
try:
    import tx_guards as _txg
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import tx_guards as _txg
    except Exception:
        raise

logger = logging.getLogger("forex_agent.daemon.launch_scanner.grad_raw")

try:
    import rt_sync as _rt_journal  # P7 reconciliation journal (fail-soft)
except Exception:  # pragma: no cover
    _rt_journal = None

# The four evidence filters (2026-11-10 filter port, user-ordered):
# grad_select's filter code, imported and SHARED — never copied —
# so the two lanes cannot drift. Prefer an already-loaded module
# object (package import in tests) over a fresh flat import, so the
# recorder index is one shared index, not two. Fail-closed: a
# missing module rejects every entry (see _filter_verdict).
try:
    _select = sys.modules.get("daemon.launch_scanner.grad_select") \
        or sys.modules.get("grad_select")
    if _select is None:
        try:
            import grad_select as _select
        except Exception:
            from daemon.launch_scanner import grad_select as _select
except Exception:  # pragma: no cover
    _select = None

# Decision memory (2026-11-17): fail-soft import — a missing
# module leaves entry behaviour byte-identical. Lessons are
# context only, never a filter.
try:
    import lessons as _lessons
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import lessons as _lessons
    except Exception:
        _lessons = None  # type: ignore

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
        logger.warning("grad_raw feed STALE (stream down)")
        return
    down = _FEED_STATE["down_since"]
    _FEED_STATE["down_since"] = None
    logger.warning("grad_raw feed LIVE again")
    if down is not None and _rt_journal is not None:
        try:
            _rt_journal.journal_reconciliation({
                "ts": now, "source": "grad-raw-feed",
                "gap_s": round(now - down, 1),
                "note": "migration stream reconnected; GT backfill "
                        "covers the gap window"})
        except Exception:
            pass

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "grad_raw_ledger.json")
LANE = "grad_raw"
CHAIN = "solana"

BANKROLL_START = 100.00
NOTIONAL_USD = 25.00     # CEO-ordered 2026-10-24: $1 -> $25/shot (paper only)
MAX_OPEN = 150            # headroom only — the bankroll binds first
DAILY_ENTRY_CAP = 100000  # NO daily cap by design; bankroll is the limit
MAX_GRAD_AGE_S = 1800.0   # 30-min window (backfill must survive GT lag)
VERDICT_SAMPLE_CLOSES = 100

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")


# ---- engine (isolated grad.py instance with raw policy) -----------------

def _no_safety_gate(mint):
    """The defining policy of this lane: there is NO safety gate.
    Signature matches grad._safety_ok. Returns a pass with no report
    — the entry snapshot therefore honestly shows no safety data."""
    return True, "", None, False


def _no_veto(mint, symbol, safety_report):
    """No veto engine on this lane. Signature matches grad._veto_ok."""
    return True, ""


def _preregistration():
    """Verdict terms for the RAW lane, stamped into its ledger header
    at creation (same anti-quiet-change rule as grad.py)."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'grad_raw'); "
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
            "safety_gate": "NONE (selection is the four evidence "
                           "filters below, not a safety gate)",
            "veto": "NONE",
            "filters": {
                "serial_deployer": "reject if deployer launches in "
                                   "trailing 24h >= 3 (recorder "
                                   "birth log)",
                "fast_curve_few_trades": "reject unless TTG <= 3600s "
                                         "and curve signatures <= 250",
                "canonical_only": "migration-event pools (stream) or "
                                  "recorder-migration pool match "
                                  "(backfill) only",
                "copycat_ticker": "reject if other same-ticker "
                                  "launches in trailing 24h >= 3",
            },
            "detection": "PumpPortal migration stream (live) + "
                         "GeckoTerminal backfill (30-min window)",
            "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h",
        },
        "registered_ts": time.time(),
    }


def make_engine():
    """Load grad.py as a fresh, isolated module and override its lane
    policy for the RAW lane. The gated lane's own module instance is
    never touched. Never raises on import shape — grad.py's deps are
    resolved by its own import block."""
    spec = importlib.util.spec_from_file_location("grad_raw_engine",
                                                  _GRAD_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    eng.LEDGER_PATH = LEDGER_PATH
    eng.LANE = LANE
    eng.NOTIONAL_USD = NOTIONAL_USD
    eng.MAX_OPEN = MAX_OPEN
    eng.DAILY_ENTRY_CAP = DAILY_ENTRY_CAP
    eng.MAX_GRAD_AGE_S = MAX_GRAD_AGE_S
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
            # Event state (2026-11-17, zero-RPC entry): the
            # pumpportal event's own fields ride along so the
            # evaluator can use them instead of re-reading the
            # curve. state_from_event is the parser's completeness
            # flag. The birth event's virtual reserves are NOT
            # carried here (the migration event never has them) —
            # they live in the birth record (grad_select index).
            "state_from_event": bool(item.get("state_from_event")),
            "market_cap_sol": extra.get("market_cap_sol"),
            "bonding_curve": str(extra.get("bonding_curve") or ""),
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


def _deployer_band(n):
    """24h launch-count band for the deployer forward test (2026-11-20).

    "0" = brand-new deployer, "1-2" = 1-2 launches, "3+" = 3 or
    more, "unknown" = the count could not be read (fail-closed:
    never defaulted to "0", so an unknown never farms the 0 band).
    Pure function; journal-only, never gates an entry."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "unknown"
    if n < 0:
        return "unknown"
    if n == 0:
        return "0"
    if n <= 2:
        return "1-2"
    return "3+"


def _stamp_detection(engine, mint, source, ff=None, values=None):
    """Record HOW this entry was detected (stream vs backfill) and
    that no gate applied, on the open position — so the two entry
    classes can be scored separately at verdict time. `ff` (optional)
    stamps the FIRE-FIRST price provenance: entry_price_source plus
    the payload price fields, or the fallback reason when the
    instant path was attempted but the live quote priced the entry.
    `values` (optional) carries the passed select-filter
    measurements: gate is stamped "select" with the values, so a
    filtered entry is auditable against the four filters; without
    it the stamp stays "none" (pre-filter entries)."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = d["positions"].get(mint)
            if pos is not None:
                pos["source"] = "graduation-raw"
                ef = pos.setdefault("entry_features", {})
                ef["detection"] = source
                ef["gate"] = "select" if values is not None else "none"
                if values is not None:
                    ef["select"] = dict(values)
                    # A+B forward paper test (2026-11-17): instrument
                    # ONLY — same arm as grad_select. "A+B" when the
                    # deployer had >=1 launch in the trailing 24h, else
                    # "A". Never gates an entry; the verdict report reads
                    # the arm off closes (entry_features.ab_arm).
                    try:
                        _n24 = int(values.get(
                            "deployer_launches_24h") or 0)
                    except (TypeError, ValueError):
                        _n24 = 0
                    ef["ab_arm"] = "A+B" if _n24 >= 1 else "A"
                    # Deployer forward test (2026-11-20): instrument
                    # ONLY — same band as grad_select, off the SAME
                    # count Filter 1 already computed. Never gates
                    # an entry; the report reads
                    # entry_features.deployer_band off closes.
                    ef["deployer_band"] = _deployer_band(
                        values.get("deployer_launches_24h"))
                    # Conviction (advanced build, piece 1): the
                    # score that admitted this entry rides into the
                    # close so the gate can be judged on real
                    # paper closes.
                    if isinstance(values.get("conviction"), dict):
                        ef["conviction"] = dict(
                            values["conviction"])
                # Venue (advanced build, piece 8, 2026-11-21):
                # which pool/venue this entry traded on, and
                # whether the pool was verified. Journal-only;
                # never gates. The raw lane does not run
                # CanonicalPoolCheck, so an unverified venue here
                # is honest, not a defect.
                if _select is not None:
                    try:
                        ef["venue"] = _select._venue_for(values, pos, ff)
                    except Exception:
                        pass
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
                # Decision memory (2026-11-17): merge the select
                # measurements into the pending decision logged at
                # entry. Journal only; never gates.
                try:
                    if _lessons is not None:
                        _lessons.log_decision(
                            LANE, mint, pos["entry_ts"],
                            _lessons.extract_features(ef, pos),
                            symbol=pos.get("symbol"),
                            home=os.path.dirname(os.path.abspath(
                                engine.LEDGER_PATH)))
                except Exception:
                    pass
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
# Canonical wSOL mint (Phase 2): the single source of truth is
# daemon/solana_ids.py — never redefine the literal here.
try:
    from daemon import solana_ids as _solana_ids
except ImportError:  # bare-file import without the repo root on path
    import os as _os, sys as _sys
    _root = _os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from daemon import solana_ids as _solana_ids
_WSOL = _solana_ids.WSOL_MINT


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
        # meta.err gate (2026-11-17, Batch 3): a reverted
        # migration tx still carries token balances — pricing it
        # would invent a phantom entry price. Refuse reverted
        # AND unknown (missing meta) txs alike; the caller falls
        # back to the free quote path.
        if not _txg.tx_succeeded(res):
            return None, "migration tx %s" % (
                _txg.tx_reverted_reason(res) or "tx_unknown")
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
            # v1: current-chain txs are version 1 (live census
            # 2026-11-23: recent pump txs fail v0 with RPC -32015);
            # v0 options would misreport them as fetch failures.
            "maxSupportedTransactionVersion": 1}])
    except Exception as e:
        return None, "migration tx fetch failed: %s" % e
    if not isinstance(res, dict):
        return None, "migration tx not available yet"
    from daemon.launch_scanner import tx_guards as source_guard
    if not source_guard.response_signature_matches(res,sig):
        return None, "migration tx response signature mismatch"
    t.join(timeout=6)
    info, why = _ff_parse_tx_price(res, mint,
                                   sol_usd=holder.get("sol", 0.0))
    # Chain age (2026-11-17, SlotClock): the migration tx's slot
    # rides on the price info so the entry loop can measure Rule A
    # from the create, not from detection — same 30s bar, zero
    # extra RPC (the tx was already fetched for pricing).
    if isinstance(info, dict):
        slot = res.get("slot")
        if isinstance(slot, int) and slot > 0:
            info["slot"] = slot
    return info, why


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
        return {"mint": mint, "info": info, "why": "",
                # Chain age (2026-11-17, SlotClock): the migration
                # tx's slot, for the entry loop's chain-age gate.
                # None when the tx fetch did not report one.
                "migration_slot": info.get("slot")
                if isinstance(info.get("slot"), int) else None}
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
            logger.warning("grad_raw payload verify %s: %s "
                           "(payload $%.8f, live $%.8f)",
                           (mint or "?")[:12], verdict["status"],
                           payload, live)
    except Exception:
        pass
    finally:
        _FF_VERIFY_LOCK.release()


def _filter_verdict(engine, cand, now):
    """One select-filter verdict for a raw candidate, via the
    SHARED grad_select code path (evaluate_candidate) — the same
    four evidence filters, thresholds, and fail-closed behavior
    grad_select runs. A missing select module rejects everything:
    the lane must never silently run unfiltered again. Never
    raises."""
    if _select is None:
        return {"verdict": "reject", "reason": "unknown_select",
                "values": {}}
    try:
        return _select.evaluate_candidate(engine, cand, now, lane=LANE)
    except Exception as e:
        return {"verdict": "reject", "reason": "unknown_error",
                "values": {"error": str(e)[:120]}}


def scan_once(engine, feed=None, gt_rows=None, now=None,
              on_items=None, fresh_only=False):
    """One detection cycle for the RAW lane: drain the migration
    stream, merge stream + GT-backfill candidates into the engine
    watchlist, and attempt a no-gate entry for everything still in
    its age window. `on_items` (P8) receives the raw drained stream
    items (births + migrations) for observers like the dossier
    builder. `fresh_only` (FIRE-FIRST: the daemon's stream-wake
    thread) restricts entry attempts to candidates drained in THIS
    call — watchlist retries stay the main loop's business, at the
    main loop's cadence. Returns counters. Never raises."""
    out = {"stream": 0, "pools": 0, "watching": 0, "opened": 0,
           "skipped": 0, "dead": 0, "select_rejected": 0,
           "select_reasons": {}}
    try:
        return _scan_once(engine, feed, gt_rows, out, now, on_items,
                          fresh_only)
    except Exception as e:
        logger.warning("grad_raw scan_once failed: %s", e)
        return out


def _scan_once(engine, feed, gt_rows, out, now, on_items=None,
               fresh_only=False):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state
    # error — the old is_halted() escaped malformed state as a
    # KeyError the except: pass swallowed, so entry proceeded.
    if not engine._ks.gate(LANE):
        logger.warning("grad_raw scan: lane halted by killswitch "
                       "— no entries (exits still run)")
        return out

    # Scan schedule (advanced build, piece 9, 2026-11-21): the bot
    # decides when scanning is worth it. REST/REDUCED skips the
    # scan portion of this tick only — exits still run (they are
    # outside _scan_once). The fire-first entry loop
    # (fresh_only=True) is never gated: a fresh graduation in the
    # queue is the opposite of a rest. A scan carrying GT backfill
    # rows (gt_rows) is never gated either: the backfill is a
    # 60s-cadence, age-sensitive path (Rule A: age <= 30s) on a
    # different vendor — skipping it would discard those rows for
    # the cycle and lose fresh entries.
    if not fresh_only and not gt_rows and _select is not None:
        try:
            _sched = _select._scan_schedule_check(engine, feed, now)
            if not _sched.get("scan", True):
                out["scan_skipped"] = str(
                    _sched.get("reason") or "schedule")
                out["scan_mode"] = str(_sched.get("mode") or "REST")
                return out
        except Exception:
            pass

    # Sit-out gate (advanced build, piece 2, shared helper): a
    # cold, bleeding lane does not scan for entries — the filter
    # RPC is not spent on candidates that will not enter. Exits
    # still run.
    try:
        if _select is not None:
            _sitting, _so_info = _select._sit_out_check(engine, now)
            if _sitting:
                out["sit_out"] = str(
                    _so_info.get("reason") or "cold_bleed")
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
            # Feed the shared select filter's recorder index live
            # (the same call grad_select's scan makes): the serial /
            # canonical checks read this index in-cycle, instead of
            # waiting on the recorder file flush.
            if _select is not None:
                try:
                    _select.note_items(items)
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

    # Scan-schedule signal (advanced build, piece 9): the stream
    # count of this scan is the fresh-graduation signal the NEXT
    # schedule check reads.
    if _select is not None:
        try:
            _select._note_scan_stream(LANE, out.get("stream", 0))
        except Exception:
            pass

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

    # ---- filter stage (2026-11-10 filter port): grad_select's four
    # evidence filters gate ENTRIES ONLY — exits, stakes, dedupe,
    # killswitch untouched. One verdict per mint via the SHARED
    # grad_select code path, cached on the row; rejects leave the
    # watchlist for dead_mints with the reason and are journaled
    # in the ledger's select_journal (same shape as grad_select).
    # Fail-closed: any candidate the filters cannot measure is
    # rejected, never entered.
    # Speed (2026-11-16, Phase 1 — same as grad_select): the
    # evaluations are I/O-bound RPC work, so unevaluated
    # candidates run in parallel (ThreadPoolExecutor, 4 workers)
    # and ALL ledger effects land in ONE batched read-modify-write
    # at the end of the stage (verdict caching + journal + pops
    # used to cost a full ledger read+write EACH). Workers never
    # touch the ledger; only this thread writes, inside one locked
    # section. The evaluation semaphore is the SHARED
    # grad_select._EVAL_SEM, so both lanes together never put
    # more than 4 RPC-heavy evaluations on the free-tier quota.
    passed = []
    decided = []
    to_eval = []
    for cand in watch:
        mint = str(cand.get("mint") or "")
        verdict = cand.get("select")
        if fresh_only and mint not in fresh_mints and \
                not isinstance(verdict, dict):
            # The stream-wake thread evaluates ONLY this cycle's
            # fresh candidates; unevaluated watchlist retries are
            # the main loop's business, at the main loop's cadence
            # (each evaluation can cost RPC round-trips).
            continue
        if isinstance(verdict, dict) and \
                verdict.get("verdict") in ("pass", "reject", "review"):
            decided.append((cand, verdict))
        else:
            to_eval.append(cand)
    evaluated = []
    if to_eval:
        def _eval_one(cand):
            if _select is not None:
                with _select._EVAL_SEM:
                    return _filter_verdict(engine, cand, now)
            return _filter_verdict(engine, cand, now)

        if len(to_eval) == 1:
            evaluated = [(to_eval[0], _eval_one(to_eval[0]))]
        else:
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=4) as ex:
                    verdicts = list(ex.map(_eval_one, to_eval))
                evaluated = list(zip(to_eval, verdicts))
            except Exception:
                # Executor failure must not lose the candidates:
                # fall back to sequential evaluation.
                evaluated = [(c, _eval_one(c)) for c in to_eval]
    results = decided + evaluated
    if results:
        with engine._ledger_lock():
            d = engine._read()
            for cand, verdict in results:
                mint = str(cand.get("mint") or "")
                row = d["watchlist"].get(mint)
                if row is not None:
                    row["select"] = verdict
                cand["select"] = verdict
                if verdict["verdict"] == "reject":
                    out["select_rejected"] += 1
                    reason = str(verdict.get("reason") or "unknown")
                    out["select_reasons"][reason] = \
                        out["select_reasons"].get(reason, 0) + 1
                    if _select is not None:
                        try:
                            _select._journal_rejection_in(
                                d, cand, verdict, now)
                        except Exception:
                            pass
                    d["watchlist"].pop(mint, None)
                    d["dead_mints"][mint] = {
                        "ts": now, "reason": "select: %s" % reason}
                    continue
                if verdict["verdict"] == "review":
                    # REVIEW sentinel (advanced build, piece 1): a
                    # WEAK candidate never enters — it is already
                    # in the review queue (enqueued at evaluation);
                    # pop it from the watchlist and mark it dead so
                    # it is not re-evaluated or entered later.
                    out["select_reviewed"] = \
                        out.get("select_reviewed", 0) + 1
                    d["watchlist"].pop(mint, None)
                    d["dead_mints"][mint] = {
                        "ts": now,
                        "reason": "review: conviction_review"}
                    continue
                passed.append(cand)
            engine._write(d)

    # ---- Rule A entry-age re-check (Item 6, shared grad_select
    # helper): a cached "pass" was earned at evaluation time;
    # re-check the age at entry time before any entry RPC is spent
    # (fire-first contexts build next).
    if _select is not None:
        passed = _select.recheck_entry_age(passed, engine, out)

    ff_ready = getattr(engine, "_ff_installed", False)
    # FIRE-FIRST: entry contexts for this cycle's stream candidates
    # are built BEFORE the consider loop, in parallel when a drain
    # brings a burst — each build is one RPC round-trip and serial
    # builds would re-queue the burst behind its own fetches.
    ff_ctxs = {}
    if ff_ready and fresh_mints:
        todo = [c for c in passed
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
    for cand in passed:
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
                logger.warning("grad_raw fire-first fallback %s: %s",
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
                             ff=ff,
                             values=(cand.get("select") or {})
                             .get("values"))
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


# 2x CONFIRMATION CARDS (user-ordered 2026-10-29, "Do the Needful")
#
# One Telegram card per grad_raw position the moment its paper ladder
# banks leg A (50% at 2x). NOTIFICATION ONLY: this section never touches
# entries, exits, stakes, or killswitch state, and every failure here
# logs and returns — the trading loop never breaks on a card.
#
# Same trigger as grad_select, three lane differences:
#   * CAP: max 10 grad_raw cards per WAT day. The spray lane banks far
#     more 2x legs than the filtered lane; the first 10 send, the rest
#     are journaled as capped-dropped and never retried stale.
#   * LABEL: every card says grad_raw, so it is never mistaken for a
#     filtered grad_select winner.
#   * HONESTY: grad_raw has NO filters — the card says so. A 2x here
#     means less than a 2x on the filtered lane.
# Dedup: CONFIRM_CARDS_JOURNAL maps mint -> sent record. The first run
# backfills every truly banked leg A as pre-existing (no flood of
# stale cards). A failed send leaves the mint unjournaled (retry).
# Leg A carries no completion timestamp, so the checker also scans
# recently finalized closes (close_ts >= last_check_ts) to catch a
# position that blew through the whole ladder between checks.
# A close counts as banked ONLY on legs.a.exit_reason == 'leg_a_2x':
# close records mark every leg done at finalization, so done alone
# would card every -50% stop and time-stop as a false 2x
# (2026-10-29: exactly that fired one false grad_select card).
# ---------------------------------------------------------------------------
CONFIRM_CARDS_JOURNAL = os.path.join(HOME, "grad_raw_confirm_cards.json")
CONFIRM_CARD_CHAT_ID = "6883056760"
_CONFIRM_CHECK_EVERY_S = 30.0
CONFIRM_CARDS_PER_DAY = 10
# PERMANENT KILL (user order 2026-10-29 ~01:09 WAT: "don't want that
# card... never again", after two true 2x cards fired 01:01:56 WAT).
# The checker returns immediately while this is False: no sends, no
# journal writes. Trading (entries, exits, killswitch) is untouched.
# Revival only on the user's explicit order, with the record stated.
_CARDS_ENABLED = False
_confirm_last_run = 0.0


def _compact_usd(value):
    """32966.64 -> '$33.0K'; None/unparseable -> '?' (never guessed)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    if v <= 0:
        return "?"
    for bound, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= bound:
            return "$%.1f%s" % (v / bound, suffix)
    return "$%.0f" % v


def _wat_ts(ts):
    """Epoch -> 'HH:MM WAT' (Africa/Lagos); '?' when unknown."""
    try:
        from datetime import datetime, timezone
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Africa/Lagos")
        except Exception:
            tz = timezone.utc
        return datetime.fromtimestamp(float(ts), tz=tz).strftime("%H:%M WAT")
    except Exception:
        return "?"


def _wat_day(now):
    """Epoch -> 'YYYY-MM-DD' in Africa/Lagos (UTC+1, no DST)."""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(now + 3600))
    except Exception:
        return "?"


def _ds_token_info(mint):
    """Fresh DexScreener context for a mint. Fail-soft: {} on any error."""
    try:
        req = urllib.request.Request(
            "https://api.dexscreener.com/latest/dex/tokens/%s" % mint,
            headers={"User-Agent": "grad-raw-confirm-cards"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())
        pairs = [p for p in (data.get("pairs") or []) if isinstance(p, dict)]
        if not pairs:
            return {}
        p = max(pairs, key=lambda q: float(
            ((q.get("liquidity") or {}).get("usd") or 0) or 0))
        return {
            "symbol": (p.get("baseToken") or {}).get("symbol"),
            "mcap": p.get("fdv") or p.get("marketCap"),
            "liq": (p.get("liquidity") or {}).get("usd"),
        }
    except Exception:
        return {}


def _leg_a_mult(pos):
    """Leg-A banked multiple (exit/entry); 2.0 fallback, never invented."""
    try:
        entry = float(pos.get("entry_price") or 0)
        exit_px = float(((pos.get("legs") or {}).get("a") or {})
                         .get("exit_price") or 0)
        if entry > 0 and exit_px > 0:
            return exit_px / entry
    except Exception:
        pass
    return 2.0


def _leg_a_banked(rec):
    """True ONLY when leg A genuinely banked at 2x (see header note:
    close records mark every leg done at finalization, so the proof
    is legs.a.exit_reason == 'leg_a_2x', never done alone)."""
    try:
        leg = (rec.get("legs") or {}).get("a") or {}
        return bool(leg.get("done")) and \
            leg.get("exit_reason") == "leg_a_2x"
    except Exception:
        return False


def render_confirmation_card(pos, fresh=None):
    """Card text for a grad_raw 2x confirmation. Never guesses data:
    unknown ticker -> mint prefix; unknown stats -> '?'. Labeled
    grad_raw and honest about the lane having no filters."""
    fresh = fresh or {}
    mint = pos.get("mint") or "?"
    symbol = pos.get("symbol") or fresh.get("symbol")
    if not symbol or symbol == "?":
        symbol = mint[:8] + "…"
    snap = pos.get("entry_snapshot") or {}
    mcap = fresh.get("mcap") or snap.get("mcap")
    liq = fresh.get("liq") or snap.get("liquidity")
    mult = _leg_a_mult(pos)
    entry_px = float(pos.get("entry_price") or 0)
    lines = [
        "✅ 2x CONFIRMED — grad_raw",
        "%s" % symbol,
        "`%s`" % mint,
        "💰 Paper leg banked: 50%% at %.2fx" % mult,
    ]
    if entry_px > 0:
        lines.append("   entry $%s" % ("%.8g" % entry_px))
    lines += [
        "📊 Mcap %s ⋅ Liq %s" % (_compact_usd(mcap), _compact_usd(liq)),
        "🕐 Entered %s · carded %s" % (
            _wat_ts(pos.get("entry_ts")), _wat_ts(time.time())),
        "🌊 No filters on this lane — buys every graduation, "
        "no selection.",
        "⚠️ No safety audit on this lane — no filters, no safety pass.",
        "This is a confirmation, not an entry: paper entered at "
        "graduation; a manual buy now starts near 2x.",
    ]
    return "\n".join(lines)


def send_confirmation_card(text, mint):
    """Direct Telegram API send, mirroring degen-feed's send_card:
    native Copy CA button + 'I bought this' tap (source code 'gr',
    resolved by the daemon's tap parser to 'grad-raw-confirm').
    Returns True on Telegram ok. Never raises."""
    try:
        with open(os.path.join(HOME, "telegram", "bot_token")) as f:
            token = f.read().strip()
        cb = None
        try:
            from signal_bus import track_callback
            cb = track_callback("solana", "grad-raw-confirm", mint)
        except Exception:
            try:
                from daemon.launch_scanner.signal_bus import track_callback
                cb = track_callback("solana", "grad-raw-confirm", mint)
            except Exception:
                cb = None
        if not cb:
            cb = "track:solana:gr:%s" % mint
        keyboard = json.dumps({"inline_keyboard": [[
            {"text": "📋 Copy CA", "copy_text": {"text": mint}},
            {"text": "📍 I bought this", "callback_data": cb},
        ]]})
        payload = {"chat_id": CONFIRM_CARD_CHAT_ID, "text": text,
                   "reply_markup": keyboard}
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return bool(json.loads(r.read().decode()).get("ok", False))
    except Exception as e:
        logger.warning("grad-raw confirm card send failed: %s", e)
        return False


def _journal_load():
    """Returns (journal, missing). A corrupt journal returns
    (None, False): fail-closed, the run sends nothing rather than
    re-carding history."""
    try:
        with open(CONFIRM_CARDS_JOURNAL) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("journal not a dict")
        d.setdefault("sent", {})
        d.setdefault("sent_by_day", {})
        return d, False
    except FileNotFoundError:
        return None, True
    except Exception as e:
        logger.warning("grad-raw confirm journal unreadable: %s", e)
        return None, False


def _journal_save(journal):
    tmp = CONFIRM_CARDS_JOURNAL + ".tmp"
    with open(tmp, "w") as f:
        json.dump(journal, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIRM_CARDS_JOURNAL)


def _scan_leg_a_done(ledger):
    """{mint: pos-like record} for every open position and close whose
    leg A truly banked. Never raises."""
    out = {}
    try:
        for mint, pos in (ledger.get("positions") or {}).items():
            if _leg_a_banked(pos):
                out[mint] = pos
    except Exception:
        pass
    try:
        for c in (ledger.get("closes") or []):
            if not isinstance(c, dict) or not c.get("mint"):
                continue
            if _leg_a_banked(c):
                out.setdefault(c["mint"], c)
    except Exception:
        pass
    return out


def maybe_confirmation_cards(engine, now=None):
    """Send one 2x confirmation card per newly banked grad_raw leg A,
    capped at CONFIRM_CARDS_PER_DAY per WAT day. Returns [mints
    carded]. Never raises; never touches trading state."""
    global _confirm_last_run
    if not _CARDS_ENABLED:
        return []  # permanently killed on the user's order, see above
    try:
        now = now if now is not None else time.time()
        journal, missing = _journal_load()
        if journal is None and not missing:
            return []  # corrupt journal: fail-closed, send nothing
        if missing:
            # First run: seed every truly banked leg A as pre-existing.
            try:
                ledger = engine._load()
            except Exception:
                return []
            done = _scan_leg_a_done(ledger)
            journal = {"sent": {m: {"preexisting": True, "seen_ts": now}
                                for m in done},
                       "sent_by_day": {}, "last_check_ts": now}
            try:
                _journal_save(journal)
            except Exception as e:
                logger.warning(
                    "grad-raw confirm journal seed failed: %s", e)
            return []
        if now - _confirm_last_run < _CONFIRM_CHECK_EVERY_S:
            return []
        _confirm_last_run = now
        try:
            ledger = engine._load()
        except Exception as e:
            logger.warning(
                "grad-raw confirm cards ledger read failed: %s", e)
            return []
        sent = journal.get("sent") or {}
        sent_by_day = journal.get("sent_by_day") or {}
        day = _wat_day(now)
        sent_today = int(sent_by_day.get(day) or 0)
        last_check = float(journal.get("last_check_ts") or 0)
        fresh_done = {}
        for mint, pos in (ledger.get("positions") or {}).items():
            if _leg_a_banked(pos) and mint not in sent:
                fresh_done[mint] = pos
        for c in (ledger.get("closes") or []):
            if not isinstance(c, dict) or not c.get("mint"):
                continue
            if c["mint"] in sent or c["mint"] in fresh_done:
                continue
            if not _leg_a_banked(c):
                continue
            if float(c.get("close_ts") or 0) < last_check:
                continue
            fresh_done[c["mint"]] = c
        carded = []
        for mint, pos in fresh_done.items():
            if sent_today >= CONFIRM_CARDS_PER_DAY:
                # Cap reached: drop, journaled as capped so it is
                # never retried stale on a later day.
                sent[mint] = {"capped": True, "seen_ts": now}
                logger.warning(
                    "grad-raw confirm card capped (10/day): %s", mint)
                continue
            try:
                text = render_confirmation_card(pos, _ds_token_info(mint))
                if send_confirmation_card(text, mint):
                    sent[mint] = {"sent_ts": now, "mult": _leg_a_mult(pos),
                                  "symbol": pos.get("symbol")}
                    sent_today += 1
                    carded.append(mint)
                else:
                    logger.warning(
                        "grad-raw confirm card unsent (retry next "
                        "run): %s", mint)
            except Exception as e:
                logger.warning(
                    "grad-raw confirm card failed for %s: %s", mint, e)
        journal["sent"] = sent
        journal["sent_by_day"] = dict(sent_by_day, **{day: sent_today})
        journal["last_check_ts"] = now
        try:
            _journal_save(journal)
        except Exception as e:
            logger.warning(
                "grad-raw confirm journal save failed: %s", e)
        return carded
    except Exception as e:
        logger.warning("grad-raw maybe_confirmation_cards failed: %s", e)
        return []
