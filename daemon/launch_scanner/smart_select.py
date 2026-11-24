#!/usr/bin/env python3
"""Smart-SELECT lane (2026-10-27, measurement-only test): selection
FIRST, smart money SECOND.

Why this lane exists: the watcher lanes that followed smart-watcher
buys blindly were both KILLED on paper verdicts (watcher v1: 106
closes, -$39.33, 31% wins; watcher v2: 112 closes, -$41.14, 25%
wins, -$0.41/close). Their established design flaws: (1) no
selection — they followed every tracked-wallet buy, and tracked
wallets buy mostly losers; (2) agreement alone did not help (v2
required convergence and still lost); (3) they copied entries but
not exits. The Oct 20 standing decision is that smart-money
agreement adds ZERO score in every existing path, and re-enabling
it requires follower-grade evidence. This lane CREATES that
evidence. It does not change any existing lane's score, filter,
stake, or killswitch term.

The design, stated exactly so the verdict means something:
- TRIGGER: a smart-watcher bus fire (a tracked wallet buy, as
  published to the normalized signal bus). The fire alone never
  opens a position.
- SELECTION: the coin must pass ALL FOUR grad_select evidence
  filters, reused exactly (grad_select.evaluate_candidate — not
  reinvented, not re-thresholded): serial deployer >= 3
  launches/24h rejected; fast curve (TTG <= 3600s AND <= 250
  curve signatures); canonical pools only; copycat tickers
  >= 3/24h rejected. ALL filters fail-closed on unknown
  (unknown -> reject, never enter). A smart fire that FAILS any
  filter must NOT enter.
- One honest subtlety, documented not hidden: the canonical check
  for a smart fire runs against the recorder's migration record
  for that mint (pool match). A mint with NO observed migration
  record fails unknown_canonical — but that is a statement about
  TIMING, not about the coin: smart wallets buy BEFORE graduation,
  so at fire time the migration record does not exist yet. A
  pre-graduation fire is therefore PARKED in the watchlist
  (deferred evaluation, 2026-10-28 fix), NOT rejected: once the
  migration record appears, all four filters are re-run at that
  evaluation, and only then does the coin enter or reject. A mint
  whose migration never appears within 1800s of the fire is
  dropped with a journaled window-expiry reason. Entry is stamped
  at evaluation time (graduation), which keeps the head-to-head
  vs grad_select apples-to-apples: grad_select also enters at
  graduation. The fail-closed rule is unchanged: unknown on ANY
  filter at evaluation time means reject, never enter.
- DETECTION WINDOW: same as grad_select — the migration must be
  within 30 minutes (MAX_GRAD_AGE_S 1800) when considered. This
  keeps the head-to-head vs grad_select clean: same age window,
  same filters, same exits, same stakes — ONLY the trigger
  differs (smart-fired subset vs every graduation). The verdict
  therefore isolates ENTRY value (marginal value of smart money).
- NO safety gate/veto beyond those four filters (same posture as
  grad_select: the filters ARE the selection).
- Stakes: $1 paper per coin, $100 starting bankroll. Honest fills
  + 1.25% venue fees + the grad engine's 25% cost gate (entry
  blocked when modeled round-trip costs exceed 25% of notional,
  logged COST-BLOCKED by the cost module).
- Exits: the user's standard ladder, UNCHANGED (50% @2x, 25% @3x,
  final 25% on 30% trailing stop, -50% hard stop, 24h time stop)
  — deliberately identical to grad_select, so the verdict does
  not mix in a new exit.
- Dedupe-ever (same mint never opens twice, enforced by the grad
  engine plus this lane's dead_mints). No daily cap (smart fires
  are naturally rare). NO cards in v1 — measurement only.
- PRE-REGISTERED VERDICT (stamped into the ledger header): at 100
  closes, expectancy <= 0 halts the lane permanently (killswitch
  lane "smart_select" {100,100}, human-only re-enable). The
  verdict is reported RAW and EX-TOP-1, plus a same-period
  head-to-head vs grad_select on per-$1 expectancy and win rate
  (raw dollars are not comparable across stakes).

Paper only. Free-only: signal bus (local file), recorder files,
DexScreener, Helius RPC via reserves.rpc. Nothing here touches
real money, keys, or wallets.
"""
import importlib.util
import json
import logging
import os
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.smart_select")

try:
    import grad_select as _sel
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import grad_select as _sel
    except Exception:
        _sel = None

