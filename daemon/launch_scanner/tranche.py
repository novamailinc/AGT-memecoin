#!/usr/bin/env python3
"""Tranche lane (2026-10-30, user-ordered): grad_select's ENTRIES,
5FG8SAT5-style EXITS, paper only.

Why this lane exists: the 5FG8SAT5 wallet traced on STONKLANA
(76.5 SOL, 34 live positions, zero failed transactions) does not
trade like our lanes. It trades around a core position: it trims
into strength in rising tranches, buys back a dip once, and keeps
a core riding, instead of our fixed ladder that sells everything
on fixed multiples. Its entries are unknown (never traced), so
this lane does NOT copy its entries. It copies only its exits,
on top of grad_select's entries, which already passed a verdict.
Same entries, different exits: if tranche wins, the exits added
the edge; if it loses, they did not.

Entries: EXACTLY grad_select's four evidence filters, evaluated
by grad_select.evaluate_candidate (shared code, shared recorder
index): serial deployer (>= 3 launches/24h rejected), fast curve
(TTG <= 3600s AND <= 250 curve signatures), canonical pools
only, copycat tickers (>= 3 other same-ticker launches/24h
rejected). Fail-closed on unknown. NO safety gate, NO veto.
Detection: live PumpPortal migration stream + GeckoTerminal
60s backfill over the 30-minute window, FIRE-FIRST payload
pricing (grad_raw's machinery, installed on the engine).

Exits (the tranche ladder, overriding grad.py's leg constants
on this lane's isolated engine instance):
  leg A: 25% at 1.5x (first tranche into strength)
  leg B: 25% at 3x (second tranche)
  leg C: 50% on 30% trailing stop from peak (the core)
  hard stop -50%, 24h time stop (unchanged from grad.py)
Buyback (the 5FG8SAT5 dip rule, once per position): after at
least one tranche has banked, if price has retraced >= 30%
from peak AND is still at or above entry AND banked proceeds
cover the cost AND the position is younger than 24h, rebuy
25% of the original token count at an honest (adverse) fill.
Banked proceeds fund it: realized_usd drops by the cost,
tokens rise, entry notional is UNCHANGED (so the close PnL
remains total cash in minus total cash out: leg proceeds,
minus buyback cost, minus original stake). The rebought tokens
ride the remaining legs. One buyback per position, ever.
Bookkeeping note: the interim bankroll overstates cash by the
buyback cost until the position closes (the cost settles at
finalize through realized_usd); with $6.25-scale buybacks this
is immaterial, and closes settle it exactly.

Design notes (read before touching this file):
- grad_select.py itself is NEVER modified by this lane. Its
  scan/evaluate code is reused only where it is lane-neutral:
  evaluate_candidate (pure filter decisions, no ledger writes,
  no lane references) and the shared recorder index via
  note_items. The scan loop is COPIED here (not reused) because
  grad_select.scan_once hardcodes its halt check to its own
  lane and stamps its own labels; reusing it would check the
  wrong killswitch lane and mislabel tranche positions.
- Consequence of the copy: this lane's rejection journal key
  is "tranche_journal" (same shape as select_journal, incl.
  counts_by_day in WAT dates) and its position stamps read
  "graduation-tranche" / gate "tranche".
- Bankroll starts at $500 (not grad_select's $100): the lane
  must be able to hold its full MAX_OPEN 20 from night one so
  the 1-week window yields a real sample. The verdict is on
  expectancy per close, which bankroll size does not bias.

PRE-REGISTERED VERDICT (stamped into the ledger header): at
100 closes OR 7 days after lane creation, whichever comes
first, if mean pnl per close <= 0 the lane halts permanently
(killswitch lane "tranche" {100,100}, human-only re-enable).
Zero closes at the deadline also halts (no evidence). The
verdict is reported RAW and EX-TOP-1, plus a same-window
head-to-head vs grad_select (same entries, different exits).

Paper only. Free-only: PumpPortal stream, GeckoTerminal,
DexScreener, Helius RPC via reserves.rpc, local recorder files.
NO Telegram cards in v1 (ledger + verdict chain only).
"""
import importlib.util
import json
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger("forex_agent.daemon.launch_scanner.tranche")

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

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "tranche_ledger.json")
GRAD_SELECT_LEDGER_PATH = os.path.join(HOME, "grad_select_ledger.json")
LANE = "tranche"
CHAIN = "solana"

