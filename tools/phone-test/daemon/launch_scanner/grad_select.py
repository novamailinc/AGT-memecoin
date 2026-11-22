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
   construction; a GeckoTerminal backfill row passes only when
   its pool address equals the pool in the recorder's migration
   record for that mint. Unknown -> reject.
4. COPYCAT TICKERS — skip when the same ticker has already
   launched >= 3 times in the trailing 24h, counted from the
   ticker-saturation tally (UTC day buckets today+yesterday —
   the tally's own census of the launch firehose; the candidate
   itself is subtracted when the tally watermark has passed its
   creation). Stale tally (> 30 min) -> reject (fail-closed).

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
    import trials as _trials
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import trials as _trials  # type: ignore
    except Exception:
        _trials = None  # type: ignore

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
VERDICT_SAMPLE_CLOSES = 100

# ---- the four filters: exact operational definitions -------------------
SERIAL_MAX_LAUNCHES_24H = 3      # reject at >= 3 launches / trailing 24h
TTG_MAX_S = 3600.0               # curve must complete within 1 hour
CURVE_TRADES_MAX = 250           # <= 250 curve signatures (see docstring)
GRAD_SOL = 85.0                  # canonical curve completion, SOL (real)
COPYCAT_MAX_OTHERS_24H = 3       # reject at >= 3 OTHER same-ticker launches
SATURATION_STALE_S = 1800.0      # tally older than this = unknown
CURVE_PAGE = 1000                # getSignaturesForAddress page size
CURVE_MAX_PAGES = 4              # hard bound on pagination work

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
                "bonding_curve": str(extra.get("bonding_curve") or "")}
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


def _curve_stats(curve_addr):
    """Paginate the bonding curve's signatures (newest first).

    Returns {"trades": int|None, "created_ts": float|None,
    "oldest_sig": str|None, "capped": bool, "exhausted": bool}.
    trades is EXACT when the pagination exhausts; when the count
    passes CURVE_TRADES_MAX we stop early (capped=True, trades is
    a lower bound > max — all the filter needs). oldest_sig is
    the curve's CREATE transaction ONLY when exhausted=True —
    callers must not treat it as the creation tx otherwise. Any
    RPC failure returns trades=None (unknown) with a diagnostic
    "reason" string (empty_curve_addr, no_reserves,
    non_list_result, not_exhausted, exception). Never raises."""
    out = {"trades": None, "created_ts": None, "oldest_sig": None,
           "capped": False, "exhausted": False, "reason": None,
           "rpc_error": None}
    if not curve_addr:
        out["reason"] = "empty_curve_addr"
        return out
    if _reserves is None:
        out["reason"] = "no_reserves"
        return out
    try:
        count = 0
        oldest_ts = None
        oldest_sig = None
        before = None
        exhausted = False
        for _ in range(CURVE_MAX_PAGES):
            opts = {"limit": CURVE_PAGE}
            if before:
                opts["before"] = before
            res = _reserves.rpc("getSignaturesForAddress",
                                [curve_addr, opts])
            if not isinstance(res, list):
                out["reason"] = "non_list_result"
                out["rpc_error"] = _rpc_err_text(res)
                return out
            if not res:
                exhausted = True
                break
            for s in res:
                count += 1
                bt = s.get("blockTime")
                if bt:
                    oldest_ts = float(bt)
                    oldest_sig = str(s.get("signature") or "")
            if count > CURVE_TRADES_MAX:
                out.update({"trades": count, "capped": True,
                            "created_ts": oldest_ts,
                            "oldest_sig": oldest_sig})
                return out
            if len(res) < CURVE_PAGE:
                exhausted = True
                break
            before = str(res[-1].get("signature") or "")
            if not before:
                break
        out.update({"trades": count if exhausted else None,
                    "created_ts": oldest_ts,
                    "oldest_sig": oldest_sig,
                    "exhausted": exhausted,
                    "reason": None if exhausted else "not_exhausted"})
        return out
    except Exception as e:
        return {"trades": None, "created_ts": None,
                "oldest_sig": None, "capped": False,
                "exhausted": False, "reason": "exception",
                "rpc_error": type(e).__name__}


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
        return _deployers._first_signer(tx)
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

def evaluate_candidate(engine, cand, now):
    """Run the four select filters for one candidate.

    Returns {"verdict": "pass"|"reject", "reason": <stable key or
    "">, "values": {...measured values...}}. Fail-closed: any
    input that cannot be measured rejects with an unknown_* key.
    Never raises."""
    values = {}
    try:
        return _evaluate(engine, cand, now, values)
    except Exception as e:
        return {"verdict": "reject", "reason": "unknown_error",
                "values": dict(values, error=str(e)[:120])}


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