try:
    import grad_raw as _raw
except Exception:  # pragma: no cover
    _raw = None

try:
    import signal_bus as _bus
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import signal_bus as _bus
    except Exception:
        _bus = None

try:
    import trials as _trials
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import trials as _trials
    except Exception:
        _trials = None

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "smart_select_ledger.json")
GRAD_SELECT_LEDGER_PATH = os.path.join(HOME, "grad_select_ledger.json")
LANE = "smart_select"
CHAIN = "solana"

BANKROLL_START = 100.00
NOTIONAL_USD = 1.00       # new-lane standard ($1/shot, like moon)
MAX_OPEN = 20             # grad_select's value (grad default is 20)
DAILY_ENTRY_CAP = None    # NO daily cap by design; fires are rare
MAX_GRAD_AGE_S = 1800.0   # 30-min detection window, same as grad_select
VERDICT_SAMPLE_CLOSES = 100
MAX_FIRE_AUDIT = 500      # watcher-lane pattern
MAX_SEEN_KEYS = 2000      # watcher-lane pattern

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")


# ---- engine (isolated grad.py instance with smart_select policy) -------

def _no_safety_gate(mint):
    """No safety gate beyond the four filters. Signature matches
    grad._safety_ok. Fallback when grad_raw is unavailable."""
    return True, "", None, False


def _no_veto(mint, symbol, safety_report):
    """No veto engine on this lane. Signature matches grad._veto_ok."""
    return True, ""


def _preregistration():
    """Verdict terms for the SMART_SELECT lane, stamped into its
    ledger header at creation (same anti-quiet-change rule as
    grad.py)."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'smart_select'); "
                 "if > 0 the lane survived its sample. Re-enable is "
                 "human-only. The verdict is reported RAW and "
                 "EX-TOP-1, plus a same-period head-to-head vs "
                 "grad_select on per-$1 expectancy and win rate."
                 % VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": None,
            "max_grad_age_s": MAX_GRAD_AGE_S,
            "runner_rule_applies": False,
            "safety_gate": "NONE (by design — the four grad_select "
                           "filters ARE the selection)",
            "veto": "NONE",
            "filters": {
                "serial_deployer": "reject if deployer launches in "
                                   "trailing 24h >= 3 (recorder "
                                   "birth log)",
                "fast_curve_few_trades": "reject unless TTG <= 3600s "
                                         "and curve signatures <= 250",
                "canonical_only": "recorder-migration pool match "
                                  "only (no migration record -> "
                                  "unknown_canonical reject)",
                "copycat_ticker": "reject if other same-ticker "
                                  "launches in trailing 24h >= 3",
            },
            "detection": "smart-watcher bus fires + grad_select "
                         "evidence filters (all four, fail-closed)",
            "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h",
            "experiment_vs_grad_select": "trigger differs ONLY: "
                                        "smart-fired subset vs every "
                                        "graduation; filters, exits, "
                                        "stakes, cost gate identical",
        },
        "registered_ts": time.time(),
    }


def make_engine():
    """Load grad.py as a fresh, isolated module and override its
    lane policy for the SMART_SELECT lane. The grad, grad_raw, and
    grad_select module instances are never touched. Never raises on
    import shape."""
    spec = importlib.util.spec_from_file_location("smart_select_engine",
                                                  _GRAD_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    eng.LEDGER_PATH = LEDGER_PATH
    eng.LANE = LANE
    eng.NOTIONAL_USD = NOTIONAL_USD
    eng.MAX_OPEN = MAX_OPEN
    eng.DAILY_ENTRY_CAP = DAILY_ENTRY_CAP
    eng.MAX_GRAD_AGE_S = MAX_GRAD_AGE_S
    # Exits are the user's STANDARD ladder — grad.py's defaults
    # already equal it, so nothing is overridden here (unlike moon).
    if _raw is not None:
        eng._safety_ok = _raw._no_safety_gate
        eng._veto_ok = _raw._no_veto
    else:
        eng._safety_ok = _no_safety_gate
        eng._veto_ok = _no_veto
    eng._preregistration = _preregistration
    return eng


def register_trial():
    """Register this lane in the trial registry (idempotent for
    identical terms). Never raises."""
    try:
        if _trials is None:
            return None
        pre = _preregistration()
        return _trials.register(
            LANE, "lane", pre["terms"],
            note="smart_select lane (2026-10-27 build); verdict: %s"
                 % pre["rule"])
    except Exception:
        return None


def seed_checkpoint(engine):
    """Anchor the lane in the tamper-evident verdict ledger with a
    kind=checkpoint record at 0 closes. Once only. Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            if d.get("smart_select_seeded_ts"):
                return False
            d["smart_select_seeded_ts"] = time.time()
            engine._write(d)
        engine._verdict_event(LANE, "checkpoint", {
            "closes": 0, "expectancy": 0.0,
            "note": "smart_select lane start (2026-10-27): "
                    "smart-watcher fires + all four grad_select "
                    "evidence filters (fail-closed); standard ladder; "
                    "$1 stakes; pre-registered 100-close verdict, "
                    "raw + ex-top-1 + head-to-head vs grad_select."})
        return True
    except Exception:
        return False