BANKROLL_START = 500.00   # full MAX_OPEN capacity from night one
NOTIONAL_USD = 25.00      # same stake as grad_select ($25/shot, paper)
MAX_OPEN = 20
DAILY_ENTRY_CAP = None    # NO daily cap by design; bankroll is the limit
MAX_GRAD_AGE_S = 1800.0   # 30-min window, same as grad_select
VERDICT_SAMPLE_CLOSES = 100
VERDICT_WINDOW_S = 7 * 86400  # 1 week from lane creation

# ---- tranche legs (override grad.py's constants on this engine) ----
LEG_A_FRAC, LEG_A_TARGET = 0.25, 1.5   # first tranche into strength
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0   # second tranche
LEG_C_FRAC, LEG_C_TRAIL = 0.50, 0.30   # the core, on 30% trail
# Hard stop -50% and 24h time stop stay at grad.py's values.

# ---- buyback (dip) rule ----
BUYBACK_FRAC = 0.25        # of the original token count, once
BUYBACK_RETRACE = 0.30     # price must be >= 30% below peak
BUYBACK_CHECK_EVERY_S = 60.0
_LAST_BUYBACK_CHECK = 0.0

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")


# ---- engine (isolated grad.py instance with tranche policy) ----------

def _preregistration():
    """Verdict terms for the TRANCHE lane, stamped into its ledger
    header at creation. Same shape as grad_select's, plus the
    tranche legs, the buyback rule, and the 1-week deadline."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes OR %d days after lane creation, "
                 "whichever comes first, if mean pnl per close <= 0 "
                 "the lane halts permanently (killswitch lane "
                 "'tranche'); if > 0 the lane survived its sample. "
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
            "ladder": "25%@1.5x / 25%@3x / 50% trail 30% / -50% stop / 24h",
            "buyback": "once per position, after >= 1 tranche banked, "
                       "on >= 30% retrace from peak, price >= entry, "
                       "proceeds cover cost, age < 24h: rebuy 25% of "
                       "original tokens at honest fill, funded from "
                       "banked proceeds, riding remaining legs",
        },
        "registered_ts": time.time(),
    }


def make_engine():
    """Load grad.py as a fresh, isolated module and override its
    lane policy for the TRANCHE lane: tranche ledger/limits/legs/
    bankroll, no safety gate, no veto (the grad_raw overrides),
    grad_raw's FIRE-FIRST wrappers, tranche preregistration.
    The grad_select and grad_raw module instances are never
    touched. Never raises on import shape."""
    spec = importlib.util.spec_from_file_location("tranche_engine",
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
    # Chase guard (2026-10-31, tightened same night from 600s to
    # 120s): never buy a candidate that waited more than 2 minutes
    # since first sighting. Today's ledger: prompt entries (<60s)
    # won, delayed entries lost across every wait kind — slot waits
    # (lane full, 1284 max-open skips), quote waits (40 no-quote +
    # 39 no-pool-address skips on the 60-300s bucket alone) and
    # evaluation waits. Prompt entries decide in ~13s, so 120s keeps
    # a wide margin for legitimate slowness while dropping the
    # waits the ledger says lose. Live tranche only; test lanes
    # keep their own entry behavior.
    eng.MAX_SLOT_WAIT_S = 120.0
    # Exit fix (2026-10-31 night, user order): port the
    # tranche_protect breakeven stop. Once leg A banks, the
    # remaining legs' hard stop moves to breakeven, so a banked
    # position can no longer retrace to -50% (the sized leak:
    # 31 positions -$295.72 in-sample, 18 positions -$177.16 on
    # Oct 31 alone). Same helper protect already proves live
    # (14 protect-be closes Oct 31, +$94). Protect lane keeps
    # running as the control record.
    eng.PROTECT_AFTER_A = True
    eng.VERDICT_SAMPLE_CLOSES = VERDICT_SAMPLE_CLOSES
    eng.LEG_A_FRAC, eng.LEG_A_TARGET = LEG_A_FRAC, LEG_A_TARGET
    eng.LEG_B_FRAC, eng.LEG_B_TARGET = LEG_B_FRAC, LEG_B_TARGET
    eng.LEG_C_FRAC, eng.LEG_C_TRAIL = LEG_C_FRAC, LEG_C_TRAIL
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
            note="tranche lane (2026-10-30 build); verdict: %s"
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
            if d.get("tranche_seeded_ts"):
                return False
            now = time.time()
            d["tranche_seeded_ts"] = now
            d["verdict_deadline_ts"] = now + VERDICT_WINDOW_S
            engine._write(d)
        engine._verdict_event(LANE, "checkpoint", {
            "closes": 0, "expectancy": 0.0,
            "note": "tranche lane start (2026-10-30): grad_select "
                    "entries + tranche exits (25%@1.5x / 25%@3x / "
                    "50% trail 30% + one dip buyback); pre-registered "
                    "100-close-or-1-week verdict, raw + ex-top-1 + "
                    "same-window head-to-head vs grad_select."})
        return True
    except Exception:
        return False


# ---- rejection journal ---------------------------------------------------

def _journal_rejection(engine, cand, verdict, now):
    """Append one filter rejection to the lane ledger's
    tranche_journal (counters by reason + bounded recent list +
    counts_by_day in WAT dates, same shape as grad_select's
    select_journal). Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            j = d.get("tranche_journal")
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
            d["tranche_journal"] = j
            engine._write(d)
    except Exception:
        pass


