#!/usr/bin/env python3
"""Mimic lane (2026-11-01, user-ordered): the traced outside bot's
launch playbook, measured on paper.

The outside bot (traced 2026-11-01 across two coins) buys in the
first second of a launch, in a pack of about four wallets at about
20 SOL each, with priority fees, firing through failure, and exits
fully. Its edge is speed: it is THERE at launch. This lane tests
the closest honest paper version of that edge:

- Detection is the LIVE PumpPortal new-token stream (every pump.fun
  token creation, free stream) — NOT graduations. The lane buys at
  launch, not at graduation.
- Entries price from the LIVE QUOTE at decision time, with honest
  (adverse) fills — deliberately NOT from the creation price. Buying
  at a stale creation price seconds after the real market moved
  would flatter the lane with a price no real trader could get.
  The lane therefore measures launch-FAST entries, and its verdict
  judges whether that is close enough to capture the edge.
- Speed discipline is enforced, not assumed: a stream-wake thread
  (like grad_raw's) attempts fresh candidates the moment the stream
  delivers, so entries never wait behind a scan cycle; and the
  120-second chase guard drops any candidate that waited for a slot
  or a quote and only gets its decision later. Stale entries are
  final-dropped, never retried at a later price.
- NO safety gate, NO veto, NO filters — the outside bot buys
  without them, and the point is to measure its game, not a
  filtered copy of it. NO cards in v1. NO backfill: a launch found
  late is stale by definition, so stream gaps mean missed launches,
  not late entries.
- Everything else is the SAME machinery as the grad lanes, so the
  verdict is comparable: $25 paper shots, the user's standard
  exit ladder (50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h),
  one entry per mint ever, $500 paper bankroll, 20 open slots.
- Exits are the PROVISIONAL standard ladder. The whales' real sell
  timing and multiples are still unread (their transaction fetch
  is running); the exit leg must not be described as copied from
  the whales until their sells are actually read. When that data
  lands, the exit leg is re-designed from it and reported.
- PRE-REGISTERED VERDICT (stamped into the ledger header): at 100
  closes, mean pnl per close <= 0 halts the lane permanently
  (killswitch lane "mimic", human-only re-enable); > 0 survives.

Implementation: grad.py's engine (ledger, fills, ladder, verdict)
loaded as an ISOLATED module instance via importlib, with lane
policy overridden on that instance only — the same pattern as
grad_raw, tranche, and the other test lanes. No other lane's
module, daemon, or ledger is touched. Exits run in this lane's own
daemon loop (~5s cycle) via the engine's live-quote price path.

Paper only. Nothing here touches real money, keys, or wallets.
Free-only: PumpPortal public stream + DexScreener/tape quotes.
"""
import importlib.util
import logging
import os
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.mimic")

# Curve-direct pricing (fail-soft: a missing module never stops the
# lane — entries fall back to the base live quote, exactly as
# before this path existed).
try:
    import mimic_curve as _curve
except Exception:
    _curve = None

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "mimic_ledger.json")
LANE = "mimic"
CHAIN = "solana"

BANKROLL_START = 500.00
NOTIONAL_USD = 25.00
MAX_OPEN = 20
DAILY_ENTRY_CAP = 100000  # NO daily cap by design; bankroll is the limit
MAX_LAUNCH_AGE_S = 120.0  # launch window: a launch older than this
# is stale by definition (the chase guard below enforces the same
# bound on the decision, so the two can never disagree).
CHASE_GUARD_S = 120.0     # max seconds since first sighting to entry
VERDICT_SAMPLE_CLOSES = 100

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")


# ---- engine (isolated grad.py instance with mimic policy) -----------

def _no_safety_gate(mint):
    """No safety gate on this lane, by design (the outside bot buys
    without one). Signature matches grad._safety_ok."""
    return True, "", None, False


def _no_veto(mint, symbol, safety_report):
    """No veto engine on this lane. Signature matches grad._veto_ok."""
    return True, ""


