#!/usr/bin/env python3
"""Mimic-EDGE2 lane (2026-11-02, user-ordered thorough fix of the
three edge limits): edge's design with ONLY the exit rate changed.

Edge halted on its pre-registered verdict 2026-11-02 (100 closes,
-$4.09/close, 36 hard stops / 66 TWAP completes): the completes
averaged ~0.93x — the ~5min TWAP window decayed the position
before it emptied — and the stops gapped far past the -25%
trigger (down to 0.16x). Fix (1): TWAP rate 1 -> 4 slices/poll
(64 slices in ~80s instead of ~5min); below 0.90x the fall is
accelerating, so 8 slices/poll dump the rest before the stop
even triggers. Stop (-25%), 60min hold, filter (copycat/serial
>=3/24h), stake ($25), bankroll ($500), MAX_OPEN (20),
window/chase (120s), curve pricing, no gate/veto/cards/demo —
all unchanged from edge, so edge2's verdict isolates exit speed.

Fix (2): the outside bot's launch filter stays unknown, now
precisely bounded. Its confirmed play set is 12 (10 Oct 7
evening-session plays, one at a time ~30-90min apart, plus
INCOGPUP + ASghcy Oct 11) — not 2, and not the dust flows
(net-0 with large gross = completed plays, not dust). No chain
feature separates plays from skips: the skip set (launches seen
but not played) is unobservable from their histories, and
tickers/creators are unrecoverable from free APIs (DexScreener
delisted these coins, pump API blocked). The leading alternative
is size/manufactured graduation: 85 SOL forces graduation plus
a post-graduation crowd to sell into; $25 paper cannot, and no
paper filter can test that. Edge2's verdict tests whether
speed + filter suffice without size.

Fix (3): the two pre-fix-code closes (SGataev, corn) are
corrected by sidecar note, never by ledger rewrite (see
paper-trading-bot-ops hidden_files
mimic-edge-close-corrections-2026-11-02.md).

PRE-REGISTERED VERDICT: 100 closes, expectancy <= 0 halts the
lane permanently (killswitch lane "mimic_edge2", human-only
re-enable); > 0 survives.

Paper only. Nothing here touches real money, keys, or wallets.
Free-only: PumpPortal public stream + DexScreener/tape quotes.
"""
import importlib.util
import logging
import os
import threading
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.mimic_edge2")

# Curve-direct pricing (fail-soft: a missing module never stops the
# lane — entries fall back to the base live quote, exactly as
# before this path existed).
try:
    import mimic_curve as _curve
except Exception:
    _curve = None

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "mimic_edge2_ledger.json")
LANE = "mimic_edge2"
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

# TWAP exit (the copied edge): 64 equal slices, one per exit poll
# (~5s) -> ~5min to fully exit, inside the measured 87s-7min real
# windows. Hard stop -25% on the unsold remainder (raw_tight
# precedent). 60min max hold (the real exit finishes in minutes).
TWAP_SLICES = 64
TWAP_MAX_HOLD_S = 3600.0
HARD_STOP_MULT = 0.75
# Edge2 exit rate (the limit-1 fix): 4 slices per poll (~80s to
# empty) instead of edge's 1 (~5min); below 0.90x the fall is
# accelerating, so 8 slices per poll dump the rest.
TWAP_SLICES_PER_POLL = 4
TWAP_ACCEL_BELOW_MULT = 0.90
TWAP_ACCEL_SLICES_PER_POLL = 8

# Launch filter (grad_select's evidence filters, adapted to
# launches): reject when the 24h occurrence count INCLUDING this
# launch reaches 3 — i.e. the 3rd same-ticker or same-deployer
# launch in 24h is rejected. The deployer is the PumpPortal
# payload's trader field (presumed creator/first buyer — NOT
# verified as the deployer); a missing trader means the deployer
# rule cannot judge, so it does not block (the ticker rule still
# gates). Timestamps older than the window never count.
FILTER_WINDOW_S = 86400
FILTER_MAX_OCCUR = 3

# In-memory launch journal (see module docstring): per-key
# timestamp lists, pruned to the window, capped per key so a
# restart-bounded process can never grow it without limit.
JOURNAL = {"tickers": {}, "deployers": {}}
JOURNAL_LOCK = threading.Lock()

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")