def rejection_breakdown(engine):
    """Cumulative filter-rejection counters (for the daemon log
    and reports). Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
        return dict((d.get("tranche_journal") or {}).get("counts")
                    or {})
    except Exception:
        return {}


# ---- entry stamping -------------------------------------------------------

def _deployer_band(n):
    """24h launch-count band for the deployer forward test (2026-11-20).

    "0" = brand-new deployer, "1-2" = 1-2 launches, "3+" = 3 or
    more, "unknown" = the count could not be read (fail-closed:
    never defaulted to "0", so an unknown never farms the 0 band).
    Pure function; journal-only, never gates an entry. The count
    comes from grad_select.evaluate_candidate's values, which the
    tranche filter stage already computed — no recompute, no RPC."""
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


def _stamp_detection(engine, mint, source, values, ff=None):
    """Stamp HOW this entry was detected and its full filter
    measurements onto the open position's entry_features.
    Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = d["positions"].get(mint)
            if pos is not None:
                pos["source"] = "graduation-tranche"
                ef = pos.setdefault("entry_features", {})
                ef["detection"] = source
                ef["gate"] = "tranche"
                ef["tranche"] = dict(values or {})
                # Deployer forward test (2026-11-20): instrument
                # ONLY. Band off the deployer count already in
                # values (grad_select filter stage). Never gates
                # an entry; the report reads
                # entry_features.deployer_band off closes.
                ef["deployer_band"] = _deployer_band(
                    (values or {}).get("deployer_launches_24h"))
                # Conviction (advanced build, piece 1): the score
                # that admitted this entry rides into the close so
                # the gate can be judged on real paper closes.
                if isinstance((values or {}).get("conviction"), dict):
                    ef["conviction"] = dict(values["conviction"])
                # Venue (advanced build, piece 8, 2026-11-21):
                # which pool/venue this entry traded on, and
                # whether the pool was verified. Journal-only;
                # never gates an entry.
                if _sel is not None:
                    try:
                        ef["venue"] = _sel._venue_for(values, pos, ff)
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
                # Decision memory (2026-11-17): merge the filter
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


# ---- detection scan (grad_select's shape, tranche lane) -------------------

def scan_once(engine, feed=None, gt_rows=None, now=None,
              on_items=None, fresh_only=False):
    """One detection cycle for the TRANCHE lane. Returns counters
    incl. tranche_rejected and a per-reason breakdown. Never
    raises. Fail-closed: without grad_select's evaluator, no
    entries are made at all."""
    out = {"stream": 0, "pools": 0, "watching": 0, "opened": 0,
           "skipped": 0, "dead": 0, "tranche_rejected": 0,
           "tranche_reasons": {}, "select_rejected": 0,
           "select_reasons": {}}
    try:
        return _scan_once(engine, feed, gt_rows, out, now, on_items,
                          fresh_only)
    except Exception as e:
        logger.warning("tranche scan_once failed: %s", e)
        return out