# ---- signal bus consumption (watcher-lane pattern) -----------------------

def _read_bus_signals():
    """All signals currently on the bus (oldest first). The bus is
    rewritten (bounded, atomic) by publishers, so consumers re-read
    it whole and dedupe by fire key — offsets are unsafe here.
    Never raises."""
    try:
        if _bus is None:
            return []
        path = _bus.BUS_PATH
        with open(path) as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            sig = json.loads(line)
        except Exception:
            continue
        if isinstance(sig, dict):
            out.append(sig)
    return out


def _is_smart_fire(sig):
    """A bus record is a tradeable smart fire for this lane when:
    schema-valid, source smart-watcher, Solana, mint present.
    NOTE: convergence is deliberately NOT required here. The
    watcher lanes already proved agreement does not predict; this
    lane's selection is the four filters, and the fire is only the
    trigger. Evidence (wallets/groups) is journaled for later
    analysis but never gates entry. Fail-closed on shape."""
    try:
        if not isinstance(sig, dict):
            return False
        if _bus is not None:
            ok, _errs = _bus.validate(sig)
            if not ok:
                return False
        if (sig.get("source") or "") != "smart-watcher":
            return False
        if (sig.get("chain") or "") != "solana":
            return False
        if not sig.get("mint"):
            return False
        return True
    except Exception:
        return False


def _fire_key(sig):
    if isinstance(sig.get('event_id'),str) and sig['event_id']:
        return '%s:%s:%s' % (sig.get('chain'),sig.get('source'),sig['event_id'])
    return "%.3f:%s" % (float(sig.get("ts") or 0), sig.get("mint") or "")


