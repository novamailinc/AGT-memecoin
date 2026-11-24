#!/usr/bin/env python3
"""Graduation SELECT lane (2026-10-24, user-ordered): grad_raw's
spray, filtered ONLY by the selection rules with external
large-sample evidence — head-to-head against grad_raw (the control).

Why this lane exists: grad_raw (buy EVERY graduation, no filters)
is the only profitable lane on the book (+$120.15 on 2026-10-23,
pre-registered verdict SURVIVED), and every internally-invented
filter we ever ran lost money. But the published large-sample
literature names a small set of selection effects that are NOT our
inventions, and none of them has ever been measured on our paper:

1. SERIAL DEPLOYERS — tokens whose deployer launched >= 3 tokens
   in the trailing 24h: median return -80.2% at T+10min (Aporius,
   n=6,817). REJECT when the deployer's trailing-24h launch count
   >= 3. A launch is counted from the P4 recorder's birth log
   (PumpPortal create events carry the creator in extra.trader) —
   NOT from the serial-rugger registry, which is outcome-based
   (it counts dead/rugged outcomes, not launches). The creator
   falls back to the bonding curve's create-transaction signer
   when the birth record is missing. Counts are the recorder's
   census: a deployer with >= 3 CAPTURED launches is definitively
   serial (the filter's precision does not depend on coverage);
   coverage_start is journaled on every decision for audit.
2. FAST CURVE, FEW TRADES — the strongest positive predictor in
   the literature (Marino, 655,770 tokens): fast SOL accumulation
   through FEW trades. Operationalized exactly, from free data:
   the token's bonding curve (PDA of the mint) is paginated via
   getSignaturesForAddress; curve_trades = total signature count
   on the curve account (create + buys/sells + migrate; each swap
   is one signature — a documented proxy for curve trades), and
   time-to-graduate (TTG) = migration instant - blockTime of the
   curve's OLDEST signature (the create tx). PASS requires
   TTG <= 3600s AND curve_trades <= 250 (implied average fill
   >= 85 SOL / 250 = 0.34 SOL/trade at the canonical ~85 SOL
   curve completion). Fail-closed: unknown inputs reject.
3. CANONICAL POOLS ONLY — the 2026-10-23 fee audit: canonical
   PumpSwap pools (created by the pump.fun migration) follow the
   tiered fee schedule; non-canonical flat-fee (0.30%) pools are
   third-party creations. A PumpPortal migration event IS the
   canonical pool's creation, so stream candidates pass by
   construction; a GeckoTerminal backfill row passes when its
   pool address equals the pool in the recorder's migration
   record for that mint — or, when the recorder has no record
   (stream down, 2026-11-20), when its pool address is one of
   the migration transaction's account keys. The migration tx
   is found by scanning the curve's newest successful
   signatures around the pool's creation second (the
   candidate's created_ts, ±2s — GeckoTerminal's
   pool_created_at lags the migration by ~1s) for the one
   whose logs say "Instruction: Migrate". Newest-successful
   alone is WRONG — post-migration SweepProtocolFee txs also
   touch the curve (verified live 2026-11-20) — and an exact-
   second match is WRONG too (GT's 1s lag). At most 1
   signatures call + 3 tx calls, paced, backfill candidates
   only. A non-migration pick is skipped, never a verdict.
   Unknown -> reject.
4. COPYCAT TICKERS — skip when the same ticker has already
   launched >= 3 times in the trailing 24h, counted from the
   ticker-saturation tally's per-ticker launch timestamps (exact
   rolling window (now-24h, now]; the candidate's own birth is
   always in-window at graduation evaluation, so it is subtracted
   unconditionally). Stale tally (> 30 min) -> reject (fail-closed).

Design (mirrors grad_raw exactly, so the ledgers differ ONLY in
the filters): grad.py's engine loaded as an ISOLATED instance,
NO RugCheck safety gate, NO veto (the gated grad lane is the
safety comparison; grad_raw is the no-filter control), live
PumpPortal migration stream + GeckoTerminal 60s backfill,
FIRE-FIRST migration-tx payload pricing (grad_raw's machinery),
$1 paper shots, honest fills + 1.25% venue fees, 25% cost gate,
dedupe-ever, NO daily cap, MAX_OPEN 20, $100 paper bankroll,
the user's ladder (50%@2x / 25%@3x / 25% trail 30% / -50% stop /
24h), F2 wired incl. the 50x upside guard. NO Telegram cards.

Every filter's measured values are journaled on every entry
(entry_features.select) and every rejection (ledger
select_journal: counters by reason + bounded recent list), so
the thresholds can be re-judged on evidence later.

PRE-REGISTERED VERDICT (stamped into the ledger header): at 100
closes, expectancy <= 0 halts the lane permanently (killswitch
lane "grad_select" {100,100}, human-only re-enable). The verdict
is reported RAW and EX-TOP-1, plus a same-window head-to-head
vs grad_raw (grad_raw closes inside this lane's lifetime).

Paper only. Free-only: PumpPortal stream, GeckoTerminal,
DexScreener, Helius RPC via reserves.rpc, local recorder files.
"""
import glob
import importlib.util
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.grad_select")

try:
    import grad_raw as _raw
except Exception:  # pragma: no cover
    _raw = None

try:
    import reserves as _reserves
except Exception:  # pragma: no cover
    _reserves = None

try:
    import curve_cache as _curve_cache
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import curve_cache as _curve_cache
    except Exception:  # pragma: no cover
        _curve_cache = None

try:
    import deployers as _deployers
except Exception:
    # Flat import fails in the trader: deployers.py uses a relative
    # import (from . import helius_ratelimit) that only resolves
    # under the daemon.launch_scanner package. Without this fallback
    # _deployers is None and the curve-oldest-tx creator fallback
    # silently never runs, so every missing-birth candidate lands in
    # unknown_serial. Package import proven to resolve in-trader.
    try:
        from daemon.launch_scanner import deployers as _deployers
    except Exception:  # pragma: no cover
        _deployers = None

try:
    import saturation as _saturation
except Exception:  # pragma: no cover
    _saturation = None

try:
    import ds_ticker as _ds_ticker
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import ds_ticker as _ds_ticker
    except Exception:  # pragma: no cover
        _ds_ticker = None

try:
    import trials as _trials
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import trials as _trials  # type: ignore
    except Exception:
        _trials = None  # type: ignore

# Decision memory (2026-11-17): fail-soft import — a missing
# module leaves evaluation byte-identical. Lessons are context
# only: evaluate_candidate journals which lesson ids were in
# context (values["lessons_in_context"]) and NEVER gates on them.
try:
    import lessons as _lessons
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import lessons as _lessons
    except Exception:
        _lessons = None  # type: ignore

# Bear scorer (2026-11-17): fail-soft import — a missing module
# leaves evaluation byte-identical. JOURNAL-ONLY in v1: the score
# is journaled on every evaluation (values["bear_score"]) and
# NEVER gates an entry. A gate needs a calibration win first
# (scripts/bear-calibrate) and then only as its own distinct
# reject reason ("bear_score").
try:
    import bear_score as _bear
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import bear_score as _bear
    except Exception:
        _bear = None  # type: ignore

# Conviction scoring + REVIEW queue (advanced build, piece 1,
# 2026-11-21): fail-soft import — a missing module leaves
# evaluation byte-identical (the gate is skipped, the filters +
# Rule A decide alone, exactly as before). When present, the gate
# runs ONLY after the filters + Rule A pass, so it can only remove
# entries, never add one; WEAK candidates go to the REVIEW
# sentinel queue instead of entering.
try:
    import conviction as _conviction
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import conviction as _conviction
    except Exception:
        _conviction = None  # type: ignore

try:
    import review_queue as _review_queue
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import review_queue as _review_queue
    except Exception:
        _review_queue = None  # type: ignore

# Declared vendor fallback chains (advanced build, piece 4,
# 2026-11-21): fail-soft import — a missing module leaves
# evaluation byte-identical. JOURNAL-ONLY: each check stamps
# which vendor answered into ctx.values["vendor_chain"] (keyed
# by need) so the select journal records the fallback path of
# every curve read, copycat tally, canonical verification, and
# deployer lookup. NEVER gates an entry.
try:
    import vendors as _vendors
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import vendors as _vendors
    except Exception:
        _vendors = None  # type: ignore

# Venue classification + scan scheduling (advanced build,
# pieces 8 + 9, 2026-11-21): fail-soft imports — a missing
# module leaves stamping/scanning byte-identical (venue stamps
# unverified, schedule fails open to FULL). JOURNAL-ONLY.
try:
    import venue as _venue
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import venue as _venue
    except Exception:
        _venue = None  # type: ignore
try:
    import scan_schedule as _scan_schedule
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import scan_schedule as _scan_schedule
    except Exception:
        _scan_schedule = None  # type: ignore

# Regime-aware stakes + sit-out (advanced build, piece 2,
# 2026-11-21): fail-soft import — a missing module leaves the
# scan loop byte-identical (no sit-out gate). When present, a
# cold, bleeding lane stops scanning for entries (exits still
# run) and the decision is journaled, not silent.
try:
    import regime as _regime
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import regime as _regime
    except Exception:
        _regime = None  # type: ignore

# Transaction-outcome guards (2026-11-17, Batch 3): meta.err
# gating + whole-line log matching (tx_guards.py). REQUIRED, not
# fail-soft: _creator_from_oldest_tx refuses reverted create
# txs through it, so a missing module must fail loud at trader
# startup instead of silently naming deployers from dead txs.
try:
    import tx_guards as _txg
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import tx_guards as _txg
    except Exception:
        raise

# Check pipeline (2026-11-17): two-phase pre-buy checks
# (check_pipeline.py). REQUIRED, not fail-soft: _evaluate is
# built on it, so a missing module must fail loud at trader
# startup instead of silently rejecting every candidate.
try:
    import check_pipeline as _cp
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import check_pipeline as _cp
    except Exception:
        raise

# SlotClock (2026-11-17): chain age for Rule A — on-demand,
# throttled getSlot, no background thread (a 2s background reader
# would burn ~43k credits/day against a 1M/month free tier).
# Fail-soft import — a missing module leaves evaluation
# byte-identical; the chain-age gate simply never fires and
# detection age governs, exactly as before.
_SLOT_CLOCK = None


def _slot_clock():
    global _SLOT_CLOCK
    if _SLOT_CLOCK is None:
        try:
            from daemon.launch_scanner import slot_clock as _sc
        except Exception:
            try:
                import slot_clock as _sc  # type: ignore
            except Exception:
                _sc = None
        try:
            _SLOT_CLOCK = _sc.SlotClock() if _sc is not None else None
        except Exception:
            _SLOT_CLOCK = None
    return _SLOT_CLOCK


HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "grad_select_ledger.json")
GRAD_RAW_LEDGER_PATH = os.path.join(HOME, "grad_raw_ledger.json")
LANE = "grad_select"
CHAIN = "solana"

BANKROLL_START = 100.00
NOTIONAL_USD = 25.00    # CEO-ordered 2026-10-24: $1 -> $25/shot (paper only)
MAX_OPEN = 20             # task terms (grad_raw runs 150; select is 20)
DAILY_ENTRY_CAP = None    # NO daily cap by design; bankroll is the limit
MAX_GRAD_AGE_S = 1800.0   # 30-min window, same as grad_raw
ENTRY_MAX_AGE_S = 30.0    # Rule A (2026-11-16, CEO tuning on ledger
                          # evidence): enter only within 30s of
                          # graduation. Last 500 stored closes:
                          # <=30s net +$8,235.72 (272 closes),
                          # >30s net -$959.75 (228 closes).
VERDICT_SAMPLE_CLOSES = 100

# ---- the four filters: exact operational definitions -------------------
SERIAL_MAX_LAUNCHES_24H = 3      # reject at >= 3 launches / trailing 24h
TTG_MAX_S = 3600.0               # curve must complete within 1 hour
CURVE_TRADES_MAX = 250           # <= 250 curve signatures (see docstring)
GRAD_SOL = 85.0                  # canonical curve completion, SOL (real)
COPYCAT_MAX_OTHERS_24H = 3       # reject at >= 3 OTHER same-ticker launches
SATURATION_STALE_S = 1800.0      # tally older than this = unknown
CURVE_SIG_LIMIT = CURVE_TRADES_MAX + 1  # 251: ONE bounded
# getSignaturesForAddress call is the whole curve-stats budget
# (2026-11-17, zero-RPC entry). The filter only needs the
# <=250 verdict: a page that returns fewer than the limit is
# exhausted (exact count, oldest signature IS the create tx); a
# full page means trades > 250 and the candidate rejects on the
# spot. The old 4-page x 1000 pagination could never run past
# page 1 in practice (a full 1000-page already exceeds the bar),
# so this changes no verdict — it makes the one-call bound
# explicit, shrinks the response, and stops the capped path
# reporting a non-creation timestamp as created_ts.