def _scan_once(engine, feed, gt_rows, out, now, on_items=None,
               fresh_only=False):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state
    # error — the old is_halted() escaped malformed state as a
    # KeyError the except: pass swallowed, so entry proceeded.
    if not engine._ks.gate(LANE):
        logger.warning("tranche scan: lane halted by "
                       "killswitch — no entries (exits still run)")
        return out
    if _sel is None:
        logger.warning("tranche scan: grad_select evaluator missing "
                       "— no entries (fail-closed)")
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
    if not fresh_only and not gt_rows:
        try:
            _sched = _sel._scan_schedule_check(engine, feed, now)
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
        _sitting, _so_info = _sel._sit_out_check(engine, now)
        if _sitting:
            out["sit_out"] = str(
                _so_info.get("reason") or "cold_bleed")
            return out
    except Exception:
        pass

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

    # Scan-schedule signal (advanced build, piece 9): the stream
    # count of this scan is the fresh-graduation signal the NEXT
    # schedule check reads.
    if _sel is not None:
        try:
            _sel._note_scan_stream(LANE, out.get("stream", 0))
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
        verdict = cand.get("tranche")
        if fresh_only and mint not in fresh_mints and \
                not isinstance(verdict, dict):
            continue
        if not isinstance(verdict, dict) or \
                verdict.get("verdict") not in (
                    "pass", "reject", "review"):
            verdict = _sel.evaluate_candidate(engine, cand, now, lane=LANE)
            with engine._ledger_lock():
                d = engine._read()
                row = d["watchlist"].get(mint)
                if row is not None:
                    row["tranche"] = verdict
                    engine._write(d)
            cand["tranche"] = verdict
        if verdict["verdict"] == "reject":
            out["tranche_rejected"] += 1
            reason = str(verdict.get("reason") or "unknown")
            out["tranche_reasons"][reason] = \
                out["tranche_reasons"].get(reason, 0) + 1
            _journal_rejection(engine, cand, verdict, now)
            with engine._ledger_lock():
                d = engine._read()
                d["watchlist"].pop(mint, None)
                d["dead_mints"][mint] = {
                    "ts": now, "reason": "tranche: %s" % reason}
                engine._write(d)
            continue
        if verdict["verdict"] == "review":
            # REVIEW sentinel (advanced build, piece 1): a WEAK
            # candidate never enters — it is already in the review
            # queue (enqueued at evaluation); pop it from the
            # watchlist and mark it dead so it is not re-evaluated
            # or entered later.
            out["tranche_reviewed"] = \
                out.get("tranche_reviewed", 0) + 1
            with engine._ledger_lock():
                d = engine._read()
                d["watchlist"].pop(mint, None)
                d["dead_mints"][mint] = {
                    "ts": now, "reason": "review: conviction_review"}
                engine._write(d)
            continue
        passed.append(cand)

    # ---- Rule A entry-age re-check (Item 6, shared grad_select
    # helper): a cached "pass" was earned at evaluation time;
    # re-check the age at entry time before any entry RPC is spent
    # (fire-first contexts build next). Without this, tranche
    # entered 50-115s-old graduations Nov 18 02:16-05:36 UTC that
    # grad_select's recheck would have blocked. Mirrors grad_raw.
    if _sel is not None:
        passed = _sel.recheck_entry_age(passed, engine, out)

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
    # ---- Rule A chain-age gate (2026-11-17, SlotClock, shared
    # grad_select helper): detection age can look fresh while the
    # chain says minutes — the fire-first ctx already fetched the
    # migration tx, so its slot is free. Mirrors grad_select.
    if _sel is not None:
        passed = _sel.recheck_chain_age(passed, engine, out,
                                        ff_ctxs, now)
    for cand in passed:
        mint = str(cand.get("mint") or "")
        if fresh_only and mint not in fresh_mints:
            continue
        ctx = None
        if ff_ready and mint in fresh_mints:
            ctx = ff_ctxs.get(mint) or _raw._ff_build_ctx(engine, cand)
            if ctx is not None and not ctx.get("info"):
                logger.warning("tranche fire-first fallback %s: %s",
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
                (cand.get("tranche") or {}).get("values") or {},
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
            if d.get("verdict") and not d.get("tranche_report"):
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
                    "tranche DEADLINE VERDICT at %d closes: "
                    "expectancy %.4f -> %s",
                    len(closes), exp, d["verdict"]["action"])
        rep = verdict_report(engine)
        if not rep:
            return None
        with engine._ledger_lock():
            d = engine._read()
            d["tranche_report"] = rep
            engine._write(d)
        try:
            engine._verdict_event(LANE, "checkpoint", {
                "closes": rep.get("n_closes"),
                "expectancy": rep.get("expectancy_raw"),
                "expectancy_ex_top1": rep.get("expectancy_ex_top1"),
                "head_to_head_grad_select":
                    rep.get("head_to_head_grad_select_same_window"),
                "note": "tranche pre-registered verdict report "
                        "(raw + ex-top-1 + same-window head-to-head "
                        "vs grad_select)"})
        except Exception:
            pass
        return rep
    except Exception:
        return None


# ---- buyback (dip) rule -----------------------------------------------------

def _buyback_impact_refusal(pa):
    """Fail-closed price-impact gate for a buyback. Returns a refusal reason (never None today): the owner limit
    max_price_impact_pct is a Jupiter quote price-impact figure and the model-fill buyback path has no such measurement."""
    cap = pa.max_price_impact_pct()
    if cap is None:
        return "no owner-approved max_price_impact_pct; buyback blocked fail-closed"
    return ("no Jupiter quote-impact measurement on the model-fill buyback path (owner limit %s%%); buyback blocked fail-closed" % (cap,))