def _ensure_extra(engine):
    """Initialize this lane's extra ledger keys (fire audit, seen
    keys, filter journal) without disturbing the engine's own
    keys. Never raises. Never nests locks: called unlocked."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            changed = False
            if not isinstance(d.get("fire_audit"), list):
                d["fire_audit"] = []
                changed = True
            if not isinstance(d.get("bus_seen"), list):
                d["bus_seen"] = []
                changed = True
            if not isinstance(d.get("select_journal"), dict):
                d["select_journal"] = {"counts": {}, "recent": [],
                                       "started_ts": time.time()}
                changed = True
            if changed:
                engine._write(d)
    except Exception:
        pass


def _audit(d, sig, outcome, reason, now):
    """Append one fire outcome to the ledger dict's fire_audit
    (caller holds NO lock here — this mutates the passed dict
    only; the caller writes). Bounded. Never raises."""
    try:
        d.setdefault("fire_audit", []).append({
            "fire_ts": float(sig.get("ts") or 0),
            "seen_ts": now,
            "mint": sig.get("mint") or "",
            "symbol": str(sig.get("symbol") or "?")[:32],
            "outcome": outcome,
            "reason": str(reason)[:200],
        })
        d["fire_audit"] = d["fire_audit"][-MAX_FIRE_AUDIT:]
    except Exception:
        pass


# ---- candidate construction + filter stage --------------------------------

def build_candidate(sig, now):
    """Build a grad-engine candidate from a smart fire. The
    migration instant + pool come from the recorder's migration
    index (grad_select's index, refreshed here): a mint with NO
    migration record gets created_ts 0 and an empty pool, which
    the canonical filter rejects as unknown_canonical — exactly
    the fail-closed behavior. Dex is pumpswap: current-era
    pump.fun migrations land on PumpSwap (the migration record
    itself is a PumpPortal migration event). Never raises."""
    try:
        mint = str(sig.get("mint") or "")
        symbol = str(sig.get("symbol") or "?")[:32]
        mig = {}
        try:
            if _sel is not None:
                _sel._index_refresh(now)
                mig = _sel._INDEX.get("by_mig", {}).get(mint) or {}
        except Exception:
            mig = {}
        return {
            "mint": mint,
            "symbol": symbol,
            "created_ts": float(mig.get("ts") or 0),
            "dex": "pumpswap",
            "pool_address": str(mig.get("pool") or ""),
            "reserve_usd": 0.0,
            "fdv_usd": 0.0,
            "source": "smart-watcher",
            "fire_ts": float(sig.get("ts") or 0),
        }
    except Exception:
        return {"mint": str(sig.get("mint") or ""), "symbol": "?",
                "created_ts": 0, "dex": "pumpswap", "pool_address": "",
                "reserve_usd": 0.0, "fdv_usd": 0.0,
                "source": "smart-watcher",
                "fire_ts": float(sig.get("ts") or 0)}


def _stamp_detection(engine, mint, sig, values):
    """Stamp HOW this entry was detected and its full filter
    measurements onto the open position's entry_features, plus
    the smart fire's evidence (journaling only — evidence never
    gated entry). Never raises. Caller holds no lock."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = d["positions"].get(mint)
            if pos is not None:
                pos["source"] = "smart-watcher-select"
                ef = pos.setdefault("entry_features", {})
                ef["detection"] = "smart-watcher"
                ef["gate"] = "smart_select"
                ef["select"] = dict(values or {})
                ef["fire_ts"] = float(sig.get("ts") or 0)
                try:
                    ev = sig.get("evidence") or {}
                    wallets = ev.get("wallets") or []
                    if isinstance(wallets, list) and wallets:
                        ef["fire_wallets"] = \
                            [str(w) for w in wallets[:20]]
                    comp = ev.get("components") or {}
                    if comp.get("convergence_groups") is not None:
                        ef["fire_convergence_groups"] = \
                            int(comp["convergence_groups"])
                except Exception:
                    pass
                engine._write(d)
    except Exception:
        pass


# ---- detection scan ---------------------------------------------------------

def scan_once(engine, signals=None, now=None):
    """One detection cycle for the SMART_SELECT lane: consume new
    smart-watcher bus fires, run the four filters, open entries
    for passes still in the age window. Returns counters incl.
    select_rejected and a per-reason breakdown. Never raises.
    NOTE on locks: this function never nests ledger locks — each
    locked section is short, and _journal_rejection (which locks
    internally) is always called unlocked."""
    out = {"fires": 0, "opened": 0, "blocked": 0, "skipped": 0,
           "pending": 0, "watching": 0, "select_rejected": 0,
           "select_reasons": {}, "parked": 0, "expired": 0}
    try:
        return _scan_once(engine, signals, out, now)
    except Exception as e:
        logger.warning("smart_select scan_once failed: %s", e)
        return out