_GRAD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "grad.py")


# ---- engine (isolated grad.py instance with select policy) -------------

def _preregistration():
    """Verdict terms for the SELECT lane, stamped into its ledger
    header at creation. Same shape as grad/grad_raw plus the
    ex-top-1 and head-to-head reporting this lane pre-registers."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'grad_select'); "
                 "if > 0 the lane survived its sample. Re-enable is "
                 "human-only. The verdict is reported RAW and "
                 "EX-TOP-1, plus a same-window head-to-head vs "
                 "grad_raw." % VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": None,
            "max_grad_age_s": MAX_GRAD_AGE_S,
            "runner_rule_applies": False,
            "safety_gate": "NONE (by design — this lane tests "
                           "evidence-selection, not safety-gating)",
            "veto": "NONE",
            "filters": {
                "serial_deployer": "reject if deployer launches in "
                                   "trailing 24h >= %d (recorder "
                                   "birth log)" % SERIAL_MAX_LAUNCHES_24H,
                "fast_curve_few_trades": "reject unless TTG <= %.0fs "
                                         "and curve signatures <= %d"
                                         % (TTG_MAX_S, CURVE_TRADES_MAX),
                "canonical_only": "migration-event pools (stream) or "
                                  "recorder-migration pool match "
                                  "(backfill) only",
                "copycat_ticker": "reject if other same-ticker "
                                  "launches in trailing 24h >= %d"
                                  % COPYCAT_MAX_OTHERS_24H,
            },
            "detection": "PumpPortal migration stream (live) + "
                         "GeckoTerminal backfill (30-min window)",
            "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h",
        },
        "registered_ts": time.time(),
    }


def make_engine():
    """Load grad.py as a fresh, isolated module and override its
    lane policy for the SELECT lane: no safety gate, no veto (the
    grad_raw overrides), select ledger/limits/preregistration, and
    grad_raw's FIRE-FIRST wrappers. The gated and raw lanes' own
    module instances are never touched. Never raises on import
    shape."""
    spec = importlib.util.spec_from_file_location("grad_select_engine",
                                                  _GRAD_PY)
    eng = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eng)
    eng.LEDGER_PATH = LEDGER_PATH
    eng.LANE = LANE
    eng.NOTIONAL_USD = NOTIONAL_USD
    eng.MAX_OPEN = MAX_OPEN
    eng.DAILY_ENTRY_CAP = DAILY_ENTRY_CAP
    eng.MAX_GRAD_AGE_S = MAX_GRAD_AGE_S
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
            note="grad-select lane (2026-10-24 build); verdict: %s"
                 % pre["rule"])
    except Exception:
        return None


def seed_checkpoint(engine):
    """Anchor the lane in the tamper-evident verdict ledger with a
    kind=checkpoint record at 0 closes (momentum/watcher pattern).
    Once only — a flag in the lane ledger prevents re-seeding on
    every boot. Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            if d.get("select_seeded_ts"):
                return False
            d["select_seeded_ts"] = time.time()
            engine._write(d)
        engine._verdict_event(LANE, "checkpoint", {
            "closes": 0, "expectancy": 0.0,
            "note": "grad_select lane start (2026-10-24): grad_raw "
                    "spray + evidence filters (serial deployer, "
                    "fast-curve/few-trades, canonical-only, "
                    "copycat ticker); pre-registered 100-close "
                    "verdict, raw + ex-top-1 + head-to-head."})
        return True
    except Exception:
        return False


# ---- recorder birth/migration index (filter data) ----------------------
# The P4 recorder logs every PumpPortal birth + migration the
# bot's feeds see, into HOME/recorder/events-YYYYMMDD*.jsonl.
# This index scans those files INCREMENTALLY (per-file byte
# offsets) so steady-state refreshes read only new bytes. The
# daemon's own feed items are also folded in live via note_items.

_INDEX = {
    "by_mint": {},        # mint -> {"trader","ts","bonding_curve"}
    "by_trader": {},      # trader -> [ts, ...] (pruned to 48h)
    "by_mig": {},         # mint -> {"pool","ts"}
    "files": {},          # path -> byte offset consumed
    "coverage_start": None,   # earliest birth ts ever indexed
    "available": False,   # any recorder data seen at all
}
_INDEX_LOCK = threading.Lock()
_INDEX_PRUNE_S = 48 * 3600

# Cap on concurrent evaluate_candidate() calls process-wide.
# scan_once runs from TWO threads (main loop + fire-first entry
# thread) and the filter stage parallelizes within a thread, so
# without this cap a burst could put 8 RPC-heavy evaluations on
# the shared free-tier Helius quota at once. Four in flight is
# the same width the entry stage already uses for _ff_build_ctx.
_EVAL_SEM = threading.BoundedSemaphore(4)


def _recorder_dir():
    try:
        import recorder as _rec
        return _rec.recorder_dir()
    except Exception:
        return os.path.join(HOME, "recorder")


def _index_paths(now):
    """Recorder event files for yesterday + today (UTC), including
    rotated .N siblings — the trailing-24h window lives there."""
    out = []
    try:
        rdir = _recorder_dir()
        for days_ago in (1, 0):
            day = time.strftime("%Y%m%d",
                                time.gmtime(now - days_ago * 86400))
            out.extend(sorted(glob.glob(
                os.path.join(rdir, "events-%s*.jsonl" % day))))
    except Exception:
        pass
    return out


def _index_absorb(item, kind):
    """Fold one parsed recorder/feed item into the index."""
    try:
        extra = item.get("extra") or {}
        ts = float(item.get("created_ts") or item.get("ts") or 0)
        mint = str(item.get("mint") or "")
        if not mint:
            return
        if kind == "birth":
            if mint in _INDEX["by_mint"]:
                return      # first record wins — the same birth
                            # arrives via the live feed AND the
                            # recorder file; counting it twice
                            # would double-count the launch.
            trader = str(extra.get("trader") or "")
            _INDEX["by_mint"][mint] = {
                "trader": trader, "ts": ts,
                "bonding_curve": str(extra.get("bonding_curve") or ""),
                # Event state (2026-11-17): opening virtual
                # reserves + mcap + initial buy as the create
                # event carried them, so evaluation can use the
                # event instead of re-reading the curve. Raw
                # units as sent (vTokens raw, vSol SOL).
                "virtual_token_reserves": extra.get(
                    "virtual_token_reserves"),
                "virtual_sol_reserves": extra.get(
                    "virtual_sol_reserves"),
                "market_cap_sol": extra.get("market_cap_sol"),
                "initial_buy_sol": extra.get("initial_buy_sol"),
                "state_from_event": bool(item.get(
                    "state_from_event"))}
            if trader and ts > 0:
                _INDEX["by_trader"].setdefault(trader, []).append(ts)
                cs = _INDEX["coverage_start"]
                if cs is None or ts < cs:
                    _INDEX["coverage_start"] = ts
            _INDEX["available"] = True
        elif kind == "migration":
            _INDEX["by_mig"][mint] = {
                "pool": str(extra.get("market_id") or ""),
                "ts": ts}
            _INDEX["available"] = True
    except Exception:
        pass


def note_items(items):
    """Fold raw feed items (the trader daemon's on_items stream:
    pumpportal-new births + pumpportal-migration rows) into the
    index immediately — no waiting for the recorder's flush.
    Never raises."""
    try:
        with _INDEX_LOCK:
            for item in items or []:
                src = (item or {}).get("source")
                if src == "pumpportal-new":
                    _index_absorb(item, "birth")
                elif src == "pumpportal-migration":
                    _index_absorb(item, "migration")
    except Exception:
        pass


def _index_refresh(now):
    """Incrementally absorb new recorder bytes. Prunes trader
    timestamps older than 48h. Never raises."""
    try:
        with _INDEX_LOCK:
            for path in _index_paths(now):
                try:
                    size = os.path.getsize(path)
                except Exception:
                    continue
                off = int(_INDEX["files"].get(path) or 0)
                if size < off:      # rotated/replaced: rescan
                    off = 0
                if size == off:
                    continue
                try:
                    with open(path, "rb") as f:
                        f.seek(off)
                        blob = f.read()
                except Exception:
                    continue
                # Consume only complete lines; keep the tail bytes
                # for the next refresh (a line may be mid-write).
                nl = blob.rfind(b"\n")
                if nl < 0:
                    continue
                _INDEX["files"][path] = off + nl + 1
                for line in blob[:nl + 1].decode(
                        "utf-8", "replace").splitlines():
                    if '"birth"' not in line and \
                            '"migration"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    kind = rec.get("kind")
                    if kind in ("birth", "migration"):
                        _index_absorb(rec, kind)
            cutoff = now - _INDEX_PRUNE_S
            for trader in list(_INDEX["by_trader"]):
                keep = [t for t in _INDEX["by_trader"][trader]
                        if t >= cutoff]
                if keep:
                    _INDEX["by_trader"][trader] = keep
                else:
                    del _INDEX["by_trader"][trader]
    except Exception:
        pass


def _birth_record(mint):
    rec = _INDEX["by_mint"].get(mint)
    return dict(rec) if rec else None


def _trader_launches_24h(trader, now):
    """(count, coverage_start) from the index, or (None, None)
    when the index has no data at all (instrument down)."""
    if not _INDEX["available"]:
        return None, None
    stamps = _INDEX["by_trader"].get(trader) or []
    n = sum(1 for t in stamps if now - t <= 86400)
    return n, _INDEX["coverage_start"]


# ---- bonding-curve stats (filter 2 + creator fallback) -----------------

def _rpc_err_text(res):
    """Short, safe text for a non-list RPC reply: error code + message
    when present, a distinct marker when the RPC returned nothing
    (bare None must never again render as the string "None"),
    otherwise the reply itself, truncated. Never raises,
    never carries credentials (public RPC error bodies only)."""
    try:
        if res is None:
            return "rpc_returned_none"
        if isinstance(res, dict):
            err = res.get("error")
            if isinstance(err, dict):
                return (str(err.get("code")) + ": "
                        + str(err.get("message"))[:160])
            return str(res)[:200]
        return str(res)[:200]
    except Exception:
        return "unprintable"


def _curve_cache_store(curve_addr, count, oldest):
    """Best-effort store of raw curve data in the shared cache.
    Failures are silent and harmless (the next lane just calls
    live). Never raises."""
    if _curve_cache is None:
        return
    try:
        _curve_cache.curve_cache().put(curve_addr, count, oldest)
    except Exception:
        pass


def _curve_stats_from_cached(cached, out):
    """Fill the _curve_stats result from cached raw curve data.

    Applies the EXACT same derivation as the live path: a count
    over CURVE_TRADES_MAX is capped (the oldest sig on a full page
    is not the create tx — never reported as creation data); a
    smaller count is exhausted, with creation data from the oldest
    signature. Only raw data is cached, so the filters re-derive
    pass/reject identically from a hit or a live read."""
    count = cached["count"]
    oldest = cached.get("oldest") or {}
    out["rpc_source"] = "cache"
    out["cache"] = "hit"
    if count > CURVE_TRADES_MAX:
        out.update({"trades": count, "capped": True,
                    "created_ts": None, "oldest_sig": None,
                    "creation_slot": None})
        return out
    out.update({"trades": count, "exhausted": True,
                "created_ts": float(oldest["blockTime"])
                if oldest.get("blockTime") else None,
                "oldest_sig": str(oldest.get("signature") or "")
                or None,
                "creation_slot": int(oldest["slot"])
                if isinstance(oldest.get("slot"), int) else None})
    return out