_BB_POSTFILL_REFUSALS = ("refused:post_quote_gate", "refused:fill_economics_invalid", "refused:proceeds_below_actual_debit", "refused:account")


def _bb_fingerprint(live, pa):
    """Deterministic state fingerprint for same-condition refusal suppression: the banked proceeds on the lane and the account's cash.
    NOT a strategy limit and NOT an attempt cap: a post-fill refusal is retried only after one of these changed (or an explicit
    live['buyback_retry_requested'] is set). None when the account cash cannot be read (then no suppression claim is made)."""
    try:
        snap = pa.snapshot()
        if snap is None or snap.get("degraded"):
            return None
        return [round(float(live.get("realized_usd") or 0.0), 6), round(float(snap.get("cash")), 6)]
    except Exception:
        return None


def _bb_mark(engine, d, live, state, why=None, fop=None, fp=None):
    """Persist the buyback INTENT/outcome on the lane position (durable, inside the caller's ledger lock). State machine:
    quoting (fill op recorded BEFORE the quote) -> filled (quote done, gates passed, BEFORE the account add) -> applied | refused:<why>.
    A crash leaves the last state, so an orphan fill or an applied add is always explained by a lane record. Prior attempts are kept
    (last 5). Best effort: a failed write raises to the caller (nothing is applied after a failed pre-fill write)."""
    it = dict(live.get("buyback_intent") or {})
    if fop is not None and it.get("fill_op") not in (None, fop):
        hist = list(live.get("buyback_attempts") or [])
        hist.append(it)
        live["buyback_attempts"] = hist[-5:]
        it = {}
    if fop is not None:
        it["fill_op"] = fop
        it.setdefault("ts", time.time())
        # COMPLETE durable list of every fill op ever attempted for this position (never truncated; buyback_attempts is only a
        # convenience tail). Every journaled orphan fill is findable from the lane record.
        _all = list(live.get("buyback_fill_ops") or [])
        if fop not in [x.get("fop") for x in _all if isinstance(x, dict)]:
            _all.append({"fop": fop, "ts": it["ts"]})
        live["buyback_fill_ops"] = _all
    if fp is not None:
        it["fp"] = fp
    it["state"] = state
    if why is not None:
        it["why"] = str(why)[:200]
    live["buyback_intent"] = it
    engine._write(d)