def _resolve_parked(engine, row, now, out):
    """Re-evaluate one parked (pre-graduation) watchlist row.

    Returns the converted passed-row dict when the mint has
    graduated AND passes all four filters at this evaluation;
    otherwise None (the row was dropped on window expiry,
    rejected on a real filter verdict, or stays parked because
    no migration record exists yet). Every terminal decision is
    journaled with its reason. Fail-closed throughout: unknown
    on any filter at evaluation time rejects, never enters.
    Caller holds NO lock (this locks internally, like
    _journal_rejection). Never raises."""
    try:
        mint = str(row.get("mint") or "")
        symbol = str(row.get("symbol") or "?")
        # The window runs from the fire; fall back to the park
        # time (first_seen_ts) when the fire timestamp is absent,
        # so a malformed row is never expired instantly.
        fire_ts = float(row.get("fire_ts") or
                        row.get("first_seen_ts") or now)
        fresh = build_candidate({"mint": mint, "symbol": symbol,
                                 "ts": fire_ts}, now)
        if not fresh.get("created_ts"):
            # Still pre-graduation: no migration record yet.
            if now - fire_ts > MAX_GRAD_AGE_S:
                with engine._ledger_lock():
                    d = engine._read()
                    if mint in d["watchlist"]:
                        d["watchlist"].pop(mint, None)
                    d["dead_mints"][mint] = {
                        "ts": now,
                        "reason": "graduation window expired "
                                  "(no migration record)"}
                    _audit(d, {"mint": mint, "symbol": symbol,
                               "ts": fire_ts},
                           "dropped",
                           "graduation window expired (no migration "
                           "record)", now)
                    engine._write(d)
                out["expired"] += 1
            return None
        # Migration appeared: re-run ALL FOUR filters now. The
        # filter code is byte-identical to the fire-stage path
        # (same evaluate_candidate, same fail-closed semantics);
        # only the TIMING moved, from fire time to graduation.
        verdict = _sel.evaluate_candidate(engine, fresh, now)
        if verdict.get("verdict") != "pass":
            reason = str(verdict.get("reason") or "unknown")
            _sel._journal_rejection(engine, fresh, verdict, now)
            with engine._ledger_lock():
                d = engine._read()
                if mint in d["watchlist"]:
                    d["watchlist"].pop(mint, None)
                d["dead_mints"][mint] = {
                    "ts": now, "reason": "smart_select: %s" % reason}
                _audit(d, {"mint": mint, "symbol": symbol,
                           "ts": fire_ts},
                       "blocked", "deferred reject: %s" % reason, now)
                engine._write(d)
            out["select_rejected"] += 1
            out["select_reasons"][reason] = \
                out["select_reasons"].get(reason, 0) + 1
            out["blocked"] += 1
            return None
        # Pass: convert the parked row to a passed row in place.
        # Entry itself is stamped at evaluation (graduation)
        # time by the normal entry stage below; the original
        # fire timestamp rides along for journaling only.
        with engine._ledger_lock():
            d = engine._read()
            cur = d["watchlist"].get(mint)
            if cur is None:
                return None  # decided elsewhere meanwhile
            fresh["select"] = verdict
            fresh["first_seen_ts"] = cur.get("first_seen_ts") or now
            if isinstance(cur.get("fire_evidence"), dict):
                fresh["fire_evidence"] = cur["fire_evidence"]
            d["watchlist"][mint] = fresh
            _audit(d, {"mint": mint, "symbol": symbol, "ts": fire_ts},
                   "pending", "deferred filter pass, awaiting entry",
                   now)
            engine._write(d)
        return fresh
    except Exception as e:
        logger.warning("smart_select parked resolution failed: %s", e)
        return None