def _curve_stats(curve_addr):
    """One bounded getSignaturesForAddress call on the bonding
    curve (2026-11-17: CURVE_SIG_LIMIT = 251, the whole budget).

    Returns {"trades": int|None, "created_ts": float|None,
    "oldest_sig": str|None, "creation_slot": int|None,
    "capped": bool, "exhausted": bool}.
    trades is EXACT when the call exhausts (fewer than the limit
    returned); when the page is full, trades is a lower bound >
    max and the candidate rejects on the bar, so no second call
    is ever spent. oldest_sig is the curve's CREATE transaction
    ONLY when exhausted=True — callers must not treat it as the
    creation tx otherwise. creation_slot is the create tx's slot
    (same condition); a capped result reports created_ts None and
    oldest_sig None, never a mid-curve timestamp as creation
    data. Any RPC failure returns trades=None (unknown) with a
    diagnostic "reason" string (empty_curve_addr, no_reserves,
    non_list_result, exception). Never raises."""
    out = {"trades": None, "created_ts": None, "oldest_sig": None,
           "creation_slot": None, "capped": False, "exhausted": False,
           "reason": None, "rpc_error": None, "rpc_source": None,
           "cache": "miss"}
    if not curve_addr:
        out["reason"] = "empty_curve_addr"
        return out
    if _reserves is None:
        out["reason"] = "no_reserves"
        return out
    # Shared curve cache (2026-11-18): a hit reuses the raw curve
    # data another lane fetched within the TTL instead of spending
    # a Helius call. Cache errors fail open to a live call;
    # failures are never cached. Never raises.
    if _curve_cache is not None:
        try:
            cached = _curve_cache.curve_cache().get(curve_addr)
        except Exception:
            cached = None
        if cached is not None:
            return _curve_stats_from_cached(cached, out)
    try:
        res = _reserves.rpc("getSignaturesForAddress",
                            [curve_addr, {"limit": CURVE_SIG_LIMIT}])
        # Which provider answered (helius / public-rpc / None):
        # CurveStatsCheck stamps it into ctx.values so the select
        # journal records the source of every curve read.
        _src_fn = getattr(_reserves, "last_rpc_source", None)
        out["rpc_source"] = _src_fn() if callable(_src_fn) else None
        if not isinstance(res, list):
            out["reason"] = "non_list_result"
            out["rpc_error"] = _rpc_err_text(res)
            return out
        if not res:
            out["exhausted"] = True
            out["trades"] = 0
            _curve_cache_store(curve_addr, 0, None)
            return out
        count = len(res)
        # Signatures arrive newest first: the LAST is the oldest.
        oldest = res[-1] or {}
        # Store the raw data for the other lanes (best-effort; a
        # failed put is harmless — the next lane calls live).
        _curve_cache_store(curve_addr, count, oldest)
        oldest_ts = oldest.get("blockTime")
        oldest_sig = str(oldest.get("signature") or "")
        oldest_slot = oldest.get("slot")
        if count > CURVE_TRADES_MAX:
            # Over the bar: the exact count is irrelevant, and the
            # oldest signature on this page is NOT the create tx
            # (more signatures exist) — never report it as
            # creation data.
            out.update({"trades": count, "capped": True,
                        "created_ts": None, "oldest_sig": None,
                        "creation_slot": None})
            return out
        out.update({"trades": count, "exhausted": True,
                    "created_ts": float(oldest_ts) if oldest_ts
                    else None,
                    "oldest_sig": oldest_sig,
                    "creation_slot": int(oldest_slot)
                    if isinstance(oldest_slot, int) else None})
        return out
    except Exception as e:
        return {"trades": None, "created_ts": None,
                "oldest_sig": None, "creation_slot": None,
                "capped": False, "exhausted": False,
                "reason": "exception",
                "rpc_error": type(e).__name__, "rpc_source": None,
                "cache": "miss"}


def _creator_from_oldest_tx(oldest_sig):
    """The create transaction's first signer = the deployer.
    Never raises."""
    try:
        if not oldest_sig or _reserves is None or _deployers is None:
            return None
        tx = _reserves.rpc("getTransaction", [oldest_sig, {
            "encoding": "jsonParsed",
            # v1 cap, not 0: modern pump.fun create txs are version
            # 1, and a 0 cap makes the node refuse them (-32015),
            # so the fallback silently yields no creator.
            "maxSupportedTransactionVersion": 1}])
        if not tx:
            return None
        # meta.err gate (2026-11-17, Batch 3): a CreateEvent can
        # survive in a REVERTED tx's logs, and a reverted create
        # names no deployer — the mint doesn't exist. Refuse.
        if not _txg.tx_succeeded(tx) or not _txg.response_signature_matches(tx,oldest_sig):
            return None
        return _deployers._first_signer(tx)
    except Exception:
        return None


# How many migration-tx candidates the chain fallback fetches
# before giving up (fail-closed). The migration is found by
# scanning the newest successful curve signatures around the
# pool's creation second for the Migrate instruction — a single
# pick is not reliable (see _canonical_pool_from_chain).
_MIG_TX_TRIES = 3


def _canonical_pool_from_chain(curve_addr, pool, created_ts):
    """Resolve a backfill candidate's canonical pool from the chain.

    Used ONLY when the recorder has no migration record for the
    mint (PumpPortal stream down) and the candidate is a
    GeckoTerminal backfill carrying a pool_address. ``created_ts``
    is the pool's creation time (the candidate's created_ts, from
    GeckoTerminal's pool_created_at). At most 1 signatures call
    plus up to _MIG_TX_TRIES transaction calls (4 paced RPC calls
    max), all through _reserves.rpc. That is one more tx call
    than the 2-call budget the task first sketched — the single-
    pick version was proven wrong on live data (see below), and
    a bounded scan is the only correct identification; the
    volume stays small (backfill candidates are ~20/hour, the
    pacer allows ~540/hour) and the gate stays fail-closed.

    1. getSignaturesForAddress on the bonding curve (limit 251).
       Candidate migration sigs are the SUCCESSFUL signatures
       whose blockTime is within [created_ts - 2, created_ts + 1]:
       GeckoTerminal's pool_created_at can lag the migration by
       ~1s (verified live 2026-11-20: GT said 12:26:36Z for a
       pool whose migration tx has blockTime 1795177595, one
       second earlier), so an exact-second match misses real
       migrations. A failed post-migration trade against the
       closed curve carries err != null and is skipped; a missing
       err field reads as success (the standard response always
       carries it). Candidates are tried newest first (highest
       slot, then list order).
    2. For each candidate (at most _MIG_TX_TRIES), getTransaction
       (jsonParsed) and check its logs for "Instruction:
       Migrate". The first Migrate found IS the migration tx. A
       non-migration pick is skipped, never a verdict — the
       migration may be another sig in the window. This scan is
       required because "the newest successful signature" is
       WRONG: after migration the curve is still touched by
       successful admin txs (pump.fun SweepProtocolFee — verified
       live 2026-11-20 on FomoCoin, whose newest successful curve
       sig was a fee sweep, not the migration), so newest-first
       mis-identifies the migration and would reject legitimate
       canonical pools as non-canonical.
    3. For the migration tx, the pool is canonical iff it is one
       of the tx's account keys: the migration creates exactly
       one pool. (The pool is identified by membership in the tx,
       not by parsing the migrate instruction: the instruction's
       account layout is not verifiable from this codebase, and a
       wrong layout guess would silently mis-verify pools.
       Membership uses only the stable top-level tx structure.)

    Returns True (pool is the migration pool), False (pool is NOT
    in the migration tx — a non-canonical pool), or None
    (unresolvable — fail-closed, the caller rejects as
    unknown_canonical). Also stores the raw curve data in the
    shared curve cache, so the chain phase's CurveStatsCheck
    reuses it instead of spending a second signatures call.
    Never raises."""
    try:
        if not curve_addr or not pool or _reserves is None:
            return None
        try:
            created = int(float(created_ts))
        except (TypeError, ValueError):
            return None
        if created <= 0:
            return None
        res = _reserves.rpc("getSignaturesForAddress",
                            [curve_addr, {"limit": CURVE_SIG_LIMIT}])
        if not isinstance(res, list) or not res:
            return None
        # Successful signatures around the pool's creation second,
        # newest first. The window tolerates GeckoTerminal's ~1s
        # lag on pool_created_at (see docstring).
        cands = []
        for entry in res:
            if not isinstance(entry, dict):
                continue
            if entry.get("err") is not None:
                continue
            try:
                bt = int(entry.get("blockTime"))
            except (TypeError, ValueError):
                continue
            if bt < created - 2 or bt > created + 1:
                continue
            sig = str(entry.get("signature") or "")
            if not sig:
                continue
            try:
                slot = int(entry.get("slot") or 0)
            except (TypeError, ValueError):
                slot = 0
            cands.append((slot, sig))
        if not cands:
            return None
        cands.sort(key=lambda t: t[0], reverse=True)
        # Share the raw curve data with the chain phase (same shape
        # _curve_stats stores): count + oldest signature.
        try:
            oldest = res[-1] if isinstance(res[-1], dict) else None
            _curve_cache_store(curve_addr, len(res), oldest)
        except Exception:
            pass
        # Scan the newest candidates for the Migrate instruction.
        # A non-migration pick is skipped, never a verdict.
        for _slot, mig_sig in cands[:_MIG_TX_TRIES]:
            tx = _reserves.rpc("getTransaction", [mig_sig, {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 1}])
            if not tx:
                continue
            if _txg is None or not _txg.tx_succeeded(tx) or not _txg.response_signature_matches(tx,mig_sig):
                continue
            logs = ((tx.get("meta") or {}).get("logMessages") or [])
            if not any("Instruction: Migrate" in str(l)
                       for l in logs):
                continue
            msg = (tx.get("transaction") or {}).get("message") or {}
            keys = msg.get("accountKeys") or []
            for k in keys:
                pub = (k or {}).get("pubkey") \
                    if isinstance(k, dict) else k
                if pub and str(pub) == pool:
                    return True
            return False
        return None
    except Exception:
        return None


def _curve_address(mint, birth_rec):
    if birth_rec and birth_rec.get("bonding_curve"):
        return str(birth_rec["bonding_curve"])
    try:
        if _reserves is not None:
            return _reserves.derive_curve_pda(mint)
    except Exception:
        pass
    return None


# ---- the filter evaluation ----------------------------------------------

def _review_home(engine):
    """Home dir for the REVIEW queue: the engine's ledger dir, so
    temp ledgers in tests stay isolated from the live runtime.
    Returns None when the engine carries no LEDGER_PATH (the queue
    then falls back to FOREX_AGENT_HOME / ~/.forex-agent).
    Never raises."""
    try:
        path = getattr(engine, "LEDGER_PATH", None)
        if path:
            return os.path.dirname(os.path.abspath(str(path)))
    except Exception:
        pass
    return None


def evaluate_candidate(engine, cand, now, lane=None):
    """Run the four select filters for one candidate.

    Returns {"verdict": "pass"|"reject", "reason": <stable key or
    "">, "values": {...measured values...}}. Fail-closed: any
    input that cannot be measured rejects with an unknown_* key.
    Never raises.

    Decision memory (2026-11-17, journal-only): the returned
    values carry ``lessons_in_context`` — the ids of the top
    lessons for ``lane`` (default grad_select) that were in
    context at decision time. Lessons are context only and NEVER
    gate an entry: the filters + Rule A decide alone.

    Bear scorer (2026-11-17, journal-only): the returned values
    carry ``bear_score`` — the devil's-advocate score computed
    from the SAME already-fetched measurements in ``values``
    (deployer, copycat, curve speed/trades, grad age; liquidity
    and top-10 are unknown at evaluate time and scored as such).
    The score NEVER gates an entry in v1: the filters + Rule A
    decide alone. A gate needs a calibration win first."""
    values = {}
    try:
        result = _evaluate(engine, cand, now, values)
    except Exception as e:
        result = {"verdict": "reject", "reason": "unknown_error",
                  "values": dict(values, error=str(e)[:120])}
    # Conviction gate (advanced build, piece 1, 2026-11-21):
    # runs ONLY after the filters + Rule A pass, so it can only
    # remove entries, never add one. WEAK candidates go to the
    # REVIEW sentinel queue instead of entering; SKIP/BLOCK reject
    # with their own distinct reasons. Fail-soft: any error here
    # leaves the filter verdict untouched (the gate never breaks
    # an entry the filters passed).
    if result.get("verdict") == "pass":
        try:
            if _conviction is not None:
                score_vals = dict(result.get("values") or {})
                # mcap/liquidity come from the candidate row — the
                # select values don't carry them at evaluate time.
                if score_vals.get("mcap") is None:
                    for _k in ("fdv_usd", "mcap_usd"):
                        if cand.get(_k) is not None:
                            score_vals["mcap"] = cand.get(_k)
                            break
                if score_vals.get("liquidity") is None:
                    for _k in ("reserve_usd", "liq_usd"):
                        if cand.get(_k) is not None:
                            score_vals["liquidity"] = cand.get(_k)
                            break
                gated = _conviction.gate(score_vals)
                vals = result.setdefault("values", {})
                vals["conviction"] = gated["conviction"]
                # The conviction rides in values (and into the
                # close via _stamp_detection); the verdict/reason
                # change ONLY when the gate refuses or reviews, so
                # a filter pass keeps its existing shape (verdict
                # "pass", reason "") — the fail-soft contract in
                # tests/test_bear_score.py depends on it.
                if gated["verdict"] != "pass":
                    result["verdict"] = gated["verdict"]
                    result["reason"] = gated["reason"]
                    if gated["verdict"] == "review":
                        try:
                            if _review_queue is not None:
                                _review_queue.enqueue(
                                    lane or LANE,
                                    str(cand.get("mint") or ""),
                                    gated["conviction"], score_vals,
                                    symbol=cand.get("symbol"),
                                    source=cand.get("source"),
                                    now=now,
                                    home=_review_home(engine))
                        except Exception:
                            pass
        except Exception:
            pass
    try:
        if _bear is not None:
            vals = result.setdefault("values", {})
            # The pipeline's BearScoreCheck already stamped the
            # score for candidates that reached it (passes);
            # only score here when it did not run (rejects) so
            # every evaluation still journals a bear_score.
            if "bear_score" not in vals:
                vals["bear_score"] = _bear.score_features({
                "deployer_launches_24h":
                    vals.get("deployer_launches_24h"),
                "ticker_others_24h": vals.get("ticker_others_24h"),
                "ttg_s": vals.get("ttg_s"),
                "curve_trades": vals.get("curve_trades"),
                "grad_age_s": vals.get("grad_age_s"),
            })
    except Exception:
        pass
    try:
        if _lessons is not None:
            result.setdefault("values", {})["lessons_in_context"] = \
                _lessons.lessons_in_context(lane or LANE)
    except Exception:
        pass
    return result