def _preregistration():
    """Verdict terms for the MIMIC lane, stamped into its ledger
    header at creation (same anti-quiet-change rule as grad.py)."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'mimic'); "
                 "if > 0 the lane survived its sample. Re-enable is "
                 "human-only." % VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": None,  # none — bankroll-bound by design
            "max_launch_age_s": MAX_LAUNCH_AGE_S,
            "chase_guard_s": CHASE_GUARD_S,
            "runner_rule_applies": False,
            "safety_gate": "NONE (user-ordered copy of the traced "
                           "outside bot's launch game)",
            "veto": "NONE",
            "filters": "NONE (unfiltered launch spray)",
            "detection": "PumpPortal new-token stream (live only, "
                         "no backfill)",
            "entry_price": "live quote at decision time, honest "
                           "adverse fill (NOT the stale creation "
                           "price)",
            "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / "
                      "24h (PROVISIONAL standard ladder — the "
                      "whales' real exits are still unread)",
            "cards": "NONE in v1",
        },
        "registered_ts": time.time(),
    }


def make_engine():
    """Load grad.py as a fresh, isolated module and override its lane
    policy for the MIMIC lane. The other lanes' module instances are
    never touched. Never raises on import shape — grad.py's deps are
    resolved by its own import block."""
    spec = importlib.util.spec_from_file_location("mimic_engine",
                                                  _GRAD_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    eng.LEDGER_PATH = LEDGER_PATH
    eng.LANE = LANE
    eng.BANKROLL_START = BANKROLL_START
    eng.NOTIONAL_USD = NOTIONAL_USD
    eng.MAX_OPEN = MAX_OPEN
    eng.DAILY_ENTRY_CAP = DAILY_ENTRY_CAP
    eng.MAX_GRAD_AGE_S = MAX_LAUNCH_AGE_S
    eng.MAX_SLOT_WAIT_S = CHASE_GUARD_S
    eng._safety_ok = _no_safety_gate
    eng._veto_ok = _no_veto
    eng._preregistration = _preregistration
    eng.is_graduation_candidate = _launch_filter(eng)
    eng._base_quote_price = eng.quote_price
    eng.quote_price = _quote_with_curve(eng)
    return eng


def _quote_with_curve(eng):
    """Mimic-only quote override: curve-direct price first (the
    curve exists from second 0; indexers lag fresh launches),
    base live quote fallback otherwise. Graduated curves
    (complete=True) fall through to the base quote, as do
    unreadable curves and missing modules. The base is looked up
    on the engine at call time (eng._base_quote_price) so tests
    can stub it without network. Never raises."""
    def _quote(chain, mint, spot_index=None):
        try:
            if _curve is not None:
                px, _why = _curve.curve_price_usd(mint)
                if px and px > 0:
                    return px
        except Exception:
            pass
        try:
            return eng._base_quote_price(chain, mint, spot_index)
        except Exception:
            return 0.0
    _quote.__name__ = "quote_with_curve"
    return _quote


def _launch_filter(eng):
    """Build the launch admission filter for one engine instance.

    The base engine admits only PumpSwap/Raydium graduations; a
    launch candidate trades on the pump.fun bonding curve, so the
    filter is replaced (same override pattern as the no-gate stub):
    a fresh `pump` mint whose age since detection is inside the
    launch window qualifies, else None. Fail-closed on missing mint
    or timestamp, exactly like the base filter."""
    skew = float(getattr(eng, "AGE_SKEW_S", 60.0) or 60.0)
    window = float(getattr(eng, "MAX_GRAD_AGE_S",
                           MAX_LAUNCH_AGE_S) or MAX_LAUNCH_AGE_S)

    def _is_launch(pool, now=None):
        try:
            now = time.time() if now is None else now
            mint = str((pool or {}).get("mint") or "")
            if not mint.endswith("pump"):
                return None
            created = float((pool or {}).get("created_ts") or 0)
            if created <= 0:
                return None
            age = now - created
            if age < -skew or age > window:
                return None
            return max(age, 0.0)
        except Exception:
            return None

    return _is_launch


# ---- detection ------------------------------------------------------------

def new_to_candidate(item, now=None):
    """Convert one PumpPortalFeed new-token dict into a grad-engine
    candidate row. None for anything that is not a pump.fun token
    creation (migrations and other sources are other lanes'
    business). Never raises."""
    try:
        if not isinstance(item, dict):
            return None
        if item.get("source") != "pumpportal-new":
            return None
        mint = str(item.get("mint") or "")
        if not mint.endswith("pump"):
            return None
        extra = item.get("extra") or {}
        now = time.time() if now is None else now
        created = float(item.get("created_ts") or 0) or now
        return {
            "mint": mint,
            "symbol": str(item.get("symbol") or "?")[:32],
            "created_ts": created,
            "dex": "pump",
            # The bonding curve IS the pool this token trades on
            # pre-graduation. An empty curve key fails the engine's
            # phantom guard (no real pool to trade) — fail-closed,
            # never guessed.
            "pool_address": str(extra.get("bonding_curve") or ""),
            "reserve_usd": 0.0,
            "fdv_usd": 0.0,
            "source": "pumpportal-new",
        }
    except Exception:
        return None


def _stamp_detection(engine, mint, source):
    """Record HOW this entry was detected (stream, no gate) and its
    decision latency on the open position, so entries can be scored
    by speed at verdict time. The engine already stamps born/seen/
    decided/entry timestamps; this adds the derived latencies."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = d["positions"].get(mint)
            if pos is not None:
                pos["source"] = "launch-mimic"
                ef = pos.setdefault("entry_features", {})
                ef["detection"] = source
                ef["gate"] = "none"
                try:
                    ef["latency_seen_to_decided_s"] = round(
                        float(pos.get("decided_ts") or 0) -
                        float(pos.get("seen_ts") or 0), 3)
                    ef["latency_decided_to_entry_s"] = round(
                        float(pos.get("entry_ts") or 0) -
                        float(pos.get("decided_ts") or 0), 3)
                except Exception:
                    pass
                engine._write(d)
    except Exception:
        pass


