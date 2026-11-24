#!/usr/bin/env python3
"""Paper-Live lane (2026-11-02, user-ordered): a $10 PAPER account
that trades exactly as live trading would — on live market prices,
with honest (adverse) fills and real fee modeling — but with NO
real money, NO real SOL, and NO real profit. This is a simulation
of live trading, labeled PAPER everywhere, so the user can see
what $10 would do before any real capital is ever considered.

Package (identical to tranche_protect, the only exit package
with a SURVIVED verdict, except bankroll/stake/slots):
Entries: EXACTLY grad_select's four evidence filters, evaluated
by grad_select.evaluate_candidate (shared code): serial deployer
(>= 3 launches/24h rejected), fast curve (TTG <= 3600s AND <= 250
curve signatures), canonical pools only, copycat tickers
(>= 3 other same-ticker launches/24h rejected). Fail-closed on
unknown. NO safety gate, NO veto.
Exits: 25% at 1.5x / 25% at 3x / 50% on 30% trailing stop, PROTECT
stop (breakeven once leg A banks), 24h time stop, plus one dip
buyback per position (after >= 1 leg banked, >= 30% retrace,
price >= entry, proceeds cover cost, age < 24h).
Money: $10 bankroll, $1 stakes, MAX_OPEN 10 (fully invested
from night one). Detection: live PumpPortal migration stream +
GeckoTerminal backfill, FIRE-FIRST payload pricing.
SOL note: the ledger is USD-native (grad.py engine). The SOL
start amount ($10 at the live SOL price at launch) is stamped
into the ledger header at deploy and into the launch note; SOL
figures shown anywhere are that launch-rate conversion, NOT a
live SOL balance.

PRE-REGISTERED VERDICT (stamped into the ledger header): at
100 closes OR 7 days after lane creation, whichever comes
first, if mean pnl per close <= 0 the lane halts permanently
(killswitch lane "paper_live" {100,100}, human-only re-enable).
Zero closes at the deadline also halts (no evidence). Reported
RAW and EX-TOP-1, plus a same-window head-to-head vs grad_select.

Paper only. NO Telegram cards in v1 (ledger + verdict chain
only). Nothing here touches real money, keys, or wallets.
Free-only: PumpPortal stream, GeckoTerminal, DexScreener,
Helius RPC via reserves.rpc.
"""
import importlib.util
import json
import logging
import os
import threading
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.paper_live")

try:
    import grad_raw as _raw
except Exception:  # pragma: no cover
    _raw = None

try:
    import grad_select as _sel
except Exception:  # pragma: no cover
    _sel = None

try:
    import trials as _trials
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import trials as _trials  # type: ignore
    except Exception:  # pragma: no cover
        _trials = None  # type: ignore

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "paper_live_ledger.json")
GRAD_SELECT_LEDGER_PATH = os.path.join(HOME, "grad_select_ledger.json")
LANE = "paper_live"
CHAIN = "solana"

BANKROLL_START = 10.00   # full MAX_OPEN capacity from night one
NOTIONAL_USD = 1.00      # $1/shot, paper — $10 bankroll / 10 slots
MAX_OPEN = 10
DAILY_ENTRY_CAP = None    # NO daily cap by design; bankroll is the limit
MAX_GRAD_AGE_S = 1800.0   # 30-min window, same as grad_select
VERDICT_SAMPLE_CLOSES = 100
VERDICT_WINDOW_S = 7 * 86400  # 1 week from lane creation

# ---- paper_live legs (override grad.py's constants on this engine) ----
LEG_A_FRAC, LEG_A_TARGET = 0.25, 1.5   # first paper_live into strength
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0   # second paper_live
LEG_C_FRAC, LEG_C_TRAIL = 0.50, 0.30   # the core, on 30% trail
# Hard stop: PROTECT variant — breakeven once leg A banks (see
# make_engine); 24h time stop stays at grad.py's value.

# ---- buyback (dip) rule ----
BUYBACK_FRAC = 0.25        # of the original token count, once
BUYBACK_RETRACE = 0.30     # price must be >= 30% below peak
BUYBACK_CHECK_EVERY_S = 60.0
_LAST_BUYBACK_CHECK = 0.0

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")