def _reject(reason, values):
    return {"verdict": "reject", "reason": reason, "values": values}


def _serial_check(creator, now, values):
    """Filter 1 for a resolved creator. Returns a reject verdict
    or None (pass). Stamps the measured count into values."""
    n24, coverage = _trader_launches_24h(creator, now)
    if n24 is None:
        return _reject("unknown_serial", values)
    values["deployer_launches_24h"] = n24
    values["recorder_coverage_start"] = coverage
    if n24 >= SERIAL_MAX_LAUNCHES_24H:
        return _reject("serial_deployer", values)
    return None


# ---- two-phase pre-buy check pipeline (2026-11-17) -------------------
# The select lane's evidence filters + Rule A + bear scorer, one
# class per gate, hosted in check_pipeline.CheckPipeline. Each
# check wraps the SAME helper the inline filter called before
# (no logic duplicated, no threshold changed); the pipeline only
# owns the order, the first-skip-wins semantics, the per-check
# budget, and the required/best-effort policy.
#
# Order is load-bearing and preserved exactly: free phase first
# (serial-fast, copycat), then chain phase (canonical, curve
# stats, serial-fallback, fast curve, Rule A, bear). One
# deliberate reorder vs the old inline code: copycat moved
# BEFORE curve stats (it was after). That is the point of the
# free-first shape — a copycat reject no longer spends the
# curve-stats RPC — and verdicts are unchanged: a candidate
# failing both now journals copycat_ticker instead of
# unknown_curve/many_trades_curve; the reject itself is the same.
# Canonical moved to the chain phase (2026-11-20) because its
# backfill fallback reads the chain; it runs first there so its
# signatures fetch feeds the curve cache for curve stats.
# All reason keys are byte-identical to the inline filters'.
#
# Budgets: pure checks 2s (the repo default); copycat 10s and
# chain checks 15s — above the underlying timeouts (DexScreener
# 8s, reserves.rpc 12s) so the budget never cuts off an answer
# the old code would have accepted. A hung check still cannot
# stall the entry loop past its budget.
#
# Phases run back-to-back inside _evaluate today (same call,
# same verdicts). The split is structural: a future entry path
# can run the free phase at detection and the chain phase just
# before the buy without touching the checks.
#
# Bear gate: BEAR_GATE_THRESHOLD None = journal-only (v1, the
# standing state). Set it to a score only on a calibration win
# (scripts/bear-calibrate), and the check returns "bear_score".

BEAR_GATE_THRESHOLD = None


class _SelectCheckContext:
    """Mutable state shared by the select pipeline checks.

    ``values`` is the journal dict _evaluate accumulates (the
    checks stamp their measurements into it, exactly as the
    inline filters did). ``birth``/``stats``/``creator``/
    ``coin_created`` are the intermediates the later checks
    need from the earlier ones. Never persisted; one evaluation
    only. Never raises."""

    def __init__(self, cand, now, values):
        self.cand = cand
        self.now = now
        self.values = values
        self.mint = str(cand.get("mint") or "")
        self.symbol = str(cand.get("symbol") or "?")
        self.source = str(cand.get("source") or "")
        self.mig_ts = float(cand.get("created_ts") or now)
        self.birth = None
        self.stats = None
        self.creator = ""
        self.coin_created = None


# Vendor-chain journaling (advanced build, piece 4, 2026-11-21):
# map the source string a check already journals to the declared
# chain step name, and stamp it into values["vendor_chain"]
# (keyed by need). Journal-only; never raises, never gates.
_CURVE_STEP = {"cache": "curve_cache", "helius": "helius_rpc",
               "public-rpc": "public_rpc"}
_COPYCAT_STEP = {"saturation": "local_recorder",
                 "dexscreener": "dex_screener"}


def _annotate_vendor(values, need, source):
    try:
        if _vendors is not None and isinstance(values, dict):
            _vendors.annotate(values, need, source)
    except Exception:
        pass


class CanonicalPoolCheck(_cp.PreBuyCheck):
    """Filter 3: canonical pools only.

    Stream candidates pass free on the migration event; a
    recorder hit passes on the pool match. On a recorder miss
    for a GeckoTerminal-backfill candidate (PumpPortal stream
    down), the canonical pool is resolved from the chain
    (_canonical_pool_from_chain: at most 1 signatures + 1 tx
    call, paced): a pool in the migration tx passes, a pool
    absent from it rejects as noncanonical_pool, and an
    unresolvable migration rejects as unknown_canonical
    (fail-closed). Backfill sources are "gecko-new-pool" (the
    value GeckoTerminal's new_pools rows actually carry — the
    scan's setdefault("geckoterminal-backfill") does NOT
    override it) and "geckoterminal-backfill" (the value the
    scan would set for a row without a source). Runs in the
    chain phase (reads_chain=True) so the free checks reject
    first and no RPC is spent on them; it runs before curve
    stats so its signatures fetch feeds the shared curve cache.
    Unavailable -> unknown_canonical."""
    name = "canonical_pool"
    reads_chain = True
    unavailable_reason = "unknown_canonical"

    # Backfill sources: the value GT new_pools rows carry, and
    # the value the scan assigns to a sourceless row.
    BACKFILL_SOURCES = ("gecko-new-pool", "geckoterminal-backfill")

    def check(self, cand, ctx):
        if ctx.source == "pumpportal-migration":
            ctx.values["canonical_basis"] = "migration_event"
            _annotate_vendor(ctx.values, "canonical_pool",
                             "migration_event")
            return None
        mig = _INDEX["by_mig"].get(ctx.mint)
        pool = str(cand.get("pool_address") or "")
        if mig and mig.get("pool") and pool:
            if mig["pool"] == pool:
                ctx.values["canonical_basis"] = \
                    "recorder_migration_match"
                _annotate_vendor(ctx.values, "canonical_pool",
                                 "recorder_migration_match")
                return None
            ctx.values["canonical_basis"] = \
                "recorder_migration_mismatch"
            return "noncanonical_pool"
        # Recorder miss: chain fallback, backfill candidates only.
        # Stream candidates never reach here (they pass above);
        # anything else without a pool fails closed below.
        # created_ts is the pool's creation time (GT pool_created_at)
        # — the migration tx is found by matching it, so a missing
        # created_ts means the fallback cannot run (fail closed).
        created_ts = cand.get("created_ts")
        if pool and ctx.source in self.BACKFILL_SOURCES \
                and created_ts:
            curve_addr = _curve_address(ctx.mint, ctx.birth)
            verdict = _canonical_pool_from_chain(
                curve_addr, pool, created_ts)
            if verdict is True:
                ctx.values["canonical_basis"] = \
                    "chain_migration_match"
                _annotate_vendor(ctx.values, "canonical_pool",
                                 "chain_fallback")
                return None
            if verdict is False:
                ctx.values["canonical_basis"] = \
                    "chain_migration_mismatch"
                return "noncanonical_pool"
            # None: unresolvable — fall through to unknown_canonical.
            ctx.values["canonical_basis"] = "chain_unresolvable"
        else:
            ctx.values["canonical_basis"] = "unverifiable"
        return "unknown_canonical"


class SerialDeployerFastCheck(_cp.PreBuyCheck):
    """Filter 1 fast path: creator known from the birth record.
    Free: index lookup only, no RPC. Runs before curve stats so
    a serial deployer is rejected without spending the
    signatures call. Unavailable -> unknown_serial."""
    name = "serial_deployer_fast"
    reads_chain = False
    unavailable_reason = "unknown_serial"

    def check(self, cand, ctx):
        if not ctx.creator:
            return None
        rj = _serial_check(ctx.creator, ctx.now, ctx.values)
        _annotate_vendor(ctx.values, "deployer_launches",
                         "local_recorder")
        if rj is not None:
            return rj["reason"]
        return None


class CopycatCheck(_cp.PreBuyCheck):
    """Filter 4: copycat tickers (>=3 others/24h). Free: local
    saturation tally, DexScreener HTTP fallback when the tally
    is stale (free API, not the chain). Either source failing
    leaves the count UNKNOWN -> unknown_copycat (fail-closed);
    the bar is unchanged."""
    name = "copycat"
    reads_chain = False
    unavailable_reason = "unknown_copycat"

    def check(self, cand, ctx):
        if _saturation is None and _ds_ticker is None:
            return "unknown_copycat"
        try:
            _norm = (_saturation._norm if _saturation is not None
                     else _ds_ticker.norm_symbol)
            sym = _norm(ctx.symbol)
            if sym == "?":
                # Unknown-symbol placeholder, not a real ticker:
                # never consult the tally for it. Literal '?'
                # coins in the census would otherwise "farm"
                # every symbol-less candidate once their
                # rolling-24h count passes the bar. (Fail-open
                # for unknown tickers only; real tickers are
                # checked exactly as before.)
                tally = 0
                others = 0
                ctx.values["copycat_source"] = "skipped"
            else:
                tally = None
                others = None
                tally_source = None
                if _saturation is not None:
                    state = _saturation._refresh_if_stale()
                    updated = float((state or {}).get("updated") or 0)
                    if state and ctx.now - updated <= \
                            SATURATION_STALE_S:
                        tally = _saturation.rolling_count(
                            state, sym, window_s=86400,
                            now_s=ctx.now)
                        others = max(0, tally - 1)
                        tally_source = "saturation"
                if tally_source is None and _ds_ticker is not None:
                    ds = _ds_ticker.count_24h(sym, now=ctx.now,
                                              self_mint=ctx.mint)
                    if ds is not None:
                        tally = ds["tally"]
                        others = ds["others"]
                        tally_source = "dexscreener"
                if tally_source is None:
                    return "unknown_copycat"
                ctx.values["copycat_source"] = tally_source
                _annotate_vendor(
                    ctx.values, "copycat_tally",
                    _COPYCAT_STEP.get(tally_source, "dex_screener"))
            ctx.values["ticker_tally_24h"] = tally
            ctx.values["ticker_others_24h"] = others
            if others >= COPYCAT_MAX_OTHERS_24H:
                return "copycat_ticker"
        except Exception:
            return "unknown_copycat"
        return None


class CurveStatsCheck(_cp.PreBuyCheck):
    """Curve stats: creation time + trade count. Chain: exactly
    one bounded getSignaturesForAddress call (limit 251). The
    curve-trades count is the one measurement the event cannot
    supply (DexScreener indexes only the PumpSwap pool for
    graduated tokens — verified live 2026-11-17 — so there is no
    free trades source). Unavailable -> unknown_curve."""
    name = "curve_stats"
    reads_chain = True
    unavailable_reason = "unknown_curve"

    def check(self, cand, ctx):
        ctx.values["state_from_event"] = bool(
            cand.get("state_from_event"))
        curve_addr = _curve_address(ctx.mint, ctx.birth)
        ctx.values["bonding_curve"] = curve_addr or ""
        stats = _curve_stats(curve_addr)
        ctx.stats = stats
        if stats.get("trades") is not None:
            ctx.values["curve_trades"] = stats["trades"]
            ctx.values["curve_trades_capped"] = bool(
                stats.get("capped"))
            # Source that answered: helius, public-rpc (fallback),
            # or cache (shared curve cache) — journaled per read.
            ctx.values["curve_trades_source"] = (
                stats.get("rpc_source") or "helius")
            _annotate_vendor(
                ctx.values, "curve_reads",
                _CURVE_STEP.get(stats.get("rpc_source") or "helius",
                                "helius_rpc"))
            ctx.values["curve_cache"] = stats.get("cache") or "miss"
        if stats.get("creation_slot") is not None:
            ctx.values["creation_slot"] = stats["creation_slot"]
        coin_created = float(
            (ctx.birth or {}).get("ts") or 0) or None
        ctx.values["coin_created_source"] = "event_birth" \
            if coin_created else "unknown"
        if stats.get("created_ts"):
            coin_created = float(stats["created_ts"])
            ctx.values["coin_created_source"] = "curve_rpc"
        ctx.values["coin_created_ts"] = coin_created
        ctx.coin_created = coin_created
        if stats.get("trades") is None:
            ctx.values["curve_stats_reason"] = \
                stats.get("reason") or "unknown"
            if stats.get("rpc_error"):
                ctx.values["curve_stats_error"] = \
                    stats["rpc_error"]
            return "unknown_curve"
        if stats["trades"] > CURVE_TRADES_MAX:
            return "many_trades_curve"
        return None