def scan_once(engine, feed=None, now=None, on_items=None,
              fresh_only=False):
    """One detection cycle for the MIMIC lane: drain the new-token
    stream, merge candidates into the engine watchlist, and attempt
    a no-gate entry for everything still in its launch window.
    `on_items` (optional) receives the raw drained stream items.
    `fresh_only` (the daemon's stream-wake thread) restricts entry
    attempts to candidates drained in THIS call — watchlist retries
    stay the main loop's business, at the main loop's cadence.
    Returns counters. Never raises."""
    out = {"stream": 0, "watching": 0, "opened": 0,
           "skipped": 0, "dead": 0}
    try:
        return _scan_once(engine, feed, out, now, on_items,
                          fresh_only)
    except Exception as e:
        logger.warning("mimic scan_once failed: %s", e)
        return out


def _scan_once(engine, feed, out, now, on_items=None,
               fresh_only=False):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    try:
        if engine._ks.is_halted(LANE):
            logger.warning("mimic scan: lane halted by killswitch "
                           "— no entries (exits still run)")
            return out
    except Exception:
        pass

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
                cand = new_to_candidate(item, now)
                if cand is not None:
                    rows.append(cand)
                    fresh_mints.add(str(cand.get("mint") or ""))
                    out["stream"] += 1
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
                # Pool-address merge: an empty curve key blocks
                # entry (phantom guard). A later row may carry the
                # real curve key — merge it in without disturbing
                # the first-detection stamp.
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

    for cand in watch:
        mint = str(cand.get("mint") or "")
        if fresh_only and mint not in fresh_mints:
            continue
        opened, _why, final = engine.consider_candidate(cand, now)
        if opened:
            out["opened"] += 1
            _stamp_detection(engine, mint,
                             str(cand.get("source") or "watchlist"))
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