def _evaluate(engine, cand, now, values):
    mint = str(cand.get("mint") or "")
    symbol = str(cand.get("symbol") or "?")
    source = str(cand.get("source") or "")
    mig_ts = float(cand.get("created_ts") or now)

    _index_refresh(now)
    birth = _birth_record(mint)

    # -- Filter 3: canonical pools only (free, checked first) ----
    if source == "pumpportal-migration":
        values["canonical_basis"] = "migration_event"
    else:
        mig = _INDEX["by_mig"].get(mint)
        pool = str(cand.get("pool_address") or "")
        if mig and mig.get("pool") and pool:
            if mig["pool"] == pool:
                values["canonical_basis"] = "recorder_migration_match"
            else:
                values["canonical_basis"] = "recorder_migration_mismatch"
                return _reject("noncanonical_pool", values)
        else:
            values["canonical_basis"] = "unverifiable"
            return _reject("unknown_canonical", values)

    creator = (birth or {}).get("trader") or ""
    if creator:
        values["creator_source"] = "recorder_birth"
    values["creator"] = creator

    # -- Filter 1 fast path: creator known from the birth record --
    if creator:
        rj = _serial_check(creator, now, values)
        if rj is not None:
            return rj

    # -- Curve stats: creation time + trade count -----------------
    curve_addr = _curve_address(mint, birth)
    values["bonding_curve"] = curve_addr or ""
    stats = _curve_stats(curve_addr)
    if stats.get("trades") is not None:
        values["curve_trades"] = stats["trades"]
        values["curve_trades_capped"] = bool(stats.get("capped"))
    coin_created = float((birth or {}).get("ts") or 0) or None
    if stats.get("created_ts"):
        coin_created = float(stats["created_ts"])
    values["coin_created_ts"] = coin_created
    if stats.get("trades") is None:
        values["curve_stats_reason"] = stats.get("reason") or "unknown"
        if stats.get("rpc_error"):
            values["curve_stats_error"] = stats["rpc_error"]
        return _reject("unknown_curve", values)
    if stats["trades"] > CURVE_TRADES_MAX:
        return _reject("many_trades_curve", values)

    # -- Filter 1 fallback: creator from the curve's create tx -----
    # Only valid now: an uncapped, fully-paginated curve makes
    # oldest_sig the CREATE transaction (a capped pagination's
    # oldest sig is just some mid-curve trade — never used).
    if not creator:
        if stats.get("exhausted") and stats.get("oldest_sig"):
            creator = _creator_from_oldest_tx(
                stats["oldest_sig"]) or ""
        if not creator:
            return _reject("unknown_serial", values)
        values["creator"] = creator
        values["creator_source"] = "curve_oldest_tx"
        rj = _serial_check(creator, now, values)
        if rj is not None:
            return rj

    # -- Filter 4: copycat tickers ---------------------------------
    if _saturation is None:
        return _reject("unknown_copycat", values)
    try:
        state = _saturation._refresh_if_stale()
        updated = float((state or {}).get("updated") or 0)
        if not state or now - updated > SATURATION_STALE_S:
            return _reject("unknown_copycat", values)
        days = state.get("days") or {}
        sym = _saturation._norm(symbol)
        if sym == "?":
            # Unknown-symbol placeholder, not a real ticker: never
            # consult the tally for it. Literal '?' coins in the
            # census would otherwise "farm" every symbol-less
            # candidate once their two-day bucket sum passes the bar.
            # (Fail-open for unknown tickers only; real tickers are
            # checked exactly as before.)
            tally = 0
            others = 0
        else:
            tally = int((days.get(_saturation._day_key(
                int(now * 1000)), {}) or {}).get(sym) or 0)
            tally += int((days.get(_saturation._day_key(
                int((now - 86400) * 1000)), {}) or {}).get(sym) or 0)
            wm_ms = float(state.get("wm_ts") or 0)
            counted_self = bool(coin_created
                                and wm_ms >= coin_created * 1000.0)
            others = tally - (1 if counted_self else 0)
        values["ticker_tally_24h"] = tally
        values["ticker_others_24h"] = others
        if others >= COPYCAT_MAX_OTHERS_24H:
            return _reject("copycat_ticker", values)
    except Exception:
        return _reject("unknown_copycat", values)

    # -- Filter 2 thresholds: fast curve ----------------------------
    if not coin_created:
        return _reject("unknown_curve", values)
    ttg = mig_ts - coin_created
    values["ttg_s"] = round(ttg, 1)
    values["avg_sol_per_trade"] = round(
        GRAD_SOL / stats["trades"], 4) if stats["trades"] > 0 else None
    if ttg < 0 or ttg > TTG_MAX_S:
        return _reject("slow_curve", values)

    return {"verdict": "pass", "reason": "", "values": values}


# ---- rejection journal ---------------------------------------------------

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


# ---- detection scan (grad_raw's shape + the filter stage) -----------------

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


def _scan_once(engine, feed, gt_rows, out, now, on_items=None,
               fresh_only=False):
    now = time.time() if now is None else now
    engine.ensure_ledger()
    try:
        if engine._ks.is_halted(LANE):
            logger.warning("grad_select scan: lane halted by "
                           "killswitch — no entries (exits still run)")
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
        verdict = cand.get("select")
        if fresh_only and mint not in fresh_mints and \
                not isinstance(verdict, dict):
            # The stream-wake thread evaluates ONLY this cycle's
            # fresh candidates; unevaluated watchlist retries are
            # the main loop's business, at the main loop's cadence
            # (each evaluation can cost RPC round-trips).
            continue
        if not isinstance(verdict, dict) or \
                verdict.get("verdict") not in ("pass", "reject"):
            verdict = evaluate_candidate(engine, cand, now)
            with engine._ledger_lock():
                d = engine._read()
                row = d["watchlist"].get(mint)
                if row is not None:
                    row["select"] = verdict
                    engine._write(d)
            cand["select"] = verdict
        if verdict["verdict"] == "reject":
            out["select_rejected"] += 1
            reason = str(verdict.get("reason") or "unknown")
            out["select_reasons"][reason] = \
                out["select_reasons"].get(reason, 0) + 1
            _journal_rejection(engine, cand, verdict, now)
            with engine._ledger_lock():
                d = engine._read()
                d["watchlist"].pop(mint, None)
                d["dead_mints"][mint] = {
                    "ts": now, "reason": "select: %s" % reason}
                engine._write(d)
            continue
        passed.append(cand)

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