class SerialDeployerFallbackCheck(_cp.PreBuyCheck):
    """Filter 1 fallback: creator from the curve's create tx.
    Chain: one getTransaction, only when the birth record has no
    trader. Only valid on an uncapped, fully-paginated curve:
    oldest_sig is the CREATE transaction (a capped pagination's
    oldest sig is just some mid-curve trade — never used).
    Unavailable -> unknown_serial."""
    name = "serial_deployer_fallback"
    reads_chain = True
    unavailable_reason = "unknown_serial"

    def check(self, cand, ctx):
        if ctx.creator:
            return None
        stats = ctx.stats or {}
        if stats.get("exhausted") and stats.get("oldest_sig"):
            ctx.creator = _creator_from_oldest_tx(
                stats["oldest_sig"]) or ""
        if not ctx.creator:
            return "unknown_serial"
        ctx.values["creator"] = ctx.creator
        ctx.values["creator_source"] = "curve_oldest_tx"
        _annotate_vendor(ctx.values, "deployer_launches",
                         "curve_oldest_tx_creator")
        rj = _serial_check(ctx.creator, ctx.now, ctx.values)
        if rj is not None:
            return rj["reason"]
        return None


class FastCurveCheck(_cp.PreBuyCheck):
    """Filter 2 thresholds: fast curve (ttg <= 3600s). Reads no
    chain itself; ordered in the chain phase because it needs
    coin_created + trades from CurveStatsCheck. Unavailable
    (no creation time) -> unknown_curve."""
    name = "fast_curve"
    reads_chain = True
    unavailable_reason = "unknown_curve"

    def check(self, cand, ctx):
        if not ctx.coin_created:
            return "unknown_curve"
        ttg = ctx.mig_ts - ctx.coin_created
        ctx.values["ttg_s"] = round(ttg, 1)
        trades = (ctx.stats or {}).get("trades")
        ctx.values["avg_sol_per_trade"] = round(
            GRAD_SOL / trades, 4) if trades and trades > 0 else None
        if ttg < 0 or ttg > TTG_MAX_S:
            return "slow_curve"
        return None


class RuleACheck(_cp.PreBuyCheck):
    """Filter 5: entry-speed bar (Rule A, 2026-11-16; extended
    to GT-backfill 2026-11-21, exit-study Rule 1). Reads no
    chain itself; ordered in the chain phase because it must
    only ever see candidates that passed the four filters,
    exactly as backtested. Applies to every source: stream AND
    GeckoTerminal backfill (age = now - created_ts, the pool's
    creation second). Backfill rows are minutes old by
    construction, so in practice backfill becomes
    detection-only — a backfill coin qualifies only if it
    graduated within the last 30 seconds. Fail-closed on
    unknown or future-dated age. The 30s bar is unchanged."""
    name = "rule_a"
    reads_chain = True
    unavailable_reason = "unknown_grad_age"

    def check(self, cand, ctx):
        if not cand.get("created_ts"):
            return "unknown_grad_age"
        grad_age = ctx.now - ctx.mig_ts
        ctx.values["grad_age_s"] = round(grad_age, 1)
        if grad_age < -5.0:
            return "unknown_grad_age"
        if grad_age > ENTRY_MAX_AGE_S:
            return "slow_entry"
        return None


class BearScoreCheck(_cp.PreBuyCheck):
    """Devil's-advocate scorer, hosted in the pipeline (the
    bear scorer lives in bear_score.py — this check only calls
    it, never duplicates it). JOURNAL-ONLY in v1: always passes;
    the score + contributions + unknowns are stamped into
    values for the journal. A gate needs a calibration win
    first (scripts/bear-calibrate): set BEAR_GATE_THRESHOLD to
    a score and this check returns "bear_score" above it.
    Best-effort (required=False): a scorer failure must never
    block an entry. Reads no chain itself; ordered last in the
    chain phase because it scores the accumulated values."""
    name = "bear_score"
    reads_chain = True
    required_by_default = False

    def check(self, cand, ctx):
        if _bear is None:
            return None
        vals = ctx.values
        vals["bear_score"] = _bear.score_features({
            "deployer_launches_24h":
                vals.get("deployer_launches_24h"),
            "ticker_others_24h": vals.get("ticker_others_24h"),
            "ttg_s": vals.get("ttg_s"),
            "curve_trades": vals.get("curve_trades"),
            "grad_age_s": vals.get("grad_age_s"),
        })
        if BEAR_GATE_THRESHOLD is not None:
            try:
                score = int(
                    vals["bear_score"].get("bear_score") or 0)
            except Exception:
                score = 0
            if score >= BEAR_GATE_THRESHOLD:
                return "bear_score"
        return None


# The select pipeline, in run order: free phase first
# (serial-fast, copycat), then chain phase (canonical, curve
# stats, serial-fallback, fast curve, Rule A, bear). Canonical
# moved to the chain phase (2026-11-20): its backfill fallback
# reads the chain, so it must only see candidates that passed
# the free checks, and it runs before curve stats so its
# signatures fetch feeds the shared curve cache. A candidate
# failing both copycat and canonical now journals copycat
# instead of unknown_canonical — the reject itself is the same.
# Reasons are verbatim (named=False): the filter reasons
# already name the gate, and the journal keys must stay
# byte-identical.
_SELECT_PIPELINE_CHECKS = [
    _cp.ConfiguredCheck(SerialDeployerFastCheck(), required=True,
                        budget=2.0, named=False),
    _cp.ConfiguredCheck(CopycatCheck(), required=True,
                        budget=10.0, named=False),
    _cp.ConfiguredCheck(CanonicalPoolCheck(), required=True,
                        budget=15.0, named=False),
    _cp.ConfiguredCheck(CurveStatsCheck(), required=True,
                        budget=15.0, named=False),
    _cp.ConfiguredCheck(SerialDeployerFallbackCheck(),
                        required=True, budget=15.0, named=False),
    _cp.ConfiguredCheck(FastCurveCheck(), required=True,
                        budget=2.0, named=False),
    _cp.ConfiguredCheck(RuleACheck(), required=True,
                        budget=2.0, named=False),
    _cp.ConfiguredCheck(BearScoreCheck(), required=False,
                        budget=2.0, named=False),
]


def _evaluate(engine, cand, now, values):
    """Run the select filters for one candidate, via the
    two-phase check pipeline (free phase, then chain phase;
    first skip wins).

    Returns {"verdict": "pass"|"reject", "reason": <stable key
    or "">, "values": {...}}. Fail-closed: any input that cannot
    be measured rejects with an unknown_* key. The pipeline only
    owns order/budget/policy — every measurement still comes
    from the same helpers the inline filters called, with the
    same thresholds (Rule A 30s, curve <=250 trades, ttg
    <=3600s, serial >=3, copycat >=3). Never raises."""
    _index_refresh(now)
    birth = _birth_record(str(cand.get("mint") or ""))
    ctx = _SelectCheckContext(cand, now, values)
    ctx.birth = birth
    ctx.creator = (birth or {}).get("trader") or ""
    if ctx.creator:
        values["creator_source"] = "recorder_birth"
    values["creator"] = ctx.creator
    try:
        pipeline = _cp.CheckPipeline(_SELECT_PIPELINE_CHECKS,
                                     ctx=ctx)
        reason = pipeline.run_phases(cand)
    except Exception:
        return _reject("unknown_error", values)
    if reason is not None:
        return _reject(reason, values)
    return {"verdict": "pass", "reason": "", "values": values}


# ---- rejection journal ---------------------------------------------------

def _journal_rejection_in(d, cand, verdict, now):
    """Append one filter rejection to an ALREADY-LOADED ledger
    dict's select_journal (counters by reason + bounded recent
    list). No lock, no I/O: the caller owns the read-modify-write
    (used by the batched filter stage). Never raises."""
    try:
        j = d.get("select_journal")
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
        d["select_journal"] = j
    except Exception:
        pass


def _journal_rejection(engine, cand, verdict, now):
    """Append one filter rejection to the lane ledger's
    select_journal (counters by reason + bounded recent list).
    Never raises.

    Also maintains counts_by_day: {WAT-date: {reason: n}}.
    Added 2026-10-29 to resolve a reporting gap: the cumulative
    counters alone cannot answer "how many rejections yesterday",
    and the recent list is capped at 200 (it held only Oct 29
    rejections by mid-morning). Day buckets make any day-window
    exactly countable going forward. WAT date via gmtime(now+3600)
    so day boundaries match the user's WAT days regardless of the
    daemon's TZ. Day buckets are small counters (a handful of
    reasons per day); no pruning.
    """
    try:
        with engine._ledger_lock():
            d = engine._read()
            _journal_rejection_in(d, cand, verdict, now)
            engine._write(d)
    except Exception:
        pass