# ---- launch filter -----------------------------------------------------

def _pruned(lst, now):
    """Timestamps inside the 24h window only. Pure: no I/O."""
    try:
        cutoff = now - FILTER_WINDOW_S
        return [float(ts or 0) for ts in (lst or [])
                if float(ts or 0) >= cutoff]
    except Exception:
        return []


def _filter_decision(journal, symbol, trader, now):
    """Entry-time evidence filter. Pure: no I/O, safe to unit-test.
    Returns (ok, reason): reject when the 24h occurrence count
    (including this launch) reaches FILTER_MAX_OCCUR for the
    ticker, or — when the trader is known — for the deployer.
    A corrupt journal never blocks the lane (fail-open with an
    empty decision); the ticker rule still gates every launch."""
    try:
        j = journal or {}
        tk = _pruned((j.get("tickers") or {}).get(symbol), now)
        if len(tk) >= FILTER_MAX_OCCUR:
            return False, "copycat ticker"
        if trader:
            dp = _pruned((j.get("deployers") or {}).get(trader), now)
            if len(dp) >= FILTER_MAX_OCCUR:
                return False, "serial deployer"
        return True, ""
    except Exception:
        return True, ""


def _record_launch(journal, symbol, trader, now):
    """Record one seen launch in the journal (caller holds
    JOURNAL_LOCK). Prunes to the window and caps each key's list
    so the journal stays bounded at stream rates. Never raises."""
    try:
        for store, key in (("tickers", symbol), ("deployers", trader)):
            if not key:
                continue
            d = journal.setdefault(store, {})
            lst = _pruned(d.get(key), now)
            lst.append(now)
            d[key] = lst[-64:]
        # Drop keys with nothing inside the window.
        for store in ("tickers", "deployers"):
            d = journal.get(store) or {}
            for k in [k for k, v in d.items()
                      if not _pruned(v, now)]:
                del d[k]
    except Exception:
        pass


def _filtered_consider(engine, cand, now):
    """Entry gate wrapping the engine's consider_candidate: the
    evidence filter runs on CURRENT 24h counts at decision time
    (a launch admitted to the watchlist while its ticker was clean
    is still rejected if two more same-ticker launches arrive
    before it gets a slot — the grad_select semantics). A
    rejection is final (the candidate leaves the watchlist, no
    retry) and counted in the ledger's filter_stats. Never raises
    into the caller: a counter-write failure never blocks entry
    evaluation."""
    try:
        with JOURNAL_LOCK:
            ok, reason = _filter_decision(
                JOURNAL, str(cand.get("symbol") or ""),
                str(cand.get("trader") or ""), now)
        if not ok:
            try:
                with engine._ledger_lock():
                    d = engine._read()
                    fs = d.setdefault(
                        "filter_stats",
                        {"seen": 0, "rejected_copycat": 0,
                         "rejected_serial": 0})
                    kind = ("rejected_copycat" if "copycat" in reason
                            else "rejected_serial")
                    fs[kind] = int(fs.get(kind) or 0) + 1
                    engine._write(d)
            except Exception:
                pass
            return False, "filter: " + reason, True
    except Exception:
        pass
    return engine._base_consider_candidate(cand, now)


# ---- engine (isolated grad.py instance with mimic_edge policy) --------

def _no_safety_gate(mint):
    """No safety gate on this lane, by design (the outside bot buys
    without one; the filter is evidence-based selection, not a
    safety gate). Signature matches grad._safety_ok."""
    return True, "", None, False


def _no_veto(mint, symbol, safety_report):
    """No veto engine on this lane. Signature matches grad._veto_ok."""
    return True, ""