def _scan_once(engine, signals, out, now):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    _ensure_extra(engine)
    try:
        if engine._ks.is_halted(LANE):
            logger.warning("smart_select scan: lane halted by "
                           "killswitch — no entries (exits still run)")
            return out
    except Exception:
        pass
    if _sel is None:
        logger.warning("smart_select scan: grad_select unavailable — "
                       "no filters, no entries")
        return out
    if signals is None:
        signals = _read_bus_signals()

    # Decide new fires (oldest first — the bus file is chronological).
    with engine._ledger_lock():
        d0 = engine._read()
        floor_ts = float(d0.get("created_ts") or now)
        seen = set(d0.get("bus_seen") or [])
    fresh = []
    for sig in signals:
        if not _is_smart_fire(sig):
            continue
        if float(sig.get("ts") or 0) < floor_ts:
            continue  # predates this lane — history, not a signal
        key = _fire_key(sig)
        if key in seen:
            continue
        fresh.append((key, sig))

    for key, sig in fresh:
        mint = str(sig.get("mint") or "")
        # Mark seen exactly once, then decide. The seen-mark and
        # the dedupe snapshot share one locked section; the filter
        # evaluation itself runs unlocked below.
        with engine._ledger_lock():
            d = engine._read()
            d["bus_seen"].append(key)
            d["bus_seen"] = d["bus_seen"][-MAX_SEEN_KEYS:]
            if mint in d["positions"]:
                _audit(d, sig, "skipped", "already open", now)
                engine._write(d)
                out["fires"] += 1
                out["skipped"] += 1
                continue
            if any(c.get("mint") == mint for c in d["closes"]):
                _audit(d, sig, "skipped", "already traded", now)
                engine._write(d)
                out["fires"] += 1
                out["skipped"] += 1
                continue
            if mint in d["dead_mints"]:
                _audit(d, sig, "skipped",
                       "dead: %s" % d["dead_mints"][mint].get(
                           "reason", ""), now)
                engine._write(d)
                out["fires"] += 1
                out["skipped"] += 1
                continue
            if mint in d["watchlist"]:
                _audit(d, sig, "skipped", "already watchlisted", now)
                engine._write(d)
                out["fires"] += 1
                out["skipped"] += 1
                continue
            engine._write(d)
        out["fires"] += 1

        # ---- filter stage (unlocked; the filter reads its own index)
        cand = build_candidate(sig, now)
        verdict = _sel.evaluate_candidate(engine, cand, now)
        if verdict.get("verdict") != "pass":
            reason = str(verdict.get("reason") or "unknown")
            if reason == "unknown_canonical":
                # DEFERRED EVALUATION (2026-10-28 fix): no
                # migration record exists YET — the coin has not
                # graduated, so the canonical filter cannot be
                # evaluated. This is not a rejection of the coin;
                # park the mint and re-evaluate once its
                # migration record appears (see _resolve_parked).
                # Fail-closed is preserved: the park carries no
                # entry, and evaluation-time unknown still
                # rejects. A park is NOT journaled as a
                # rejection — only a real evaluation verdict is.
                row = dict(cand)
                row["parked"] = True
                row.setdefault("first_seen_ts", now)
                if isinstance(sig.get("evidence"), dict):
                    row["fire_evidence"] = sig["evidence"]
                with engine._ledger_lock():
                    d = engine._read()
                    if mint not in d["positions"] and \
                            mint not in d["dead_mints"] and \
                            not any(c.get("mint") == mint
                                    for c in d["closes"]):
                        d["watchlist"][mint] = row
                        _audit(d, sig, "parked",
                               "awaiting graduation "
                               "(unknown_canonical at fire time)", now)
                        engine._write(d)
                        out["parked"] += 1
                    else:
                        _audit(d, sig, "skipped",
                               "decided while filtering", now)
                        engine._write(d)
                        out["skipped"] += 1
                continue
            _sel._journal_rejection(engine, cand, verdict, now)
            with engine._ledger_lock():
                d = engine._read()
                d["dead_mints"][mint] = {
                    "ts": now, "reason": "smart_select: %s" % reason}
                _audit(d, sig, "blocked",
                       "filter reject: %s" % reason, now)
                engine._write(d)
            out["select_rejected"] += 1
            out["select_reasons"][reason] = \
                out["select_reasons"].get(reason, 0) + 1
            out["blocked"] += 1
            continue
        # Pass: cache the verdict on the watchlist row and await
        # the entry stage (retries while in the age window).
        # The fire's evidence rides along for journaling only.
        row = dict(cand)
        row["select"] = verdict
        row.setdefault("first_seen_ts", now)
        if isinstance(sig.get("evidence"), dict):
            row["fire_evidence"] = sig["evidence"]
        with engine._ledger_lock():
            d = engine._read()
            if mint not in d["positions"] and \
                    mint not in d["dead_mints"] and \
                    not any(c.get("mint") == mint
                            for c in d["closes"]):
                d["watchlist"][mint] = row
                _audit(d, sig, "pending",
                       "filter pass, awaiting entry", now)
                engine._write(d)
                out["pending"] += 1
            else:
                _audit(d, sig, "skipped",
                       "decided while filtering", now)
                engine._write(d)
                out["skipped"] += 1

    # ---- entry stage: first resolve parked rows (deferred
    # evaluation at graduation), then retry passed rows while in
    # the age window
    with engine._ledger_lock():
        d = engine._read()
        watch = list(d["watchlist"].values())
    out["watching"] = len(watch)
    for cand in watch:
        mint = str(cand.get("mint") or "")
        if cand.get("parked"):
            cand = _resolve_parked(engine, cand, now, out)
            if cand is None:
                continue  # dropped, rejected, or still parked
            mint = str(cand.get("mint") or "")
        if engine.is_graduation_candidate(cand, now) is None:
            with engine._ledger_lock():
                d = engine._read()
                if mint in d["watchlist"]:
                    d["watchlist"].pop(mint, None)
                    d["dead_mints"][mint] = {
                        "ts": now, "reason": "window expired"}
                    _audit(d, {"mint": mint,
                               "symbol": str(cand.get("symbol") or "?"),
                               "ts": float(cand.get("fire_ts") or 0)},
                           "dropped", "window expired", now)
                    engine._write(d)
            continue
        opened, why, final = engine.consider_candidate(cand, now)
        if opened:
            # Recover the fire for stamping (fire_ts/symbol ride
            # on the row; evidence is journal-only and optional).
            sig = {"mint": mint,
                   "symbol": str(cand.get("symbol") or "?"),
                   "ts": float(cand.get("fire_ts") or 0)}
            if isinstance(cand.get("fire_evidence"), dict):
                sig["evidence"] = cand["fire_evidence"]
            _stamp_detection(
                engine, mint, sig,
                (cand.get("select") or {}).get("values") or {})
            with engine._ledger_lock():
                d = engine._read()
                _audit(d, sig, "opened", "opened", now)
                engine._write(d)
            out["opened"] += 1
        elif final:
            with engine._ledger_lock():
                d = engine._read()
                d["watchlist"].pop(mint, None)
                _audit(d, {"mint": mint,
                           "symbol": str(cand.get("symbol") or "?"),
                           "ts": float(cand.get("fire_ts") or 0)},
                       "blocked", why, now)
                engine._write(d)
            out["blocked"] += 1
        # else: transient — stays on the watchlist for retry.
    return out