def rejection_breakdown(engine):
    """Cumulative filter-rejection counters (for the daemon log
    and reports). Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
        return dict((d.get("select_journal") or {}).get("counts")
                    or {})
    except Exception:
        return {}


# ---- entry stamping -------------------------------------------------------

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


# Canonical bases whose migration pool is the candidate pool
# itself (the check already verified the match): the venue
# classifier takes migration_pool = pool for these. Any other
# basis (mismatch / unresolvable / unverifiable) passes no
# migration pool, so the pool stays unverified (fail-closed).
_MATCH_BASES = ("migration_event", "recorder_migration_match",
                "chain_migration_match")


def _venue_for(values, pos, ff=None):
    """Venue classification for one entry (advanced build,
    piece 8, 2026-11-21). JOURNAL-ONLY: which pool/venue the
    entry traded on, and whether the pool was verified. Never
    raises, never gates. The pool is the position's pool_address
    (the candidate's canonical pool), falling back to the
    payload pool that priced the entry; the dex is the
    position's dex label; the curve address is the select
    values' bonding_curve."""
    try:
        values = values if isinstance(values, dict) else {}
        pos = pos if isinstance(pos, dict) else {}
        pool = str(pos.get("pool_address") or "")
        if not pool and isinstance(ff, dict):
            info = ff.get("info") or {}
            pool = str(info.get("pool") or "")
        basis = str(values.get("canonical_basis") or "")
        if _venue is None:
            return {"venue": "unknown", "verified": False,
                    "reason": "unverified_pool"}
        return _venue.classify_venue({
            "pool": pool,
            "dex": str(pos.get("dex") or ""),
            "canonical_basis": basis,
            "migration_pool": pool if basis in _MATCH_BASES else "",
            "curve_address": str(values.get("bonding_curve") or ""),
        })
    except Exception:
        return {"venue": "unknown", "verified": False,
                "reason": "unverified_pool"}


def _stamp_detection(engine, mint, source, values, ff=None):
    """Stamp HOW this entry was detected and its full select
    filter measurements onto the open position's entry_features.
    Never raises."""
    try:
        with engine._ledger_lock():
            d = engine._read()
            pos = d["positions"].get(mint)
            if pos is not None:
                pos["source"] = "graduation-select"
                ef = pos.setdefault("entry_features", {})
                ef["detection"] = source
                ef["gate"] = "select"
                ef["select"] = dict(values or {})
                # Conviction (advanced build, piece 1): the score
                # that admitted this entry rides into the close so
                # the gate can be judged on real paper closes.
                if isinstance(values, dict) and \
                        isinstance(values.get("conviction"), dict):
                    ef["conviction"] = dict(values["conviction"])
                # A+B forward paper test (2026-11-17): instrument ONLY.
                # "A+B" when the deployer had >=1 launch in the trailing
                # 24h, else "A". This stamp NEVER gates an entry — the
                # filters + Rule A decide alone; the verdict report reads
                # the arm off closes (entry_features.ab_arm) after 100
                # forward closes. See tests/test_ab_forward.py.
                try:
                    _n24 = int((values or {}).get(
                        "deployer_launches_24h") or 0)
                except (TypeError, ValueError):
                    _n24 = 0
                ef["ab_arm"] = "A+B" if _n24 >= 1 else "A"
                # Deployer forward test (2026-11-20): instrument
                # ONLY. Band off the SAME count Filter 1 already
                # computed — no recompute, no extra RPC. Never
                # gates an entry; the report reads
                # entry_features.deployer_band off closes after 100
                # forward closes. "3+" is rare (Filter 1 rejects
                # >= 3) but recorded for completeness.
                ef["deployer_band"] = _deployer_band(
                    (values or {}).get("deployer_launches_24h"))
                # Venue (advanced build, piece 8, 2026-11-21):
                # which pool/venue this entry traded on, and
                # whether the pool was verified. Journal-only;
                # never gates an entry.
                ef["venue"] = _venue_for(values, pos, ff)
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
                # Decision memory (2026-11-17): merge the full
                # select measurements into the pending decision
                # logged at entry. Journal only; never gates.
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


# ---- detection scan (grad_raw's shape + the filter stage) -----------------

def _sit_out_check(engine, now):
    """Sit-out gate for the scan loop (advanced build, piece 2 +
    sit-out, 2026-11-21; bounded 2026-11-22). Returns (sitting_out,
    info): True when the lane's regime is COLD and the recent tape
    is bleeding, so the scan loop returns early WITHOUT spending
    filter RPC on candidates that will not enter. Exits still run
    (the trader loop calls poll_exits separately). The decision is
    journaled into the ledger's sit_out_log (bounded 50, at most
    one entry per hour) so sitting out is a visible decision, not
    silence.

    BOUNDED (fix 2026-11-22): a sit-out lasts at most
    SIT_OUT_MAX_SECONDS (12h) from its start (ledger
    sit_out_started_ts, backfilled from the first sit_out_log
    entry for a sit-out that began before this field existed),
    then lifts even though the evidence has not recovered — with
    no entries possible while sitting out, no new closes can
    land, so an unbounded gate latches forever (the 2026-11-21
    20:10 UTC sit-out logged the identical line 19 times over
    ~18h). When a sit-out ends (expiry or recovery) the ledger's
    sit_out_baseline_ts records its start, and only closes that
    landed after that baseline count toward a re-trigger, so the
    same stale closes can never re-engage the gate. Fail-open:
    any error returns (False, {}) — a broken sit-out check must
    never stop entries by itself. Never raises."""
    try:
        if _regime is None:
            return False, {}
        with engine._ledger_lock():
            d = engine._read()
            closes = d.get("closes") or []
            started = d.get("sit_out_started_ts")
            try:
                started = float(started) if started is not None \
                    else None
            except Exception:
                started = None
            baseline = d.get("sit_out_baseline_ts")
            try:
                baseline = float(baseline) if baseline is not None \
                    else None
            except Exception:
                baseline = None
            # Backfill the start of a sit-out that began before
            # sit_out_started_ts existed: the first journaled
            # sit-out entry is its start (the 2026-11-21 latch
            # logged 19 contiguous hourly entries from 20:10 UTC).
            # Two guards: the entry must be NEWER than the
            # baseline (entries at or before it belong to an
            # already-ended sit-out — re-deriving them would
            # re-expire on every scan), and the last entry must
            # be recent (a live sit-out journals hourly; a stale
            # log is a sit-out that already ended).
            if started is None:
                log = d.get("sit_out_log")
                if isinstance(log, list) and log:
                    try:
                        first_ts = float(log[0].get("ts") or 0) \
                            or None
                        last_ts = float(log[-1].get("ts") or 0) \
                            or None
                    except Exception:
                        first_ts = last_ts = None
                    if (first_ts is not None
                            and last_ts is not None
                            and last_ts >= now - 7200
                            and (baseline is None
                                 or first_ts > baseline)):
                        started = first_ts
            info = _regime.sit_out(
                closes, now=now, sit_out_started_ts=started,
                fresh_after_ts=baseline)
            if info.get("reason") == "sit_out_expired":
                # Bound reached: lift now, and fence the stale
                # closes off from any re-trigger.
                d["sit_out_baseline_ts"] = started
                d["sit_out_started_ts"] = None
                engine._write(d)
                logger.warning(
                    "sit-out expired: %s sat out %.1fh (started %s) "
                    "— 12h bound reached, entries resume; re-trigger "
                    "needs fresh closes after the sit-out started",
                    LANE,
                    (now - started) / 3600.0 if started else 0.0,
                    started)
                return False, info
            if not info.get("sit_out"):
                if started is not None:
                    # Evidence recovered while sitting out: lift and
                    # fence the triggering closes off as above.
                    d["sit_out_baseline_ts"] = started
                    d["sit_out_started_ts"] = None
                    engine._write(d)
                    logger.warning(
                        "sit-out lifted: %s evidence recovered "
                        "(regime %s) — entries resume",
                        LANE, info.get("regime") or "")
                return False, info
            # Sitting out: record the start on first engagement.
            dirty = False
            if started is None:
                started = now
                d["sit_out_started_ts"] = started
                dirty = True
            # Journal at most once per hour (a sit-out spans many
            # scan cycles; the log records the decision, not the
            # cycles).
            log = d.get("sit_out_log")
            if not isinstance(log, list):
                log = []
            last_ts = 0.0
            if log:
                try:
                    last_ts = float(log[-1].get("ts") or 0)
                except Exception:
                    last_ts = 0.0
            if now - last_ts >= 3600:
                log.append({"ts": now,
                            "reason": info.get("reason") or "",
                            "regime": info.get("regime") or "COLD",
                            "evidence": info.get("evidence") or {}})
                d["sit_out_log"] = log[-50:]
                ev = info.get("evidence") or {}
                logger.warning(
                    "sit-out: %s %s (regime %s, expectancy %.2f, "
                    "loss streak %d, last-10 net %.2f) — no entries "
                    "until the evidence recovers or the 12h bound "
                    "is reached",
                    LANE, info.get("reason") or "",
                    info.get("regime") or "COLD",
                    ev.get("expectancy") or 0,
                    ev.get("loss_streak") or 0,
                    ev.get("last10_net") or 0)
                dirty = True
            if dirty:
                engine._write(d)
            return True, info
    except Exception:
        return False, {}


# Scan-schedule state (advanced build, piece 9, 2026-11-21):
# per-lane last-scan time, last mode, mode-start time, and the
# stream count of the last scan. Shared across lanes (keyed by
# engine.LANE) — the four lanes' _scan_once functions all call
# _scan_schedule_check here. Process-local only; a restart
# re-derives it (fail-open to FULL on the first tick).
_SCAN_SCHEDULE_STATE = {"last_scan_ts": {}, "last_mode": {},
                        "mode_start_ts": {}, "last_stream": {}}


def _scan_schedule_check(engine, feed, now):
    """Scan-schedule decision for one lane's main-loop scan
    (advanced build, piece 9, 2026-11-21). Returns the
    scan_schedule.should_scan dict {"scan", "cadence_s", "mode",
    "reason"}. The state is built from the lane's own ledger
    closes (regime), the pacer 429 spike (rate pressure), the
    fresh graduations (the feed queue waiting now + the last
    scan's stream count — the queue MUST be in the signal, or a
    first-tick REST could deadlock: the scan that would discover
    fresh graduations would never run), and the open positions.
    Mode transitions are journaled to scan_windows.jsonl (the
    closed window: the mode that ended, when it began and
    ended). Never raises; fail-open to FULL on any error — a
    broken scheduler must never silence the entry path."""
    try:
        lane = str(getattr(engine, "LANE", "") or "")
        st = _SCAN_SCHEDULE_STATE
        # Regime from the lane's own closes (NORMAL when the
        # module is missing or the history is too thin).
        regime_name = "NORMAL"
        try:
            if _regime is not None:
                with engine._ledger_lock():
                    closes = engine._read().get("closes") or []
                regime_name = str(
                    (_regime.assess(closes) or {}).get("regime")
                    or "NORMAL")
        except Exception:
            regime_name = "NORMAL"
        # 429 pressure from the pacer (the same source the
        # 5-minute watch reads).
        rate429 = False
        try:
            import helius_ratelimit as _rl
            rate429 = bool(_rl.pacer_429_spike())
        except Exception:
            rate429 = False
        # Fresh graduations: the feed queue waiting now + the
        # stream count of the last scan (the queue drains fast,
        # so the last scan's count is the 10-minute window's
        # lower bound).
        fresh = int(st["last_stream"].get(lane, 0) or 0)
        try:
            q = getattr(feed, "_q", None) if feed is not None else None
            if q is not None:
                fresh += int(q.qsize())
        except Exception:
            pass
        # Open positions.
        n_open = 0
        try:
            with engine._ledger_lock():
                n_open = len(engine._read().get("positions") or {})
        except Exception:
            n_open = 0
        if _scan_schedule is None:
            return {"scan": True, "cadence_s": 5, "mode": "FULL",
                    "reason": "no_schedule_module"}
        dec = _scan_schedule.should_scan(now, {
            "regime": regime_name,
            "rate429_active": rate429,
            "fresh_graduations_last_10m": fresh,
            "positions_open": n_open,
            "last_scan_ts": st["last_scan_ts"].get(lane)})
        # Journal mode transitions: close the old window, open
        # the new one. The home is the ledger's home.
        mode = str(dec.get("mode") or "FULL")
        prev_mode = st["last_mode"].get(lane)
        if mode != prev_mode:
            try:
                home = os.path.dirname(os.path.abspath(
                    engine.LEDGER_PATH))
                if prev_mode is not None:
                    _scan_schedule.journal_window(home, {
                        "start_ts": st["mode_start_ts"].get(lane, now),
                        "end_ts": now, "mode": prev_mode,
                        "reason": "mode change to %s" % mode})
                st["last_mode"][lane] = mode
                st["mode_start_ts"][lane] = now
            except Exception:
                pass
        if dec.get("scan"):
            st["last_scan_ts"][lane] = now
        return dec
    except Exception:
        return {"scan": True, "cadence_s": 5, "mode": "FULL",
                "reason": "fail_open"}


def _note_scan_stream(lane, n):
    """Record the stream count of a finished scan (the fresh-
    graduation signal the NEXT schedule check reads). Never
    raises."""
    try:
        _SCAN_SCHEDULE_STATE["last_stream"][str(lane or "")] = int(n or 0)
    except Exception:
        pass


def scan_once(engine, feed=None, gt_rows=None, now=None,
              on_items=None, fresh_only=False):
    """One detection cycle for the SELECT lane. Returns counters
    incl. select_rejected and a per-reason breakdown. Never
    raises."""
    out = {"stream": 0, "pools": 0, "watching": 0, "opened": 0,
           "skipped": 0, "dead": 0, "select_rejected": 0,
           "select_reasons": {}}
    try:
        return _scan_once(engine, feed, gt_rows, out, now, on_items,
                          fresh_only)
    except Exception as e:
        logger.warning("grad_select scan_once failed: %s", e)
        return out


def recheck_entry_age(passed, engine, out, entry_now=None):
    """Rule A entry-age re-check (2026-11-16, Item 6). Returns the
    passed list with stale stream candidates removed.

    The cached "pass" verdict was earned at EVALUATION time, but the
    entry can land minutes later: entry-stage RPC (live quotes under
    Helius 429s) let 15 post-Rule-A stream entries enter Nov 16
    08:32–15:41 UTC at grad_age 51–261s, while the journal showed
    ZERO slow_entry ever — Rule A gated the evaluation age, never
    the entry age. This re-check runs between the filter stage and
    the entry stage (before the fire-first contexts are built, so a
    stale candidate burns no entry RPC): for every candidate the
    age is recomputed at entry time (time.time(), not the scan-start
    `now`); > ENTRY_MAX_AGE_S (30s) rejects as slow_entry, unknown
    or future-dated age rejects as unknown_grad_age — both
    fail-closed, journaled into select_journal with
    stage="entry_recheck", popped from the watchlist, and marked
    dead, exactly like a filter-stage reject. GT-backfill
    candidates are gated the same way (2026-11-21, exit-study
    Rule 1): backfill rows are minutes old by construction, so
    the re-check rejects them as slow_entry. Pure re-check, no
    RPC: never raises, never blocks a fresh entry."""
    entry_now = time.time() if entry_now is None else entry_now
    stale = []
    for cand in passed:
        try:
            created = float(cand.get("created_ts") or 0)
        except (TypeError, ValueError):
            created = 0.0
        age_now = entry_now - created
        if created <= 0 or age_now < -5.0:
            reason = "unknown_grad_age"
            age_now = None
        elif age_now > ENTRY_MAX_AGE_S:
            reason = "slow_entry"
            age_now = round(age_now, 1)
        else:
            continue
        stale.append((cand, reason, age_now))
    if not stale:
        return passed
    stale_mints = {str(c.get("mint") or "") for c, _, _ in stale}
    with engine._ledger_lock():
        d = engine._read()
        for cand, reason, age_now in stale:
            mint = str(cand.get("mint") or "")
            verdict = {"verdict": "reject", "reason": reason,
                       "values": {"grad_age_s": age_now,
                                  "stage": "entry_recheck"}}
            _journal_rejection_in(d, cand, verdict, entry_now)
            d["watchlist"].pop(mint, None)
            d["dead_mints"][mint] = {
                "ts": entry_now, "reason": "select: %s" % reason}
            out["select_rejected"] = \
                int(out.get("select_rejected") or 0) + 1
            out["select_reasons"][reason] = \
                out["select_reasons"].get(reason, 0) + 1
            logger.warning("grad_select entry-age recheck: %s %s "
                           "grad_age %s — rejected %s",
                           (cand.get("symbol") or "?")[:16],
                           mint[:12],
                           ("%.1fs" % age_now) if age_now is not None
                           else "unknown", reason)
        engine._write(d)
    return [c for c in passed
            if str(c.get("mint") or "") not in stale_mints]


def recheck_chain_age(passed, engine, out, ff_ctxs, now=None,
                      clock=None):
    """Rule A chain-age gate (2026-11-17, SlotClock), shared helper.

    Detection age already passed the filter stage + recheck, but a
    stale delivery (backed-up queue, reconnect) can still present
    an old migration as fresh: detection time says 0s, the chain
    says minutes. The fire-first ctx already fetched the migration
    tx for these fresh candidates, so its slot is free — no extra
    RPC is spent here. Chain age > 30s rejects as slow_entry
    (stage="entry_recheck_chain"); a stale clock or a missing
    slot falls back to the detection-age verdict already earned
    (never weaker, never a new failure mode). GT-backfill
    candidates never reach this gate (they have no migration tx /
    ctx). The 30s bar is unchanged. Pure gate on already-fetched
    ctx slots: never raises, never blocks a fresh entry. `clock`
    is injectable for tests; otherwise the shared SlotClock is
    used. Same logic as grad_select._scan_once's inline gate —
    the inline gate stays as-is; tranche/tranche_protect call
    this helper so all four lanes share one gate."""
    now = time.time() if now is None else now
    if clock is None:
        clock = _slot_clock()
    if clock is None or not ff_ctxs:
        return passed
    chain_stale = []
    for cand in passed:
        mint = str(cand.get("mint") or "")
        ctx = ff_ctxs.get(mint)
        if not ctx:
            continue
        slot = ctx.get("migration_slot")
        if not isinstance(slot, int) or slot <= 0:
            continue
        try:
            cage = clock.age(slot)
        except Exception:
            cage = None
        if cage is None:
            continue
        verdict = cand.get("select") or cand.get("tranche") or \
            cand.get("tranche_protect") or {}
        # setdefault (not `or {}`): an empty values dict is falsy,
        # so `or {}` would stamp a throwaway dict and lose the
        # chain-age basis. setdefault keeps the stamp on the
        # candidate's own verdict.
        vals = verdict.setdefault("values", {})
        vals["grad_age_basis"] = "chain"
        vals["grad_age_chain_s"] = round(cage, 1)
        if cage > ENTRY_MAX_AGE_S:
            chain_stale.append((cand, round(cage, 1)))
    if not chain_stale:
        return passed
    stale_mints = {str(c.get("mint") or "") for c, _ in chain_stale}
    with engine._ledger_lock():
        d = engine._read()
        for cand, cage in chain_stale:
            mint = str(cand.get("mint") or "")
            verdict = {"verdict": "reject",
                       "reason": "slow_entry",
                       "values": {"grad_age_s": cage,
                                  "grad_age_basis": "chain",
                                  "stage": "entry_recheck_chain"}}
            _journal_rejection_in(d, cand, verdict, now)
            d["watchlist"].pop(mint, None)
            d["dead_mints"][mint] = {
                "ts": now, "reason": "select: slow_entry"}
            out["select_rejected"] = \
                int(out.get("select_rejected") or 0) + 1
            out["select_reasons"]["slow_entry"] = \
                out["select_reasons"].get("slow_entry", 0) + 1
            logger.warning(
                "grad_select chain-age gate: %s %s "
                "chain age %.1fs — rejected slow_entry",
                (cand.get("symbol") or "?")[:16], mint[:12],
                cage)
        engine._write(d)
    return [c for c in passed
            if str(c.get("mint") or "") not in stale_mints]


def _scan_once(engine, feed, gt_rows, out, now, on_items=None,
               fresh_only=False):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state
    # error — the old is_halted() escaped malformed state as a
    # KeyError the except: pass swallowed, so entry proceeded.
    if not engine._ks.gate(LANE):
        logger.warning("grad_select scan: lane halted by "
                       "killswitch — no entries (exits still run)")
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
            _sched = _scan_schedule_check(engine, feed, now)
            if not _sched.get("scan", True):
                out["scan_skipped"] = str(
                    _sched.get("reason") or "schedule")
                out["scan_mode"] = str(_sched.get("mode") or "REST")
                return out
        except Exception:
            pass

    # Sit-out gate (advanced build, piece 2): a cold, bleeding
    # lane does not scan for entries — the filter RPC is not
    # spent on candidates that will not enter. Exits still run.
    try:
        _sitting, _so_info = _sit_out_check(engine, now)
        if _sitting:
            out["sit_out"] = str(_so_info.get("reason") or "cold_bleed")
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
            note_items(items)
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
    _note_scan_stream(LANE, out.get("stream", 0))

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
    # Speed (2026-11-16, Phase 1): evaluations are I/O-bound RPC
    # work, so unevaluated candidates run in parallel
    # (ThreadPoolExecutor, 4 workers — the same width the entry
    # stage already uses for _ff_build_ctx), and ALL ledger
    # effects of the stage land in ONE batched read-modify-write
    # at the end: verdict caching + rejection journal + watchlist
    # pops used to cost a full ledger read+write EACH (~1.1s a
    # pair on the 1.9MB ledger, up to 3 pairs per rejected
    # candidate). Workers never touch the ledger; only this thread
    # writes, inside one locked section, so no new race is
    # introduced — the lock serializes whole-stage applies
    # between the two scan_once threads. A process-wide semaphore
    # caps in-flight evaluations at 4 (both threads combined),
    # bounding the burst on the shared free-tier RPC quota.
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
            with _EVAL_SEM:
                return evaluate_candidate(engine, cand, now)

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
                    _journal_rejection_in(d, cand, verdict, now)
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

    # ---- Rule A entry-age re-check (Item 6): a cached "pass" was
    # earned at evaluation time; re-check the age at entry time
    # before any entry RPC is spent (fire-first contexts build next).
    passed = recheck_entry_age(passed, engine, out)

    # ---- entry stage (fire-first for fresh stream candidates) ---
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
    # ---- Rule A chain-age gate (2026-11-17, SlotClock) --------
    # Detection age already passed the filter stage + recheck, but
    # a stale delivery (backed-up queue, reconnect) can still
    # present an old migration as fresh: detection time says 0s,
    # the chain says minutes. The fire-first ctx already fetched
    # the migration tx for these fresh candidates, so its slot is
    # free — no extra RPC is spent here. Chain age > 30s rejects
    # as slow_entry (stage="entry_recheck_chain"); a stale clock
    # or a missing slot falls back to the detection-age verdict
    # already earned (never weaker, never a new failure mode).
    # GT-backfill candidates never reach this gate (they have no
    # migration tx / ctx). The 30s bar is unchanged.
    _clock = _slot_clock()
    if _clock is not None and ff_ctxs:
        _chain_stale = []
        for cand in passed:
            mint = str(cand.get("mint") or "")
            ctx = ff_ctxs.get(mint)
            if not ctx:
                continue
            slot = ctx.get("migration_slot")
            if not isinstance(slot, int) or slot <= 0:
                continue
            try:
                cage = _clock.age(slot)
            except Exception:
                cage = None
            if cage is None:
                continue
            vals = (cand.get("select") or {}).get("values") or {}
            vals["grad_age_basis"] = "chain"
            vals["grad_age_chain_s"] = round(cage, 1)
            if cage > ENTRY_MAX_AGE_S:
                _chain_stale.append((cand, round(cage, 1)))
        if _chain_stale:
            _stale_mints = {str(c.get("mint") or "")
                            for c, _ in _chain_stale}
            with engine._ledger_lock():
                d = engine._read()
                for cand, cage in _chain_stale:
                    mint = str(cand.get("mint") or "")
                    verdict = {"verdict": "reject",
                               "reason": "slow_entry",
                               "values": {"grad_age_s": cage,
                                          "grad_age_basis": "chain",
                                          "stage": "entry_recheck_chain"}}
                    _journal_rejection_in(d, cand, verdict, now)
                    d["watchlist"].pop(mint, None)
                    d["dead_mints"][mint] = {
                        "ts": now, "reason": "select: slow_entry"}
                    out["select_rejected"] = \
                        int(out.get("select_rejected") or 0) + 1
                    out["select_reasons"]["slow_entry"] = \
                        out["select_reasons"].get("slow_entry", 0) + 1
                    logger.warning(
                        "grad_select chain-age gate: %s %s "
                        "chain age %.1fs — rejected slow_entry",
                        (cand.get("symbol") or "?")[:16], mint[:12],
                        cage)
                engine._write(d)
            passed = [c for c in passed
                      if str(c.get("mint") or "") not in _stale_mints]
    for cand in passed:
        mint = str(cand.get("mint") or "")
        if fresh_only and mint not in fresh_mints:
            continue
        ctx = None
        if ff_ready and mint in fresh_mints:
            ctx = ff_ctxs.get(mint) or _raw._ff_build_ctx(engine, cand)
            if ctx is not None and not ctx.get("info"):
                logger.warning("grad_select fire-first fallback %s: %s",
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
                (cand.get("select") or {}).get("values") or {},
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
    return (sum(xs) / len(xs)) if xs else 0.0


def verdict_report(engine, end=None):
    """The pre-registered verdict readout for this lane: raw
    expectancy, ex-top-1 expectancy, and the same-window
    head-to-head vs grad_raw (grad_raw closes whose close_ts
    falls inside this lane's lifetime). Read-only. Never raises."""
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
        }
        if len(pnls) >= 2:
            rep["expectancy_ex_top1"] = round(
                _mean(sorted(pnls)[:-1]), 6)
        start = float(d.get("created_ts") or 0)
        end = float(end) if end else time.time()
        try:
            with open(GRAD_RAW_LEDGER_PATH) as f:
                rd = json.load(f)
            rp = [float(c.get("pnl_usd") or 0)
                  for c in (rd.get("closes") or [])
                  if start <= float(c.get("close_ts") or 0) <= end]
            rep["head_to_head_grad_raw_same_window"] = {
                "n_closes": len(rp),
                "net_usd": round(sum(rp), 4),
                "expectancy": round(_mean(rp), 6),
                "window_start": start, "window_end": end}
        except Exception:
            rep["head_to_head_grad_raw_same_window"] = None
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
            if not d.get("verdict") or d.get("select_report"):
                return None
        rep = verdict_report(engine)
        if not rep:
            return None
        with engine._ledger_lock():
            d = engine._read()
            d["select_report"] = rep
            engine._write(d)
        try:
            engine._verdict_event(LANE, "checkpoint", {
                "closes": rep.get("n_closes"),
                "expectancy": rep.get("expectancy_raw"),
                "expectancy_ex_top1": rep.get("expectancy_ex_top1"),
                "head_to_head_grad_raw":
                    rep.get("head_to_head_grad_raw_same_window"),
                "note": "grad_select pre-registered verdict report "
                        "(raw + ex-top-1 + same-window head-to-head "
                        "vs grad_raw)"})
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


# ---------------------------------------------------------------------------
# 2x CONFIRMATION CARDS (user-ordered 2026-10-28, "Build")
#
# One Telegram card per grad_select position the moment its paper ladder
# banks leg A (50% at 2x). NOTIFICATION ONLY: this section never touches
# entries, exits, filters, stakes, or killswitch state, and every failure
# here logs and returns — the trading loop never breaks on a card.
#
# Dedup: CONFIRM_CARDS_JOURNAL maps mint -> sent record. The first run
# backfills every already-done leg-A mint as pre-existing (no flood of
# stale cards for positions that hit 2x before this feature shipped).
# A failed send leaves the mint unjournaled, so the next run retries it.
# Leg A carries no completion timestamp, so the checker also scans
# recently finalized closes (close_ts >= last_check_ts) to catch a
# position that blew through the whole ladder between checks.
# ---------------------------------------------------------------------------
CONFIRM_CARDS_JOURNAL = os.path.join(HOME, "grad_select_confirm_cards.json")
CONFIRM_CARD_CHAT_ID = "6883056760"
_CONFIRM_CHECK_EVERY_S = 30.0
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


def _ds_token_info(mint):
    """Fresh DexScreener context for a mint. Fail-soft: {} on any error."""
    try:
        req = urllib.request.Request(
            "https://api.dexscreener.com/latest/dex/tokens/%s" % mint,
            headers={"User-Agent": "grad-select-confirm-cards"})
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


def render_confirmation_card(pos, fresh=None):
    """Card text for a grad_select 2x confirmation. Never guesses data:
    unknown ticker -> mint prefix; unknown stats -> '?'."""
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
        "✅ 2x CONFIRMED — grad_select",
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
        "✔ Passed evidence filters: serial-deployer, fast-curve, "
        "canonical-pool, copycat checks",
        "⚠️ No safety audit on this lane — evidence filters only, "
        "not a safety pass.",
        "This is a confirmation, not an entry: paper entered at "
        "graduation; a manual buy now starts near 2x.",
    ]
    return "\n".join(lines)