def _bb_evidence(engine, live, stake, qty):
    """Adoption evidence: find the journaled fill named by the lane intent and accept it ONLY if its own debit and quantity equal
    the account's applied add. Returns (status, fill) where status is recovered | no_intent | filler_unavailable | fill_missing |
    mismatch. The account's numbers stay authoritative in every case; anything but 'recovered' is recorded as missing evidence."""
    it = live.get("buyback_intent") or {}
    fop = it.get("fill_op")
    if not fop:
        return "no_intent", None
    try:
        df = engine._demo_filler()
    except Exception:
        df = None
    if df is None:
        return "filler_unavailable", None
    try:
        fl = df.get_fill(fop)
    except Exception:
        return "filler_unavailable", None
    if not isinstance(fl, dict) or fl.get("status") != "filled" or fl.get("side") != "buy":
        return "fill_missing", None
    try:
        ok = (abs(float(fl["total_debit_usd"]) - float(stake)) <= 1e-6
              and abs(float(fl["qty_tokens"]) - float(qty)) <= 1e-9 * max(1.0, abs(float(qty))))
    except Exception:
        ok = False
    return ("recovered", fl) if ok else ("mismatch", None)


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
            return (False, "no tranche banked", 0.0)
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
    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state
    # error (see the scan gate above).
    if not engine._ks.gate(LANE):
        return []
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
            if engine._legacy_unreconciled_buyback(pos):
                logger.error("tranche LEGACY UNRECONCILED BUYBACK %s: lane units include bought tokens the shared account never "
                             "received; its remaining legs may strand (pending_exit account_remaining_qty_mismatch). Detected only: "
                             "nothing healed, owner ledger untouched; operator resolution required.", str(pos.get("mint") or "?")[:16])
            mint = pos.get("mint") or ""
            chain = pos.get("chain") or CHAIN
            try:
                _st_row = engine._fresh_tape(mint)
                _buy_row = dict(_st_row) if isinstance(_st_row, dict) else None   # the row THIS decision read (freshness gate below)
            except Exception:
                _buy_row = None
            _q_req = time.time()   # observation time of THIS quote request (post-quote age gate at commit)
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
            _adopting = False
            # CRASH RECOVERY: an account add that was applied before the lane book was written must be adopted even when the
            # price conditions no longer hold (the account already holds those units and cost); never left as an unexplained residue.
            if not fire and not pos.get("buyback_used") and pos.get("pa_pos_id"):
                try:
                    _pa0 = engine._paper_account()
                    _ad0 = _pa0.applied_add("%s:buyback:%s:%s" % (engine.LANE, mint, pos.get("pa_pos_id"))) if _pa0 is not None else None
                except Exception as _e0:
                    # Unknown is never proof of absence: an adoption lookup that cannot be proven skips this position THIS cycle, loudly.
                    logger.error("tranche buyback %s: adoption lookup failed (%r); position skipped this cycle (unknown != no applied add)", str(mint)[:16], _e0)
                    continue
                if _ad0 is not None:
                    fire, cost, tokens, _adopting = True, float(_ad0["stake"]), float(_ad0["qty"]), True
            if not fire:
                continue
            try:
                from daemon.launch_scanner import entry_guard as _eg
            except ImportError:
                import entry_guard as _eg
            # ledger lock, then (flag on) the cycle FAULT lock held across the guard read AND the write (see entry_guard.commit_barrier). A fault-lock timeout or import failure raises here, is logged by the handler below, and nothing is written (fail closed).
            with engine._ledger_lock(), _eg.commit_barrier(engine, _eg.ENTRY_FAULT_WAIT_S):
                # Commit-time mandatory entry gate (a buyback buys tokens: it is an entry). Refusal is visible; exits unaffected.
                try:
                    _why = _eg.mandatory_entry_refusal(engine.LANE)
                except Exception as _e:
                    _why = "entry guard unavailable (%r)" % (_e,)
                if _why:
                    logger.warning("tranche buyback refused at commit: %s", _why)
                    continue
                d = engine._read()
                live = d["positions"].get(mint)
                if live is None or live.get("buyback_used"):
                    continue
                # Re-check proceeds on the live record: the
                # position may have banked or closed since listing.
                if not _adopting and float(live.get("realized_usd") or 0) < cost:
                    continue
                # RECONCILED BUYBACK (root-cause fix): a buyback is an entry. With the shared paper account enabled the cost is
                # reserved THERE first (same fail-closed admission as an entry: freshness, spendable cash, lane cap, mandatory and
                # account gates, provider health) with a stable op_id, and the lane book is edited only on success. A refusal leaves
                # both books untouched. Crash recovery: if the account already applied this op_id (crash before the lane write) the
                # applied stake/qty are ADOPTED exactly, never re-issued with fresh numbers.
                # NO fallback: the shared account is the capital authority. mandatory_entry_refusal already refuses when it is
                # disabled; an engine without an account interface, a None account, or an account that raises is a refusal, never a
                # lane-only buyback.
                _pa = engine._paper_account()
                if _pa is None:
                    logger.warning("tranche buyback refused: shared paper account unavailable; fail-closed")
                    continue
                _sim = bool(engine._is_sim(live))   # a quote-simulation position buys back through the REAL DemoFiller quote path below
                _pid = live.get("pa_pos_id")
                if not _pid:
                    logger.warning("tranche buyback refused: position has no account lot id (cannot reconcile); fail-closed")
                    continue
                _bop = "%s:buyback:%s:%s" % (engine.LANE, mint, _pid)
                _bb_fill, _fop = None, None
                _applied = _pa.applied_add(_bop)
                if _applied is not None:
                    if (_applied.get("lane"), _applied.get("mint"), _applied.get("pos_id")) != (engine.LANE, mint, _pid):
                        logger.warning("tranche buyback refused: applied account op identity mismatch; fail-closed")
                        continue
                    cost = float(_applied["stake"])
                    tokens = float(_applied["qty"])
                    _adopting = True   # also when the price conditions still hold (fire True before the lock): evidence recovery must run
                    logger.warning("tranche buyback: ADOPTING account-applied add %s (crash recovery)", _bop)
                else:
                    # SAME-CONDITION SUPPRESSION (no invented attempt limit): after a post-fill refusal the next quote would hit the same
                    # refusal and journal another orphan fill. Re-attempt only when the banked proceeds or account cash changed, or an
                    # explicit retry is requested on the lane record. Unknown fingerprint (account unreadable) makes no suppression claim.
                    _it0 = live.get("buyback_intent") or {}
                    _fp0 = _bb_fingerprint(live, _pa)
                    if (_it0.get("state") in _BB_POSTFILL_REFUSALS and _fp0 is not None and _it0.get("fp") == _fp0
                            and not live.get("buyback_retry_requested")):
                        logger.warning("tranche buyback suppressed: same-condition refusal (%s) and banked proceeds/account cash unchanged; "
                                       "no quote, no new fill (set buyback_retry_requested to force)", _it0.get("state"))
                        continue
                    live.pop("buyback_retry_requested", None)
                    # CHEAP PRE-GATE before any quote: the account must hold spendable cash for the planned cost (the account add refuses
                    # otherwise, after a fill). The exact actual-debit and fee check still runs after the fill.
                    if _fp0 is not None and _fp0[1] < cost:
                        logger.warning("tranche buyback skipped before quote: account cash %.6f < planned cost %.6f", _fp0[1], cost)
                        continue
                    _age_why = _eg.quote_age_refusal(_buy_row, _pa.max_data_age_s(), time.time())
                    if _age_why:
                        logger.warning("tranche buyback refused: %s", _age_why)
                        continue
                    # POST-QUOTE gate at commit (same function as the entry path): the quote request must still be inside the owner age cap.
                    _pq = _eg.post_quote_refusal({"provenance": {"quote_requested_at": _q_req}}, None, False,
                                                 _pa.max_data_age_s(), time.time())
                    if _pq:
                        logger.warning("tranche buyback refused: %s", _pq)
                        continue
                    # FILL-IMPACT gate (fail-closed). The owner limit max_price_impact_pct is a JUPITER QUOTE price-impact limit; modelled
                    # slippage is NOT interchangeable with it. Today this model-fill path has NO Jupiter quote-impact measurement, so
                    # _buyback_impact_refusal always refuses (reason names whether the owner limit is set). A positive buyback needs a real
                    # Jupiter-quote impact measure wired in there. Entry-side model-fill protection is tracked separately.
                    _bb_fill, _fop = None, None
                    if not _sim:
                        _imp = _buyback_impact_refusal(_pa)
                        if _imp:
                            logger.warning("tranche buyback refused: %s", _imp)
                            continue
                    else:
                        # REAL quote-simulation buyback: the fill comes from the DemoFiller (a real Jupiter V1 quote through the
                        # sim executor, impact checked against the owner cap on the QUOTE), never from the model fill. The owner cap
                        # unset or an unavailable filler REFUSES. The fill op id is fresh per attempt: a crash before the account add
                        # moved no money, so a retry re-quotes; a crash after the add is recovered by the applied-add adoption above
                        # (the account's own stake/qty are authoritative, never a re-issued fill).
                        _cap = _pa.max_price_impact_pct()
                        if _cap is None:
                            logger.warning("tranche buyback refused: %s", _buyback_impact_refusal(_pa))
                            continue
                        try:
                            _capok = (not isinstance(_cap, bool)) and 0.0 < float(_cap) < float("inf")   # NaN/inf/0/negative: refuse (a NaN cap would never trip the executor's `impact > cap`)
                        except Exception:
                            _capok = False
                        if not _capok:
                            logger.warning("tranche buyback refused: max_price_impact_pct %r is not a finite positive number; fail-closed", _cap)
                            continue
                        try:
                            _dfl = engine._demo_filler()
                        except Exception as _de:
                            logger.warning("tranche buyback refused: demo filler unavailable (%r); fail-closed", _de)
                            continue
                        if _dfl is None:
                            logger.warning("tranche buyback refused: quote-simulation filler unavailable; fail-closed")
                            continue
                        _fop = "%s:fill:%s" % (_bop, uuid.uuid4().hex)   # globally unique: a collision could replay another attempt's fill
                        # DURABLE INTENT BEFORE THE QUOTE: the fill op is on the lane record before any journal fill exists, so an
                        # orphan fill or a crash is always explained and the adoption path can find the evidence. A failed write
                        # raises (outer handler): no quote was taken.
                        _bb_mark(engine, d, live, "quoting", fop=_fop)
                        try:
                            _bb_fill = _dfl.buy(_fop, mint, cost, _cap)
                        except Exception as _fe:
                            _w = "demo fill failed (%s: %s)" % (type(_fe).__name__, getattr(_fe, "reason", None) or str(_fe)[:120])
                            logger.warning("tranche buyback refused: %s; nothing applied", _w)
                            _bb_mark(engine, d, live, "refused:no_fill", _w)
                            continue
                        _pq2 = _eg.post_quote_refusal(_bb_fill, None, False, _pa.max_data_age_s(), time.time())
                        if _pq2:
                            logger.warning("tranche buyback refused: %s", _pq2)
                            _bb_mark(engine, d, live, "refused:post_quote_gate", _pq2, fp=_bb_fingerprint(live, _pa))
                            continue
                        try:
                            _bst = float(_bb_fill["total_debit_usd"]); _bqty = float(_bb_fill["qty_tokens"]); _bpx = float(_bb_fill["price_usd"])
                        except Exception:
                            _bst = _bqty = _bpx = 0.0
                        if not all(0 < v < float("inf") for v in (_bst, _bqty, _bpx)):
                            logger.warning("tranche buyback refused: fill economics invalid; nothing applied")
                            _bb_mark(engine, d, live, "refused:fill_economics_invalid", fp=_bb_fingerprint(live, _pa))
                            continue
                        if float(live.get("realized_usd") or 0) < _bst:
                            logger.warning("tranche buyback refused: banked proceeds below the actual fill debit; nothing applied")
                            _bb_mark(engine, d, live, "refused:proceeds_below_actual_debit", fp=_bb_fingerprint(live, _pa))
                            continue
                        cost, tokens, filled = _bst, _bqty, _bpx
                        _bb_mark(engine, d, live, "filled")   # BEFORE the account add: a crash now is 'filled, add outcome unknown'
                    _ok, _why2 = _pa.add_to_position(engine.LANE, mint, _pid, cost, tokens, _bop, ts=now)
                    if not _ok:
                        logger.warning("tranche buyback refused by account: %s", _why2)
                        if _bb_fill is not None:
                            _bb_mark(engine, d, live, "refused:account", _why2, fp=_bb_fingerprint(live, _pa))
                        continue
                _open = [k for k in ("a", "b", "c") if not (live.get("legs", {}).get(k) or {}).get("done")]
                _ofrac = sum(float(live["legs"][k]["frac"]) for k in _open)
                if not _open or not (_ofrac > 0):
                    # no leg left to sell the bought units: with an account add this cannot happen (buyback needs a banked leg and
                    # an open position), so it is an inconsistency: leave the lane untouched and say so loudly.
                    logger.error("tranche buyback: no open leg to carry the bought units (account add may already be applied); lane left unchanged")
                    if _bb_fill is not None:
                        _bb_mark(engine, d, live, "account_applied_lane_unchanged:no_open_leg")
                    continue
                for _k in _open:
                    _lg = live["legs"][_k]
                    _lg["buyback_qty"] = float(_lg.get("buyback_qty") or 0.0) + tokens * float(_lg["frac"]) / _ofrac
                live["buyback_qty_total"] = float(live.get("buyback_qty_total") or 0.0) + tokens
                live["realized_usd"] = round(
                    float(live.get("realized_usd") or 0) - cost, 6)
                live["buyback_used"] = True
                _it = dict(live.get("buyback_intent") or {})
                _it["state"] = "applied"
                live["buyback_intent"] = _it
                ef = live.setdefault("entry_features", {})
                ef["buyback"] = {
                    "ts": now, "price": filled, "tokens": tokens,
                    "cost": round(cost, 6)}
                if _adopting:
                    # crash recovery: the account's own stake/qty are authoritative (never re-issued). Fill evidence is recovered
                    # from the journal ONLY when the lane intent names a fill whose debit and qty equal the applied add; otherwise the
                    # record says exactly what is missing (never "complete").
                    _evs, _evf = _bb_evidence(engine, live, cost, tokens)
                    ef["buyback"]["adopted_from_account"] = True
                    ef["buyback"]["fill_evidence"] = _evs
                    if _evf is not None:
                        _bb_fill, _fop = _evf, (live.get("buyback_intent") or {}).get("fill_op")
                        try:
                            filled = float(_evf["price_usd"])   # the fill's own price, not the model's
                        except Exception:
                            pass
                    else:
                        logger.error("tranche buyback ADOPTED without fill evidence (%s): account numbers kept, provenance unavailable", _evs)
                if _bb_fill is not None:
                    ef["buyback"].update({"execution_source": _bb_fill.get("execution_source"), "fill_op": _fop,
                                          "fee_usd": _bb_fill.get("fee_usd"), "signature": _bb_fill.get("signature"),
                                          "synthetic_signature": _bb_fill.get("synthetic_signature"),
                                          "provenance": _bb_fill.get("provenance")})
                engine._write(d)
            done.append({"mint": mint, "symbol": pos.get("symbol"),
                         "price": filled, "tokens": tokens,
                         "cost": round(cost, 6)})
            logger.warning(
                "tranche BUYBACK %s %.0f tokens at $%.8g "
                "(cost $%.2f, funded from banked proceeds)",
                pos.get("symbol"), tokens, filled, cost)
        except Exception as e:
            # Never call pos.get() here: if pos is a mint string
            # (dict-shaped positions), that raises again and escapes
            # maybe_buyback entirely.
            _mint = pos.get("mint", "?")[:12] if isinstance(
                pos, dict) else str(pos)[:12]
            logger.warning("tranche buyback %s failed: %s", _mint, e)
            continue
    return done