# ---- engine (isolated grad.py instance with paper_live policy) ----------

def _preregistration():
    """Verdict terms for the TRANCHE lane, stamped into its ledger
    header at creation. Same shape as grad_select's, plus the
    paper_live legs, the buyback rule, and the 1-week deadline."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes OR %d days after lane creation, "
                 "whichever comes first, if mean pnl per close <= 0 "
                 "the lane halts permanently (killswitch lane "
                 "'paper_live'); if > 0 the lane survived its sample. "
                 "Zero closes at the deadline also halts (no "
                 "evidence). Re-enable is human-only. The verdict "
                 "is reported RAW and EX-TOP-1, plus a same-window "
                 "head-to-head vs grad_select (same entries, "
                 "different exits)."
                 % (VERDICT_SAMPLE_CLOSES, VERDICT_WINDOW_S // 86400)),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": None,
            "max_grad_age_s": MAX_GRAD_AGE_S,
            "bankroll_start": BANKROLL_START,
            "runner_rule_applies": False,
            "safety_gate": "NONE (by design — this lane tests exits, "
                           "not safety-gating)",
            "veto": "NONE",
            "filters": "grad_select's four evidence filters, "
                       "evaluated by grad_select.evaluate_candidate",
            "detection": "PumpPortal migration stream (live) + "
                         "GeckoTerminal backfill (30-min window)",
            "ladder": "25%@1.5x / 25%@3x / 50% trail 30% / PROTECT stop "
                        "(breakeven after leg A banks) / 24h",
            "buyback": "once per position, after >= 1 paper_live banked, "
                       "on >= 30% retrace from peak, price >= entry, "
                       "proceeds cover cost, age < 24h: rebuy 25% of "
                       "original tokens at honest fill, funded from "
                       "banked proceeds, riding remaining legs",
            "paper_note": "PAPER ONLY simulation of live trading with "
                          "a $10 account — NOT real money, NOT real "
                          "SOL, NOT real profit. Ledger is USD-native "
                          "(engine); SOL start amount stamped in "
                          "ledger header + launch note at launch SOL "
                          "price.",
        },
        "registered_ts": time.time(),
    }


def make_engine():
    """Load grad.py as a fresh, isolated module and override its
    lane policy for the TRANCHE lane: paper_live ledger/limits/legs/
    bankroll, no safety gate, no veto (the grad_raw overrides),
    grad_raw's FIRE-FIRST wrappers, paper_live preregistration.
    The grad_select and grad_raw module instances are never
    touched. Never raises on import shape."""
    spec = importlib.util.spec_from_file_location("paper_live_engine",
                                                  _GRAD_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    eng.LEDGER_PATH = LEDGER_PATH
    eng.LANE = LANE
    eng.BANKROLL_START = BANKROLL_START
    eng.NOTIONAL_USD = NOTIONAL_USD
    eng.MAX_OPEN = MAX_OPEN
    eng.DAILY_ENTRY_CAP = DAILY_ENTRY_CAP
    eng.MAX_GRAD_AGE_S = MAX_GRAD_AGE_S
    eng.VERDICT_SAMPLE_CLOSES = VERDICT_SAMPLE_CLOSES
    eng.LEG_A_FRAC, eng.LEG_A_TARGET = LEG_A_FRAC, LEG_A_TARGET
    eng.LEG_B_FRAC, eng.LEG_B_TARGET = LEG_B_FRAC, LEG_B_TARGET
    eng.LEG_C_FRAC, eng.LEG_C_TRAIL = LEG_C_FRAC, LEG_C_TRAIL
    eng.PROTECT_AFTER_A = True  # TEST VARIANT: after leg A banks,
    # the remaining legs' hard stop moves to breakeven (entry) instead
    # of -50% — the 2026-10-31 diagnosis found 31/31 leaked tranche
    # closes banked A then stopped at -50% (-$295.72).
    if _raw is not None:
        eng._safety_ok = _raw._no_safety_gate
        eng._veto_ok = _raw._no_veto
        _raw._ff_install(eng)
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
            note="paper_live test lane (2026-10-31 build); verdict: %s"
                 % pre["rule"])
    except Exception:
        return None


def seed_checkpoint(engine):
    """Anchor the lane in the tamper-evident verdict ledger with
    a kind=checkpoint record at 0 closes, and stamp the 1-week
    verdict deadline into the lane ledger. Once only. Never
    raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            if d.get("paper_live_seeded_ts"):
                return False
            now = time.time()
            d["paper_live_seeded_ts"] = now
            d["verdict_deadline_ts"] = now + VERDICT_WINDOW_S
            engine._write(d)
        engine._verdict_event(LANE, "checkpoint", {
            "closes": 0, "expectancy": 0.0,
            "note": "paper_live lane start (2026-10-30): grad_select "
                    "entries + paper_live exits (25%@1.5x / 25%@3x / "
                    "50% trail 30% + one dip buyback); pre-registered "
                    "100-close-or-1-week verdict, raw + ex-top-1 + "
                    "same-window head-to-head vs grad_select."})
        return True
    except Exception:
        return False