def _preregistration():
    """Verdict terms for the MIMIC_EDGE lane, stamped into its
    ledger header at creation (same anti-quiet-change rule as
    grad.py)."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'mimic_edge2'); "
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
            "filters": "copycat ticker >=3/24h reject + serial "
                       "deployer >=3/24h reject (grad_select "
                       "evidence filters adapted to launches; the "
                       "outside bot's own launch filter is unknown)",
            "detection": "PumpPortal new-token stream (live only, "
                         "no backfill)",
            "entry_price": "live quote at decision time, honest "
                           "adverse fill (curve-direct first, NOT "
                           "the stale creation price)",
            "exit": "TWAP 64 slices at 4/poll (~80s) + 8/poll "
                    "below 0.90x + -25% stop on unsold remainder + "
                    "60min max hold (edge exit, sped up: edge's "
                    "~5min window decayed completes to ~0.93x)",
            "supersedes": "mimic_edge (halted on verdict 2026-11-02: "
                          "100 closes, -$4.09/close); changes ONLY "
                          "exit rate vs edge",
            "cards": "NONE in v1",
        },
        "registered_ts": time.time(),
    }


def make_engine():
    """Load grad.py as a fresh, isolated module and override its lane
    policy for the MIMIC_EDGE lane. The other lanes' module
    instances are never touched. Never raises on import shape —
    grad.py's deps are resolved by its own import block."""
    spec = importlib.util.spec_from_file_location("mimic_edge_engine",
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
    eng.HARD_STOP_MULT = HARD_STOP_MULT
    eng.TWAP_SLICES = TWAP_SLICES
    eng.TWAP_MAX_HOLD_S = TWAP_MAX_HOLD_S
    eng.TWAP_SLICES_PER_POLL = TWAP_SLICES_PER_POLL
    eng.TWAP_ACCEL_BELOW_MULT = TWAP_ACCEL_BELOW_MULT
    eng.TWAP_ACCEL_SLICES_PER_POLL = TWAP_ACCEL_SLICES_PER_POLL
    eng._safety_ok = _no_safety_gate
    eng._veto_ok = _no_veto
    eng._preregistration = _preregistration
    eng.is_graduation_candidate = _launch_filter(eng)
    eng._base_quote_price = eng.quote_price
    eng.quote_price = _quote_with_curve(eng)
    eng._base_consider_candidate = eng.consider_candidate

    def _consider(cand, now=None):
        return _filtered_consider(eng, cand, now)
    eng.consider_candidate = _consider
    return eng


def _quote_with_curve(eng):
    """Mimic-edge-only quote override: curve-direct price first (the
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
    business). The row carries the payload's trader field so the
    entry-time deployer filter can judge it. Never raises."""
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
            "trader": str(extra.get("trader") or ""),
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
    """Record HOW this entry was detected (stream, TWAP + filter)
    and its decision latency on the open position, so entries can
    be scored by speed at verdict time. The engine already stamps
    born/seen/decided/entry timestamps; this adds the derived
    latencies."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = d["positions"].get(mint)
            if pos is not None:
                pos["source"] = "launch-mimic-edge"
                ef = pos.setdefault("entry_features", {})
                ef["detection"] = source
                ef["gate"] = "twap+filter"
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
    """One detection cycle for the MIMIC_EDGE lane: drain the
    new-token stream, record every seen launch in the filter
    journal, merge candidates into the engine watchlist, and
    attempt a filtered entry for everything still in its launch
    window. `on_items` (optional) receives the raw drained stream
    items. `fresh_only` (the daemon's stream-wake thread) restricts
    entry attempts to candidates drained in THIS call — watchlist
    retries stay the main loop's business, at the main loop's
    cadence. Returns counters. Never raises."""
    out = {"stream": 0, "watching": 0, "opened": 0,
           "skipped": 0, "dead": 0}
    try:
        return _scan_once(engine, feed, out, now, on_items,
                          fresh_only)
    except Exception as e:
        logger.warning("mimic_edge scan_once failed: %s", e)
        return out


def _scan_once(engine, feed, out, now, on_items=None,
               fresh_only=False):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    try:
        if engine._ks.is_halted(LANE):
            logger.warning("mimic_edge scan: lane halted by "
                           "killswitch — no entries (exits still run)")
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

    # Every seen launch counts toward the filter, whether or not
    # it is ever entered (the rule counts launches existing, not
    # launches bought). Recorded under the journal lock only —
    # never nested inside the ledger lock.
    try:
        with JOURNAL_LOCK:
            for cand in rows:
                _record_launch(JOURNAL, str(cand.get("symbol") or ""),
                               str(cand.get("trader") or ""), now)
    except Exception:
        pass

    with engine._ledger_lock():
        d = engine._read()
        fs = d.setdefault("filter_stats",
                          {"seen": 0, "rejected_copycat": 0,
                           "rejected_serial": 0})
        fs["seen"] = int(fs.get("seen") or 0) + len(rows)
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