# ---- verdict reporting (raw + ex-top-1 + head-to-head) --------------------

def _mean(xs):
    return (sum(xs) / len(xs)) if xs else 0.0


def verdict_report(engine, end=None):
    """The pre-registered verdict readout for this lane: raw
    expectancy, ex-top-1 expectancy, and the same-period
    head-to-head vs grad_select on PER-$1 expectancy and win rate
    (grad_select stakes $25 — raw dollars are not comparable, so
    its closes are normalized by their recorded entry notional;
    closes predating the entry_notional_usd field are skipped
    from the per-$1 computation, not guessed). Read-only. Never
    raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
        closes = d.get("closes") or []
        pnls = [float(c.get("pnl_usd") or 0) for c in closes]
        wins = sum(1 for p in pnls if p > 0)
        rep = {
            "lane": LANE,
            "n_closes": len(closes),
            "net_usd": round(sum(pnls), 4),
            "expectancy_raw": round(_mean(pnls), 6),
            "expectancy_ex_top1": None,
            "win_rate": round(wins / len(pnls), 4) if pnls else 0.0,
            "verdict": d.get("verdict"),
        }
        if len(pnls) >= 2:
            rep["expectancy_ex_top1"] = round(
                _mean(sorted(pnls)[:-1]), 6)
        start = float(d.get("created_ts") or 0)
        end = float(end) if end else time.time()
        try:
            with open(GRAD_SELECT_LEDGER_PATH) as f:
                rd = json.load(f)
            per1 = []
            gwins = 0
            for c in (rd.get("closes") or []):
                cts = float(c.get("close_ts") or 0)
                if not (start <= cts <= end):
                    continue
                notional = c.get("entry_notional_usd")
                if not notional:
                    continue
                p1 = float(c.get("pnl_usd") or 0) / float(notional)
                per1.append(p1)
                if p1 > 0:
                    gwins += 1
            rep["head_to_head_grad_select_same_period"] = {
                "n_closes": len(per1),
                "expectancy_per_usd": round(_mean(per1), 6),
                "win_rate": round(gwins / len(per1), 4) if per1 else 0.0,
                "window_start": start, "window_end": end}
        except Exception:
            rep["head_to_head_grad_select_same_period"] = None
        return rep
    except Exception:
        return {}


def maybe_verdict_report(engine):
    """When the engine's in-ledger verdict lands, attach the
    ex-top-1 + head-to-head report to the ledger exactly once and
    anchor it in the verdict ledger. Returns the report or None.
    Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            if not d.get("verdict") or d.get("smart_select_report"):
                return None
        rep = verdict_report(engine)
        if not rep:
            return None
        with engine._ledger_lock():
            d = engine._read()
            d["smart_select_report"] = rep
            engine._write(d)
        try:
            engine._verdict_event(LANE, "checkpoint", {
                "closes": rep.get("n_closes"),
                "expectancy": rep.get("expectancy_raw"),
                "expectancy_ex_top1": rep.get("expectancy_ex_top1"),
                "head_to_head_grad_select":
                    rep.get("head_to_head_grad_select_same_period"),
                "note": "smart_select pre-registered verdict report "
                        "(raw + ex-top-1 + same-period head-to-head "
                        "vs grad_select on per-$1 expectancy)"})
        except Exception:
            pass
        return rep
    except Exception:
        return None


def account_state(engine):
    """Read-only summary via the engine. Never raises."""
    try:
        return engine.account_state()
    except Exception:
        return {}