# ---- rejection journal ---------------------------------------------------

def _journal_rejection(engine, cand, verdict, now):
    """Append one filter rejection to the lane ledger's
    paper_live_journal (counters by reason + bounded recent list +
    counts_by_day in WAT dates, same shape as grad_select's
    select_journal). Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            j = d.get("paper_live_journal")
            if not isinstance(j, dict):
                j = {"counts": {}, "recent": [], "started_ts": now}
            counts = j.setdefault("counts", {})
            reason = str(verdict.get("reason") or "unknown")
            counts[reason] = int(counts.get(reason) or 0) + 1
            by_day = j.setdefault("counts_by_day", {})
            day = time.strftime("%Y-%m-%d", time.gmtime(now + 3600))
            day_counts = by_day.setdefault(day, {})
            day_counts[reason] = int(day_counts.get(reason) or 0) + 1
            recent = j.setdefault("recent", [])
            recent.append({
                "ts": now, "mint": str(cand.get("mint") or ""),
                "symbol": str(cand.get("symbol") or "?")[:32],
                "reason": reason,
                "values": verdict.get("values") or {}})
            j["recent"] = recent[-200:]
            d["paper_live_journal"] = j
            engine._write(d)
    except Exception:
        pass


def rejection_breakdown(engine):
    """Cumulative filter-rejection counters (for the daemon log
    and reports). Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
        return dict((d.get("paper_live_journal") or {}).get("counts")
                    or {})
    except Exception:
        return {}


# ---- entry stamping -------------------------------------------------------