def send_confirmation_card(text, mint):
    """Direct Telegram API send, mirroring degen-feed's send_card:
    native Copy CA button + 'I bought this' tap (source code 'gs',
    resolved by the daemon's tap parser to 'grad-select-confirm').
    Returns True on Telegram ok. Never raises."""
    try:
        with open(os.path.join(HOME, "telegram", "bot_token")) as f:
            token = f.read().strip()
        cb = None
        try:
            from signal_bus import track_callback
            cb = track_callback("solana", "grad-select-confirm", mint)
        except Exception:
            try:
                from daemon.launch_scanner.signal_bus import track_callback
                cb = track_callback("solana", "grad-select-confirm", mint)
            except Exception:
                cb = None
        if not cb:
            cb = "track:solana:gs:%s" % mint
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
        logger.warning("grad-select confirm card send failed: %s", e)
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
        return d, False
    except FileNotFoundError:
        return None, True
    except Exception as e:
        logger.warning("grad-select confirm journal unreadable: %s", e)
        return None, False


def _journal_save(journal):
    tmp = CONFIRM_CARDS_JOURNAL + ".tmp"
    with open(tmp, "w") as f:
        json.dump(journal, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIRM_CARDS_JOURNAL)


def _leg_a_banked(rec):
    """True ONLY when leg A genuinely banked at 2x.

    Close records mark every leg done at finalization (a -50% stop
    close carries legs.a.done True with exit_reason 'hard_stop_-50pct'),
    so legs.a.done alone is NOT proof of a 2x bank. The exit_reason
    is the proof: 'leg_a_2x' means the ladder sold 50% at 2x.
    Open positions only ever mark a leg done by banking it, but the
    same predicate holds for them too."""
    try:
        leg = (rec.get("legs") or {}).get("a") or {}
        return bool(leg.get("done")) and \
            leg.get("exit_reason") == "leg_a_2x"
    except Exception:
        return False