def _stamp_detection(engine, mint, source, values, ff=None):
    """Stamp HOW this entry was detected and its full filter
    measurements onto the open position's entry_features.
    Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = d["positions"].get(mint)
            if pos is not None:
                pos["source"] = "graduation-paper_live"
                ef = pos.setdefault("entry_features", {})
                ef["detection"] = source
                ef["gate"] = "paper_live"
                ef["paper_live"] = dict(values or {})
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


# ---- detection scan (grad_select's shape, paper_live lane) -------------------

def scan_once(engine, feed=None, gt_rows=None, now=None,
              on_items=None, fresh_only=False):
    """One detection cycle for the TRANCHE lane. Returns counters
    incl. paper_live_rejected and a per-reason breakdown. Never
    raises. Fail-closed: without grad_select's evaluator, no
    entries are made at all."""
    out = {"stream": 0, "pools": 0, "watching": 0, "opened": 0,
           "skipped": 0, "dead": 0, "paper_live_rejected": 0,
           "paper_live_reasons": {}}
    try:
        return _scan_once(engine, feed, gt_rows, out, now, on_items,
                          fresh_only)
    except Exception as e:
        logger.warning("paper_live scan_once failed: %s", e)
        return out


def _scan_once(engine, feed, gt_rows, out, now, on_items=None,
               fresh_only=False):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    try:
        if engine._ks.is_halted(LANE):
            logger.warning("paper_live scan: lane halted by "
                           "killswitch — no entries (exits still run)")
            return out
    except Exception:
        pass
    if _sel is None:
        logger.warning("paper_live scan: grad_select evaluator missing "
                       "— no entries (fail-closed)")
        return out

    if not fresh_only and _raw is not None:
        _raw._ff_verify_pending(engine, now)

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
            _sel.note_items(items)
            for item in items:
                cand = (_raw.migration_to_candidate(item, now)
                        if _raw is not None else None)
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
                _exist = d["watchlist"][mint]
                if not str(_exist.get("pool_address") or "") and \
                        str(row.get("pool_address") or ""):
                    _exist["pool_address"] = str(row["pool_address"])
                continue
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

    # ---- filter stage: one verdict per mint, cached on the row --
    passed = []
    for cand in watch:
        mint = str(cand.get("mint") or "")
        verdict = cand.get("paper_live")
        if fresh_only and mint not in fresh_mints and \
                not isinstance(verdict, dict):
            continue
        if not isinstance(verdict, dict) or \
                verdict.get("verdict") not in ("pass", "reject"):
            verdict = _sel.evaluate_candidate(engine, cand, now)
            with engine._ledger_lock():
                d = engine._read()
                row = d["watchlist"].get(mint)
                if row is not None:
                    row["paper_live"] = verdict
                    engine._write(d)
            cand["paper_live"] = verdict
        if verdict["verdict"] == "reject":
            out["paper_live_rejected"] += 1
            reason = str(verdict.get("reason") or "unknown")
            out["paper_live_reasons"][reason] = \
                out["paper_live_reasons"].get(reason, 0) + 1
            _journal_rejection(engine, cand, verdict, now)
            with engine._ledger_lock():
                d = engine._read()
                d["watchlist"].pop(mint, None)
                d["dead_mints"][mint] = {
                    "ts": now, "reason": "paper_live: %s" % reason}
                engine._write(d)
            continue
        passed.append(cand)

    # ---- entry stage (fire-first for fresh stream candidates) --
    ff_ready = _raw is not None and \
        getattr(engine, "_ff_installed", False)
    ff_ctxs = {}
    if ff_ready and fresh_mints:
        todo = [c for c in passed
                if str(c.get("mint") or "") in fresh_mints]
        if len(todo) == 1:
            c = todo[0]
            ff_ctxs[str(c.get("mint") or "")] = \
                _raw._ff_build_ctx(engine, c)
        elif len(todo) > 1:
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=4) as ex:
                    built = list(ex.map(
                        lambda c: _raw._ff_build_ctx(engine, c), todo))
                for c, ctx in zip(todo, built):
                    ff_ctxs[str(c.get("mint") or "")] = ctx
            except Exception:
                ff_ctxs = {}
    for cand in passed:
        mint = str(cand.get("mint") or "")
        if fresh_only and mint not in fresh_mints:
            continue
        ctx = None
        if ff_ready and mint in fresh_mints:
            ctx = ff_ctxs.get(mint) or _raw._ff_build_ctx(engine, cand)
            if ctx is not None and not ctx.get("info"):
                logger.warning("paper_live fire-first fallback %s: %s",
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
                _raw._ff_register_pending(engine, mint, ctx["info"],
                                          time.time())
            elif ctx is not None:
                ff = {"source": "live_quote",
                      "fallback": ctx.get("why")
                      or "payload price unused"}
            _stamp_detection(
                engine, mint, str(cand.get("source") or "watchlist"),
                (cand.get("paper_live") or {}).get("values") or {},
                ff=ff)
        else:
            out["skipped"] += 1
            if final:
                with engine._ledger_lock():
                    d = engine._read()
                    d["watchlist"].pop(mint, None)
                    engine._write(d)
    return out


# ---- verdict reporting (raw + ex-top-1 + head-to-head) --------------------

def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def verdict_report(engine, end=None):
    """The pre-registered verdict readout for this lane: raw
    expectancy, ex-top-1 expectancy, and the same-window
    head-to-head vs grad_select (grad_select closes whose
    close_ts falls inside this lane's lifetime — the meaningful
    comparison, since both lanes share entries and differ only
    in exits). Read-only. Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
        closes = d.get("closes") or []
        pnls = [float(c.get("pnl_usd") or 0) for c in closes]
        rep = {
            "lane": LANE,
            "n_closes": len(closes),
            "net_usd": round(sum(pnls), 4),
            "expectancy_raw": round(_mean(pnls), 6),
            "expectancy_ex_top1": None,
            "verdict": d.get("verdict"),
            "deadline_ts": d.get("verdict_deadline_ts"),
        }
        if len(pnls) >= 2:
            rep["expectancy_ex_top1"] = round(
                _mean(sorted(pnls)[:-1]), 6)
        start = float(d.get("created_ts") or 0)
        end = float(end) if end else time.time()
        try:
            with open(GRAD_SELECT_LEDGER_PATH) as f:
                sd = json.load(f)
            sp = [float(c.get("pnl_usd") or 0)
                  for c in (sd.get("closes") or [])
                  if start <= float(c.get("close_ts") or 0) <= end]
            rep["head_to_head_grad_select_same_window"] = {
                "n_closes": len(sp),
                "net_usd": round(sum(sp), 4),
                "expectancy": round(_mean(sp), 6),
                "window_start": start, "window_end": end}
        except Exception:
            rep["head_to_head_grad_select_same_window"] = None
        return rep
    except Exception:
        return {}


def maybe_verdict_report(engine):
    """Attach the verdict report once when the in-ledger verdict
    lands (via the 100-close check at finalize), and enforce the
    1-week deadline verdict: at the deadline with no verdict yet,
    judge the closes so far (zero closes also halts, for no
    evidence), halt when expectancy <= 0, and anchor everything
    in the verdict ledger. Returns the report or None. Never
    raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            if d.get("verdict") and not d.get("paper_live_report"):
                pass  # attach the report below
            elif d.get("verdict"):
                return None
            else:
                deadline = float(d.get("verdict_deadline_ts") or 0) \
                    or float(d.get("created_ts") or 0) \
                    + VERDICT_WINDOW_S
                if time.time() < deadline:
                    return None
                closes = d.get("closes") or []
                pnls = [float(c.get("pnl_usd") or 0) for c in closes]
                exp = _mean(pnls) if pnls else 0.0
                halt = exp <= 0
                d["verdict"] = {
                    "ts": time.time(),
                    "n_closes": len(closes),
                    "expectancy": round(exp, 6),
                    "net_usd": round(sum(pnls), 4),
                    "action": ("HALT — deadline expectancy <= 0"
                               if halt else
                               "SURVIVED — deadline expectancy > 0"),
                    "trigger": ("deadline-no-closes" if not closes
                                else "deadline"),
                    "sample_closes": VERDICT_SAMPLE_CLOSES,
                }
                engine._write(d)
                if halt:
                    try:
                        engine._ks.halt_lane(
                            LANE,
                            "pre-registered verdict: deadline "
                            "expectancy %.4f <= 0 over %d closes"
                            % (exp, len(closes)))
                    except Exception:
                        pass
                logger.warning(
                    "paper_live DEADLINE VERDICT at %d closes: "
                    "expectancy %.4f -> %s",
                    len(closes), exp, d["verdict"]["action"])
        rep = verdict_report(engine)
        if not rep:
            return None
        with engine._ledger_lock():
            d = engine._read()
            d["paper_live_report"] = rep
            engine._write(d)
        try:
            engine._verdict_event(LANE, "checkpoint", {
                "closes": rep.get("n_closes"),
                "expectancy": rep.get("expectancy_raw"),
                "expectancy_ex_top1": rep.get("expectancy_ex_top1"),
                "head_to_head_grad_select":
                    rep.get("head_to_head_grad_select_same_window"),
                "note": "paper_live pre-registered verdict report "
                        "(raw + ex-top-1 + same-window head-to-head "
                        "vs grad_select)"})
        except Exception:
            pass
        return rep
    except Exception:
        return None


# ---- buyback (dip) rule -----------------------------------------------------

def _buyback_due(pos, px, filled, now):
    """Pure buyback decision. Returns (True, cost, tokens) when a
    buyback should fire, else (False, reason, 0). Inputs: the
    open position dict, the current quoted price, and the honest
    (adverse) fill price for the buyback. Never raises."""
    try:
        if pos.get("buyback_used"):
            return (False, "already used", 0.0)
        legs = pos.get("legs") or {}
        la = legs.get("a") or {}
        lb = legs.get("b") or {}
        if not (la.get("done") or lb.get("done")):
            return (False, "no paper_live banked", 0.0)
        entry_price = float(pos.get("entry_price") or 0)
        if entry_price <= 0 or px <= 0:
            return (False, "no price", 0.0)
        mult = px / entry_price
        if mult < 1.0:
            return (False, "below entry", 0.0)
        peak = float(pos.get("peak_price") or entry_price)
        if px > peak * (1.0 - BUYBACK_RETRACE):
            return (False, "retrace < 30%", 0.0)
        if now - float(pos.get("entry_ts") or 0) > 24 * 3600:
            return (False, "past time stop", 0.0)
        tokens = BUYBACK_FRAC * float(pos.get("tokens") or 0)
        if tokens <= 0:
            return (False, "no tokens", 0.0)
        cost = tokens * filled
        if cost <= 0:
            return (False, "no cost", 0.0)
        if float(pos.get("realized_usd") or 0) < cost:
            return (False, "proceeds insufficient", 0.0)
        return (True, cost, tokens)
    except Exception as e:
        return (False, "error: %s" % str(e)[:80], 0.0)


def maybe_buyback(engine, now=None):
    """Execute at most one dip buyback per open position, when
    _buyback_due fires. Throttled to BUYBACK_CHECK_EVERY_S.
    Buybacks are entry-like, so a halted lane never buys back
    (exits still run via poll_exits). Returns the list of
    buybacks executed. Never raises."""
    global _LAST_BUYBACK_CHECK
    now = time.time() if now is None else now
    if now - _LAST_BUYBACK_CHECK < BUYBACK_CHECK_EVERY_S:
        return []
    _LAST_BUYBACK_CHECK = now
    done = []
    try:
        if engine._ks.is_halted(LANE):
            return []
    except Exception:
        pass
    try:
        positions = engine.open_positions()
    except Exception:
        return []
    if isinstance(positions, dict):
        # Real grad engine returns {mint: position}; the test fake
        # returns a list. Iterating the dict yields mint strings,
        # which broke every live buyback check (pos.get on a str).
        positions = list(positions.values())
    for pos in positions or []:
        try:
            mint = pos.get("mint") or ""
            chain = pos.get("chain") or CHAIN
            quoted = engine.quote_price(chain, mint)
            if not quoted or quoted <= 0:
                continue
            liq = pos.get("last_liquidity") or \
                (pos.get("entry_snapshot") or {}).get("liquidity")
            est_tokens = BUYBACK_FRAC * float(pos.get("tokens") or 0)
            est_size = est_tokens * quoted
            try:
                filled = engine._fills.honest_entry(
                    quoted, est_size, liq)
            except Exception:
                filled = quoted
            fire, cost, tokens = _buyback_due(pos, quoted, filled,
                                              now)
            if not fire:
                continue
            with engine._ledger_lock():
                d = engine._read()
                live = d["positions"].get(mint)
                if live is None or live.get("buyback_used"):
                    continue
                # Re-check proceeds on the live record: the
                # position may have banked or closed since listing.
                if float(live.get("realized_usd") or 0) < cost:
                    continue
                live["tokens"] = float(live.get("tokens") or 0) \
                    + tokens
                live["realized_usd"] = round(
                    float(live.get("realized_usd") or 0) - cost, 6)
                live["buyback_used"] = True
                ef = live.setdefault("entry_features", {})
                ef["buyback"] = {
                    "ts": now, "price": filled, "tokens": tokens,
                    "cost": round(cost, 6)}
                engine._write(d)
            done.append({"mint": mint, "symbol": pos.get("symbol"),
                         "price": filled, "tokens": tokens,
                         "cost": round(cost, 6)})
            logger.warning(
                "paper_live BUYBACK %s %.0f tokens at $%.8g "
                "(cost $%.2f, funded from banked proceeds)",
                pos.get("symbol"), tokens, filled, cost)
        except Exception as e:
            # Never call pos.get() here: if pos is a mint string
            # (dict-shaped positions), that raises again and escapes
            # maybe_buyback entirely.
            _mint = pos.get("mint", "?")[:12] if isinstance(
                pos, dict) else str(pos)[:12]
            logger.warning("paper_live buyback %s failed: %s", _mint, e)
            continue
    return done