def _scan_leg_a_done(ledger):
    """{mint: pos-like record} for every open position and close whose
    leg A is banked. Never raises."""
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
    """Send one 2x confirmation card per newly banked grad_select leg A.
    Returns [mints carded]. Never raises; never touches trading state."""
    global _confirm_last_run
    try:
        now = now if now is not None else time.time()
        journal, missing = _journal_load()
        if journal is None and not missing:
            return []  # corrupt journal: fail-closed, send nothing
        if missing:
            # First run: seed every already-done leg A as pre-existing.
            try:
                ledger = engine._load()
            except Exception:
                return []
            done = _scan_leg_a_done(ledger)
            journal = {"sent": {m: {"preexisting": True, "seen_ts": now}
                                for m in done}, "last_check_ts": now}
            try:
                _journal_save(journal)
            except Exception as e:
                logger.warning(
                    "grad-select confirm journal seed failed: %s", e)
            return []
        if now - _confirm_last_run < _CONFIRM_CHECK_EVERY_S:
            return []
        _confirm_last_run = now
        try:
            ledger = engine._load()
        except Exception as e:
            logger.warning(
                "grad-select confirm cards ledger read failed: %s", e)
            return []
        sent = journal.get("sent") or {}
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
            try:
                text = render_confirmation_card(pos, _ds_token_info(mint))
                if send_confirmation_card(text, mint):
                    sent[mint] = {"sent_ts": now, "mult": _leg_a_mult(pos),
                                  "symbol": pos.get("symbol")}
                    carded.append(mint)
                else:
                    logger.warning(
                        "grad-select confirm card unsent (retry next "
                        "run): %s", mint)
            except Exception as e:
                logger.warning(
                    "grad-select confirm card failed for %s: %s", mint, e)
        journal["sent"] = sent
        journal["last_check_ts"] = now
        try:
            _journal_save(journal)
        except Exception as e:
            logger.warning(
                "grad-select confirm journal save failed: %s", e)
        return carded
    except Exception as e:
        logger.warning("grad-select maybe_confirmation_cards failed: %s", e)
        return []


# ENTRY CARDS (user-ordered 2026-10-29, "build what's necessary now")
#
# One Telegram card per grad_select position the moment paper ENTERS
# at graduation — the closest a manual trader can get to paper's
# entry. Confirmation cards (2x) cost the trader half the move; entry
# cards cost only his own reaction delay plus real slippage.
# NOTIFICATION ONLY: this section never touches entries, exits,
# filters, stakes, or killswitch state, and every failure here logs
# and returns — the trading loop never breaks on a card.
#
# Dedup: ENTRY_CARDS_JOURNAL maps mint -> sent record. The first run
# backfills every mint already in positions or closes as pre-existing
# (no flood of stale entry cards for old positions).
# Scan is positions-ONLY, by design: a position that opens AND closes
# between 30s checks is never carded — it is already dead, and
# carding it would send the trader into a closed trade. Its 2x, if
# any, is still caught by the confirmation checker's closes scan.
# A failed send leaves the mint unjournaled, so the next run retries
# it (positions have no bank timestamp problem: an unjournaled open
# position is picked up again on the next run).
# ---------------------------------------------------------------------------
ENTRY_CARDS_JOURNAL = os.path.join(HOME, "grad_select_entry_cards.json")
ENTRY_CARD_CHAT_ID = "6883056760"
_ENTRY_CHECK_EVERY_S = 30.0
_entry_last_run = 0.0


def render_entry_card(pos, fresh=None):
    """Card text for a grad_select entry. Never guesses data:
    unknown ticker -> mint prefix; unknown stats -> '?'."""
    fresh = fresh or {}
    mint = pos.get("mint") or "?"
    symbol = pos.get("symbol") or fresh.get("symbol")
    if not symbol or symbol == "?":
        symbol = mint[:8] + "…"
    snap = pos.get("entry_snapshot") or {}
    mcap = fresh.get("mcap") or snap.get("mcap")
    liq = fresh.get("liq") or snap.get("liquidity")
    entry_px = float(pos.get("entry_price") or 0)
    lines = [
        "🚀 ENTRY — grad_select",
        "%s" % symbol,
        "`%s`" % mint,
    ]
    if entry_px > 0:
        lines.append("💰 Paper entered at $%s" % ("%.8g" % entry_px))
    lines += [
        "📊 Mcap %s ⋅ Liq %s" % (_compact_usd(mcap), _compact_usd(liq)),
        "🕐 Entered %s · carded %s" % (
            _wat_ts(pos.get("entry_ts")), _wat_ts(time.time())),
        "✔ Passed evidence filters: serial-deployer, fast-curve, "
        "canonical-pool, copycat checks",
        "📏 Paper stake $25 · ladder: 50%% at 2x, 25%% at 3x, rest on "
        "trail, −50%% stop, 24h stop",
        "⚠️ No safety audit on this lane — evidence filters only, "
        "not a safety pass.",
        "This is an entry signal, not a confirmation: paper entered "
        "at graduation; price moves fast — your entry price will "
        "differ from paper's.",
    ]
    return "\n".join(lines)


def send_entry_card(text, mint):
    """Direct Telegram API send, mirroring the confirmation send:
    native Copy CA button + 'I bought this' tap (source code 'ge',
    resolved by the daemon's tap parser to 'grad-select-entry').
    Returns True on Telegram ok. Never raises."""
    try:
        with open(os.path.join(HOME, "telegram", "bot_token")) as f:
            token = f.read().strip()
        cb = None
        try:
            from signal_bus import track_callback
            cb = track_callback("solana", "grad-select-entry", mint)
        except Exception:
            try:
                from daemon.launch_scanner.signal_bus import track_callback
                cb = track_callback("solana", "grad-select-entry", mint)
            except Exception:
                cb = None
        if not cb:
            cb = "track:solana:ge:%s" % mint
        keyboard = json.dumps({"inline_keyboard": [[
            {"text": "📋 Copy CA", "copy_text": {"text": mint}},
            {"text": "📍 I bought this", "callback_data": cb},
        ]]})
        payload = {"chat_id": ENTRY_CARD_CHAT_ID, "text": text,
                   "reply_markup": keyboard}
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return bool(json.loads(r.read().decode()).get("ok", False))
    except Exception as e:
        logger.warning("grad-select entry card send failed: %s", e)
        return False


def _entry_journal_load():
    """Returns (journal, missing). A corrupt journal returns
    (None, False): fail-closed, the run sends nothing rather than
    re-carding history."""
    try:
        with open(ENTRY_CARDS_JOURNAL) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("journal not a dict")
        d.setdefault("sent", {})
        return d, False
    except FileNotFoundError:
        return None, True
    except Exception as e:
        logger.warning("grad-select entry journal unreadable: %s", e)
        return None, False


def _entry_journal_save(journal):
    tmp = ENTRY_CARDS_JOURNAL + ".tmp"
    with open(tmp, "w") as f:
        json.dump(journal, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ENTRY_CARDS_JOURNAL)


def maybe_entry_cards(engine, now=None):
    """Send one entry card per newly opened grad_select position.
    Returns [mints carded]. Never raises; never touches trading state."""
    global _entry_last_run
    try:
        now = now if now is not None else time.time()
        journal, missing = _entry_journal_load()
        if journal is None and not missing:
            return []  # corrupt journal: fail-closed, send nothing
        if missing:
            # First run: seed every already-known mint as pre-existing.
            try:
                ledger = engine._load()
            except Exception:
                return []
            known = set((ledger.get("positions") or {}).keys())
            for c in (ledger.get("closes") or []):
                if isinstance(c, dict) and c.get("mint"):
                    known.add(c["mint"])
            journal = {"sent": {m: {"preexisting": True, "seen_ts": now}
                                for m in known}, "last_check_ts": now}
            try:
                _entry_journal_save(journal)
            except Exception as e:
                logger.warning(
                    "grad-select entry journal seed failed: %s", e)
            return []
        if now - _entry_last_run < _ENTRY_CHECK_EVERY_S:
            return []
        _entry_last_run = now
        try:
            ledger = engine._load()
        except Exception as e:
            logger.warning(
                "grad-select entry cards ledger read failed: %s", e)
            return []
        sent = journal.get("sent") or {}
        fresh = {}
        for mint, pos in (ledger.get("positions") or {}).items():
            if mint not in sent:
                fresh[mint] = pos
        carded = []
        for mint, pos in fresh.items():
            try:
                text = render_entry_card(pos, _ds_token_info(mint))
                if send_entry_card(text, mint):
                    sent[mint] = {"sent_ts": now,
                                  "symbol": pos.get("symbol")}
                    carded.append(mint)
                else:
                    logger.warning(
                        "grad-select entry card unsent (retry next "
                        "run): %s", mint)
            except Exception as e:
                logger.warning(
                    "grad-select entry card failed for %s: %s", mint, e)
        journal["sent"] = sent
        journal["last_check_ts"] = now
        try:
            _entry_journal_save(journal)
        except Exception as e:
            logger.warning(
                "grad-select entry journal save failed: %s", e)
        return carded
    except Exception as e:
        logger.warning("grad-select maybe_entry_cards failed: %s", e)
        return []
