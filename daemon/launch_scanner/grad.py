#!/usr/bin/env python3
"""Graduation test lane (2026-10-21, user-ordered): buy AT graduation.

Every confirmed-runner lane is red (CEO re-halted today at expectancy
-0.306 over its fresh 15 closes; mirror ~-$16; Stacker 0-for-7). The
user's standing question — "my friend's bot buys at graduation and
wins, why don't we?" — has never actually been MEASURED by this
system: Stacker enters after a 2x confirmation, the sniper window was
killed untested at graduation size, and the runner-only rule
($25k liq / $50k mcap / rising) structurally excludes the graduation
moment. This lane is the measurement of that strategy, nothing more:

- Detection: GeckoTerminal newest Solana pools (free). A candidate is
  a mint ending in "pump" whose PumpSwap/Raydium pool is <= 10 minutes
  old — i.e. the coin graduated moments ago.
- NO runner rule on this lane. That is the deliberate, user-ordered
  exception being tested. Everything else stays fail-closed:
  instant safety (safety.check_token: RugCheck report, else the
  instant_safety on-chain fallback incl. Token-2022 traps; unknown =
  no entry), the veto engine, a real pool with a live price, the
  chain-cost gate, honest fills.
- $1 paper shots, one entry per mint EVER, MAX_OPEN 20, daily entry
  cap 20. Exits are the user's ladder, identical constants to
  ceo.py/mirror.py, with the same peak journaling on closes.
- PRE-REGISTERED VERDICT (recorded in the ledger header at creation
  and in killswitch CFG): the sample is 100 closes. At >= 100 closes,
  if mean pnl per close <= 0 the lane halts via killswitch lane
  "grad" and STAYS halted (re-enable is human-only). If > 0, the
  lane survived its sample and continues under the rolling rule.
- NO Telegram cards in v1 — journal/ledger only. This lane earns a
  card feed only by proving itself on this ledger.

Paper only. Nothing here touches real money, keys, or wallets.
Free-only: GeckoTerminal + DexScreener public endpoints, Solana RPC
via the existing safety modules.
"""
import json
import threading
import logging
import os
import time
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.grad")

try:
    from daemon.launch_scanner import fills as _fills
    from daemon.launch_scanner import risk_controls as _risk
    from daemon.launch_scanner import chain_costs as _costs
    from daemon.launch_scanner import killswitch as _ks
    from daemon.launch_scanner import veto as _veto
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import sources as _sources
    _HAS_DEPS = True
except Exception:
    try:
        import fills as _fills
        import risk_controls as _risk
        import chain_costs as _costs
        import killswitch as _ks
        import veto as _veto
        import safety as _safety
        import sources as _sources
        _HAS_DEPS = True
    except Exception:
        _HAS_DEPS = False

# P1/P8 accelerators (2026-10-23): direct-reserve pricing and birth
# dossiers. Both are pure accelerators — any failure or absence
# leaves the pre-existing behavior byte-identical.
try:
    from daemon.launch_scanner import reserves as _reserves
    from daemon.launch_scanner import dossiers as _dossiers
except Exception:
    try:
        import reserves as _reserves  # type: ignore
        import dossiers as _dossiers  # type: ignore
    except Exception:
        _reserves = None  # type: ignore
        _dossiers = None  # type: ignore

# P4 event recorder (2026-10-23): fail-soft import, same pattern as
# the reserves import above — recording must never break trading.
try:
    from daemon.launch_scanner import recorder as _recorder
except Exception:
    try:
        import recorder as _recorder  # type: ignore
    except Exception:
        _recorder = None  # type: ignore


def _record_event(kind, payload):
    """Append one recorder event, stamped with this instance's LANE.
    Never raises; a missing/dead recorder changes nothing."""
    try:
        if _recorder is not None:
            body = dict(payload or {})
            body.setdefault("lane", LANE)
            _recorder.record(kind, body)
    except Exception:
        pass


# Tamper-evident verdict ledger (2026-10-23): fail-soft import, same
# pattern as the recorder above — journaling must never break trading.
try:
    from daemon.launch_scanner import verdict_ledger as _verdict_ledger
except Exception:
    try:
        import verdict_ledger as _verdict_ledger  # type: ignore
    except Exception:
        _verdict_ledger = None  # type: ignore

# F2 sustained-unquotability exit (2026-10-23, user-approved):
# fail-soft import — a missing f2 module leaves the pre-F2
# skip-on-no-quote behavior byte-identical.
try:
    from daemon.launch_scanner import f2 as _f2
except Exception:
    try:
        import f2 as _f2  # type: ignore
    except Exception:
        _f2 = None  # type: ignore

# Jupiter Price v3 for exit pricing (2026-11-17): fail-soft import —
# a missing module leaves the pre-Jupiter behavior byte-identical.
# See jup_price.py: free, no key, batchable; graduated tokens only.
try:
    from daemon.launch_scanner import jup_price as _jup
except Exception:
    try:
        import jup_price as _jup  # type: ignore
    except Exception:
        _jup = None  # type: ignore

# Shadow tighter early stop (2026-11-20, faster-exits item 1c):
# fail-soft import — a missing module leaves exit behaviour
# byte-identical. Journal-only: note_quote records the first eval
# at/below -30% within 15 min of entry into pos["shadow_stop"].
# NOTHING here gates, rejects, or exits a position; the live stop
# below is untouched.
try:
    from daemon.launch_scanner import shadow_stop as _shadow_stop
except Exception:
    try:
        import shadow_stop as _shadow_stop  # type: ignore
    except Exception:
        _shadow_stop = None  # type: ignore

# Limit-order exit model + cover decisions (advanced build,
# pieces orig-3 + new-3, 2026-11-21): fail-soft import — a missing
# module leaves exit behaviour byte-identical. Journal-only:
# LimitModel notes every exit-eval quote (what a resting Jupiter
# limit order WOULD have filled) and cover_decision journals what
# the cover layer would do. NOTHING here gates, rejects, or exits
# a position; the live ladder below is untouched. The model +
# cover journal ride into the close record via _finalize so the
# fair comparison accrues on live closes — the peak-replay model
# alone understates limit orders (it credits exactly the rung
# while live polls bank above it; pooled rung overshoot
# $21,276.16 over 2,000 closes).
try:
    from daemon.launch_scanner import limit_exits as _limit_exits
except Exception:
    try:
        import limit_exits as _limit_exits  # type: ignore
    except Exception:
        _limit_exits = None  # type: ignore
try:
    from daemon.launch_scanner import cover as _cover
except Exception:
    try:
        import cover as _cover  # type: ignore
    except Exception:
        _cover = None  # type: ignore

# Declared vendor fallback chains (advanced build, piece 4,
# 2026-11-21): fail-soft import — a missing module leaves exit
# behaviour byte-identical. JOURNAL-ONLY: _poll_exits stamps
# which vendor priced each exit eval into pos["vendor_chain"]
# (keyed by need); _finalize carries it into the close record.
# NEVER gates an exit.
try:
    from daemon.launch_scanner import vendors as _vendors
except Exception:
    try:
        import vendors as _vendors  # type: ignore
    except Exception:
        _vendors = None  # type: ignore

# Decision memory (2026-11-17): fail-soft import — a missing
# module leaves entry/close behaviour byte-identical. See
# lessons.py: per-lane decision log (pending at entry, settled
# at close, idempotent) + templated lessons. Advisory only in
# v1: lessons are context, never a filter — nothing here gates,
# rejects, or sizes an entry.
try:
    from daemon.launch_scanner import lessons as _lessons
except Exception:
    try:
        import lessons as _lessons  # type: ignore
    except Exception:
        _lessons = None  # type: ignore

# Regime-aware paper stakes + sit-out (advanced build, piece 2,
# 2026-11-21): fail-soft import — a missing module leaves entry
# behaviour byte-identical (base stake, base MAX_OPEN, no
# sit-out). When present, the lane sizes by regime (COLD 0.5x,
# NORMAL 1x, HOT 1.5x) and sits out only when cold AND bleeding.
# PAPER ONLY: the stakes scaled here are paper stakes; Live10 is
# never touched (the demo mirror normalizes back to $10/trade).
try:
    from daemon.launch_scanner import regime as _regime
except Exception:
    try:
        import regime as _regime  # type: ignore
    except Exception:
        _regime = None  # type: ignore


def _lessons_home():
    """Home dir for this engine instance's decision log: the
    ledger's directory (== HOME in production; a temp ledger in
    tests isolates the log with it)."""
    try:
        return os.path.dirname(os.path.abspath(LEDGER_PATH))
    except Exception:
        return HOME


def _verdict_event(lane, kind, payload):
    """Append one verdict-ledger record. Never raises. The chain path
    is derived from this instance's LEDGER_PATH directory (== HOME in
    production, so the chain lands at HOME/verdict_ledger/events.jsonl;
    a temp ledger in tests isolates the chain with it)."""
    try:
        if _verdict_ledger is not None:
            _p = os.path.join(
                os.path.dirname(os.path.abspath(LEDGER_PATH)),
                "verdict_ledger", "events.jsonl")
            _verdict_ledger.append(lane, kind, payload, path=_p)
    except Exception:
        pass

HOME = (os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser("~/.forex-agent"))
LEDGER_PATH = os.path.join(HOME, "grad_ledger.json")
MAX_CLOSES = 500

# NOTIONAL bookkeeping only (2026-11-23 labeling): the lane's own
# pre-registered verdict-sample counter, NOT funded capital. Real cash
# authority is the shared $10 paper account (paper_account.try_debit).
BANKROLL_START = 100.00
NOTIONAL_USD = 1.00       # the user's $1/shot rule
MAX_OPEN = 20
# Daily entry cap REMOVED 2026-10-24 (user order, CEO-mode authority):
# the 20/day cap caused the Oct 23 ODIE miss — the stream caught ODIE
# 1s after graduation, the lane passed the safety gate at +22s, but the
# cap was already exhausted; ODIE ran +4,739% to $2.76M mcap. Entry
# limits are now the bankroll + MAX_OPEN only (the same bankroll-only
# limiting as grad_raw). None = no cap; the entries_day counter still
# increments for journaling but never refuses an entry.
DAILY_ENTRY_CAP = None
LANE = "grad"


def _paper_account():
    """The shared cross-lane paper account (Stage 1, 2026-11-22),
    or None when disabled. Opt-in via PAPER_ACCOUNT_ENABLED=1: when
    on, the shared account is the authoritative capital gate for
    entries and closes credit it the same realized proceeds the
    lane books. The lane's private bankroll and verdict accounting
    are unchanged either way. Default OFF preserves historical
    behavior until the runtime config opts in. paper_account is
    stdlib-only; a defensive failure here disables the gate rather
    than breaking the lane."""
    if os.environ.get("PAPER_ACCOUNT_ENABLED") != "1":
        return None
    try:
        # Absolute with sibling fallback, like this module's own
        # import block: engine-loaded lane variants (grad_raw,
        # grad_select, tranche, ...) exec this file OUTSIDE the
        # package, where a relative import would fail and the
        # fail-open guard below would silently disable the shared
        # gate for every variant lane.
        try:
            from daemon.launch_scanner import paper_account
        except ImportError:
            import paper_account
        try:   # unproven-legacy marks hook for THIS module instance's lane/ledger (engine variants register their own)
            paper_account.register_legacy_hook(LANE, legacy_unproven_pos_ids)
        except Exception:
            pass
        return paper_account
    except Exception:
        logger.exception("grad: paper account unavailable; shared "
                         "gate disabled for this call")
        return None


def _close_intents():
    """The cross-ledger close-intent journal (Stage C, 2026-11-22),
    or None when the shared account is off (no second ledger, no
    divergence to journal). Same engine-loading caveats as
    _paper_account."""
    if _paper_account() is None:
        return None
    try:
        try:
            from daemon.launch_scanner import close_intents
        except ImportError:
            import close_intents
        return close_intents
    except Exception:
        logger.exception("grad: close-intent journal unavailable")
        return None


# Intents whose account side is durable but whose lane-ledger write
# has not yet been confirmed; _write flushes them after a
# successful persist, recovery re-drives whatever survives a kill.
_PENDING_CLOSE_INTENTS = []
_RECOVERY_DONE = False
# A FROZEN intent blocks every entry (parent review 2026-11-22):
# contradictory lane/account capital must not fund new risk. Set
# by recovery, cleared never (a freeze is terminal until operator
# resolution).
_ENTRY_BLOCKED = False


def _recover_close_intents():
    """Re-drive incomplete close intents once per process, before
    this process's first new close. The account side re-applies
    idempotently (op_id dedupe); the lane side re-books from the
    durable payload only when the position is no longer open (an
    open position re-closes naturally on the next poll and the
    intent stays pending until then)."""
    global _RECOVERY_DONE, _ENTRY_BLOCKED
    if _RECOVERY_DONE:
        return _ENTRY_BLOCKED
    _RECOVERY_DONE = True
    _ci = _close_intents()
    if _ci is None:
        return False

    def account_apply(op):
        _pa = _paper_account()
        if _pa is None:
            return False, "paper account disabled"
        if op.get("kind") == "write_down":
            ok, why = _pa.write_down(
                LANE, op.get("mint"), op.get("op_id"),
                pos_id=op.get("pos_id"), qty=op.get("qty"),
                reason=op.get("reason") or "recovered")
        else:
            ok, why = _pa.credit(
                LANE, op.get("mint"), op.get("amount"),
                op.get("op_id"), pos_id=op.get("pos_id"),
                qty=op.get("qty"), price_kind=op.get("price_kind"))
        if not ok and _pa.op_outcome(op.get("op_id")) == "orphan" \
                and not _pa.pos_ever_existed(
                    pos_id=op.get("pos_id"), lane=LANE,
                    mint=op.get("mint")):
            return True, "orphan (converged, epoch-proven)"
        return ok, why

    def lane_has_close(intent):
        pos_id = intent.get("pos_id")
        for rec in _read().get("closes", []):
            if pos_id and rec.get("pa_pos_id") == pos_id:
                return True
            if not pos_id and rec.get("mint") == intent.get("mint"):
                return True
        return False

    def lane_apply(intent):
        d = _read()
        mint = intent.get("mint")
        pos = (d.get("positions") or {}).get(mint)
        if pos is not None and (
                not intent.get("pos_id")
                or pos.get("pa_pos_id") == intent.get("pos_id")):
            return None  # natural re-close on the next poll
        info = intent.get("close_info") or {}
        realized = float(info.get("realized_usd") or 0.0)
        d["bankroll"] = round(float(d["bankroll"]) + realized, 6)
        d.setdefault("closes", []).append({
            "mint": mint, "lane": LANE,
            "pa_pos_id": intent.get("pos_id"),
            "realized_usd": realized,
            "entry_notional_usd": info.get("entry_notional_usd"),
            "closed_reason": info.get("closed_reason"),
            "close_ts": time.time(), "recovered": True})
        _write(d)
        return True

    frozen = _ci.recover_lane(LANE, account_apply, lane_has_close,
                              lane_apply)
    if frozen:
        _ENTRY_BLOCKED = True
        logger.error("grad: %d close intent(s) FROZEN - ledgers "
                     "diverge, ENTRIES BLOCKED until operator "
                     "resolution: %s", len(frozen), ",".join(frozen))
    return _ENTRY_BLOCKED
CHAIN = "solana"
# Venue for cost accounting (P3 fee audit, 2026-10-23): graduation
# trades execute on pump.fun venues — the bonding curve at 1.25% per
# swap and PumpSwap canonical pools at 1.25% per swap in the first
# (graduation) market-cap tier, per pump.fun's own Fees Page
# (https://pump.fun/docs/fees). chain_costs charges that fee on top
# of the $0.001 Solana gas whenever this venue is named. This changes
# COST ACCOUNTING ONLY — no gates, rungs, or caps move.
VENUE = "pump"

# Graduation detection: a `pump` mint on a REAL pool (PumpSwap is the
# modern pump.fun graduation venue; Raydium is the legacy one). The
# bonding curve itself appears in GT new_pools as dex "pump-fun" and
# is NOT a graduation. Pool age is measured from pool_created_at.
MAX_GRAD_AGE_S = 600.0     # pool <= 10 minutes old
AGE_SKEW_S = 120.0         # tolerate GT/VM clock skew on fresh pools
GT_PAGES = 3               # newest-pools pages per scan (~60 pools)
DEAD_TTL_S = 48 * 3600     # final-skip records pruned after 48h


def _is_grad_dex(dex):
    d = (dex or "").lower()
    return d in ("pumpswap", "pump-fun-amm") or d.startswith("raydium")


def is_graduation_candidate(pool, now=None):
    """Pure filter: fresh PumpSwap/Raydium pool for a `pump` mint.

    Returns the pool-age in seconds when it qualifies, else None.
    Fail-closed: missing mint/dex/timestamp never qualifies."""
    try:
        now = time.time() if now is None else now
        mint = str(pool.get("mint") or "")
        if not mint.endswith("pump"):
            return None
        if not _is_grad_dex(pool.get("dex")):
            return None
        created = float(pool.get("created_ts") or 0)
        if created <= 0:
            return None
        age = now - created
        if age < -AGE_SKEW_S or age > MAX_GRAD_AGE_S:
            return None
        return max(age, 0.0)
    except Exception:
        return None


# The user's ladder — identical constants to ceo.py/mirror.py.
LEG_A_FRAC, LEG_A_TARGET = 0.50, 2.0   # sell half at 2x
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0   # half the remainder at 3x
LEG_C_FRAC, LEG_C_TRAIL = 0.25, 0.30   # final quarter on 30% trailing stop
HARD_STOP_MULT = 0.50                  # hard exit at -50%
# Test-lane stop variants (default: off — every existing lane keeps
# the exact -50% behavior and "hard_stop_-50pct" reason). A lane
# module sets these on its own isolated engine instance only:
# PROTECT_AFTER_A moves the stop to breakeven once leg A has banked
# (tranche_protect); a HARD_STOP_MULT override tightens it flat
# (raw_tight). Never set on the shared base module.
PROTECT_AFTER_A = False
# TWAP EXIT (mimic_edge, 2026-11-01): when TWAP_SLICES is set,
# poll_exits sells one equal slice per poll instead of the ladder
# (the traced outside bot's sequenced-sell exit, paper-adapted:
# the real recipients sold in 46-128 slices over 87s-7min; 64
# slices at ~5s polls is ~5min). Default None = ladder behavior,
# unchanged, for every existing lane. TWAP_MAX_HOLD_S is the TWAP
# backstop (own reason strings, never the 24h label). A lane
# module sets these on its own isolated engine instance only.
TWAP_SLICES = None
TWAP_MAX_HOLD_S = None
# TWAP exit rate (edge2, 2026-11-02): slices sold per exit poll.
# Default 1 = the edge behavior (64 polls, ~5min). A lane module
# sets these on its own isolated engine instance only.
# TWAP_ACCEL_BELOW_MULT: when the position's multiple falls below
# it, the fall is accelerating, so dump faster still (the stop
# below still guards the remainder). None = no acceleration.
TWAP_SLICES_PER_POLL = 1
TWAP_ACCEL_BELOW_MULT = None
TWAP_ACCEL_SLICES_PER_POLL = None
# MAX_SLOT_WAIT_S caps how long a candidate may wait since first
# sighting before it is entered (chase guard, 2026-10-31): a coin that
# sat on the watchlist while the lane was full and then gets a slot
# minutes later must not be bought at whatever price it has run to
# (6a81Pfhejw: seen 17:13, entered 17:30 at 38x graduation, -$11.56,
# while prompt lanes banked +$310/+$174 on the same coin). None =
# off. A lane module sets it on its own isolated engine instance
# only. Candidates dropped by it are final (no retry): the lane's
# 30-minute graduation window still governs re-sighting.
MAX_SLOT_WAIT_S = None


def _effective_stop(pos):
    """(threshold multiple, close reason) for this position.

    Pure: no I/O, safe to unit-test. The default path returns the
    exact legacy values, so existing lanes and tests are untouched.
    """
    if PROTECT_AFTER_A and (pos.get("legs") or {}).get("a", {}).get("done"):
        return 1.0, "hard_stop_protect_be"
    if HARD_STOP_MULT != 0.50:
        return HARD_STOP_MULT, "hard_stop_-%dpct" % int(round((1.0 - HARD_STOP_MULT) * 100))
    return HARD_STOP_MULT, "hard_stop_-50pct"


TIME_STOP_H = 24                       # hygiene backstop

# Pre-registered verdict sample (see module docstring + ledger header).
VERDICT_SAMPLE_CLOSES = 100


# ---- ledger -------------------------------------------------------------
# Same inter-process locking pattern as ceo.py/mirror.py: every
# read-modify-write section holds an exclusive flock on a lock file
# next to the ledger, and writes are atomic tmp+rename. The lock path
# is derived from LEDGER_PATH at call time so tests can isolate it.

import fcntl


def _lock_path():
    return LEDGER_PATH + ".lock"


_LOCK_DEPTH = {"n": 0, "tid": None, "pid": None}   # in-process: >0 while THIS process holds the ledger lock (flock on a second fd would self-deadlock)


class _ledger_lock:
    """Exclusive inter-process lock spanning a read-modify-write section."""

    def __enter__(self):
        self._lf = open(_lock_path(), "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        _LOCK_DEPTH["n"] += 1; _LOCK_DEPTH["tid"] = threading.get_ident(); _LOCK_DEPTH["pid"] = os.getpid()
        return self

    def __exit__(self, *a):
        _LOCK_DEPTH["n"] = max(0, _LOCK_DEPTH["n"] - 1)
        if _LOCK_DEPTH["n"] == 0: _LOCK_DEPTH["tid"] = None; _LOCK_DEPTH["pid"] = None
        try:
            fcntl.flock(self._lf, fcntl.LOCK_UN)
        finally:
            self._lf.close()
        return False


def _preregistration():
    """The verdict terms, stamped into the ledger header at creation
    so they cannot be quietly changed later — changing them means
    deleting the ledger, which is itself visible."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'grad'); if > 0 "
                 "the lane survived its sample. Re-enable is "
                 "human-only." % VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": DAILY_ENTRY_CAP,
            "max_grad_age_s": MAX_GRAD_AGE_S,
            "runner_rule_applies": False,
            "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h",
        },
        "registered_ts": time.time(),
    }


def _blank():
    return {
        "bankroll_start": BANKROLL_START,
        "bankroll": BANKROLL_START,
        # NOTIONAL, unfunded: see BANKROLL_START. Real cash authority is
        # the shared $10 paper account. Verdict math is frozen; this
        # label changes nothing it computes.
        "bankroll_kind": "notional-verdict-counter-unfunded",
        "positions": {},
        "closes": [],
        "created_ts": time.time(),
        "state": "active",
        "preregistered_verdict": _preregistration(),
        "verdict": None,
        "dead_mints": {},     # mint -> {"ts":, "reason":} final skips
        "watchlist": {},      # mint -> candidate dict (retry window)
        "entries_day": {"date": "", "count": 0},
    }


def _read():
    """Bare ledger read. Hold _ledger_lock if you will modify+write.

    Missing file -> blank ledger (first run). An EXISTING but
    unparseable file is never silently blanked (2026-10-24: a hard
    VM reset left grad_select's ledger unreadable and the next boot
    re-seeded $100, silently dropping 17 closes + 2 open positions):
    the file is preserved as .corrupt-<ts> and an error is logged
    before returning blank, so the loss is loud and recoverable."""
    try:
        with open(LEDGER_PATH) as f:
            d = json.load(f)
    except FileNotFoundError:
        return _blank()
    except Exception:
        aside = None
        try:
            stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
            aside = LEDGER_PATH + ".corrupt-" + stamp
            try:
                os.replace(LEDGER_PATH, aside)
            except OSError:
                import shutil
                shutil.copyfile(LEDGER_PATH, aside)
        except Exception:
            aside = None
        logger.error(
            "LEDGER CORRUPT: %s failed to parse; preserved as %s; "
            "starting blank — recover from the verdict chain / "
            "backups, do not trust the blank ledger",
            LEDGER_PATH, aside)
        return _blank()
    if isinstance(d, dict):
        b = _blank()
        for k, v in b.items():
            d.setdefault(k, v)
        if not isinstance(d.get("positions"), dict):
            d["positions"] = {}
        if not isinstance(d.get("closes"), list):
            d["closes"] = []
        if not isinstance(d.get("dead_mints"), dict):
            d["dead_mints"] = {}
        if not isinstance(d.get("watchlist"), dict):
            d["watchlist"] = {}
        if not isinstance(d.get("entries_day"), dict):
            d["entries_day"] = {"date": "", "count": 0}
        return d
    return _blank()


def _write(d):
    """Bare ledger write (atomic tmp+rename, fsync'd). Hold _ledger_lock.

    The fsyncs matter: this VM hard-resets, and an un-fsync'd rename
    can come back hollow/stale after a reset (2026-10-24 ledger
    loss). Flush+fsync the tmp file before the rename, fsync the
    directory after it (best-effort)."""
    try:
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, LEDGER_PATH)
        try:
            dfd = os.open(os.path.dirname(LEDGER_PATH) or ".",
                          os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        # Stage C: a successful persist makes a close whose
        # account side already settled durable on the lane side -
        # the last boundary of the cross-ledger intent. Flush ONLY
        # intents whose close record THIS persist actually carries:
        # an unrelated write (an entry, a watchlist update) must
        # not mark a close it does not contain.
        if _PENDING_CLOSE_INTENTS:
            _ciw = _close_intents()
            if _ciw is not None:
                _closed_pids = {r.get("pa_pos_id")
                                for r in d.get("closes", [])
                                if r.get("pa_pos_id")}
                _closed_mints = {r.get("mint")
                                 for r in d.get("closes", [])}
                _keep = []
                for _iid, _pid, _mint in _PENDING_CLOSE_INTENTS:
                    if (_pid and _pid in _closed_pids) or (
                            not _pid and _mint in _closed_mints):
                        _ciw.mark_lane_applied(LANE, _iid)
                    else:
                        _keep.append((_iid, _pid, _mint))
                _PENDING_CLOSE_INTENTS[:] = _keep
            else:
                _PENDING_CLOSE_INTENTS.clear()
        return True
    except Exception:
        return False


def _load():
    """Locked single read (for read-only callers)."""
    with _ledger_lock():
        return _read()


def ensure_ledger():
    """Create the ledger on first run (stamps the pre-registered
    verdict terms into its header). Returns the ledger dict."""
    with _ledger_lock():
        if os.path.exists(LEDGER_PATH):
            return _read()
        d = _blank()
        _write(d)
        logger.warning("grad ledger created: NOTIONAL verdict "
                       "counter $%.2f (unfunded bookkeeping; real cash "
                       "authority is the shared paper account), verdict "
                       "pre-registered at %d closes",
                       d["bankroll"], VERDICT_SAMPLE_CLOSES)
        return d


def account_state():
    """Read-only summary for reports/tests. Never raises."""
    try:
        d = _load()
        return {
            "bankroll": d["bankroll"],
            "bankroll_start": d["bankroll_start"],
            "state": d["state"],
            "n_open": len(d["positions"]),
            "n_closes": len(d["closes"]),
            "verdict": d.get("verdict"),
            "created_ts": d["created_ts"],
            # operator-visible reconciliation state (recon notes, pending exits and their ages, settlement_pending, aborted poll cycles);
            # None means the ledger could not be read (unknown, never "clean")
            "recon_health": recon_health(),
        }
    except Exception:
        return {}


# ---- price feeds (same routes as ceo.py, Solana only) ---------------------

def _http_json(url, timeout=12, payload=None):
    try:
        data = None
        headers = {"User-Agent": "forex-agent/1.0"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


_LAST_TAPE_SOURCE = ""  # venue of the last _fresh_tape read, for logs


def _fresh_tape(mint):
    """Fresh tape stats dict (DexScreener, GeckoTerminal fallback per
    sources.tape_batch_stats). None = unknown. Never raises."""
    global _LAST_TAPE_SOURCE
    try:
        stats = _sources.tape_batch_stats([mint], chain="solana")
        st = (stats or {}).get(mint) or None
        _LAST_TAPE_SOURCE = str((st or {}).get("tape_source") or "")
        return st
    except Exception:
        _LAST_TAPE_SOURCE = ""
        return None


# Entry quote: identical selection rule to ceo.py's 2026-10-21 repair
# — highest-liquidity priced pair (never pairs[0] blindly), the tape
# read as fallback, one bounded retry, every attempt journaled into
# _LAST_QUOTE_DETAIL so a skip names what was tried.
QUOTE_RETRY_GAP_S = 2.0
_LAST_QUOTE_DETAIL = ""
_LAST_QUOTE_PAIR = ""      # pair address of the last DS quote (P1)
_LAST_QUOTE_LIQ = 0.0      # its USD liquidity (F5)


def _ds_pairs_price(pairs):
    """(price, note, pair_address, liquidity_usd): price of the
    highest-liquidity pair that carries one — the same selection
    rule as the tape (sources.py). Pair address + liquidity ride
    along for the P1 graduation handler (pair-change detection) and
    F5 (exit-time liquidity refresh)."""
    if not isinstance(pairs, list) or not pairs:
        return 0.0, "no pairs returned", "", 0.0
    best_px, best_liq, priced = 0.0, -1.0, 0
    best_pair = ""
    for p in pairs:
        if not isinstance(p, dict):
            continue
        try:
            px = float(p.get("priceUsd") or 0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0:
            priced += 1
            if liq > best_liq:
                best_px, best_liq = px, liq
                best_pair = str(p.get("pairAddress") or "")
    if best_px > 0:
        return best_px, "%d pair(s), %d priced" % (len(pairs), priced), \
            best_pair, best_liq
    return 0.0, "%d pair(s), none priced" % len(pairs), "", 0.0


def _quote_detail(mint):
    """(price, trail): best-pair pairs fetch -> tape fallback -> one
    bounded retry. Trail is plain-language, for skip logs."""
    global _LAST_QUOTE_DETAIL, _LAST_QUOTE_PAIR, _LAST_QUOTE_LIQ
    url = "https://api.dexscreener.com/tokens/v1/solana/%s" % mint
    trail = []
    px, note, pair, liq = _ds_pairs_price(_http_json(url))
    trail.append("pairs fetch: " + note)
    if px <= 0:
        st = _fresh_tape(mint)
        tpx = float((st or {}).get("price_usd") or 0) if st else 0.0
        if tpx > 0:
            trail.append("tape fallback: priced via %s"
                         % ((st or {}).get("tape_source") or "unknown"))
            logger.warning("grad quote via tape fallback %s",
                           (mint or "?")[:16])
            px = tpx
            liq = float((st or {}).get("usd_liquidity") or 0)
        else:
            trail.append("tape fallback: "
                         + ("no tape" if not st else "tape unpriced"))
    if px <= 0:
        time.sleep(QUOTE_RETRY_GAP_S)
        px2, note2, pair2, liq2 = _ds_pairs_price(_http_json(url))
        trail.append("pairs retry: " + note2)
        if px2 > 0:
            px, pair, liq = px2, pair2, liq2
    # F6 phantom-print guard (2026-10-23): a priced print with no
    # backing pool liquidity ($0 or unknown) is untradable — a real
    # fill can never happen there, so the price is fiction. Fail
    # closed: treat as no quote (entry skips; exit polls skip the
    # cycle). The 2026-10-23 phantom booked +$60,749 off exactly
    # such a print ($0-liq pool, empty pool address).
    if px > 0 and liq <= 0:
        trail.append("zero-liquidity print: treated as no quote")
        logger.warning("grad quote %s: priced $%s but liquidity $%.2f "
                       "-> fail-closed (no quote)", (mint or "?")[:16],
                       px, liq)
        px, pair, liq = 0.0, "", 0.0
    _LAST_QUOTE_DETAIL = "; ".join(trail)
    _LAST_QUOTE_PAIR = pair if px > 0 else ""
    _LAST_QUOTE_LIQ = liq if px > 0 else 0.0
    return px, _LAST_QUOTE_DETAIL


def quote_price(chain, mint, spot_index=None):
    """Live quoted price in USD (Solana only — the grad lane trades
    nothing else; the signature matches ceo/mirror so rt_sync's
    price_fn protocol applies unchanged). 0.0 = no source can price
    the mint; exit polling treats 0 as 'skip this cycle'."""
    try:
        if (chain or "solana") != "solana":
            return 0.0
        return _quote_detail(mint)[0]
    except Exception:
        return 0.0


# ---- entry gates ------------------------------------------------------------

# Journaling only (2026-10-23): the birth-dossier summary consulted
# at entry, stamped into entry_features so closes carry it (the
# dossier's curve velocity and deployer census were previously read
# for the gate and then thrown away — zero closes carried them).
# Reset in _consider_candidate before the safety call; _safety_ok
# fills it. grad_raw's engine overrides _safety_ok with a no-gate
# stub that never touches this dict (and never reads a dossier), so
# raw-lane entries stamp dossier_used=False by construction.
_LAST_DOSSIER_STAMP = {"dossier_used": False}


def _safety_ok(mint):
    """(ok, why, report, transient). Instant safety via
    safety.check_token — RugCheck report when one exists, else the
    instant_safety on-chain fallback (incl. Token-2022 traps).
    Fail-closed: unknown chain data is NOT a pass (transient=True so
    the candidate is retried while its pool is still in window).

    P8 (2026-10-23): a birth dossier may already hold this exact
    check, computed during the curve phase. A fresh, conclusive
    dossier report is reused; a miss/stale/unknown falls through to
    the live check — identical behavior either way."""
    if _dossiers is not None:
        # Journaling only: capture what the birth dossier holds for
        # this mint right now (curve velocity + deployer census), so
        # the entry can stamp it into entry_features. The gate
        # decision below is unchanged — it rides on read_safety
        # exactly as before; an absent dossier stamps nothing extra.
        try:
            _doss = _dossiers.read_dossier(mint)
        except Exception:
            _doss = None
        if isinstance(_doss, dict):
            if _doss.get("curve_velocity_sol_s") is not None:
                _LAST_DOSSIER_STAMP["curve_velocity_sol_s"] = \
                    _doss["curve_velocity_sol_s"]
            if _doss.get("deployer_dossiers_live") is not None:
                _LAST_DOSSIER_STAMP["deployer_dossiers_live"] = \
                    _doss["deployer_dossiers_live"]
            _rug = _doss.get("deployer_rugger")
            if isinstance(_rug, dict):
                if _rug.get("launches") is not None:
                    _LAST_DOSSIER_STAMP["deployer_launches"] = \
                        _rug["launches"]
                if _rug.get("dead") is not None:
                    _LAST_DOSSIER_STAMP["deployer_dead"] = \
                        _rug["dead"]
        try:
            drep = _dossiers.read_safety(mint)
        except Exception:
            drep = None
        if isinstance(drep, dict) and not drep.get("unknown"):
            # Journaling only: the entry's safety verdict IS the
            # dossier's precomputed report — record that provenance.
            _LAST_DOSSIER_STAMP["dossier_used"] = True
            _LAST_DOSSIER_STAMP["dossier_safety_source"] = \
                str(drep.get("source") or "")
            _LAST_DOSSIER_STAMP["dossier_safety_verdict"] = \
                "pass" if drep.get("passed") else "fail"
            if drep.get("score") is not None:
                _LAST_DOSSIER_STAMP["dossier_safety_score"] = \
                    drep["score"]
            if not drep.get("passed"):
                return False, "; ".join(
                    drep.get("fails", []))[:160], drep, False
            return True, "", drep, False
    try:
        rep = _safety.check_token(CHAIN, mint, timeout=10)
    except Exception as e:
        return False, "safety error (%s)" % e, None, True
    if not isinstance(rep, dict) or rep.get("unknown"):
        return False, "safety unknown", rep, True
    if not rep.get("passed"):
        return False, "; ".join(rep.get("fails", []))[:160], rep, False
    return True, "", rep, False


def _veto_ok(mint, symbol, safety_report):
    """(ok, why). Veto engine, same call shape as ceo/mirror
    (run_bundle=False — the cheap vetoes: concentration from the fresh
    safety report, wash pattern, repeat-rug deployer). A veto is
    final: no score, no retry overrides it."""
    try:
        rep = _veto.veto_check(mint, CHAIN, symbol=symbol or "",
                               safety_report=safety_report,
                               run_bundle=False, lane=LANE)
        return bool(rep.get("passed")), "; ".join(rep.get("vetoes", []))
    except Exception as e:
        # Same semantics as ceo._veto_ok: vetoes act on positive
        # evidence; an engine exception is not evidence.
        return True, "veto unavailable (%s)" % e


def _mark_dead(mint, reason):
    try:
        with _ledger_lock():
            d = _read()
            d["dead_mints"][mint] = {"ts": time.time(),
                                     "reason": str(reason)[:160]}
            d["watchlist"].pop(mint, None)
            _write(d)
    except Exception:
        pass


def _skip(mint, why, final):
    logger.warning("grad SKIP %s: %s%s", (mint or "?")[:16], why,
                   "" if final else " (will retry in window)")
    return False, why, final


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _configured_stake():
    """Owner-configured exact trade size when the shared paper account is
    enabled and a valid size is set, else None (lane/regime sizing; the
    account gate still refuses invalid or conflicting sizes)."""
    _pa = _paper_account()
    if _pa is None:
        return None
    try:
        v = _pa.configured_trade_size()
    except Exception:
        return None
    return None if v is None else float(v)


def _ei():
    """Entry-intent recovery module, or None when the separate default-off flag is not '1'. Flag on and the module cannot import: RAISE (callers refuse entries; exits never call this without a guard)."""
    if os.environ.get("AGT_GRAD_ENTRY_INTENT_ENABLED") != "1":
        return None
    try:
        from daemon.launch_scanner import grad_entry_intent as _m
    except ImportError:
        import grad_entry_intent as _m
    return _m


def _ei_recover():
    """Classify/adopt surviving entry-intent records. CALLER HOLDS the lane ledger lock. Returns None (nothing blocks) or a reason that blocks ENTRIES only.
    Never raises. Flag off: returns None without any I/O."""
    try:
        _m = _ei()
    except Exception as _e:
        return "entry-intent module unavailable (%r); entries blocked fail-closed" % (_e,)
    if _m is None:
        return None
    try:
        try:
            from daemon.launch_scanner import entry_guard as _egr
        except ImportError:
            import entry_guard as _egr
        _pa = _paper_account()

        def _evidence(op_id):
            if _pa is None:
                return {"ok": False, "why": "paper account unavailable"}
            return _m.account_evidence(_pa, op_id, _egr.ENTRY_ACCOUNT_WAIT_S)

        def _adopt(rec, pid):
            d = _read()
            mint = rec["mint"]
            if mint in d["positions"]:
                raise RuntimeError("lane already holds the mint")
            pos = json.loads(json.dumps(rec["pos"]))
            pos["pa_pos_id"] = pid
            d["bankroll"] = round(float(d["bankroll"]) - float(rec["stake"]), 6)
            ed = d["entries_day"]
            if ed.get("date") != _today():
                ed = {"date": _today(), "count": 0}
            ed["count"] = int(ed.get("count") or 0) + 1   # the original entry incremented this in the same write that never landed
            d["entries_day"] = ed
            d["positions"][mint] = pos
            d["watchlist"].pop(mint, None)
            _write(d)

        def _drop(op_id, mint):
            try:
                _df = _demo_filler()
                if _df is not None:
                    _k = "%s|%s" % (LANE, mint)
                    _df._locked(lambda j: j.setdefault("intents", {}).pop(_k, None) if (j.get("intents") or {}).get(_k) == op_id else None)
            except Exception as _de:
                logger.warning("%s: entry-intent drop_intent failed (%r)", LANE, _de)

        _why, _acts = _m.recover_pass(LEDGER_PATH, LANE, _read, _adopt, _evidence, _drop)
        for _op, _mt, _cl, _wh in _acts:
            logger.warning("%s entry-intent op=%s mint=%s class=%s: %s", LANE, _op, _mt, _cl, _wh)
        return _why
    except Exception as _e:
        return "entry-intent recovery failed (%r); entries blocked fail-closed" % (_e,)


def consider_candidate(cand, now=None):
    """Decide one graduation candidate. Returns (opened, reason,
    final): final=True means never reconsider this mint (entered,
    hard-gated, or already traded); final=False means a transient
    block (caps full, safety unknown, no quote yet) — the scan loop
    keeps it on the watchlist while its pool is in the age window.
    Never raises."""
    try:
        return _consider_candidate(cand, now)
    except Exception as e:
        logger.warning("grad consider_candidate failed: %s", e)
        return False, "grad error: %s" % e, False


def _consider_candidate(cand, now):
    global _LAST_DOSSIER_STAMP
    if not _HAS_DEPS:
        return _skip(cand.get("mint"), "grad deps missing", False)
    now = time.time() if now is None else now
    decided_ts = time.time()  # P5: the decision clock starts here
    mint = str(cand.get("mint") or "")
    symbol = str(cand.get("symbol") or "?")[:32]
    if not mint:
        return _skip(mint, "no mint", True)
    age = is_graduation_candidate(cand, now)
    if age is None:
        return _skip(mint, "not a fresh graduation", True)

    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state
    # error — the old is_halted() escaped malformed state as a
    # KeyError the except: pass swallowed, so entry proceeded.
    if not _ks.gate(LANE):
        return _skip(mint, "grad lane halted by killswitch", True)

    # Fast dedupe/cap checks on a locked snapshot (authoritative
    # re-check happens under the write lock below).
    # Regime-aware stakes (advanced build, piece 2): the regime
    # is assessed from this snapshot's closes, so the stake and
    # capacity used below are the regime's — COLD halves both,
    # HOT sizes up, NORMAL is the base. A missing regime module
    # leaves the base stake/capacity (fail-safe).
    _reg = None
    _stake = NOTIONAL_USD
    _max_open = MAX_OPEN
    _sit_out = None
    with _ledger_lock():
        d = _read()
        try:
            if _regime is not None:
                _reg = _regime.assess(d.get("closes") or [])
                _stake = _regime.effective_stake(
                    NOTIONAL_USD, _reg["regime"])
                _max_open = _regime.effective_max_open(
                    MAX_OPEN, _reg["regime"])
                _sit_out = _regime.sit_out(d.get("closes") or [])
        except Exception:
            _reg = None
            _stake = NOTIONAL_USD
            _max_open = MAX_OPEN
            _sit_out = None
        # An owner-configured EXACT trade size is the stake; regime
        # scaling must not silently change it (regime still drives
        # capacity and sit-out).
        _cfg = _configured_stake()
        if _cfg is not None:
            _stake = _cfg
        if float(d["bankroll"]) < _stake:
            return _skip(mint, "bankroll exhausted (notional counter $%.2f)"
                         % float(d["bankroll"]), True)
        if mint in d["positions"]:
            return _skip(mint, "already open", True)
        if any(c.get("mint") == mint for c in d["closes"]):
            return _skip(mint, "already traded (one entry per mint "
                               "ever)", True)
        if mint in d["dead_mints"]:
            return _skip(mint, "dead: %s"
                         % d["dead_mints"][mint].get("reason", ""), True)
        if len(d["positions"]) >= _max_open:
            return _skip(mint, "max open positions (%d)" % _max_open,
                         False)
        ed = d["entries_day"]
        if DAILY_ENTRY_CAP is not None and \
                ed.get("date") == _today() and \
                int(ed.get("count") or 0) >= DAILY_ENTRY_CAP:
            return _skip(mint, "daily entry cap (%d)" % DAILY_ENTRY_CAP,
                         False)

    # Sit-out (defence in depth — the scan loop also gates before
    # the filter stage, so this rarely fires): a cold, bleeding
    # lane does not enter. Retryable (final=False): the candidate
    # stays on the watchlist and ages out naturally.
    if _sit_out is not None and _sit_out.get("sit_out"):
        return _skip(mint, "sit-out: %s (regime %s)"
                     % (_sit_out.get("reason") or "cold_bleed",
                        _sit_out.get("regime") or "COLD"), False)

    # Pre-registered cost gate (Solana passes trivially at $1; the
    # check stays so the lane obeys the same cost law as the others).
    blocked, why = _costs.cost_gate(CHAIN, _stake, venue=VENUE)
    if blocked:
        logger.warning("grad %s: %s", mint, why)
        return False, why, True

    # Tape sanity + quote: a real pool with a live price must exist.
    st = _fresh_tape(mint)
    _entry_row = dict(st) if isinstance(st, dict) else None  # immutable: the row THIS entry read (a later read must not mask it)
    quoted = quote_price(CHAIN, mint)
    if quoted <= 0:
        why = "no live quote"
        if _LAST_QUOTE_DETAIL:
            why = "no live quote (%s)" % _LAST_QUOTE_DETAIL
        return _skip(mint, why, False)
    liquidity = float((st or {}).get("usd_liquidity") or 0) or \
        float(cand.get("reserve_usd") or 0)
    mcap = float((st or {}).get("usd_market_cap") or 0) or \
        float(cand.get("fdv_usd") or 0)
    tape_source = (st or {}).get("tape_source") or ""

    # Phantom-entry guard (2026-10-23): an empty pool address means
    # there is no real pool behind this candidate — the 2026-10-23
    # phantom (mint 274rXqtA) entered on exactly such a row, then
    # "filled" exits at a phantom print. Refused but retryable: a
    # backfill row may carry the pool address later (the watchlist
    # merges it in), and the mint stays in the age window.
    pool_addr = str(cand.get("pool_address") or "")
    if not pool_addr:
        return _skip(mint, "no pool address (no real pool to trade)",
                     False)

    # Phantom-entry guard 2 (2026-10-24): price/mcap consistency.
    # A real quote and a real mcap imply a sane token supply
    # (supply = mcap/price). The 2026-10-24 phantom (mint BVY5qMsi9G5f,
    # CI) entered on a 4.7e-12 print against an $81k mcap — implied
    # supply 1.7e16, ~7 orders of magnitude above any real pump.fun
    # graduation supply (~1e9 by construction). The Oct 23 guards
    # (pool address present, liquidity > 0) both passed because the
    # row LOOKED real; the numbers were mutually inconsistent.
    # Fail closed when both numbers are present; fail open when mcap
    # is unknown (0), same spirit as the existing guards. Retryable:
    # a later row may carry sane numbers. Bounds [1e6, 1e13] span
    # every real memecoin supply (micro-supply ~1e6 to trillion-
    # supply ~1e12, pump.fun ~1e9) while sitting ~3 orders of
    # magnitude inside the phantom's 1.7e16.
    if mcap > 0 and quoted > 0:
        implied_supply = mcap / quoted
        if not (1e6 <= implied_supply <= 1e13):
            return _skip(
                mint, "price/mcap inconsistent (implied supply %.2e)"
                % implied_supply, False)

    # Journaling only: reset the dossier stamp for THIS evaluation.
    # The gated _safety_ok fills it when a dossier is consulted;
    # grad_raw's no-gate override never does, so raw entries stamp
    # dossier_used=False without any dossier being read.
    _LAST_DOSSIER_STAMP = {"dossier_used": False}
    # Instant safety — fail-closed, hard fail is final.
    ok, why, rep, transient = _safety_ok(mint)
    if not ok:
        if not transient:
            _mark_dead(mint, "safety: " + why)
        return _skip(mint, "safety: " + why, not transient)

    # Veto engine — a veto is final.
    ok, why = _veto_ok(mint, symbol, rep)
    if not ok:
        _mark_dead(mint, "veto: " + why)
        return _skip(mint, "veto at entry: %s" % why, True)

    # Chase guard (2026-10-31): do not buy a stale candidate. A
    # prompt entry decides seconds after sighting; a candidate that
    # waited for a slot (or a quote) and only gets its decision
    # minutes later is being bought at a moved price. Fail-open when
    # no sighting timestamp exists; final when it fires, so the coin
    # is not retried again at an even later price.
    if MAX_SLOT_WAIT_S is not None:
        seen = float(cand.get("first_seen_ts") or
                     cand.get("created_ts") or 0)
        if seen > 0 and now - seen > MAX_SLOT_WAIT_S:
            return _skip(
                mint,
                "stale candidate: %.0fs since first seen "
                "(chase guard)" % (now - seen), True)

    gas_in = _costs.tx_cost_usd(CHAIN, _stake, venue=VENUE)
    eff_notional = _stake - gas_in
    if eff_notional <= 0:
        return _skip(mint, "costs exceed notional", True)
    # Declared slippage cap (fills.MAX_SLIPPAGE_PCT, tighten-only via
    # AGT_RISK_MAX_SLIPPAGE_PCT): a fill the model can only price at
    # the cap is not an honest entry. Retryable - liquidity can
    # recover.
    _slip_ok, _slip_why, _slip_cap = _risk.check_slippage(
        eff_notional, liquidity or None)
    if not _slip_ok:
        return _skip(mint, "slippage: " + _slip_why, False)
    # MANDATORY capital gate BEFORE any fill: for the live lane honest_entry
    # executes a real buy swap, so a refusal after it would orphan the swap.
    try:
        try:
            from daemon.launch_scanner import entry_guard as _eg0
        except ImportError:
            import entry_guard as _eg0
        _why0 = _eg0.mandatory_entry_refusal(LANE)
    except Exception:
        _why0 = ("mandatory entry guard unavailable; entries blocked "
                 "fail-closed")
    if _why0:
        return _skip(mint, _why0, False)
    try:
        filled = _fills.honest_entry(quoted, eff_notional,
                                      liquidity or None)
    except Exception:
        filled = quoted
    if filled <= 0:
        return _skip(mint, "bad fill price", False)
    tokens = eff_notional / filled

    grad_block = {
        "pool_created_ts": float(cand.get("created_ts") or 0),
        "grad_age_s": round(age, 1),
        "pool_address": pool_addr,
        "dex": str(cand.get("dex") or ""),
        "tape_source": tape_source,
    }
    pos = {
        "mint": mint, "symbol": symbol,
        "chain": CHAIN, "source": "graduation", "lane": LANE,
        "score": 0.0, "tier": "graduation",
        "entry_ts": time.time(), "quoted_price": quoted,
        "entry_price": filled, "notional_usd": _stake,
        "peak_price": filled,
        "peak_ts": time.time(),
        # P5 latency stamps: born (pool creation) -> seen (first
        # detection) -> decided (this evaluation) -> entered
        # (entry_ts above). The report script medians each stage.
        "born_ts": float(cand.get("created_ts") or 0),
        "seen_ts": float(cand.get("first_seen_ts") or now),
        "decided_ts": decided_ts,
        "tokens": tokens, "gas_in_usd": round(gas_in, 6),
        "legs": {
            "a": {"frac": LEG_A_FRAC, "target": LEG_A_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "b": {"frac": LEG_B_FRAC, "target": LEG_B_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "c": {"frac": LEG_C_FRAC, "trail": LEG_C_TRAIL,
                  "peak": filled, "done": False, "exit_price": 0,
                  "realized_usd": 0},
        },
        "realized_usd": 0.0,
        # TRANCHE CREDIT marker: positions opened from here on credit the shared account per ladder leg
        # (sim path), so account qty/basis shrink with the lane and marks/equity stay correct while
        # partly sold. Positions WITHOUT the marker (opened before this) keep the deferred full-close credit.
        "tranche_credit": True, "tranche_credited": {"net": 0.0, "qty": 0.0},
        "status": "open",
        "spot_index": None, "pair": grad_block["pool_address"],
        "entry_snapshot": {
            "score": 0.0, "tier": "graduation",
            "mcap": mcap, "liquidity": liquidity,
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(CHAIN, _stake, venue=VENUE),
            "graduation": dict(grad_block),
            "safety": {
                "score": (rep or {}).get("score", 0),
                "flags": list((rep or {}).get("flags", []))[:6],
                "source": (rep or {}).get("source", ""),
            },
        },
        "entry_features": {
            "chain": CHAIN, "source": "graduation",
            "grad_age_s": round(age, 1),
            "pool": grad_block["pool_address"],
            "dex": grad_block["dex"],
            "mcap": mcap, "liquidity": liquidity,
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(CHAIN, _stake, venue=VENUE),
            # HOW this entry was detected (P2): stream rows carry
            # "pumpportal-migration"; GT rows default to backfill.
            "detection": str(cand.get("source")
                             or "geckoterminal-backfill"),
        },
    }
    # Journaling only: the dossier summary consulted at entry rides
    # into entry_features — and therefore into the close record,
    # which _finalize builds by copying entry_features verbatim.
    pos["entry_features"].update(_LAST_DOSSIER_STAMP)

    # Regime journal (advanced build, piece 2): the regime and
    # stake that sized this entry ride into the close record so
    # the scaling can be judged on real paper closes.
    if _reg is not None:
        pos["entry_features"]["regime"] = dict(_reg)
        pos["entry_features"]["stake_usd"] = _stake

    # Authoritative section: dedupe + caps + bankroll re-checked
    # under the same lock that debits cash and inserts the position.
    with _ledger_lock():
        _eib = _ei_recover()
        if _eib:
            return _skip(mint, _eib, False)
        d = _read()
        if float(d["bankroll"]) < _stake:
            if d["state"] != "bankroll_exhausted":
                d["state"] = "bankroll_exhausted"
                _write(d)
            return _skip(mint, "bankroll exhausted (notional counter $%.2f)"
                         % float(d["bankroll"]), True)
        if mint in d["positions"]:
            return _skip(mint, "already open (race)", True)
        if any(c.get("mint") == mint for c in d["closes"]):
            return _skip(mint, "already traded (race)", True)
        if mint in d["dead_mints"]:
            return _skip(mint, "dead (race)", True)
        if len(d["positions"]) >= _max_open:
            return _skip(mint, "max open positions (%d) (race)"
                         % _max_open, False)
        ed = d["entries_day"]
        if ed.get("date") != _today():
            ed = {"date": _today(), "count": 0}
        if DAILY_ENTRY_CAP is not None and \
                int(ed.get("count") or 0) >= DAILY_ENTRY_CAP:
            return _skip(mint, "daily entry cap (%d) (race)"
                         % DAILY_ENTRY_CAP, False)
        ed["count"] = int(ed.get("count") or 0) + 1
        d["entries_day"] = ed
        # Shared cross-lane paper account (Stage 1, 2026-11-22,
        # owner-approved shared demo account): when enabled it is
        # the AUTHORITATIVE capital gate — the entry happens only
        # if the shared cash covers the stake, so two lanes can
        # never spend the same dollars. Lock order is always
        # lane-ledger -> account (the account never calls back),
        # and the debit lands immediately before the position
        # insert, which cannot fail. Fail-closed and transient:
        # cash freed by another lane's close re-enables entries.
        # MANDATORY capital gate (function-local import: not rebindable by
        # variant engines; no private-counter fallback; see entry_guard).
        try:
            try:
                from daemon.launch_scanner import entry_guard as _eg
            except ImportError:
                import entry_guard as _eg
            _why_blocked = _eg.mandatory_entry_refusal(LANE)
        except Exception:
            _why_blocked = ("mandatory entry guard unavailable; entries "
                            "blocked fail-closed")
        if _why_blocked:
            return _skip(mint, _why_blocked, False)
        _pa = _paper_account()
        if _pa is not None:
            # Stage C: reconcile pending close intents BEFORE any
            # entry/gate/capital decision - a restart must not
            # permit new entries while a prior close's account
            # settle is unconfirmed, and a FROZEN intent blocks
            # the entry outright (parent review 2026-11-22).
            _recover_close_intents()
            if _ENTRY_BLOCKED:
                return (False,
                        "close-intent divergence frozen: entries "
                        "blocked until operator resolution", False)
            # Stable op_id from the pool's durable identity (mint +
            # pool-creation ts, falling back to entry ts), so a
            # retry of an uncertain debit response — even across a
            # restart — returns the original outcome instead of
            # double-spending. qty is the filled unit amount.
            _born = float(cand.get("created_ts") or 0) or pos["entry_ts"]
            # Account units must equal the LANE's token units (pos["tokens"] is net of entry gas), or the
            # per-tranche credits (qty = leg tokens) can never consume the account position exactly.
            _qty = float(pos["tokens"])
            # LANE (not a literal) keys the op_id: engine-loaded lane
            # variants (grad_raw, grad_select, tranche, ...) run this
            # same code under their own LANE override, and two lanes
            # entering the SAME pool must never collapse onto one
            # op_id — the retry dedupe would hand the second lane the
            # first lane's outcome without debiting.
            from daemon.launch_scanner import entry_guard as _eg
            _age_why = _eg.quote_age_refusal(
                _entry_row, _pa.max_data_age_s(), time.time())
            if _age_why:
                return _skip(mint, "risk: " + _age_why, False)
            _op = "%s:entry:%s:%d" % (LANE, mint, int(_born * 1000))
            _ei_fill = None
            if _demo_mode():
                # DEMO: fill = Jupiter-quote simulation, NEVER the model fill; no filler or a skip
                # REFUSES the entry (no silent fallback to modelled P&L).
                _df = _demo_filler()
                if _df is None:
                    return _skip(mint, "demo exec: quote-simulation filler unavailable; entry refused", False)
                try:
                    _op = _df.intent_id(LANE, mint)  # durable intent; stale/unknown classified from the account
                    _prior_fill = _df.get_fill(_op)  # durable journal read BEFORE buy
                    _committed = _prior_fill is not None and _df.op_committed(_op) is True  # replay needs account debit evidence
                    _fill = _df.buy(_op, mint, _stake, _pa.max_price_impact_pct())
                except _demo.DemoSkip as _e:
                    return _skip(mint, "demo exec: %s" % _e.reason, False)
                from daemon.launch_scanner import entry_guard as _eg_pq
                _pq = _eg_pq.post_quote_refusal(_fill, _prior_fill, _committed, _pa.max_data_age_s(), time.time())
                if _pq:
                    try:
                        _df.release_intent(LANE, mint)
                    except Exception as _re:
                        logger.warning("%s: release_intent failed (%r); a retry may replay the original fill", LANE, _re)
                    return _skip(mint, "risk: " + _pq, False)
                _qty = _fill["qty_tokens"]
                _ei_fill = _fill
                filled = _fill["price_usd"]
                pos.update({"tokens": _qty, "entry_price": filled, "peak_price": filled,
                            "gas_in_usd": round(_fill["fee_usd"], 6),
                            "execution": {"source": _fill["execution_source"], "entry_op": _op,
                                          "signature": _fill["signature"],
                                          "synthetic_signature": _fill["synthetic_signature"],
                                          "provenance": _fill["provenance"]}})
                pos["legs"]["c"]["peak"] = filled
                pos["entry_snapshot"]["filled_price"] = filled
                pos["entry_snapshot"]["cost_summary_basis"] = "MODEL_NOT_EXECUTION"
        # COMMIT BARRIER (root-cause fix for the entry race against cycle/entry-scan fault writers, which hold only the fault lock): from just before the account debit through the
        # lane write, hold the cycle FAULT lock (order ledger -> fault, as the abort hook) and RE-CHECK the mandatory guard under it, so a fault is either recorded before this
        # re-check (entry refused, nothing debited) or only after the write. The wait is SHORT (entry_guard.ENTRY_FAULT_WAIT_S, held while the ledger lock is held, so it bounds
        # the delay an exit can see behind an entry); a timeout refuses the entry. Flag off: a no-op. Never taken by exits.
        _ei_path = None
        _eim = None
        _cbx = None
        try:
            _cbx = _eg.commit_barrier(type("_LedgerRef", (), {"LEDGER_PATH": LEDGER_PATH})(), _eg.ENTRY_FAULT_WAIT_S)
            _cbx.__enter__()
        except Exception as _cbe:
            if _demo_mode() and _pa is not None:
                try:
                    _demo_filler().release_intent(LANE, mint)
                except Exception as _re:
                    logger.warning("%s: release_intent failed (%r) after commit-barrier refusal", LANE, _re)
            return _skip(mint, "entry commit barrier unavailable (%r); entries blocked fail-closed" % (_cbe,), False)
        try:
            try:
                _why2 = _eg.mandatory_entry_refusal(LANE)
            except Exception:
                _why2 = "mandatory entry guard unavailable at commit; entries blocked fail-closed"
            if _why2:
                if _demo_mode() and _pa is not None:
                    try:
                        _demo_filler().release_intent(LANE, mint)
                    except Exception as _re:
                        logger.warning("%s: release_intent failed (%r) after commit-time refusal", LANE, _re)
                return _skip(mint, _why2, False)
            if _pa is not None:
                # The account flock is shared by the whole roster and its plain acquire blocks without a bound; while we hold the lane ledger lock AND the fault lock, bound the ACQUIRE
                # (AccountBusy is raised before the account critical section starts: nothing was written, so this is a clean transient refusal; the demo intent is released below).
                # A fake account without the scope helper falls back to the plain call. The bound does not cover I/O inside the account critical section.
                try:
                    _eim = _ei() if (_demo_mode() and _ei_fill is not None) else None
                    if _eim is not None:
                        _ei_path = _eim.write_record(LEDGER_PATH, _eim.build_record(LANE, mint, _op, _stake, _qty, pos, _ei_fill,
                                                     {"mint": mint, "pool": cand.get("pool_address"), "created_ts": cand.get("created_ts")}))
                except Exception as _eie:
                    try:
                        _demo_filler().release_intent(LANE, mint)
                    except Exception as _re:
                        logger.warning("%s: release_intent failed (%r) after entry-intent record failure", LANE, _re)
                    return _skip(mint, "entry intent record unavailable (%r); entry refused before any debit" % (_eie,), False)
                try:
                    _bw = getattr(_pa, "bounded_lock_wait", None)
                    if _bw is not None:
                        with _bw(_eg.ENTRY_ACCOUNT_WAIT_S):
                            _ok, _why, _pid = _pa.try_debit(LANE, mint, _stake, _qty,
                                                            _op, durable_refusal=_demo_mode())
                    else:
                        _ok, _why, _pid = _pa.try_debit(LANE, mint, _stake, _qty,
                                                        _op, durable_refusal=_demo_mode())
                except getattr(_pa, "AccountBusy", ()) as _ab:
                    _ok, _why, _pid = False, "account busy (%s): entry refused, nothing written" % (_ab,), None
                if not _ok:
                    if _demo_mode():
                        try:
                            _demo_filler().release_intent(LANE, mint)
                        except Exception as _re:
                            logger.warning("%s: release_intent failed (%r); a retry may replay the original (stale) fill", LANE, _re)
                    return _skip(mint, "paper account: " + _why, False)
                # Pin the open instance so the close can never settle
                # a later reopen of the same mint.
                pos["pa_pos_id"] = _pid
                _INFLIGHT_OPEN.add(str(_pid))   # account holds it, lane books do not yet: not "unknown" for the marks hook
            d["bankroll"] = round(float(d["bankroll"]) - _stake, 6)
            d["positions"][mint] = pos
            d["watchlist"].pop(mint, None)
            try:
                _write(d)
            finally:
                _INFLIGHT_OPEN.discard(str(pos.get("pa_pos_id")))
            if _ei_path is not None:
                _eim.remove_record(_ei_path)   # AFTER the lane commit; idempotent, a failure is class A on the next pass
        finally:
            _cbx.__exit__(None, None, None)
        if _demo_mode():
            try:
                _demo_filler().consume_intent(LANE, mint)
            except Exception as _ce:  # safe: a stale intent is classified from the account on re-entry
                logger.warning("%s: consume_intent failed (%r); stale intent dropped on re-entry", LANE, _ce)
    # Decision memory (2026-11-17): log a PENDING decision at
    # entry — pool age / mcap / liq / entry price now; the lane's
    # _stamp_detection merges the select measurements in after.
    # Journal only; never gates anything.
    try:
        if _lessons is not None:
            _lessons.log_decision(
                LANE, mint, pos["entry_ts"],
                _lessons.extract_features(
                    pos.get("entry_features") or {}, pos),
                symbol=symbol, home=_lessons_home())
    except Exception:
        pass
    logger.warning("grad OPEN %s %s @ %s ($%.2f, pool age %.0fs, liq $%.0f, "
                   "mcap $%.0f, NOTIONAL counter now $%.2f [unfunded; "
                   "real cash is the shared paper account])",
                   symbol, mint[:12], filled, _stake, age, liquidity, mcap,
                   account_cash())
    # P4: entry event (fires for both grad and grad_raw engine
    # instances; _record_event stamps this instance's LANE).
    _record_event("entry", {
        "mint": mint, "symbol": symbol, "entry_price": filled,
        "quoted_price": quoted,
        "born_ts": pos.get("born_ts"), "seen_ts": pos.get("seen_ts"),
        "decided_ts": pos.get("decided_ts"),
        "entry_ts": pos.get("entry_ts"),
        "detection": (pos.get("entry_features") or {}).get("detection"),
    })
    return True, "opened", True


try:
    from daemon.launch_scanner import demo_fill as _demo
except Exception:
    _demo = None  # type: ignore


def _demo_filler():
    return _demo.default_filler() if _demo is not None else None


def _demo_mode():
    return bool(_demo is not None and _demo.mode_enabled())


# EXIT POLICY (open owner decision, NOT chosen here): the owner max_price_impact_pct also gates
# exits when set; only an unset/invalid cap falls back to this so a missing config cannot strand
# a position. The effective cap + source are recorded in each exit's provenance.
_EXIT_IMPACT_CAP_PCT = 100.0


def _exit_cap():
    try:
        v = float(_paper_account().max_price_impact_pct())
    except Exception:
        v = 0.0
    if v > 0:
        return v, "owner_max_price_impact_pct"
    return _EXIT_IMPACT_CAP_PCT, "fallback_unset_or_invalid"


def _fill_key(pos, key):
    _fk = "%s:exit:%s:%s" % (LANE, pos["mint"], key)
    if pos.get("tranche_credit") and pos.get("pa_pos_id"):
        _fk += ":%s" % pos["pa_pos_id"]
    return _fk


def _journaled_fills(pos):
    """{leg-key-suffix: fill record} for this position instance's journaled exit fills, or None if unreadable."""
    try:
        _df = _demo_filler()
        if _df is None:
            return None
        pre = "%s:exit:%s:" % (LANE, pos["mint"])
        suf = ":%s" % pos["pa_pos_id"] if (pos.get("tranche_credit") and pos.get("pa_pos_id")) else ""
        allf = _df._locked(lambda j: dict(j["fills"]))
        out = {}
        for k, v in allf.items():
            if k.startswith(pre) and (not suf or k.endswith(suf)):
                out[k[len(pre):len(k) - len(suf)] if suf else k[len(pre):]] = v
        return out
    except Exception:
        return None


def _replay_identity(rec, reason, pkind, receipt_kind):
    """(reason, kind, ts) for the replay of an executed sale. Reason and timestamp are ALWAYS the original fill's stamp (the retry's
    or the reconcile sentinel's reason never overwrites history). Kind: the account RECEIPT's kind when the reconcile path supplies one
    (receipt_kind is a dedicated parameter only reconcile sets; a reason string grants nothing), else the original stamp's kind.
    An unstamped (older) fill keeps the caller's reason and gets kind UNKNOWN unless a receipt kind exists, never upgraded."""
    it = (rec or {}).get("intent") or {}
    stamped = bool(it.get("reason") or it.get("kind") is not None)
    r0 = it.get("reason") if stamped and it.get("reason") else reason
    k0 = it.get("kind") if stamped else (_pk.UNKNOWN if _pk is not None else None)
    return r0, (receipt_kind if receipt_kind is not None else k0), (it.get("ts") if stamped else None)


def _intent_of(rec, reason, pkind):
    """Original economics of a journaled fill win over whatever the retry carries: a write-down (or any other) retry can
    never relabel or re-price a sale that already happened. A fill with no stamped intent (older journal) keeps the
    retry's reason but its kind is UNKNOWN, never upgraded."""
    it = (rec or {}).get("intent") or {}
    if it.get("kind") is not None or it.get("reason"):
        return it.get("reason") or reason, it.get("kind")
    return "reconciled_journaled_fill", (_pk.UNKNOWN if _pk is not None else None)


def _receipt_state(pos, key):
    """('ok', receipt) | ('none', None) (journal readable, no credit for this stable op id) | ('unreadable', None) | ('na', None)."""
    if not pos.get("tranche_credit") or not pos.get("pa_pos_id"):
        return "na", None
    _pa = _paper_account()
    if _pa is None:
        return "na", None
    _op = "%s:leg:%s:%s:%s" % (LANE, key, pos["mint"], pos["pa_pos_id"])
    try:
        r = _pa.op_receipts([_op])
    except Exception:
        return "unreadable", None
    if r.get("_unreadable"):
        return "unreadable", None
    rc = r.get(_op)
    return ("ok", rc) if rc is not None else ("none", None)


def _unstamped_receipt_kind(pos, key, rec, receipt_kind):
    """Upgrade compatibility: a journaled fill with NO intent stamp predates the stamp, and its credit (if any) was recorded under the OLD
    code's kind. The account refuses a credit whose kind differs from the recorded op, so the replay must use the ACTUAL receipt's kind.
    Returns (ok, receipt_kind): ok False means the receipt journal is unreadable (caller must stay pending, never invent UNKNOWN);
    a readable journal with no receipt proves nothing was credited, so UNKNOWN (never upgraded) is honest."""
    if receipt_kind is not None or rec is None:
        return True, receipt_kind
    it = (rec or {}).get("intent") or {}
    if it.get("reason") or it.get("kind") is not None:
        return True, receipt_kind
    st, rc = _receipt_state(pos, key)
    if st == "unreadable":
        return False, None
    if st == "ok":
        return True, (rc.get("price_kind") or (_pk.UNKNOWN if _pk is not None else None))
    return True, None


def _sim_twap_sale(pos, key, tokens, reason, pkind, receipt_kind, default_kind=None):
    """ONE identity path for the sim TWAP slice and remainder (same rules as the ladder): the fill journal must be readable; an
    existing fill for this key is REPLAYED with its ORIGINAL stamp (reason, kind, ts) whatever the retry carries (a write-down or a
    different reason cannot relabel or skip it; the explicit receipt_kind, set only by reconcile, wins for kind); no fill and a
    write-down kind means no sale is made (returns "writedown"). Returns None (refused, visible pending / filler skip),
    "writedown", or (fill, cap, src, reason, kind, ts)."""
    _jfs = _journaled_fills(pos)
    if _jfs is None:
        pos["pending_exit"] = {"leg": key, "why": "fill_journal_unreadable", "ts": time.time()}
        return None
    _jf = _jfs.get(key)
    _ots = None
    if _jf is not None:
        _okr, receipt_kind = _unstamped_receipt_kind(pos, key, _jf, receipt_kind)
        if not _okr:
            pos["pending_exit"] = {"leg": key, "why": "receipt_journal_unreadable", "ts": time.time()}
            return None
        reason, _k, _ots = _replay_identity(_jf, reason, pkind, receipt_kind)
    else:
        _k = pkind if pkind else (receipt_kind if receipt_kind is not None else (default_kind if default_kind is not None else _pkind(reason, None)))
        if _k == "write_down_zero":
            return "writedown"
    _r = _demo_sell(pos, tokens, key, meta={"reason": reason, "kind": _k, "legs": [key], "ts": time.time()})
    if _r is None:
        return None
    return _r[0], _r[1], _r[2], reason, _k, _ots, (_jf is not None)


def _recon_note(pos, key, what, **detail):
    """Visible (never silent) reconciliation note on the position: both values and a warning. Deduped by (key, what)."""
    notes = pos.setdefault("recon_notes", [])
    if not any(n.get("key") == key and n.get("what") == what for n in notes):
        notes.append(dict(key=key, what=what, ts=time.time(), **detail))
        _warn_throttled(("recon", pos.get("mint", ""), key, what), "grad RECON (needs operator review) %s %s %s %s",
                        pos.get("mint", "")[:12], key, what, detail)


def _sim_settle(pos, key, tokens, fill, replayed=True):
    """ONE receipt-governed settlement for every sim credit site (ladder leg, TWAP slice, TWAP remainder).
    Returns None when the leg must stay pending (pending_exit set with a SPECIFIC why: no fallback recompute), else a dict
    {gross, fee, kind (account kind, may be None), lane_kind, acct_qty (None = recompute), price}.
    - 'na' (unmarked / no account): the fill's own economics, unchanged behaviour.
    - 'unreadable': pending receipt_journal_unreadable (the sale is journaled; the retry replays it).
    - 'none': the fill's economics, credited fresh (the caller's kind).
    - 'ok': the receipt is the account's recorded fact. When its proceeds, fee and qty AGREE with the journaled fill (technical tolerance) the
      credit uses the receipt's exact kind/qty (including a legitimate old qty None / kind None). A money or qty DISAGREEMENT is QUARANTINED
      (pending receipt_fill_money_mismatch / receipt_qty_mismatch, no credit, no booking): it never governs silently. An incomplete receipt
      (no proceeds or no qty) is pending receipt_incomplete. The ORIGINAL fill gross/price are kept (fill_gross/fill_price)
      next to the receipt economics; differences beyond tolerance leave a visible recon note, never silent."""
    out = {"gross": fill["gross_usd"], "fee": fill["fee_usd"], "kind": None, "acct_qty": None, "price": fill["price_usd"],
           "fill_gross": fill["gross_usd"], "fill_price": fill["price_usd"], "receipt": False}
    if not replayed:
        return out   # a FRESH sale (no pre-existing journaled fill) has no earlier receipt to consult: credit it from its own fill (stable op id keeps retries idempotent)
    st, rc = _receipt_state(pos, key)
    if st == "unreadable":
        pos["pending_exit"] = {"leg": key, "why": "receipt_journal_unreadable", "ts": time.time()}
        return None
    if st != "ok":
        return out
    if rc.get("proceeds") is None:
        pos["pending_exit"] = {"leg": key, "why": "receipt_incomplete", "ts": time.time()}
        return None
    # qty None is a LEGITIMATE old receipt (credit journaled with qty None): it is passed through exactly (sentinel), never recomputed.
    _rq = rc.get("qty")
    out.update(receipt=True, gross=float(rc["proceeds"]), fee=float(rc.get("fee") or 0.0), kind=rc.get("price_kind"),
               acct_qty=(float(_rq) if _rq is not None else None), qty_none=(_rq is None))
    out["price"] = out["gross"] / tokens if tokens else out["price"]
    # Technical rounding tolerance (scale-aware), NOT an owner risk threshold: 1e-6 USD absolute or 1e-4 relative, whichever is larger.
    def _tol(x): return max(1e-6, 1e-4 * abs(float(x)))
    _gd = abs(out["gross"] - float(fill["gross_usd"])) > _tol(fill["gross_usd"])
    _fd = abs(out["fee"] - float(fill["fee_usd"])) > _tol(fill["fee_usd"])
    if _gd:
        _recon_note(pos, key, "receipt_proceeds_differ_from_fill", receipt_gross=out["gross"], fill_gross=float(fill["gross_usd"]))
    if _fd:
        _recon_note(pos, key, "receipt_fee_differs_from_fill", receipt_fee=out["fee"], fill_fee=float(fill["fee_usd"]))
    _qd = False
    try:
        _want = _acct_qty(pos, key, tokens)
        if _rq is None:
            _recon_note(pos, key, "receipt_qty_none", recomputed_qty=_want, lane_tokens=tokens)
        elif _want and abs(out["acct_qty"] - _want) > max(1e-9, 1e-6 * abs(_want)):
            _qd = True
            _recon_note(pos, key, "receipt_qty_mismatch", receipt_qty=out["acct_qty"], recomputed_qty=_want, lane_tokens=tokens)
    except Exception as e:
        _recon_note(pos, key, "qty_check_failed", error=repr(e)[:120])
        pos["pending_exit"] = {"leg": key, "why": "qty_check_failed", "ts": time.time()}
        return None
    if _gd or _fd or _qd:
        # QUARANTINE, never silently govern: a receipt whose money or qty disagrees with the journaled fill is an unproven economic
        # difference (corruption, a different sale, or an old formula we have not proven). No credit, no lane booking, no second sale;
        # the leg stays open with both values on the pending record and the recon note, until a proven resolution exists.
        pos["pending_exit"] = {"leg": key, "why": "receipt_fill_money_mismatch" if (_gd or _fd) else "receipt_qty_mismatch",
                               "receipt": {"gross": out["gross"], "fee": out["fee"], "qty": out["acct_qty"]},
                               "fill": {"gross": float(fill["gross_usd"]), "fee": float(fill["fee_usd"]), "tokens": tokens}, "ts": time.time()}
        return None
    return out


def _credit_st(pos, key, tokens, st, kind):
    """Credit from a _sim_settle result. acct_qty is passed ONLY when a receipt governs (call shape otherwise unchanged)."""
    if st.get("qty_none"):
        return _tranche_credit(pos, key, tokens, st["gross"], st["fee"], kind, acct_qty=_QTY_NONE)
    if st.get("acct_qty") is not None:
        return _tranche_credit(pos, key, tokens, st["gross"], st["fee"], kind, acct_qty=st["acct_qty"])
    return _tranche_credit(pos, key, tokens, st["gross"], st["fee"], kind)


def recon_health(now=None):
    """Operator summary (read-only, no thresholds invented): open positions with recon notes or a pending_exit, with ages in seconds.
    Returns {"n_recon": int, "n_pending": int, "items": [{mint, why, age_s, notes}]} or None if the ledger is unreadable (visible, never empty)."""
    now = time.time() if now is None else now
    try:
        ps = open_positions()
    except Exception:
        return None
    items = []
    for m, p in ps.items():
        pe = p.get("pending_exit") or {}
        ns = [n.get("what") for n in (p.get("recon_notes") or [])]
        sp = p.get("settlement_pending") or {}
        mb = p.get("migration_blocked") or {}
        if pe or ns or sp or mb:
            items.append({"mint": m, "migration_blocked": mb.get("code"), "migration_blocked_age_s": (now - float(mb["since"])) if mb.get("since") else None,
                          "migration_tries": mb.get("tries"), "why": pe.get("why"), "age_s": (now - float(pe["ts"])) if pe.get("ts") else None, "notes": ns,
                          "settlement_pending": sp.get("why"), "settlement_age_s": (now - float(sp["since"])) if sp.get("since") else None,
                          "settlement_tries": sp.get("tries")})
    return {"poll_aborts": dict(_POLL_ABORTS), "price_obs_unsupported_polls": _OBS_UNSUPPORTED["n"], "n_recon": sum(1 for i in items if i["notes"]), "n_pending": sum(1 for i in items if i["why"]),
            "n_settlement_pending": sum(1 for i in items if i["settlement_pending"]),
            "n_migration_blocked": sum(1 for i in items if i.get("migration_blocked")), "items": items}


def _demo_sell(pos, leg_tokens, key, meta=None):
    """Sell through the quote sim. Returns (fill, cap, src) or None (leg stays PENDING with
    diagnostics; nothing invented)."""
    _df = _demo_filler()
    if _df is None:
        pos["pending_exit"] = {"leg": key, "why": "filler unavailable", "ts": time.time()}
        return None
    cap, src = _exit_cap()
    try:
        # Marked (per-tranche) positions scope the fill op to the position instance, so a same-mint RE-ENTRY can
        # never replay an older position's journaled fill. Legacy positions keep the original key (their
        # journaled fills stay addressable); only positions created after this change carry the marker.
        _fk = _fill_key(pos, key)
        return _df.sell(_fk, pos["mint"], leg_tokens,
                        cap, meta=meta), cap, src
    except _demo.DemoSkip as e:
        pos["pending_exit"] = {"leg": key, "why": e.reason, "detail": e.detail, "ts": time.time()}
        logger.warning("grad demo exit pending %s %s: %s", pos["mint"][:12], key, e.reason)
        return None


def account_cash():
    try:
        return float(_load()["bankroll"])
    except Exception:
        return 0.0


# ---- detection scan ---------------------------------------------------------

def fetch_candidates(pages=GT_PAGES):
    """Newest Solana pools from GeckoTerminal (free). Never raises."""
    try:
        return _sources.geckoterminal_new_pools("solana", pages=pages) \
            or []
    except Exception:
        return []


def _migration_rows(feed, now):
    """Drain a PumpPortalFeed into graduation candidate rows (P2:
    the LIVE migration stream as an additional detection source for
    the gated lane — the KWH fix: GT indexed that pool ~10 min after
    graduation, the stream sees it in seconds). Conversion is
    grad_raw's, shared so both lanes read the stream identically.
    Never raises."""
    rows = []
    try:
        try:
            from daemon.launch_scanner import grad_raw as _raw_mod
        except Exception:
            import grad_raw as _raw_mod  # type: ignore
        for item in feed.drain() or []:
            # P4: record every drained stream item as a migration
            # event (the raw item's own `source` rides along, so a
            # birth that arrived on this drain stays identifiable).
            try:
                body = dict(item) if isinstance(item, dict) else {}
                body.setdefault("mint", str((item or {}).get("mint")
                                            or ""))
                _record_event("migration", body)
            except Exception:
                pass
            cand = _raw_mod.migration_to_candidate(item, now)
            if cand is not None:
                cand.setdefault("first_seen_ts", now)
                rows.append(cand)
    except Exception:
        pass
    return rows


def scan_once(now=None, feed=None, fetch_gt=True):
    """One detection cycle: fold new graduations into the watchlist
    and attempt entries for everything still in its age window.

    Sources: the PumpPortal migration stream when `feed` is given
    (primary, sub-second) and GeckoTerminal newest pools as backfill
    (fetch_gt — the daemon calls with fetch_gt=False between its
    60s backfill beats so the stream drives the loop). Returns
    counters. Never raises."""
    out = {"pools": 0, "stream": 0, "watching": 0, "opened": 0,
           "skipped": 0, "dead": 0}
    try:
        return _scan_once(out, now, feed, fetch_gt)
    except Exception as e:
        logger.warning("grad scan_once failed: %s", e)
        return out


def _scan_once(out, now, feed=None, fetch_gt=True):
    now = time.time() if now is None else now
    ensure_ledger()
    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state
    # error (see the entry gate above).
    if not _ks.gate(LANE):
        logger.warning("grad scan: lane halted by killswitch — "
                       "no entries (exits still run)")
        return out
    pools = fetch_candidates() if fetch_gt else []
    out["pools"] = len(pools)
    if feed is not None:
        stream_rows = _migration_rows(feed, now)
        out["stream"] = len(stream_rows)
        pools = stream_rows + pools
    with _ledger_lock():
        d = _read()
        # Fold fresh graduation pools into the watchlist.
        for p in pools:
            mint = str(p.get("mint") or "")
            if not mint or mint in d["positions"] or \
                    mint in d["dead_mints"] or \
                    any(c.get("mint") == mint for c in d["closes"]):
                continue
            if is_graduation_candidate(p, now) is None:
                continue
            row = dict(p)
            row.setdefault("first_seen_ts", now)
            d["watchlist"][mint] = row
        # Prune: aged-out watchlist rows become final skips; stale
        # dead records expire (a mint can never re-graduate, the 48h
        # TTL is pure file hygiene).
        for mint, row in list(d["watchlist"].items()):
            if is_graduation_candidate(row, now) is None:
                d["dead_mints"][mint] = {
                    "ts": now, "reason": "window expired"}
                del d["watchlist"][mint]
                out["dead"] += 1
        cutoff = now - DEAD_TTL_S
        d["dead_mints"] = {m: v for m, v in d["dead_mints"].items()
                           if float(v.get("ts") or 0) >= cutoff}
        watch = list(d["watchlist"].values())
        _write(d)
    out["watching"] = len(watch)
    for cand in watch:
        opened, _why, final = consider_candidate(cand, now)
        if opened:
            out["opened"] += 1
        else:
            out["skipped"] += 1
            if final:
                with _ledger_lock():
                    d = _read()
                    d["watchlist"].pop(str(cand.get("mint") or ""), None)
                    _write(d)
    return out


# ---- exits (the user's ladder, ceo/mirror-identical constants) ------------

# Exit price provenance (2026-11-22): additive settlement labels.
try:
    from daemon.launch_scanner import exit_provenance as _pk
except Exception:
    try:
        import exit_provenance as _pk  # type: ignore
    except Exception:
        _pk = None  # type: ignore


def _pkind(reason, pkind=None):
    """Resolve the exit price kind for one settlement tranche."""
    if pkind:
        return pkind
    if _pk is None:
        return None
    return _pk.classify_reason(
        reason, getattr(_f2, "F2_REASON", None) if _f2 else None)


_WARN_LAST = {}
_WARN_EVERY_S = 300.0


def _warn_throttled(key, msg, *args):
    """Operator-visible, rate-limited warning: the first occurrence of `key` logs immediately, repeats within
    _WARN_EVERY_S are suppressed (the persisted pending_exit still records the state on every poll)."""
    now = time.time()
    last = _WARN_LAST.get(key)
    if last is not None and now - last < _WARN_EVERY_S:
        return
    _WARN_LAST[key] = now
    logger.warning(msg, *args)


_QTY_NONE = object()   # acct_qty sentinel: the receipt itself recorded qty=None, so the credit must pass None EXACTLY


def _tranche_credit(pos, leg_key, qty, gross, fee, kind, acct_qty=None):
    """Credit the shared account for ONE ladder leg sold through the quote sim, so the account's
    qty/basis shrink with the lane (marks, equity and the drawdown peak then see the true remaining
    units). Idempotent by op_id (position instance + leg), so a replayed leg fill (the DemoFiller
    journals the original) cannot double-credit. Returns False when the account refused (leg stays
    pending). Positions without the tranche marker, a pa_pos_id, or with the account off keep the
    deferred full-close credit (returns True, no credit). Write-down legs are never credited as sales.
    Gap: no close-intent is staged for a tranche; crash safety rests on op_id idempotency + journaled fill."""
    if not pos.get("tranche_credit") or not pos.get("pa_pos_id") or kind == "write_down_zero":
        return True
    _pa = _paper_account()
    if _pa is None:
        return True
    _op = "%s:leg:%s:%s:%s" % (LANE, leg_key, pos["mint"], pos["pa_pos_id"])
    # ACCOUNT units = lane units, plus (migrated legacy position only, ONE leg) the opening-unit dust: a legacy entry
    # debited stake/price units while the lane holds net-of-gas tokens, so the first migrated leg also removes that
    # difference and the account then holds exactly the lane's unsold tokens. The lane tally stays in LANE units.
    _lane_qty = qty
    qty = (None if acct_qty is _QTY_NONE else (acct_qty if acct_qty is not None else _acct_qty(pos, leg_key, qty))) if qty is not None else qty
    ok, why = _pa.credit(LANE, pos["mint"], gross, _op, pos_id=pos["pa_pos_id"], qty=qty, fee=fee, price_kind=kind)
    if not ok:
        # Same convergence rule as the final close: an orphan (the account never held these units, and never did
        # for this position) is a legitimate pre-account position, not a refusal: the leg proceeds and the
        # lane keeps its own books. Everything else is a refusal: the leg stays pending WITH its diagnostic.
        try:
            if _pa.op_outcome(_op) == "orphan" and not _pa.pos_ever_existed(pos_id=pos["pa_pos_id"], lane=LANE, mint=pos["mint"]):
                return True
        except Exception:
            pass
        _diverged = False
        try:
            # A DEBITED position the account no longer holds (stale restore) is a ledger DIVERGENCE, not a
            # transient refusal: freeze it loud and block entries, exactly like a final-close intent that
            # cannot settle (the pre-tranche contract). Never leave it as a silent per-poll warning.
            _diverged = (_pa.op_outcome(_op) == "orphan"
                         and bool(_pa.pos_ever_existed(pos_id=pos["pa_pos_id"], lane=LANE, mint=pos["mint"])))
        except Exception:
            _diverged = False
        if _diverged:
            global _ENTRY_BLOCKED
            _ENTRY_BLOCKED = True
            _jfail = False
            _prior = pos.get("pending_exit") or {}
            _iid = _prior.get("intent_id") if _prior.get("leg") == leg_key else None
            if _prior.get("why") != "tranche_credit_divergence":
                try:
                    _ci = _close_intents()
                    if _ci is not None:
                        if not _iid:
                            _iid = _ci.begin(LANE, pos["mint"], pos["pa_pos_id"],
                                             [{"kind": "credit", "op_id": _op, "mint": pos["mint"], "amount": gross,
                                               "qty": qty, "price_kind": kind, "pos_id": pos["pa_pos_id"]}],
                                             {"realized_usd": round(gross - fee, 6), "close_ts": int(float(pos.get("entry_ts") or 0) * 1000) + ord(str(leg_key)[-1]),   # STABLE: a retry regenerates the same intent id
                                              "closed_reason": "tranche_leg_" + str(leg_key)})
                        _ci.freeze(LANE, _iid, "tranche credit refused: debited position absent from account (divergence): %s" % str(why)[:120])
                        logger.error("grad: tranche credit %s %s FROZEN: debited position absent from the account; "
                                     "ENTRIES BLOCKED until operator resolution", pos["mint"][:12], leg_key)
                except Exception as _fe:
                    # journal write failed: entries stay blocked in-process; the intent id (if begin landed) is kept so
                    # the retry freezes the SAME intent instead of opening a second one
                    _jfail = True
                    logger.error("grad: divergence freeze journal write failed (%r); entries blocked, will retry", _fe)
            pos["pending_exit"] = {"leg": leg_key, "why": ("tranche_credit_refused" if _jfail else "tranche_credit_divergence"),
                                   "detail": str(why)[:200], "ts": time.time()}
            if _iid:
                pos["pending_exit"]["intent_id"] = _iid
            return False
        # a credit larger than the account's remaining units (receipt qty on an earlier leg was smaller than a recompute) is a QTY
        # DIVERGENCE with a specific why, not a generic refusal
        pos["pending_exit"] = {"leg": leg_key, "why": ("account_remaining_qty_mismatch" if "exceeds remaining" in str(why) else "tranche_credit_refused"),
                               "detail": str(why)[:200], "ts": time.time()}
        _warn_throttled(("refused", pos["mint"], leg_key, str(why)[:60]), "grad: tranche credit %s %s refused: %s (leg stays pending)", pos["mint"][:12], leg_key, why)
        return False
    tc = pos.setdefault("tranche_credited", {"net": 0.0, "qty": 0.0})
    # the ledger credit is idempotent; the lane-side tally is keyed per leg so a replay never double counts
    seen = tc.setdefault("legs", {})
    if leg_key not in seen:
        seen[leg_key] = {"net": round(gross - fee, 6), "qty": _lane_qty}
        tc["net"] = round(sum(v["net"] for v in seen.values()), 6)
        tc["qty"] = sum(v["qty"] for v in seen.values())
    return True


def _sold_legs_all_credited(pos):
    """Structural check (not a numeric residual): every SOLD (non write-down) leg/slice/remainder of a marked
    position is in the per-tranche credit tally, so the final close has no sale left to credit."""
    if not pos.get("tranche_credit"):
        return False
    seen = (pos.get("tranche_credited") or {}).get("legs") or {}
    if pos.get("twap") is not None:
        tw = pos["twap"] or {}
        for i in range(1, int(tw.get("done") or 0) + 1):
            if "twap%d" % i not in seen:
                return False
        rm = tw.get("remainder")
        if rm and rm.get("exit_price_kind") != "write_down_zero" and "twap_remainder" not in seen:
            return False
        return True
    for lk, lv in pos["legs"].items():
        if lv.get("done") and lv.get("exit_price_kind") != "write_down_zero" and lk not in seen:
            return False
    return True


def _uncredited_kinds(pos):
    """Price kinds of the SOLD legs/slices that the per-tranche credit has NOT already booked (the final close's
    remainder routes on these only, so a stale credited leg A cannot quarantine a current leg C again)."""
    if not pos.get("tranche_credit"):
        return None
    seen = (pos.get("tranche_credited") or {}).get("legs") or {}
    out = []
    if pos.get("twap") is not None:
        tw = pos["twap"] or {}
        if int(tw.get("done") or 0) > len([k for k in seen if k.startswith("twap") and k != "twap_remainder"]):
            out += list(tw.get("price_kinds") or [])
        rm = tw.get("remainder")
        if rm and rm.get("exit_price_kind") != "write_down_zero" and "twap_remainder" not in seen:
            out.append(rm.get("exit_price_kind"))
        return out
    for lk, lv in pos["legs"].items():
        if lv.get("done") and lv.get("exit_price_kind") != "write_down_zero" and lk not in seen:
            out.append(lv.get("exit_price_kind"))
    return out


def _tranche_receipt(pos, leg_key):
    """The account's journaled credit receipt for this leg, or None (not credited yet, unmarked position, or
    unreadable journal). Non-sim legs are not journaled as fills, so a replay after a crash must use the
    RECEIPT's proceeds/fee/qty/kind and never re-derive a different price."""
    if not pos.get("tranche_credit") or not pos.get("pa_pos_id"):
        return None
    _pa = _paper_account()
    if _pa is None:
        return None
    _op = "%s:leg:%s:%s:%s" % (LANE, leg_key, pos["mint"], pos["pa_pos_id"])
    try:
        r = _pa.op_receipts([_op])
    except Exception:
        return None
    if r.get("_unreadable"):
        return None
    return r.get(_op)


def _close_leg(pos, leg_key, exit_price, reason, pkind=None, receipt_kind=None):
    """Close one leg at an honest exit. Returns realized USD for the leg."""
    leg = pos["legs"][leg_key]
    if leg["done"]:
        return 0.0
    leg_tokens = pos["tokens"] * leg["frac"]
    _sim = (pos.get("execution") or {}).get("source") == "jupiter_quote_sim"
    _jfs_l = _journaled_fills(pos) if _sim else {}
    if _sim and _jfs_l is None and pos.get("tranche_credit") and pos.get("pa_pos_id"):
        # the fill journal is unreadable: whether this leg was already sold is unknown. Never fall through to a fresh sale.
        pos["pending_exit"] = {"leg": leg_key, "why": "fill_journal_unreadable", "ts": time.time()}
        return 0.0
    _jf = (_jfs_l or {}).get(leg_key) if _sim else None
    if _sim and (_pkind(reason, pkind) != "write_down_zero" or _jf is not None):
        _ots = None
        if _jf is not None:
            _okr, receipt_kind = _unstamped_receipt_kind(pos, leg_key, _jf, receipt_kind)
            if not _okr:
                pos["pending_exit"] = {"leg": leg_key, "why": "receipt_journal_unreadable", "ts": time.time()}
                return 0.0
            reason, pkind, _ots = _replay_identity(_jf, reason, pkind, receipt_kind)   # executed sale: original reason/ts, original (or receipt) kind; the retry never relabels it
        elif receipt_kind is not None and not pkind:
            pkind = receipt_kind
        _r = _demo_sell(pos, leg_tokens, leg_key, meta={"reason": reason, "kind": _pkind(reason, pkind), "legs": [leg_key], "ts": time.time()})
        if _r is None:
            return 0.0
        _fill, _cap, _src = _r
        pos.pop("pending_exit", None)
        _kind = _pkind(reason, pkind)
        filled_exit, gross, gas_out = _fill["price_usd"], _fill["gross_usd"], _fill["fee_usd"]
        _st = _sim_settle(pos, leg_key, leg_tokens, _fill, replayed=(_jf is not None))
        if _st is None:
            return 0.0
        gross, gas_out, filled_exit = _st["gross"], _st["fee"], _st["price"]
        _ckind = _st["kind"] if _st["receipt"] else _kind
        if _st["receipt"]:
            _kind = _ckind or (_pk.UNKNOWN if _pk is not None else None)
        net = gross - gas_out
        if not _credit_st(pos, leg_key, leg_tokens, _st, _ckind):
            return 0.0   # account side not applied: the leg stays PENDING and replays the journaled fill
        leg.update(done=True, exit_price=filled_exit, realized_usd=round(net, 6),
                   exit_reason=reason, exit_price_kind=_kind, fill_gross=round(_st["fill_gross"], 6), fill_price=_st["fill_price"],
                   **({"exit_ts": _ots} if _ots else {}),
                   exit_execution={"source": _fill["execution_source"], "signature": _fill["signature"],
                                   "synthetic_signature": True, "effective_impact_cap_pct": _cap,
                                   "impact_cap_source": _src, "provenance": _fill["provenance"]})
        pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
        return net
    _rc = _tranche_receipt(pos, leg_key)
    if _rc is not None and _rc.get("proceeds") is not None and leg_tokens > 0:
        # crash replay: the account already holds this leg's credit; finish the lane side from the RECEIPT.
        # The receipt must describe THIS leg (qty agrees) and the idempotent credit must be accepted; anything
        # else leaves the leg pending and loud (the lane never books what the account did not accept).
        if not _receipt_qty_ok(_rc, _acct_qty(pos, leg_key, leg_tokens)):
            pos["pending_exit"] = {"leg": leg_key, "why": "receipt_qty_mismatch", "ts": time.time()}
            logger.warning("grad: receipt for %s %s disagrees with the leg quantity; leg stays pending", pos["mint"][:12], leg_key)
            return 0.0
        gross = float(_rc["proceeds"]); gas_out = float(_rc.get("fee") or 0.0)
        filled_exit = gross / leg_tokens
        _kind = _rc.get("price_kind") or (_pk.UNKNOWN if _pk is not None else None)
        net = gross - gas_out
        if not _tranche_credit(pos, leg_key, leg_tokens, gross, gas_out, _kind):   # idempotent; records the lane tally
            return 0.0
        leg.update(done=True, exit_price=filled_exit, realized_usd=round(net, 6),
                   exit_reason=reason, exit_price_kind=_kind)
        pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
        return net
    try:
        leg_usd = leg_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd,
            # F5: exit-time liquidity (refreshed every poll) beats
            # the entry snapshot — pools drain between entry and exit.
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = leg_tokens * filled_exit
    _kind = _pkind(reason, pkind)
    # A write-down is NOT a sale (design v4 Stage B): no fee is
    # booked on units nobody bought.
    gas_out = (0.0 if _kind == "write_down_zero"
               else _costs.tx_cost_usd(pos["chain"], gross,
                                       venue=VENUE))
    net = gross - gas_out
    if not _tranche_credit(pos, leg_key, leg_tokens, gross, gas_out, _kind):
        return 0.0   # account refused: leg stays pending with the diagnostic
    leg.update(done=True, exit_price=filled_exit,
               realized_usd=round(net, 6), exit_reason=reason,
               exit_price_kind=_kind)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _twap_remaining_tokens(pos):
    """Tokens a TWAP position still holds, computed from the slice COUNT (tokens * (1 - done/slices)) rather than
    from the rounded sold_tokens tally: accumulating 6-dp rounding could make the remainder a hair larger than the
    account's remaining units, which the account refuses ("qty exceeds remaining units")."""
    tw = pos.get("twap") or {}
    n = int(tw.get("slices") or 0)
    done = int(tw.get("done") or 0)
    if n > 0 and 0 <= done <= n:
        return pos["tokens"] - done * (pos["tokens"] / n)
    return pos["tokens"] - float(tw.get("sold_tokens") or 0)


def _close_twap_slice(pos, exit_price):
    """Sell one equal TWAP slice (1/TWAP_SLICES of the original
    tokens) at an honest exit. Mirrors _close_leg's fill + cost
    treatment (one tx cost per slice, like the real recipients'
    per-sell transactions). Returns realized USD for the slice."""
    try:
        n = int(TWAP_SLICES)
    except Exception:
        n = 0
    if n <= 0:
        return 0.0
    tw = pos.setdefault("twap", {"slices": n, "done": 0,
                                 "sold_tokens": 0.0, "sold_usd": 0.0})
    if tw["done"] >= n:
        return 0.0
    slice_tokens = pos["tokens"] / n
    if (pos.get("execution") or {}).get("source") == "jupiter_quote_sim":
        _sk = "twap%d" % (tw["done"] + 1)
        _r = _sim_twap_sale(pos, _sk, slice_tokens, "twap_slice", None, None, default_kind=_pk.CURRENT if _pk is not None else None)
        if _r is None or _r == "writedown":
            return 0.0
        _fill, _cap, _src, _rs, _kd, _ots, _rp = _r
        pos.pop("pending_exit", None)
        _st = _sim_settle(pos, _sk, slice_tokens, _fill, replayed=_rp)
        if _st is None:
            return 0.0
        if _st["receipt"]:
            _kd = _st["kind"] or (_pk.UNKNOWN if _pk is not None else None)
        net = _st["gross"] - _st["fee"]
        if not _credit_st(pos, _sk, slice_tokens, _st, _st["kind"] if _st["receipt"] else _kd):
            return 0.0   # account side not applied: slice stays pending, replays the journaled fill
        tw["done"] += 1
        if _pk is not None:
            _pks = set(tw.get("price_kinds") or [])
            _pks.add(_kd)
            tw["price_kinds"] = sorted(_pks)
        tw["sold_tokens"] = round(tw["sold_tokens"] + slice_tokens, 6)
        tw["sold_usd"] = round(tw["sold_usd"] + net, 6)
        tw["last_exit_price"] = _fill["price_usd"]
        pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
        return net
    _skey = "twap%d" % (tw["done"] + 1)
    _rc = _tranche_receipt(pos, _skey)
    _skind = _pk.CURRENT if _pk is not None else None
    if _rc is not None and _rc.get("proceeds") is not None and slice_tokens > 0:
        # crash replay: the account already holds this slice's credit; use the RECEIPT, never a re-derived price
        if not _receipt_qty_ok(_rc, slice_tokens):
            pos["pending_exit"] = {"leg": _skey, "why": "receipt_qty_mismatch", "ts": time.time()}
            return 0.0
        gross = float(_rc["proceeds"]); gas_out = float(_rc.get("fee") or 0.0)
        filled_exit = gross / slice_tokens
        _skind = _rc.get("price_kind") or (_pk.UNKNOWN if _pk is not None else None)
        if not _tranche_credit(pos, _skey, slice_tokens, gross, gas_out, _skind):   # idempotent; records the lane tally
            return 0.0
    else:
        try:
            slice_usd = slice_tokens * exit_price
            filled_exit = _fills.honest_exit(
                exit_price, slice_usd,
                pos.get("last_liquidity")
                or pos["entry_snapshot"].get("liquidity") or None)
        except Exception:
            filled_exit = exit_price
        gross = slice_tokens * filled_exit
        gas_out = _costs.tx_cost_usd(pos["chain"], gross, venue=VENUE)
        if not _tranche_credit(pos, _skey, slice_tokens, gross, gas_out, _skind):
            return 0.0   # account refused: slice stays pending with the diagnostic
    net = gross - gas_out
    tw["done"] += 1
    if _pk is not None:
        _pks = set(tw.get("price_kinds") or [])
        _pks.add(_skind)
        tw["price_kinds"] = sorted(_pks)
    tw["sold_tokens"] = round(tw["sold_tokens"] + slice_tokens, 6)
    tw["sold_usd"] = round(tw["sold_usd"] + net, 6)
    tw["last_exit_price"] = filled_exit
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


_WD0 = "write_down_zero"


def _rebuild_tally_from_receipts(pos):
    """A position still marked MIGRATING (marker + sold legs, some legs credited to the account, tally possibly lagging
    because the process died or the migration could not resume) must never reach the final close with the lane tally
    out of step with the account: the close credits `realized - tally`, so a credited leg missing from the tally would be
    credited twice. Rebuild the per-leg tally from the ACCOUNT's own receipts (ground truth). Returns False when the
    receipts cannot be read (the caller defers the close: fail closed, retried next poll)."""
    try:
        _pa = _paper_account()
        if _pa is None or not pos.get("pa_pos_id"):
            return True
        mint, pid = pos["mint"], pos["pa_pos_id"]
        legs = [(lk, lv) for lk, lv in pos["legs"].items()
                if lv.get("done") and lv.get("exit_price_kind") != "write_down_zero"]
        ops = {lk: "%s:leg:%s:%s:%s" % (LANE, lk, mint, pid) for lk, _ in legs}
        rc = _pa.op_receipts(list(ops.values()))
        if rc.get("_unreadable"):
            return False
        tc = pos.setdefault("tranche_credited", {"net": 0.0, "qty": 0.0})
        seen = tc.setdefault("legs", {})
        for lk, lv in legs:
            r = rc.get(ops[lk])
            if r is None or lk in seen:
                continue
            seen[lk] = {"net": round(float(r["proceeds"]) - float(r.get("fee") or 0.0), 6),
                        "qty": pos["tokens"] * lv["frac"]}
        tc["net"] = round(sum(v["net"] for v in seen.values()), 6)
        tc["qty"] = sum(v["qty"] for v in seen.values())
        return True
    except Exception:
        return False


def _migration_receipts_unreadable(pos):
    """True when a migrating position's per-leg account receipts cannot be read (no side effects beyond the tally rebuild)."""
    try:
        import copy as _cp
        return not _rebuild_tally_from_receipts(_cp.deepcopy(pos))
    except Exception:
        return True


def _close_remaining(pos, exit_price, reason, pkind=None, receipt_kind=None):
    """Close all remaining legs as ONE combined exit (one tx cost) —
    same rule as ceo.py/mirror.py."""
    # Any stop/time/TWAP exit path lands here: first finish lane legs whose account credit is already durable
    # (crash window), so a credited leg is never sold a second time inside the combined remainder.
    _migrate_legacy_tranches(pos)
    if receipt_kind is not None and not pkind:
        pkind = receipt_kind   # reconcile of a credited TWAP remainder: the account RECEIPT's kind (never re-derived from the reason string)
    _only_legs = None
    _rc_ok = _reconcile_credited_legs(pos)
    if _rc_ok is False and _RECON_WHY.get((pos["mint"], pos.get("pa_pos_id"))) == "unreadable" and not _no_unrecorded_sale_proof(pos):
        _part = _partition_open_legs(pos)
        if _part and _part[0]:
            # leg-scoped stop: sell ONLY the legs the fill journal proves were never sold; the uncertain legs stay open and
            # visible (pending_exit below) until the account journal is readable and reconciles them.
            _only_legs = set(_part[0])
            _rc_ok = True
            _pend_unc = {"leg": "uncertain", "why": "applied_credit_unreconciled", "legs": _part[1], "ts": time.time()}
    elif _rc_ok is False and _RECON_WHY.get((pos["mint"], pos.get("pa_pos_id"))) == "unreadable":
        _rc_ok = True   # proof holds: nothing unrecorded
    if _rc_ok is False:
        _spec = any(n.get("what") == "receipt_fill_money_mismatch" and n.get("key") == "twap_remainder" for n in (pos.get("recon_notes") or []))
        pos["pending_exit"] = {"leg": "remaining", "why": ("receipt_fill_money_mismatch" if _spec else "applied_credit_unreconciled"), "ts": time.time()}
        return 0.0   # structural refusal: a combined sale could sell an already-credited leg again
    if pos.get("twap") is not None:
        # TWAP positions never touch the ladder legs: close ONLY
        # the tokens the slices have not sold yet, as one exit.
        # (Using the legs here would re-sell already-sold tokens.)
        tw = pos["twap"]
        remaining = _twap_remaining_tokens(pos)
        if remaining <= 0:
            return 0.0
        _tsr = None
        if (pos.get("execution") or {}).get("source") == "jupiter_quote_sim":
            _tsr = _sim_twap_sale(pos, "twap_remainder", remaining, reason, pkind if pkind else None, receipt_kind)
            if _tsr is None:
                return 0.0
        if _tsr is not None and _tsr != "writedown":
            _fill, _cap, _src, reason, _kind, _ots, _rp = _tsr
            pos.pop("pending_exit", None)
            _st = _sim_settle(pos, "twap_remainder", remaining, _fill, replayed=_rp)
            if _st is None:
                return 0.0
            if _st["receipt"]:
                _kind = _st["kind"] or (_pk.UNKNOWN if _pk is not None else None)
            net = _st["gross"] - _st["fee"]
            if not _credit_st(pos, "twap_remainder", remaining, _st, _st["kind"] if _st["receipt"] else _kind):
                return 0.0
            tw["remainder"] = {"tokens": round(remaining, 6), "exit_price": _fill["price_usd"],
                               "realized_usd": round(net, 6), "exit_reason": reason,
                               "exit_price_kind": _kind}
            if _pk is not None:
                _pks = set(tw.get("price_kinds") or [])
                _pks.add(_kind)
                tw["price_kinds"] = sorted(_pks)
            pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
            return net
        _rrc = _tranche_receipt(pos, "twap_remainder")
        if _rrc is not None and _rrc.get("proceeds") is not None:
            # crash replay from the account's receipt (original kind; missing -> unknown, never upgraded)
            if not _receipt_qty_ok(_rrc, remaining):
                pos["pending_exit"] = {"leg": "twap_remainder", "why": "receipt_qty_mismatch", "ts": time.time()}
                return 0.0
            gross = float(_rrc["proceeds"]); gas_out = float(_rrc.get("fee") or 0.0)
            filled_exit = gross / remaining
            _kind = _rrc.get("price_kind") or (_pk.UNKNOWN if _pk is not None else None)
            if not _tranche_credit(pos, "twap_remainder", remaining, gross, gas_out, _kind):
                return 0.0
        else:
            try:
                rem_usd = remaining * exit_price
                filled_exit = _fills.honest_exit(
                    exit_price, rem_usd,
                    pos.get("last_liquidity")
                    or pos["entry_snapshot"].get("liquidity") or None)
            except Exception:
                filled_exit = exit_price
            gross = remaining * filled_exit
            _kind = _pkind(reason, pkind)
            # A write-down is NOT a sale (design v4 Stage B): no fee is
            # booked on units nobody bought.
            gas_out = (0.0 if _kind == "write_down_zero"
                       else _costs.tx_cost_usd(pos["chain"], gross,
                                               venue=VENUE))
            if not _tranche_credit(pos, "twap_remainder", remaining, gross, gas_out, _kind):
                return 0.0   # account refused: remainder stays pending with the diagnostic
        net = gross - gas_out
        tw["remainder"] = {"tokens": round(remaining, 6),
                           "exit_price": filled_exit,
                           "realized_usd": round(net, 6),
                           "exit_reason": reason,
                           "exit_price_kind": _kind}
        if _pk is not None:
            _pks = set(tw.get("price_kinds") or [])
            _pks.add(tw["remainder"]["exit_price_kind"])
            tw["price_kinds"] = sorted(_pks)
        pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
        return net
    open_legs = [k for k in ("a", "b", "c") if not pos["legs"][k]["done"]]
    if _only_legs is not None:
        open_legs = [k for k in open_legs if k in _only_legs]
    if not open_legs:
        return 0.0
    total_tokens = sum(pos["tokens"] * pos["legs"][k]["frac"]
                       for k in open_legs)
    _sim = (pos.get("execution") or {}).get("source") == "jupiter_quote_sim"
    _cjf = None
    if _sim and pos.get("tranche_credit") and pos.get("pa_pos_id"):
        _jfs = _journaled_fills(pos)
        if _jfs is None:
            pos["pending_exit"] = {"leg": "remaining", "why": "fill_journal_unreadable", "ts": time.time()}
            return 0.0
        if _jfs:
            _open_all = {k for k in ("a", "b", "c") if not pos["legs"][k]["done"]}
            _comb = {k: v for k, v in _jfs.items() if k.startswith("remaining:") and set(k[10:]) & _open_all}
            _own = [k for k in open_legs if k in _jfs]
            _conf = [k for k, v in _comb.items() if set(k[10:]) != set(open_legs) or [o for o in _own if o in set(k[10:])]]
            if _conf or len(_comb) > 1:
                # the ORIGINAL combined sale covered a different group than what is open now (or overlaps a per-leg fill):
                # never regenerate a key and sell the same units again, never guess which group is credited: explicit conflict.
                pos["pending_exit"] = {"leg": "remaining", "why": "combined_fill_group_conflict",
                                       "covered": sorted(_comb), "own": sorted(_own), "ts": time.time()}
                return 0.0
            if _comb:
                _cjf = (list(_comb)[0], list(_comb.values())[0])
            elif _own:
                # per-leg fills exist for open legs: settle them from their ORIGINAL fills (stable per-leg op id credit),
                # never inside a new "remaining:" sale; sell only the legs that have no fill.
                _pre_net = 0.0
                for k in _own:
                    _pre_net += _close_leg(pos, k, exit_price, reason, pkind, receipt_kind=receipt_kind)
                open_legs = [k for k in open_legs if k not in _jfs and not pos["legs"][k]["done"]]
                if not open_legs:
                    return _pre_net
                total_tokens = sum(pos["tokens"] * pos["legs"][k]["frac"] for k in open_legs)
    if _sim and (_pkind(reason, pkind) != "write_down_zero" or _cjf is not None):
        _rkey = _cjf[0] if _cjf is not None else "remaining:%s" % "".join(open_legs)
        _ots = None
        if _cjf is not None:
            reason, pkind, _ots = _replay_identity(_cjf[1], reason, pkind, receipt_kind)
        elif receipt_kind is not None and not pkind:
            pkind = receipt_kind
        _r = _demo_sell(pos, total_tokens, _rkey, meta={"reason": reason, "kind": _pkind(reason, pkind), "legs": list(open_legs), "ts": time.time()})
        if _r is None:
            return 0.0
        _fill, _cap, _src = _r
        pos.pop("pending_exit", None)
        if _only_legs is not None:
            pos["pending_exit"] = _pend_unc
        _kind = _pkind(reason, pkind)
        filled_exit, gross, gas_out = _fill["price_usd"], _fill["gross_usd"], _fill["fee_usd"]
        net = gross - gas_out
        for key in open_legs:
            leg = pos["legs"][key]
            share = (pos["tokens"] * leg["frac"]) / total_tokens if total_tokens else 0
            leg.update(done=True, exit_price=filled_exit, realized_usd=round(net * share, 6),
                       exit_reason=reason, exit_price_kind=_kind, **({"exit_ts": _ots} if _ots else {}),
                       exit_execution={"source": _fill["execution_source"], "signature": _fill["signature"],
                                       "synthetic_signature": True, "effective_impact_cap_pct": _cap,
                                       "impact_cap_source": _src, "provenance": _fill["provenance"]})
        pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
        return net
    try:
        leg_usd = total_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd,
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = total_tokens * filled_exit
    _kind = _pkind(reason, pkind)
    # A write-down is NOT a sale (design v4 Stage B): no fee is
    # booked on units nobody bought.
    gas_out = (0.0 if _kind == "write_down_zero"
               else _costs.tx_cost_usd(pos["chain"], gross,
                                       venue=VENUE))  # ONE tx total
    net = gross - gas_out
    for key in open_legs:
        leg = pos["legs"][key]
        share = (pos["tokens"] * leg["frac"]) / total_tokens \
            if total_tokens else 0
        leg.update(done=True, exit_price=filled_exit,
                   realized_usd=round(net * share, 6), exit_reason=reason,
                   exit_price_kind=_kind)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _peak_fields(pos):
    """Peak price/multiple for the close record, from RECORDED prices
    only — never fabricated. Identical semantics to ceo.py: tracked
    positions report their running poll-observed peak
    (peak_source="tracked"); positions opened before tracking existed
    report the max of their recorded prices as an honest lower bound
    (peak_source="legacy_lower_bound"); no recorded price -> nulls
    with peak_unknown=True."""
    entry = float(pos.get("entry_price") or 0)
    candidates = []
    tracked = pos.get("peak_price")
    if tracked:
        candidates.append(float(tracked))
    legs = pos.get("legs") or {}
    lc = legs.get("c") or {}
    if lc.get("peak"):
        candidates.append(float(lc["peak"]))
    for v in legs.values():
        if v.get("done") and v.get("exit_price"):
            candidates.append(float(v["exit_price"]))
    if entry <= 0 or not candidates:
        return {"peak_price": None, "peak_mult": None,
                "peak_unknown": True, "peak_ts": None}
    peak_price = max(candidates)
    return {"peak_price": peak_price,
            "peak_mult": round(peak_price / entry, 4),
            # F3: WHEN the peak happened (was discarded before P1 —
            # peak-to-close lag is now measurable from closes).
            "peak_ts": pos.get("peak_ts"),
            "peak_source": ("legacy_lower_bound"
                            if pos.get("peak_partial") else "tracked")}


def _verdict_visibility(closes):
    """Additive Stage-C visibility (owner-approved 2026-11-22): the
    registered halt math is UNCHANGED - this only attaches the
    sample's price-kind breakdown and not-qualified status to the
    verdict record. Defensive: a visibility failure never breaks a
    verdict."""
    try:
        try:
            from daemon.launch_scanner import exit_provenance as _pk
        except ImportError:
            import exit_provenance as _pk  # type: ignore
        return _pk.verdict_visibility(closes)
    except Exception:
        return {}

def _close_basis(c):
    """Frozen-verdict basis of one close: True (compatible), False
    (incompatible) or None (UNKNOWN). The stamp wins; otherwise the
    recorded notional_usd is compared with the frozen NOTIONAL_USD; with
    neither, the basis is unknown and cannot support SURVIVED."""
    st = c.get("frozen_verdict_notional_compatible")
    try:
        _n = c.get("notional_usd")
        if _n is not None and st in (True, False):
            _act = abs(float(_n) - float(NOTIONAL_USD)) < 1e-9
            if _act != st:
                return False  # stamp contradicts the recorded notional: invalid
    except (TypeError, ValueError):
        return None
    if st is True or st is False:
        return st
    try:
        n = c.get("notional_usd")
        if n is None:
            n = c.get("entry_notional_usd")  # legacy grad record
        if n is None:
            return None
        return abs(float(n) - float(NOTIONAL_USD)) < 1e-9
    except (TypeError, ValueError):
        return None



def _verdict_check(d):
    """The PRE-REGISTERED verdict, evaluated in-ledger at the moment
    the sample completes. Mutates d (lock held by caller) and returns
    True when the lane must halt. Runs exactly once: a recorded
    verdict is never re-evaluated."""
    if d.get("verdict"):
        return False
    closes = d.get("closes") or []
    if len(closes) < VERDICT_SAMPLE_CLOSES:
        return False
    exp = sum(float(c.get("pnl_usd") or 0) for c in closes) / len(closes)
    halt = exp <= 0
    # FROZEN at NOTIONAL_USD: a close at any other notional (configured
    # size OR regime-scaled stake) cannot yield a QUALIFIED verdict or a
    # SURVIVED claim. Losses stay in the sample; the safety halt on
    # non-positive expectancy is unchanged. Basis per close: see _close_basis (stamp,
    # recorded notional or entry_notional_usd; missing both = unknown).
    bases = [_close_basis(c) for c in closes]
    qualified = all(b is True for b in bases)
    d["verdict"] = {
        "ts": time.time(),
        "n_closes": len(closes),
        "expectancy": round(exp, 6),
        "net_usd": round(sum(float(c.get("pnl_usd") or 0)
                             for c in closes), 4),
        "action": (("HALT — sample expectancy <= 0" if halt else
                    "SURVIVED — sample expectancy > 0") if qualified else
                   ("HALT — sample expectancy <= 0 (UNQUALIFIED: closes at a "
                    "non-frozen notional)" if halt else
                    "UNQUALIFIED — closes at a non-frozen notional; "
                    "no SURVIVED claim")),
        "qualified": qualified,
        "basis_valid": qualified,
        "n_incompatible_basis": sum(1 for b in bases if b is False),
        "n_unknown_basis": sum(1 for b in bases if b is None),
        "sample_closes": VERDICT_SAMPLE_CLOSES,
    }
    d["verdict"].update(_verdict_visibility(closes))
    logger.warning("grad VERDICT at %d closes: expectancy %.4f -> %s",
                   len(closes), exp, d["verdict"]["action"])
    return halt


def _all_exits_executed(pos):
    """True when every lane unit has been sold or written down on the lane side (ladder legs all done, or a TWAP position
    with no remaining tokens): only then is a deferred close a pure SETTLEMENT wait."""
    try:
        if pos.get("twap") is not None:
            return _twap_remaining_tokens(pos) <= 0
        return all(v.get("done") for v in pos["legs"].values())
    except Exception:
        return False


def _account_residue(mint, pos_id, pos):
    """(why, residue_qty) for a lane-flat marked position: (None, 0.0) when the account holds no units for this pos_id (flat) or the
    residue is within technical rounding (max(1e-9, 1e-6 * tokens)); ("account_remaining_qty_mismatch", qty) for a residue;
    ("account_snapshot_unreadable", None) when the account view cannot be read (unknown is never proof of flat)."""
    _pa = _paper_account()
    if _pa is None:
        return None, 0.0
    try:
        snap = _pa.snapshot()
    except Exception:
        snap = None
    if snap is None or snap.get("degraded"):
        return "account_snapshot_unreadable", None   # a degraded (untrusted WAL) view may omit positions: unknown is never flat
    q = sum(float(p.get("qty") or 0.0) for p in ((snap.get("lanes") or {}).get(LANE) or {}).get("positions") or []
            if str(p.get("pos_id")) == str(pos_id))
    if q > max(1e-9, 1e-6 * abs(float(pos.get("tokens") or 0.0))):
        return "account_remaining_qty_mismatch", q
    return None, 0.0


def legit_receipt_qty(pos, key, lane_qty, receipt_qty, receipt_kind):
    """PURE. Which HISTORICAL receipt-qty form a credit matches (design G5; evidence = published 2b26710 `_tranche_credit` passing the lane leg
    qty, plus the migration's stored dust leg): "L1" receipt qty == lane leg qty (exact within rounding 1e-9 relative); "L2" == lane qty + stored
    dust on pos["tranche_dust_leg"] (only when tranche_dust_leg AND tranche_dust_qty are persisted: absence is unknown, never zero dust);
    "L3" qty None with kind None (older credit journaled without qty); None = matches no historical formula (unexplained). NO tolerance widening."""
    if receipt_qty is None:
        return "L3" if receipt_kind is None else None
    try:
        rq, lq = float(receipt_qty), float(lane_qty)
    except Exception:
        return None
    tol = max(1e-9, 1e-9 * abs(lq))
    if abs(rq - lq) <= tol:
        return "L1"
    if pos.get("tranche_dust_leg") == key and pos.get("tranche_dust_qty") is not None:
        try:
            if abs(rq - (lq + float(pos["tranche_dust_qty"]))) <= tol:
                return "L2"
        except Exception:
            return None
    return None


def diagnose_position(mint):
    """READ-ONLY diagnostics for a SIM position's account/lane qty divergence (design G1-G7 evidence). Writes nothing of its own (it reads the lane
    ledger, the fill journal, the account journal and ledger op, and one snapshot() WITHOUT marks, which records no baseline); byte immutability of
    the lane ledger, account ledger+journal and fill journal is asserted by test P7z2. Returns {mint, gates, keys, extra_fills, residue, scale,
    verdict}. Verdicts: "consistent" ONLY when every core gate (G1-G6) was evaluated and passed, the residue is nil, the position is a ladder
    (not TWAP), and no journaled fill lacks a done leg; "unsupported:twap" (TWAP keys are not examined, so never a pass);
    "pending_crash_window:<keys>" (a journaled fill with no done leg: the crash-window state, not corruption and not consistent);
    "quarantine:<gate>:<why>"; "evidence_missing:<field>" (an input is absent: never defaulted); or
    "explained_residue:stored_dust" when the account residue equals the persisted dust record exactly (label only; unexplained residue is
    "quarantine:G5:unexplained_residue"). NOT a repair and NOT an authority to apply one."""
    out = {"mint": mint, "gates": {}, "keys": [], "extra_fills": [], "residue": None, "scale": None, "verdict": None, "unavailable_gates": []}   # unavailable_gates is always present; it is only POPULATED once G3/G6 were evaluated (early verdicts: [])
    def gate(g, ok, why=None, unavailable=False):
        out["gates"][g] = {"ok": bool(ok), "why": why, "unavailable": bool(unavailable) and not ok}
        return ok
    try:
        pos = open_positions().get(mint)
    except Exception:
        pos = None
    if not pos:
        out["verdict"] = "evidence_missing:position"; return out
    pid = pos.get("pa_pos_id")
    if not gate("G1", _is_sim(pos), "not_sim"):
        out["verdict"] = "quarantine:G1:not_sim"; return out
    if pos.get("twap") is not None:
        out["verdict"] = "unsupported:twap"; return out      # slice/remainder keys are not examined: explicit, never "consistent"
    _df, _pa = _demo_filler(), _paper_account()
    if _df is None or _pa is None or not pid or not pos.get("tranche_credit"):
        miss = "fill_journal" if _df is None else ("account" if _pa is None else ("pa_pos_id" if not pid else "tranche_marker"))
        gate("G1", False, "evidence_missing:" + miss); out["verdict"] = "evidence_missing:" + miss; return out
    if not (pos.get("execution") or {}).get("entry_op"):
        gate("G1", False, "evidence_missing:entry_op"); out["verdict"] = "evidence_missing:entry_op"; return out
    toks = float(pos.get("tokens") or 0.0)
    dbt = None
    try: dbt = _pa.position_debit(pid)
    except Exception: dbt = None
    if not dbt or not (float(dbt.get("qty") or 0.0) > 0) or not (toks > 0):
        out["scale"] = "evidence_missing:scale"
    else:
        r = float(dbt["qty"]) / toks
        out["scale"] = 1.0 if abs(r - 1.0) <= 1e-9 else "evidence_missing:scale"   # scale 1 only when the stored debit EQUALS the lane tokens; otherwise unknown
        out["entry_qty_ratio"] = r
    legs = [(k, v) for k, v in (pos.get("legs") or {}).items() if v.get("done") and v.get("exit_price_kind") != "write_down_zero"]
    ops = ["%s:leg:%s:%s:%s" % (LANE, k, mint, pid) for k, _ in legs]
    # crash window: journaled fills for this position with no done leg (never listed by the done-flag selection above)
    try:
        for k, v in (pos.get("legs") or {}).items():
            if v.get("done"): continue
            fl = _df.get_fill(_fill_key(pos, k)) or _df.get_fill("%s:exit:%s:%s" % (LANE, mint, k))
            if fl: out["extra_fills"].append({"key": k, "why": "fill_without_done", "fill_qty": fl.get("qty_tokens"), "gross": fl.get("gross_usd")})
    except Exception:
        gate("G1", False, "fill_journal_unreadable"); out["verdict"] = "quarantine:G1:fill_journal_unreadable"; return out
    try: rc = _pa.op_receipts(ops)
    except Exception: rc = {"_unreadable": True}
    if rc.get("_unreadable"):
        gate("G3", False, "journal_unreadable"); out["verdict"] = "quarantine:G3:journal_unreadable"; return out
    fsum, g1ok, g2ok, g3ok, g4ok, g5ok = 0.0, True, True, True, True, True
    g3_unavail = False
    for (k, v), op in zip(legs, ops):
        ltok = toks * float(v.get("frac") or 0.0)
        fl = _df.get_fill(_fill_key(pos, k)) or _df.get_fill("%s:exit:%s:%s" % (LANE, mint, k)); r = rc.get(op)   # instance-scoped key, else the legacy unstamped key
        row = {"key": k, "lane_qty": ltok, "fill": None if not fl else {"qty": fl.get("qty_tokens"), "gross": fl.get("gross_usd"), "fee": fl.get("fee_usd")},
               "receipt": None if r is None else {"qty": r.get("qty"), "proceeds": r.get("proceeds"), "fee": r.get("fee"), "kind": r.get("price_kind")}}
        if not fl: g1ok = False; row["why"] = "no_journaled_fill"
        else:
            fq = float(fl.get("qty_tokens") or 0.0); fsum += fq
            row["fill_qty_vs_lane"] = fq - ltok
            if abs(fq - ltok) > max(1e-9, 1e-6 * abs(ltok)): g2ok = False; row["why"] = "fill_qty_differs_from_lane"
        if r is None: g3ok = False; row["why"] = row.get("why") or "no_receipt"
        else:
            row["form"] = legit_receipt_qty(pos, k, ltok, r.get("qty"), r.get("price_kind"))
            if row["form"] is None: g5ok = False; row["why"] = row.get("why") or "receipt_qty_no_historical_signature"
            # G3: the account LEDGER op (what credit's conflict check compares) must equal the JOURNAL credit event
            try: lop, lerr = _pa.operation_receipt(op)
            except Exception as e: lop, lerr = None, "exception:%r" % (e,)
            row["ledger_op"] = None if lop is None else {x: lop.get(x) for x in ("proceeds", "fee", "qty", "price_kind")}
            if lerr and "no exact instance contract" in str(lerr):
                g3_unavail = True; row["why"] = row.get("why") or "ledger_op_no_instance_contract"   # the lane debits through legacy try_debit: its ops carry no params to compare
            elif lerr or lop is None:
                g3ok = False; row["why"] = row.get("why") or ("ledger_op_unreadable:%s" % lerr if lerr else "ledger_op_missing")
            elif any(lop.get(x) != r.get(x) for x in ("proceeds", "fee", "qty", "price_kind")):
                g3ok = False; row["why"] = row.get("why") or "ledger_op_differs_from_journal"
            if fl:
                pr, fe = float(r.get("proceeds") or 0.0), float(r.get("fee") or 0.0)
                if abs(pr - float(fl.get("gross_usd") or 0.0)) > max(1e-6, 1e-4 * abs(pr)) or abs(fe - float(fl.get("fee_usd") or 0.0)) > max(1e-6, 1e-4 * abs(fe)):
                    g4ok = False; row["why"] = "receipt_money_differs_from_fill"
        out["keys"].append(row)
    gate("G1", g1ok, None if g1ok else "fill_missing")
    gate("G3", g3ok and not g3_unavail, None if (g3ok and not g3_unavail) else ("receipt_or_ledger_op_fault" if not g3ok else "ledger_op_no_instance_contract"), unavailable=(g3ok and g3_unavail))
    gate("G4", g4ok, None if g4ok else "money_differs"); gate("G5", g5ok, None if g5ok else "no_historical_signature")
    all_done = bool(pos.get("legs")) and all(v.get("done") for v in pos["legs"].values())
    g2ok = g2ok and ((not all_done) or abs(fsum - toks) <= max(1e-9, 1e-6 * toks))
    gate("G2", g2ok, None if g2ok else "fill_qty_does_not_prove_flat")
    # G6 identity: exactly one open account instance for this mint in the lane, its pos_id == the lane's, instance contract, no unresolved entry
    rwhy, rq = _account_residue(mint, pid, pos); out["residue"] = {"why": rwhy, "qty": rq}
    try: snap = _pa.snapshot()
    except Exception: snap = None
    g6why = None; g6_unavail = False
    if snap is None or snap.get("degraded"): g6why = "account_snapshot_unreadable"
    else:
        lanepos = [x for x in (((snap.get("lanes") or {}).get(LANE) or {}).get("positions") or []) if x.get("mint") == mint]
        mine = [x for x in lanepos if str(x.get("pos_id")) == str(pid)]
        unres = [x for x in (snap.get("unresolved") or []) if str(x.get("pos_id")) == str(pid)]
        if len(lanepos) > 1: g6why = "second_open_instance_for_mint"
        elif lanepos and not mine: g6why = "account_open_position_has_other_pos_id"
        elif unres: g6why = "unresolved_register_entry"
        elif mine and not mine[0].get("instance"): g6why = "account_position_not_instance"; g6_unavail = True
        out["g6_evidence"] = {"open_instances": len(lanepos), "own_open": len(mine), "unresolved_entries": len(unres)}
    gate("G6", g6why is None, g6why, unavailable=g6_unavail)
    for g in ("G1", "G2", "G3", "G4", "G5", "G6"):
        if not out["gates"][g]["ok"] and not out["gates"][g]["unavailable"]:
            out["verdict"] = "quarantine:%s:%s" % (g, out["gates"][g]["why"]); return out
    out["unavailable_gates"] = [g for g in ("G3", "G6") if not out["gates"][g]["ok"] and out["gates"][g]["unavailable"]]     # populated once the gates were evaluated (verdicts reached after the quarantine loop); earlier returns carry []
    if out["extra_fills"]:
        out["verdict"] = "pending_crash_window:" + ",".join(x["key"] for x in out["extra_fills"]); return out
    if out["scale"] != 1.0: out["verdict"] = "evidence_missing:scale"; return out
    for g in out["unavailable_gates"]:     # evidence the lane never writes (non-instance account ops/positions): the gate CANNOT be evaluated, so no positive verdict
        out["verdict"] = "evidence_unavailable:%s:%s" % (g, out["gates"][g]["why"]); return out
    # expected account units = the lane's still-open legs (all legs done: 0). Any other quantity is a residue; it is labelled "explained" ONLY
    # when the delta equals the persisted dust record exactly, otherwise "unexplained" (no other class is invented).
    expect = toks * sum(float(v.get("frac") or 0.0) for v in (pos.get("legs") or {}).values() if not v.get("done"))
    acct_q = float(rq or 0.0) if rwhy == "account_remaining_qty_mismatch" else 0.0
    delta = acct_q - expect
    tol = max(1e-9, 1e-6 * toks)
    out["residue"].update(expected_qty=expect, account_qty=acct_q, delta=delta)
    if abs(delta) <= tol:
        out["residue"]["label"] = "none"; out["verdict"] = "consistent"
    else:
        dq = pos.get("tranche_dust_qty")
        expl = dq is not None and abs(delta - float(dq)) <= tol
        out["residue"]["label"] = "explained:stored_dust" if expl else "unexplained"
        out["verdict"] = "explained_residue:stored_dust" if expl else "quarantine:G5:unexplained_residue"
    return out


def _finalize(d, mint, pos, closed_reason):
    """Move an open position to closes, settle cash, record the lane
    trade, evaluate the pre-registered verdict. Must be called with
    _ledger_lock held; writes via _write."""
    _recover_close_intents()
    # SETTLEMENT PENDING (crash-recovery / unreadable-journal state). A half-migrated position whose per-leg account
    # receipts cannot be read cannot be SETTLED: the close credits `realized - tally` and the tally is unknowable, so
    # booking either way would invent cash (double credit or lost cash). The ECONOMIC exit has already run (legs sold,
    # stops never frozen); only the settlement waits. Nothing below is touched before this check (no bankroll, no tally,
    # no credit, no close record): the position stays OPEN in the lane books in a visible settlement_pending state, is
    # persisted, and the next poll retries once the journal is readable. Returns None (no close record yet).
    if pos.get("tranche_credit") and pos.get("tranche_migrating") and not _rebuild_tally_from_receipts(pos):
        if not _all_exits_executed(pos):
            # The economic exit did NOT run (e.g. _close_remaining refused because the account journal could not prove
            # which legs were already credited, so a combined sale could sell them twice). That is NOT a pending
            # settlement: label it truthfully so it is never mistaken for "sold, only the settlement waits".
            pos["pending_exit"] = {"leg": "remaining", "why": "exit_not_executed_journal_unreadable", "ts": time.time()}
            logger.error("grad: %s exit NOT executed (%s): account journal unreadable, unsold legs remain; no cash booked", mint[:12], closed_reason)
            _write(d)
            return None
        _sp = pos.get("settlement_pending") or {}
        pos["settlement_pending"] = {"why": "migration_receipts_unreadable", "closed_reason": closed_reason,
                                     "since": _sp.get("since") or time.time(), "last_try": time.time(),
                                     "tries": int(_sp.get("tries") or 0) + 1}
        pos["pending_exit"] = {"leg": "settlement", "why": "migration_receipts_unreadable", "ts": time.time()}
        logger.error("grad: %s SETTLEMENT PENDING (%s): economic exit done, account receipts unreadable; position kept "
                     "open, no cash booked, retry next poll", mint[:12], closed_reason)
        _write(d)
        return None
    pos.pop("settlement_pending", None)
    realized = round(float(pos["realized_usd"]), 6)
    # CLOSE DENOMINATION (2026-10-27 fix): a close is denominated
    # at the stake the position actually opened with, never at the
    # lane's CURRENT notional. When a lane's stake changes while
    # positions are still open, the old code recorded those closes
    # against the new notional (a $1 entry closed after a switch to
    # $25 booked a $24 phantom loss on a position that only ever
    # risked $1). The position's stored entry notional is
    # authoritative; fall back to the current notional only for
    # legacy positions that predate the field.
    entry_notional = float(pos.get("notional_usd") or NOTIONAL_USD)
    pnl = round(realized - entry_notional, 4)
    d["bankroll"] = round(float(d["bankroll"]) + realized, 6)
    # Shared account settle (same flag as the entry gate): the
    # close credits the exact proceeds the lane books. A failure
    # (e.g. a position that predates the flag) is journaled by the
    # account module and never blocks the lane's own settle.
    _close_kinds = [v.get("exit_price_kind")
                    for v in pos["legs"].values() if v.get("done")]
    _close_kinds += list((pos.get("twap") or {}).get("price_kinds")
                         or [])
    _close_kind = (_pk.aggregate(_close_kinds)
                   if _pk is not None else "unknown")
    # Unit split (parent review 2026-11-22): written-down units are
    # NOT sold, so a mixed close must settle the sold units as a
    # sale AND move the written-down units + their cost to the
    # unresolved register - never route the entire close as a sale.
    _WD = "write_down_zero"
    _sold_qty = 0.0
    _wd_qty = 0.0
    _sold_kinds = []
    _split_complete = True
    if pos.get("twap") is not None:
        _sold_qty += float((pos["twap"] or {}).get("sold_tokens")
                           or 0)
        _rem = (pos["twap"] or {}).get("remainder") or {}
        if _rem.get("exit_price_kind") == _WD:
            _wd_qty += float(_rem.get("tokens") or 0)
        _sold_kinds += [k for k in ((pos["twap"] or {})
                                    .get("price_kinds") or [])
                        if k != _WD]
    else:
        for _lk, _lv in pos["legs"].items():
            if not _lv.get("done"):
                continue
            if "frac" not in _lv:
                # Legacy/minimal position without per-leg fracs:
                # units cannot be attributed, so the split is
                # INCOMPLETE and the close falls back to aggregate
                # routing below (an aggregate write-down is still
                # booked as a write-down, never a sale).
                _split_complete = False
                break
            _lq = pos["tokens"] * _lv["frac"]
            if _lv.get("exit_price_kind") == _WD:
                _wd_qty += _lq
            else:
                _sold_qty += _lq
                _sold_kinds.append(_lv.get("exit_price_kind"))
    # Legacy/minimal positions without per-leg fracs cannot split
    # units. CONSERVATION RULE (parent review 2026-11-22): when
    # ANY done leg is a write-down and units cannot be attributed,
    # the WHOLE position goes to the unresolved register - units
    # and full original cost are conserved there, no sale is
    # invented for unquotable units, and no cash is claimed while
    # the sold/units split is unknown (the register's later
    # resolution recognizes proceeds with evidence; until then the
    # account under-credits versus the lane, the direction that
    # can only hurt, never help). With NO write-down leg every
    # unit was priced and sold, so the aggregate credit conserves
    # exactly: all units settled, realized proceeds credited,
    # quarantined by the aggregate kind.
    _agg_wd = False
    if not _split_complete:
        _saw_wd = any(
            v.get("exit_price_kind") == _WD
            for v in pos["legs"].values() if v.get("done"))
        _sold_qty = 0.0
        _wd_qty = 0.0
        _sold_kinds = []
        _agg_wd = _saw_wd or (_close_kind == _WD)
    _sold_kind = (_pk.aggregate(_sold_kinds)
                  if (_pk is not None and _sold_kinds) else None)
    # Legs already credited per tranche are NOT credited again: the close credits only the remainder
    # (lane realized/bankroll/pnl keep the full total). Legacy positions have no marker: nothing credited.
    if pos.get("tranche_credit") and pos.get("tranche_migrating"):
        # Half-migrated position reaching its final close (the migration could not resume): the lane tally may lag the
        # account, and the close credits `realized - tally`, so rebuild the tally from the account's own receipts. If the
        # journal cannot be read, assume EVERY sold leg was credited: the close then under-credits (loud) and can never
        # credit an already-credited leg twice.
        if not _rebuild_tally_from_receipts(pos):
            # Never book unknown credits as settled: the poll loop defers a migrating position whose receipts are
            # unreadable BEFORE any close path, so reaching here means a caller bypassed that guard. Refuse loudly;
            # nothing is written (no tally, no credit, no close).
            raise RuntimeError("grad: %s final close refused: legacy migration unresolved and account receipts "
                               "unreadable (deferred, no credit booked)" % mint[:12])
    _tc = pos.get("tranche_credited") if pos.get("tranche_credit") else None
    _cr_net = float((_tc or {}).get("net") or 0.0)
    _cr_qty = float((_tc or {}).get("qty") or 0.0)
    _pa = _paper_account()
    if _pa is not None:
        # op_ids are stable per position instance so a retried
        # close after an uncertain response cannot double-settle.
        _op = "%s:close:%s:%s" % (LANE, mint,
                                  pos.get("pa_pos_id") or "legacy")
        _pos_id = pos.get("pa_pos_id")
        # Stage C: plan the settle as durable intent payload BEFORE
        # the first account op, so a kill between the account write
        # and the lane-ledger write is recoverable on restart.
        if _agg_wd or (_sold_qty <= 0 and _wd_qty > 0):
            # Pure write-down: no sale happened, so the units +
            # original cost move to the unresolved register
            # (design v4 Stage B), never a zero/negative credit.
            _plan = [{"kind": "write_down", "op_id": _op,
                      "mint": mint, "pos_id": _pos_id,
                      "reason": closed_reason}]
        elif _wd_qty > 0:
            # Mixed close: priced portion settles as a sale of the
            # SOLD units; the unquotable remainder moves to the
            # unresolved register with its original cost. The
            # write-down's qty=None moves ALL remaining account
            # units: the close is final, and grad leg fractions
            # can leave dust the per-leg qty would strand.
            _plan = [{"kind": "credit", "op_id": _op, "mint": mint,
                      "amount": round(realized - _cr_net, 6), "qty": max(_sold_qty - _cr_qty, 0.0) + (_dust(pos) if pos.get("tranche_dust_leg") and pos["tranche_dust_leg"] not in ((_tc or {}).get("legs") or {}) else 0.0),
                      "price_kind": _sold_kind, "pos_id": _pos_id},
                     {"kind": "write_down", "op_id": _op + ":wd",
                      "mint": mint, "pos_id": _pos_id,
                      "reason": closed_reason}]
            _uk = _uncredited_kinds(pos)
            if _uk and _pk is not None:
                _plan[0]["price_kind"] = _pk.aggregate(_uk)
            if _sold_legs_all_credited(pos):
                # every sale was credited per tranche: only the write-down is left (a zero-qty credit would be
                # refused and, breaking the loop, would also skip the write-down)
                _plan = _plan[1:]
        else:
            # Full close (qty=None settles all remaining units).
            # A non-current aggregate kind routes the WHOLE credit
            # to the quarantined estimated bucket.
            _rem = round(realized - _cr_net, 6)
            _ukinds = _uncredited_kinds(pos)
            _rem_kind = (_pk.aggregate(_ukinds) if (_pk is not None and _ukinds) else _close_kind)
            if _sold_legs_all_credited(pos):
                # Every leg was already credited per tranche (leg C included): nothing is left to settle,
                # and a zero-amount credit would be refused and leave a close intent open forever.
                _plan = []
            else:
                _plan = [{"kind": "credit", "op_id": _op, "mint": mint,
                          "amount": _rem, "qty": None,
                          "price_kind": _rem_kind,
                          "pos_id": _pos_id}]
        if not _plan and pos.get("tranche_credit") and _pos_id:
            # FLAT RECONCILIATION: every sold leg was credited per tranche, so the lane is flat. The account must be flat too: read its
            # ACTUAL remaining units for this pos_id. A residue (an earlier receipt qty smaller than the lane's) or an unreadable account
            # leaves the position open in a visible settlement_pending state (the economic exit already ran; nothing is sold again and no
            # cash is invented). Never silently closed with units stranded in the account.
            _res, _resq = _account_residue(mint, _pos_id, pos)
            if _res is not None:
                _sp = pos.get("settlement_pending") or {}
                pos["settlement_pending"] = {"why": _res, "closed_reason": closed_reason, "residue_qty": _resq,
                                             "since": _sp.get("since") or time.time(), "last_try": time.time(),
                                             "tries": int(_sp.get("tries") or 0) + 1}
                pos["pending_exit"] = {"leg": "settlement", "why": _res, "residue_qty": _resq, "ts": time.time()}
                _warn_throttled(("settle_pending", mint, _res), "grad: %s SETTLEMENT PENDING (%s): %s residue_qty=%s; position kept open, no cash booked (see recon_health)", mint[:12], closed_reason, _res, _resq)
                _write(d)
                return None
        _ci = _close_intents() if _plan else None
        _iid = None
        if _ci is not None:
            _iid = _ci.begin(LANE, mint, _pos_id, _plan,
                             {"realized_usd": realized,
                              "entry_notional_usd": entry_notional,
                              "closed_reason": closed_reason,
                              "close_ts": time.time()})
        _ok, _why = True, ""
        for _sop in _plan:
            if _sop["kind"] == "write_down":
                _ok, _why = _pa.write_down(
                    LANE, mint, _sop["op_id"], pos_id=_pos_id,
                    qty=_sop.get("qty"), reason=_sop.get("reason"))
            else:
                _ok, _why = _pa.credit(
                    LANE, mint, _sop["amount"], _sop["op_id"],
                    pos_id=_pos_id, qty=_sop.get("qty"),
                    price_kind=_sop.get("price_kind"))
            if not _ok:
                # Orphan tolerance needs POSITIVE evidence of a
                # pre-account epoch: the account's journal shows
                # the unreasoned absent-position orphan AND no
                # debit/settle for this position ever. A missing
                # open position alone proves nothing (a stale
                # restore also removes debited positions) - those
                # stay open for recovery and freeze loud.
                if _pa.op_outcome(_sop["op_id"]) == "orphan" \
                        and not _pa.pos_ever_existed(
                            pos_id=_pos_id, lane=LANE, mint=mint):
                    _ok, _why = True, ""
                    continue
                break
        if _ok:
            # Account side durable; _write marks the lane side once
            # the ledger persist lands.
            if _ci is not None and _iid is not None:
                _ci.mark_account_applied(LANE, _iid)
                _PENDING_CLOSE_INTENTS.append((_iid, _pos_id, mint))
        else:
            # The intent stays open: recovery re-applies the
            # account side at the next start (the lane close below
            # is still booked, so lane_has_close confirms it).
            logger.warning("grad: shared paper account settle "
                           "for %s failed: %s (journaled, intent "
                           "open for recovery)", mint, _why)
    # Minimum fundable stake is the COLD stake (base x 0.5): the
    # lane can still enter while the bankroll covers it, so the
    # exhausted flag must use the COLD stake, not the base.
    _min_stake = NOTIONAL_USD
    try:
        if _regime is not None:
            _min_stake = _regime.effective_stake(NOTIONAL_USD, "COLD")
    except Exception:
        pass
    if d["bankroll"] < _min_stake:
        d["state"] = "bankroll_exhausted"
    rec = {
        "mint": mint, "symbol": pos["symbol"], "chain": pos["chain"],
        "source": pos["source"], "lane": LANE,
        "entry_ts": pos["entry_ts"], "close_ts": time.time(),
        # P5 latency stamps ride into the close so stage medians
        # survive the position (entered = entry_ts).
        "born_ts": pos.get("born_ts"), "seen_ts": pos.get("seen_ts"),
        "decided_ts": pos.get("decided_ts"),
        "entry_price": pos["entry_price"],
        "realized_usd": realized,
        "realized_mult": round(realized / entry_notional, 4),
        "entry_notional_usd": entry_notional,
        "pnl_usd": pnl,
        "closed_reason": closed_reason,
        # Stage C: the account position this close settles, so the
        # intent journal's lane-write flush and recovery can match
        # lane closes to account ops without re-deriving anything.
        "pa_pos_id": pos.get("pa_pos_id"),
        "legs": {k: {"done": v["done"],
                     "exit_price": v.get("exit_price", 0),
                     "realized_usd": v.get("realized_usd", 0),
                     "exit_reason": v.get("exit_reason", ""),
                     "exit_price_kind": v.get("exit_price_kind")}
                 for k, v in pos["legs"].items()},
        "entry_snapshot": pos["entry_snapshot"],
        "entry_features": pos.get("entry_features", {}),
        # F3/P1 journals ride into the close record: the tail of the
        # per-eval quote journal, the exit-time liquidity, and any
        # graduation/pair transitions observed while held.
        "quote_journal": list(pos.get("quote_journal") or [])[-10:],
        "exit_liquidity": pos.get("last_liquidity"),
        # Jupiter v3 audit (2026-11-17): which source priced the
        # exit that closed this position ("jup-v3", "external",
        # "ds/gt", "reserves:...", "f2-last-real-price",
        # "unquotable", or "" for legacy closes).
        "price_source": pos.get("last_price_source") or "",
        "transitions": list(pos.get("transitions") or []),
    }
    # Frozen-verdict basis: the verdict is pre-registered at NOTIONAL_USD.
    rec["notional_usd"] = entry_notional
    rec["frozen_verdict_notional_compatible"] = abs(
        entry_notional - NOTIONAL_USD) < 1e-9
    if pos.get("twap") is not None:
        # TWAP positions never used the ladder legs: the close
        # record carries the slice journal instead of empty legs
        # (so no leg-A bank is ever claimed for a TWAP close).
        rec["legs"] = {}
        rec["twap"] = dict(pos["twap"])
    rec.update(_peak_fields(pos))
    rec["exit_price_kind"] = _close_kind
    # F2 stamp (2026-10-23): pos["_f2"] is set ONLY by the F2 exit
    # branch, immediately before _close_remaining/_finalize — its
    # presence is the unmistakable marker. Copied into the close
    # record and the verdict-ledger payload so verdict math can
    # include/exclude F2 closes explicitly.
    if pos.get("_f2"):
        rec["f2"] = dict(pos["_f2"])
    # SHADOW STOP (2026-11-20, faster-exits item 1c): the first
    # -30%-within-15-min crossing journaled live by note_quote, if
    # any. Journal-only — carried so scripts/shadow-stop-report can
    # compare the shadow counterfactual against this live close.
    if pos.get("shadow_stop"):
        rec["shadow_stop"] = dict(pos["shadow_stop"])
    # LIMIT MODEL + COVER (advanced build, pieces orig-3 + new-3,
    # 2026-11-21): the journal-only model state at close — the
    # limit-order replay (fills at rung / 0.475x stop / trail),
    # the cover decisions that fired, and the latest cover
    # decision. Carried so the fair limit-vs-live comparison
    # accrues on real closes (see scripts/limit-exits-report).
    if isinstance(pos.get("limit_model"), dict):
        rec["limit_model"] = dict(pos["limit_model"])
    if pos.get("cover_journal"):
        rec["cover_journal"] = list(pos["cover_journal"])
    if isinstance(pos.get("cover_last"), dict):
        rec["cover_last"] = dict(pos["cover_last"])
    # VENDOR CHAIN (advanced build, piece 4, 2026-11-21): the
    # exit-price vendor that answered (keyed by need), plus the
    # select-side vendor stamps already inside entry_features.
    if isinstance(pos.get("vendor_chain"), dict):
        rec["vendor_chain"] = dict(pos["vendor_chain"])
    if pos.get("recon_notes"):
        rec["recon_notes"] = [dict(n) for n in pos["recon_notes"]]
    d["closes"].append(rec)
    d["closes"] = d["closes"][-MAX_CLOSES:]
    del d["positions"][mint]
    had_verdict = bool(d.get("verdict"))
    halt_verdict = _verdict_check(d)
    _write(d)  # lock held by caller
    # Decision memory (2026-11-17): settle the pending decision
    # for this close — idempotent, and a close with no pending
    # record is recorded once from the close record itself.
    # Journal only; never affects the close.
    try:
        if _lessons is not None:
            _lessons.settle_from_close(LANE, rec,
                                       home=_lessons_home())
    except Exception:
        pass
    try:
        _ks.record_trade(LANE, pnl)
        _ks.check_lane(LANE)
        if halt_verdict:
            _ks.halt_lane(
                LANE,
                "pre-registered verdict: expectancy %.4f <= 0 over "
                "%d closes" % (d["verdict"]["expectancy"],
                               d["verdict"]["n_closes"]))
    except Exception:
        pass
    logger.warning("grad CLOSE %s %s pnl=$%.2f realized=%.3fx (%s) "
                   "cash=$%.2f", pos["chain"], pos["symbol"], pnl,
                   rec["realized_mult"], closed_reason, d["bankroll"])
    # P4: exit event (both engine instances; LANE-stamped).
    _record_event("exit", {
        "mint": mint, "symbol": pos["symbol"],
        "pnl_usd": pnl, "realized_mult": rec["realized_mult"],
        "realized_usd": realized, "closed_reason": closed_reason,
        "peak_mult": rec.get("peak_mult"),
        "peak_price": rec.get("peak_price"),
        "entry_price": pos["entry_price"],
        "entry_ts": pos["entry_ts"], "close_ts": rec["close_ts"],
    })
    _vp = {"mint": mint, "pnl_usd": pnl,
           "realized_mult": rec["realized_mult"],
           "closed_reason": closed_reason,
           "closes_count": len(d["closes"])}
    if rec.get("f2"):
        _vp["f2"] = rec["f2"]
    _verdict_event(LANE, "close", _vp)
    if not had_verdict and d.get("verdict"):
        _verdict_event(LANE, "verdict", {"closes": d["verdict"].get("n_closes"), "expectancy": d["verdict"].get("expectancy"), "decision": d["verdict"].get("action")})
    return rec


def should_exit_on_time(pos, now):
    """(fire, reason): the max-hold stop, price-independent.

    Pure predicate — no price read, no RPC, no ledger. TWAP
    positions use the TWAP max hold (twap_time_stop_60min),
    everything else the 24h hygiene backstop (time_stop_24h).
    Same thresholds _poll_exits Phase 3 applies, so the two
    paths never disagree about WHEN a position expires — only
    about what price it exits at. Never raises."""
    try:
        entry_ts = float(pos.get("entry_ts") or 0)
        if entry_ts <= 0:
            return (False, None)
        if TWAP_SLICES is not None and TWAP_MAX_HOLD_S is not None:
            hold = TWAP_MAX_HOLD_S
            reason = "twap_time_stop_60min"
        else:
            hold = TIME_STOP_H * 3600
            reason = "time_stop_24h"
        if float(now) - entry_ts > hold:
            return (True, reason)
        return (False, None)
    except Exception:
        return (False, None)


def last_known_price(pos):
    """Last real observed price, or 0.0 when none is on record.

    Reads the quote journal (px > 0 entries only — a 0.0 read is
    never stored there, so it can never floor an exit) and
    returns the latest real quote. 0.0 means "no real price on
    record", never a measured price: the caller closes such a
    position as unquotable at 0, exactly like the unpriced branch
    of _poll_exits. The peak is deliberately NOT a fallback — the
    peak is the best price ever seen, not the last known one,
    and exiting an expired position at its peak would bank an
    unrealized high. Never raises."""
    try:
        best_px = 0.0
        best_ts = 0.0
        for entry in pos.get("quote_journal") or []:
            try:
                px = float((entry or {}).get("px") or 0)
                ts = float((entry or {}).get("ts") or 0)
            except Exception:
                continue
            if px > 0 and ts >= best_ts:
                best_px, best_ts = px, ts
        return best_px
    except Exception:
        return 0.0


def poll_time_exits():
    """Close max-hold positions with NO price read at all.

    The time-only exit split (2026-11-17, Batch 3): the 24h stop
    fires against the LAST KNOWN price even when every pricing
    source is down (Helius spent, Jupiter dark) — exactly the
    situation the bot is in today. This function never fetches a
    quote, never touches Jupiter/reserves/DexScreener, and never
    waits on Phase 2: it scans the open positions under one lock
    and closes whatever should_exit_on_time fires on, at
    last_known_price (0.0 when no real price is on record — the
    unquotable variant of the reason, same as _poll_exits'
    unpriced branch). Trader loops call this BEFORE poll_exits
    so the stop fires even if the pricing path then stalls.
    Returns close records. Never raises."""
    _POLL_SEQ["n"] += 1   # one identity per time-exit cycle too (migration marker tries count cycles)
    try:
        with _ledger_lock():
            d = _read()
            closes = []
            now = time.time()
            for mint, pos in list(d["positions"].items()):
                try:
                    # TWAP positions must carry their slice
                    # journal BEFORE any exit path runs (same
                    # rule as _poll_exits Phase 3).
                    if TWAP_SLICES is not None:
                        pos.setdefault("twap", {
                            "slices": TWAP_SLICES, "done": 0,
                            "sold_tokens": 0.0, "sold_usd": 0.0})
                    fire, reason = should_exit_on_time(pos, now)
                    if not fire:
                        continue
                    px = last_known_price(pos)
                    if px <= 0:
                        reason = ("twap_time_stop_60min_unquotable"
                                  if reason.startswith("twap")
                                  else "time_stop_24h_unquotable")
                        pos["last_price_source"] = "unquotable"
                    _close_remaining(
                        pos, px, reason,
                        pkind=((_pk.STALE if px > 0 else _pk.WRITEDOWN)
                               if _pk is not None else None))
                    closes.append(_finalize(d, mint, pos, reason))
                except Exception as e:
                    logger.warning("grad poll_time_exits %s failed: "
                                   "%s", mint, e)
                    continue
            return [c for c in closes if c]  # None = settlement pending (position kept open)
    except Exception as e:
        logger.warning("grad poll_time_exits failed: %s", e)
        return []


def _receipt_matches(fill, r, tokens):
    """A receipt agrees with the journaled fill when BOTH gross proceeds and (when journaled) qty match."""
    try:
        if abs(float(fill["gross_usd"]) - float(r.get("proceeds") if r.get("proceeds") is not None else -1)) > 1e-6:
            return False
        q = r.get("qty")
        return q is None or abs(float(q) - float(tokens)) <= max(1e-6, 1e-9 * abs(float(tokens)))
    except Exception:
        return False


_RECONCILING = set()   # in-memory re-entrancy guard keyed by position instance (never persisted: a stale on-disk flag must not bypass)


def _is_sim(pos):
    return (pos.get("execution") or {}).get("source") == "jupiter_quote_sim"


def _dust(pos):
    try:
        v = float(pos.get("tranche_dust_qty") or 0.0)
    except Exception:
        return 0.0
    return v if v > 0 and v == v and v != float("inf") else 0.0


def _acct_qty(pos, leg_key, lane_qty):
    """Account units for one leg credit: the lane qty, plus the one-time opening-unit dust on the migration's
    designated leg (pos["tranche_dust_leg"]); identity for every position opened with the marker."""
    if pos.get("tranche_dust_leg") == leg_key:
        return lane_qty + _dust(pos)
    return lane_qty


def _receipt_qty_ok(r, qty):
    q = r.get("qty")
    return r.get("proceeds") is not None and q is not None and abs(float(q) - qty) <= max(1e-6, 1e-9 * abs(qty))


def _sold_any(pos):
    """True when ANY part of the lane position has been sold or written down (ladder leg done, or TWAP slice/remainder)."""
    if any(lv.get("done") for lv in (pos.get("legs") or {}).values()):
        return True
    tw = pos.get("twap")
    if tw is not None:
        tw = tw or {}
        return bool(int(tw.get("done") or 0) > 0 or float(tw.get("sold_tokens") or 0) > 0 or tw.get("remainder"))
    return False


# Account pos_ids this process has debited but not yet written to the lane ledger (the debit precedes the ledger write).
# Only the window inside one entry: a crash there leaves an account position the lane books never learned of, which the
# NEXT process correctly reads as unprovable (the set is in-memory, never persisted).
_INFLIGHT_OPEN = set()


def legacy_unproven_pos_ids():
    """Account pos_ids of lane positions whose ACCOUNT view is not provably consistent with the lane: positions that
    predate the per-tranche marker (or are mid-migration) and have sold something, so the account still carries units
    the lane has already sold. Their marks would overstate equity (and could inflate the persisted peak), so the
    account marker OMITS them: the account then reports marks_status "no mark", records no baseline/peak, and the
    entry gates fail closed. Exits are untouched. A position leaves this set only when the migration has proven and
    credited its sold legs. Returns None when the lane ledger cannot be read (callers must treat every lane position
    as unprovable): unknown evidence is never read as zero."""
    try:
        # Read the file DIRECTLY: _read() turns a corrupt ledger into a blank one (moving the file aside), which would
        # read as "no legacy positions". A corrupt/unparseable ledger, or one a past corruption moved aside, is UNKNOWN.
        import glob
        if glob.glob(LEDGER_PATH + ".corrupt-*"):
            return None
        try:
            with open(LEDGER_PATH) as _f:
                _raw = json.load(_f)
            if not isinstance(_raw, dict):
                return None
        except FileNotFoundError:
            pass                      # missing: _read() below returns a blank ledger; the account cross-check catches held positions
        d = _read()                   # content via the lane's own reader (corruption was ruled out above)
        out = set()
        _lp = {}   # lane positions by account pos_id, and by mint for positions that predate pa_pos_id
        _by_mint = {}
        for mint, pos in (d.get("positions") or {}).items():
            if not isinstance(pos, dict):
                continue
            if pos.get("pa_pos_id"):
                _lp[str(pos["pa_pos_id"])] = pos
            _by_mint[mint] = pos
        def _legacy_unproven(pos):
            if pos.get("tranche_credit") and not pos.get("tranche_migrating"):
                return False
            return _sold_any(pos)
        _pa = _paper_account()
        if _pa is None:
            return {pid for pid, pos in _lp.items() if _legacy_unproven(pos)}
        _snap = _pa.snapshot()
        if _snap is None:
            return None
        for _p in ((_snap.get("lanes") or {}).get(LANE) or {}).get("positions") or []:
            _id = str(_p.get("pos_id"))
            if _id in _INFLIGHT_OPEN:
                continue
            pos = _lp.get(_id)
            if pos is None and not _p.get("instance"):
                pos = _by_mint.get(_p.get("mint"))
            # An account position of THIS lane that the lane's own books do not know (ledger missing, blanked, restored from
            # an older copy) cannot be proven consistent either: unprovable.
            if pos is None or _legacy_unproven(pos):
                out.add(_id)
        return out
    except Exception:
        return None


_MIGRATING = set()   # in-memory re-entrancy guard (never persisted)


_POLL_SEQ = {"n": 0, "boot": "%d-%d" % (os.getpid(), time.time_ns())}   # incremented once per _poll_exits cycle: "tries" counts POLLS that found the position blocked, not calls


def _lock_owned_by_me():
    """True only when THIS thread of THIS process currently holds the ledger flock (a different thread, or a forked child that inherited the
    counter, does not own it and must take the flock normally, i.e. block until the owner releases)."""
    return _LOCK_DEPTH["n"] > 0 and _LOCK_DEPTH["tid"] == threading.get_ident() and _LOCK_DEPTH["pid"] == os.getpid()


def _stamp_migration_blocked(pos, code, why):
    """DURABLE, visible record that a legacy position could not be migrated: {code, why, since, tries, last_try, poll_seq}.
    - tries counts polls (one per _poll_exits cycle via _POLL_SEQ), not calls: the migration is invoked several times per poll.
    - A ledger write happens ONLY when the marker is new or its code/why changed (a transition); an unchanged marker only
      updates tries/last_try in memory, which the caller's normal position write persists.
    - Persistence is a fresh read-modify-write of THIS position only. Every production caller (_poll_exits, poll_time_exits)
      already holds _ledger_lock for the whole loop (audited: grad.py _poll_exits `with _ledger_lock()` spans every
      _close_remaining/_migrate call). flock is not re-entrant across file descriptors, so the lock is taken here ONLY when
      this process does not already hold it (direct non-poll callers, tests). Never raises."""
    try:
        now = time.time(); seq = "%s:%d" % (_POLL_SEQ["boot"], _POLL_SEQ["n"]); prev = pos.get("migration_blocked") or {}
        changed = (not prev) or prev.get("code") != code or prev.get("why") != str(why)[:300]
        counted = changed or prev.get("poll_seq") != seq
        mb = {"code": code, "why": str(why)[:300], "since": (prev.get("since") if not changed or prev else None) or now,
              "tries": int(prev.get("tries") or 0) + (1 if counted else 0), "last_try": now, "poll_seq": seq}
        pos["migration_blocked"] = mb
        if not changed:
            return
        def _persist():
            d = _read(); cur = (d.get("positions") or {}).get(pos.get("mint"))
            if cur is not None:
                cur["migration_blocked"] = dict(mb)
                _write(d)
        if _lock_owned_by_me():
            _persist()   # THIS thread in THIS process holds the lock (a forked child or another thread takes the lock normally)
        else:
            with _ledger_lock():
                _persist()
    except Exception as e:
        logger.warning("grad: migration_blocked marker could not be persisted: %r", e)


def _migrate_legacy_tranches(pos):
    """LEGACY LADDER MIGRATION. A position opened before the tranche marker keeps the account at its FULL qty/basis
    until the final close, so marks overstate equity (and can inflate the persisted peak) once legs have sold. Bring
    it onto the per-tranche contract: credit each ALREADY-SOLD leg to the account (same op_id scheme as the marked
    path, so idempotent and receipt-replayable), scale credit qty by account_units/lane_units so the account keeps
    the same fraction as the lane, then set the marker. Best effort and NEVER blocks an exit: the final close already
    credits only `realized - tranche tally`, so a partial or skipped migration can never double count or lose value.
    Evidence order: existing receipt, then the lane leg record (net, price, tokens) and, for the quote sim, the
    JOURNALED fill (read-only get_fill; never a fresh quote). Anything unknown leaves the position legacy (no credit)
    with a loud throttled warning. Ladder positions only. TWAP legacy positions (and any ladder position this cannot prove) stay unmigrated and are
    NOT MARKED by the account marker (see legacy_unproven_pos_ids): exits remain possible, equity/peak/entry gates fail closed.
    DUST TOLERANCE: the opening-unit difference must equal gas_in_usd/entry_price (both recorded on the lane position) within
    1e-4 x the opening account qty (float/rounding slack of the recorded gas figures only; it is not a risk limit). Anything
    larger is unexplained provenance and the position stays legacy.
    Returns True when the position is (now) on the tranche contract."""
    try:
        if pos.get("twap") is not None or not pos.get("pa_pos_id"):
            return False
        if pos.get("tranche_credit") and not pos.get("tranche_migrating"):
            return True
        mint, pid = pos["mint"], pos["pa_pos_id"]
        key = (mint, pid)
        if key in _MIGRATING or key in _RECONCILING:
            return False
        legs = [(lk, lv) for lk, lv in pos["legs"].items()
                if lv.get("done") and lv.get("exit_price_kind") != "write_down_zero"]
        if not legs:
            return False                      # nothing sold: stays legacy; adopted the poll after a leg sells
        _pa = _paper_account()
        if _pa is None:
            return False
        def _stay(why, code="unspecified"):
            _warn_throttled(("migrate", mint, str(why)[:40]), "grad: legacy tranche migration for %s skipped (stays legacy, no credit): %s", mint[:12], why)
            _stamp_migration_blocked(pos, code, why)
            return False
        close_op = "%s:close:%s:%s" % (LANE, mint, pid)
        ops = ["%s:leg:%s:%s:%s" % (LANE, lk, mint, pid) for lk, _ in legs]
        rc = _pa.op_receipts(ops + [close_op])
        if rc.get("_unreadable"):
            return _stay("account journal unreadable", "journal_unreadable")
        if rc.get(close_op) is not None or _pa.op_outcome(close_op) != "unknown":
            return _stay("a final-close op already exists for this position", "final_close_exists")
        _ci = _close_intents()
        if _ci is None:
            return _stay("close-intent journal unavailable", "close_intent_journal_unavailable")
        for it in (_ci.pending(LANE) or []):
            if str((it.get("intent") or {}).get("pos_id")) == str(pid):
                return _stay("a close intent is open for this position", "close_intent_open")
        if pos.get("tranche_dust_leg") is None:
            dbt = _pa.position_debit(pid)
            q0 = float((dbt or {}).get("qty") or 0.0)
            toks = float(pos.get("tokens") or 0.0)
            if not dbt or dbt.get("mint") != mint or dbt.get("lane") != LANE or not (q0 > 0) or not (toks > 0):
                return _stay("opening debit not provable from the journal", "opening_debit_unprovable")
            dust = q0 - toks
            # The ONLY lawful source of a difference is the entry gas paid in tokens (stake/price units debited vs net
            # tokens held): expected dust = gas_in_usd / entry_price, both recorded on the lane position. Anything else is
            # unexplained provenance and stays legacy (no bound is invented; the evidence must explain the number).
            try:
                exp_dust = float(pos.get("gas_in_usd") or 0.0) / float(pos["entry_price"])
            except Exception:
                exp_dust = None
            _equal = abs(dust) <= max(1e-9, 1e-9 * q0)      # account qty already equals the lane tokens (net-token debit): nothing to explain, nothing to remove
            if not _equal and (exp_dust is None or dust < 0 or abs(dust - exp_dust) > max(1e-9, 1e-4 * q0)):
                return _stay("opening-unit difference %.9f not explained by entry gas (%s)" % (dust, exp_dust), "dust_unexplained")
            dust_leg = legs[0][0]
        else:
            dust, dust_leg = _dust(pos), pos["tranche_dust_leg"]
        plan = []
        _df = _demo_filler() if _is_sim(pos) else None
        for (lk, lv), op in zip(legs, ops):
            ltok = pos["tokens"] * lv["frac"]
            r = rc.get(op)
            if r is not None:
                if not _receipt_qty_ok(r, ltok + (dust if lk == dust_leg else 0.0)):
                    return _stay("receipt qty disagrees for leg %s" % lk, "receipt_qty_disagrees")
                plan.append((lk, ltok, float(r["proceeds"]), float(r.get("fee") or 0.0),
                             r.get("price_kind") or (_pk.UNKNOWN if _pk is not None else None)))
                continue
            net = float(lv.get("realized_usd") or 0.0)
            gross = float(lv.get("exit_price") or 0.0) * ltok
            fee = gross - net
            if not (ltok > 0 and gross > 0) or fee < -1e-9 or fee > gross + 1e-9:
                return _stay("leg %s evidence inconsistent" % lk, "leg_evidence_inconsistent")
            if _df is not None:
                jf = _df.get_fill("%s:exit:%s:%s" % (LANE, mint, lk))
                if not jf or abs(float(jf.get("gross_usd", -1)) - gross) > 1e-6:
                    return _stay("journaled fill missing or disagrees for leg %s" % lk, "fill_missing_or_disagrees")
            plan.append((lk, ltok, gross, max(fee, 0.0), lv.get("exit_price_kind") or (_pk.UNKNOWN if _pk is not None else None)))
        # intent-first: durable marker (+ scale) BEFORE the first account credit
        pos["tranche_dust_qty"] = dust
        pos["tranche_dust_leg"] = dust_leg
        pos.pop("migration_blocked", None)   # evidence now proves the migration: the block is lifted
        pos["tranche_credit"] = True
        pos["tranche_migrating"] = True
        pos.setdefault("tranche_credited", {"net": 0.0, "qty": 0.0})
        d = _read(); d["positions"][mint] = pos; _write(d)
        _MIGRATING.add(key)
        try:
            for lk, ltok, gross, fee, kind in plan:
                if not _tranche_credit(pos, lk, ltok, gross, fee, kind):
                    return False              # refusal persisted by _tranche_credit; marker stays, next poll resumes
            pos.pop("tranche_migrating", None)
            d = _read(); d["positions"][mint] = pos; _write(d)
            return True
        finally:
            _MIGRATING.discard(key)
    except Exception as e:
        logger.warning("grad: legacy tranche migration failed (position left as is): %r", e)
        try:
            _stamp_migration_blocked(pos, "exception", repr(e)[:200])
        except Exception:
            pass
        return False


_RECON_WHY = {}


def _no_unrecorded_sale_proof(pos):
    """The account journal is unreadable, so credits cannot be read. A sale ALWAYS journals its fill BEFORE its account
    credit (fill -> _tranche_credit -> lane mark), and fill keys are scoped to the position instance. So when the
    fill journal is readable, DOES hold this position's own entry fill (a wiped or missing journal proves nothing),
    and holds NO exit fill for any leg the lane has not marked done, no un-marked leg can have been credited and the
    stop may sell the remainder. Any such fill is uncertain (credited or not): refuse (visible pending_exit), never
    guess. Sim positions only: a non-sim leg leaves no fill evidence, so it is never proven. Residual: this binds the
    fill-journal's durability, not the account's."""
    try:
        if not _is_sim(pos) or not pos.get("tranche_credit") or not pos.get("pa_pos_id"):
            return False
        _df = _demo_filler()
        eop = (pos.get("execution") or {}).get("entry_op")
        if _df is None or not eop:
            return False
        fills = _df._locked(lambda j: dict(j["fills"]))
        if eop not in fills:
            return False
        pre, suf = "%s:exit:%s:" % (LANE, pos["mint"]), ":%s" % pos["pa_pos_id"]
        done = {k for k, v in (pos.get("legs") or {}).items() if v.get("done")}
        for k in fills:
            if k.startswith(pre) and k.endswith(suf):
                leg = k[len(pre):-len(suf)]
                if leg in done:
                    continue
                # A COMBINED remainder fill ("remaining:<legs>") journaled by an interrupted stop is the SAME intent a
                # retry regenerates, and it replays iff the legs still open are EXACTLY the legs it covered (the combined
                # path books no per-leg credit, so nothing can already be credited for them). Any other shape (a different
                # open set: a leg was sold or reconciled since; per-leg, TWAP slice or remainder fills) is uncertain.
                if leg.startswith("remaining:") and set(leg[len("remaining:"):]) == {
                        lk for lk, lv in (pos.get("legs") or {}).items() if not lv.get("done")}:
                    continue
                return False
        return True
    except Exception:
        return False


def _partition_open_legs(pos):
    """Leg-scoped version of the fill-journal proof (ladder, sim, tranche-marked only). Returns (certain, uncertain) open leg
    keys, or None when the fill journal cannot be trusted (no readable journal, no entry fill for THIS position, not sim,
    TWAP). A leg is UNCERTAIN when any journaled exit fill for this position instance mentions it (its own key, or a combined
    "remaining:<legs>" key that does not cover exactly the open set): it may or may not have been credited. Every other open
    leg has NO fill and therefore cannot have been credited (fill precedes credit), so it is certain."""
    try:
        if not _is_sim(pos) or not pos.get("tranche_credit") or not pos.get("pa_pos_id") or pos.get("twap") is not None:
            return None
        _df = _demo_filler()
        eop = (pos.get("execution") or {}).get("entry_op")
        if _df is None or not eop:
            return None
        fills = _df._locked(lambda j: dict(j["fills"]))
        if eop not in fills:
            return None
        pre, suf = "%s:exit:%s:" % (LANE, pos["mint"]), ":%s" % pos["pa_pos_id"]
        open_ = {k for k, v in (pos.get("legs") or {}).items() if not v.get("done")}
        unc = set()
        for k in fills:
            if not (k.startswith(pre) and k.endswith(suf)):
                continue
            leg = k[len(pre):-len(suf)]
            if leg.startswith("remaining:"):
                covered = set(leg[len("remaining:"):])
                if covered == open_:
                    continue   # exact replay of the interrupted combined stop
                unc |= (covered & open_)
            elif leg in open_:
                unc.add(leg)
            elif leg not in (pos.get("legs") or {}):
                return None   # unknown key shape (twap etc.): cannot reason about it
        return sorted(open_ - unc), sorted(unc)
    except Exception:
        return None


def _reconcile_credited_legs(pos):
    """Crash window: a leg's (ladder leg, TWAP slice or TWAP remainder) account credit became durable but the
    lane-ledger write never landed, so the lane still shows it pending while the account already shrank. Detect it
    from the account journal (one batched read, receipt for the stable op_id) and finish the lane side from the
    journaled fill, whether or not any exit trigger fires again. The credit inside the close helpers is
    idempotent by op_id. The ORIGINAL kind comes from the receipt (missing -> unknown, never upgraded); a receipt
    whose proceeds disagree with the journaled fill, or no receipt, leaves the leg pending and loud. The original
    exit REASON is not journaled, so the reconciled leg is labeled reconciled_credited_leg.
    Returns False when an applied credit could NOT be reconciled (or the journal is unreadable): the caller must
    not run a combined remainder sale that could sell those units again."""
    if not pos.get("tranche_credit") or not pos.get("pa_pos_id") or (pos["mint"], pos["pa_pos_id"]) in _RECONCILING:
        return True
    _pa = _paper_account()
    if _pa is None:
        return True
    mint, pid = pos["mint"], pos["pa_pos_id"]
    def _op(k): return "%s:leg:%s:%s:%s" % (LANE, k, mint, pid)
    tw = pos.get("twap")
    if tw is not None:
        n = int(tw.get("slices") or 0)
        keys = ["twap%d" % i for i in range(int(tw.get("done") or 0) + 1, n + 1)]
        if not tw.get("remainder"):
            keys.append("twap_remainder")
    else:
        keys = [lk for lk, lv in pos["legs"].items() if not lv.get("done")]
    if not keys:
        return True
    _RECON_WHY.pop((mint, pid), None)
    try:
        rc = _pa.op_receipts([_op(k) for k in keys])
    except Exception:
        _RECON_WHY[(mint, pid)] = "unreadable"
        return False
    if rc.get("_unreadable"):
        _RECON_WHY[(mint, pid)] = "unreadable"
        _warn_throttled(("unreadable", mint), "grad: account journal unreadable; cannot prove which legs were credited for %s", mint[:12])
        return False
    ok_all = True
    def _kind(r):
        k = (r or {}).get("price_kind")
        return k or (_pk.UNKNOWN if _pk is not None else None)
    _RECONCILING.add((mint, pid))
    try:
        if tw is None:
            for lk in keys:
                r = rc.get(_op(lk))
                if r is None:
                    continue
                _ltok = pos["tokens"] * pos["legs"][lk]["frac"]
                if _is_sim(pos):
                    f = _demo_sell(pos, _ltok, lk)
                    _okm = f is not None and _receipt_matches(f[0], r, _acct_qty(pos, lk, _ltok))
                else:   # non-sim: no journaled fill exists; the receipt is the only evidence, qty must agree
                    _okm = _receipt_qty_ok(r, _acct_qty(pos, lk, _ltok))
                if not _okm:
                    _warn_throttled(("unrec", mint, lk), "grad: credited leg %s %s cannot be reconciled with its receipt; leg stays pending", mint[:12], lk)
                    ok_all = False
                    continue
                _close_leg(pos, lk, 0.0, "reconciled_credited_leg", receipt_kind=_kind(r))
                if not pos["legs"][lk].get("done"):
                    # the replay was refused (credit refused / qty mismatch / divergence): the leg is NOT reconciled
                    ok_all = False
            return ok_all
        # TWAP: slices strictly in order (slice i+1 cannot have been credited without slice i), then the remainder
        _slice_gap = False
        for k in keys:
            r = rc.get(_op(k))
            if r is None:
                if k != "twap_remainder":
                    _slice_gap = True   # slice i+1 cannot have been credited without slice i; the remainder still can
                continue
            if k != "twap_remainder" and _slice_gap:
                ok_all = False   # a later slice credited while an earlier one has no receipt: inconsistent, refuse
                logger.warning("grad: TWAP slice %s credited without its predecessor for %s", k, mint[:12])
                continue
            if k == "twap_remainder":
                rem_tok = _twap_remaining_tokens(pos)
                if _is_sim(pos):
                    f = _demo_sell(pos, rem_tok, "twap_remainder")
                    _okm = f is not None and _receipt_matches(f[0], r, rem_tok)
                else:
                    _okm = _receipt_qty_ok(r, rem_tok)
                if not _okm:
                    logger.warning("grad: credited TWAP remainder %s cannot be reconciled with its receipt", mint[:12])
                    _fl = (f[0] if (_is_sim(pos) and f is not None) else {}) or {}
                    _recon_note(pos, "twap_remainder", "receipt_fill_money_mismatch", receipt_proceeds=r.get("proceeds"), receipt_fee=r.get("fee"),
                                receipt_qty=r.get("qty"), fill_gross=_fl.get("gross_usd"), fill_fee=_fl.get("fee_usd"), fill_qty=_fl.get("qty_tokens"),
                                expected_qty=rem_tok, fill_found=bool(_fl))
                    ok_all = False
                    continue
                _close_remaining(pos, 0.0, "reconciled_credited_leg", receipt_kind=_kind(r))
                if not (pos.get("twap") or {}).get("remainder"):
                    ok_all = False
            else:
                _stok = pos["tokens"] / max(n, 1)
                if _is_sim(pos):
                    f = _demo_sell(pos, _stok, k)
                    _okm = f is not None and _receipt_matches(f[0], r, _stok)
                else:
                    _okm = _receipt_qty_ok(r, _stok)
                if not _okm:
                    logger.warning("grad: credited TWAP slice %s %s cannot be reconciled with its receipt", mint[:12], k)
                    ok_all = False
                    break
                _d0 = int(tw.get("done") or 0)
                _close_twap_slice(pos, 0.0)
                if int(tw.get("done") or 0) <= _d0:
                    ok_all = False   # replay refused: slice not reconciled, later slices must not proceed
                    break
        return ok_all
    finally:
        _RECONCILING.discard((mint, pid))


def poll_exits(price_fn=None):
    """Evaluate exits for all open grad positions. price_fn(chain,
    mint, spot_index) -> float override for tests / rt_sync. Returns
    close records. Never raises."""
    try:
        return _poll_exits(price_fn)
    except Exception as e:
        logger.warning("grad poll_exits failed: %s", e)
        return []
    finally:
        _publish_health()


def _health_path():
    return LEDGER_PATH + ".health.json"


_HEALTH_LAST = {"sig": None, "ts": 0.0, "fail": None}
HEALTH_HEARTBEAT_S = 300.0   # TECHNICAL freshness interval (bounds fsync rate; not an owner policy, not an alert threshold)


def _strip_volatile(x):
    """recon_health content without time- or poll-derived counters (ages, tries, last_try), so the signature changes only on a real state transition."""
    if isinstance(x, dict):
        return {k: _strip_volatile(v) for k, v in x.items() if not (k.endswith("age_s") or k.endswith("tries") or k in ("last_try", "last_ts"))}
    if isinstance(x, list):
        return [_strip_volatile(v) for v in x]
    return x


def _publish_health():
    """DURABLE operator file (no daemon status consumer of this lane exists in the repo): after every poll cycle, atomically write
    {recon_health, heartbeat{boot_id, poll_seq, last_poll_ts}} to <ledger>.health.json when (a) the state signature (volatile ages excluded)
    changed, (b) it has not been written for HEALTH_HEARTBEAT_S, or (c) the first poll of this boot. Freshness: a consumer must treat a file
    whose last_poll_ts is older than its own poll cadence as STALE = UNKNOWN (poller stopped), never as clean. The file write fsyncs and CAN
    block on a slow disk; no non-blocking claim is made. A write failure is logged, kept in memory and reported in the next successful file
    as prior_write_failure. Pure diagnostics: no thresholds, no actions."""
    try:
        now = time.time()
        h = recon_health()
        sig = json.dumps(_strip_volatile({"recon_health": h}), sort_keys=True, default=str)
        if sig == _HEALTH_LAST["sig"] and (now - _HEALTH_LAST["ts"]) < HEALTH_HEARTBEAT_S and os.path.exists(_health_path()):
            return
        body = {"recon_health": h, "heartbeat": {"boot_id": _POLL_SEQ["boot"], "poll_seq": _POLL_SEQ["n"], "last_poll_ts": now},
                "heartbeat_interval_s": HEALTH_HEARTBEAT_S, "written_ts": now, "prior_write_failure": _HEALTH_LAST["fail"]}
        tmp = _health_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(body, fh, default=str); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, _health_path())
        _HEALTH_LAST.update(sig=sig, ts=now, fail=None)
    except Exception as e:
        _HEALTH_LAST["fail"] = "%s at %s" % (repr(e)[:200], time.time())
        logger.warning("grad: health surface not written: %r", e)


# In-process status of aborted poll cycles (no durable marker is possible when the I/O that failed is the ledger itself): surfaced by recon_health.
_POLL_ABORTS = {"count": 0, "last_ts": None, "last_mint": None, "last_error": None}
# Seam (default None = no behavior): called inside the ledger lock, right after an abort is counted, so a wiring layer can make the abort durable
# BEFORE the lock is released. It must not raise; any exception is swallowed here so the abort path is unchanged.
_ABORT_HOOK = None
# Write-ahead seams (default None = no behavior). BEGIN is called inside the ledger lock when a per-position poll exception is caught, BEFORE the restore
# I/O (_persist_poll_failure) and before the abort is counted, so a process death anywhere after it leaves a durable in-flight marker. It returns an opaque
# token (or None when no marker was written). END is called with that token only after the restore succeeded. After an abort the marker is NOT removed
# here: _ABORT_HOOK itself decides, and removes it only once its fault is durable. Hooks must not raise and must not take the cycle or ledger lock; exceptions are swallowed so exits and the abort path are
# unchanged. Hooks run only on the exception path: the normal poll adds no I/O.
_FAILURE_BEGIN_HOOK = None
_FAILURE_END_HOOK = None


def _call_failure_hook(hook, *a, **kw):
    if hook is None:
        return None
    try:
        return hook(*a, **kw)
    except Exception:
        return None


def _persist_poll_failure(d, mint, pos, exc):
    """Make a per-position poll exception VISIBLE and durable without masking it or pretending success. The primary exception is already
    logged (with traceback) by the caller. Here: (1) mark the in-memory position pending_exit "poll_exception" (a more specific existing
    pending why is kept; the error rides along), so any later write of this cycle carries it; (2) best-effort durable write that re-reads
    the ledger FRESH and stamps ONLY the marker and the recon notes onto that position, never the half-mutated in-memory state. Never
    raises; a persistence failure is logged at error level. No close, no credit, no cash change, the position stays open and fail-closed.
    Returns True only when the in-memory ledger was replaced by the persisted fresh view; False means the caller MUST abort the cycle.
    LIMIT (stated, not closed): writes the failing position's own code path made BEFORE it raised (an unquotable-branch _write, _finalize's write)
    may already have persisted partial state; the restore repairs that only when the fresh read and write succeed."""
    err = repr(exc)[:200]
    try:
        _prior = pos.get("pending_exit") or {}
        _mk = {"leg": "poll", "why": "poll_exception", "error": err, "ts": time.time()}
        if not _prior:
            pos["pending_exit"] = _mk
        else:
            _prior.setdefault("poll_error", err)
    except Exception:
        pass
    try:
        d2 = _read()
        p2 = (d2.get("positions") or {}).get(mint)
        if p2 is None:
            return False
        if not p2.get("pending_exit"):
            p2["pending_exit"] = {"leg": "poll", "why": "poll_exception", "error": err, "ts": time.time()}
        else:
            p2["pending_exit"].setdefault("poll_error", err)
        _ns = [n for n in ((pos or {}).get("recon_notes") or []) if isinstance(n, dict)]
        if _ns:
            have = {(n.get("key"), n.get("what")) for n in (p2.get("recon_notes") or [])}
            p2["recon_notes"] = list(p2.get("recon_notes") or []) + [dict(n) for n in _ns if (n.get("key"), n.get("what")) not in have]
        _write(d2)
        # the in-memory ledger of THIS cycle held half-mutated state for the failed position: replace it with the freshly persisted view
        # so no later write in the cycle can persist a partial mutation (all earlier closes were already persisted by their own writes).
        d.clear(); d.update(d2)
        return True
    except Exception as e2:
        logger.error("grad poll_exits %s: failure state could NOT be persisted (%r); original error %s", str(mint)[:12], e2, err)
        return False


_OBS_UNSUPPORTED = {"n": 0}   # polls where the price module had no get_prices_observed (observation times then unknown, counted, visible)


def _obs_fields(q, now):
    """Validate a price observation timestamp at the source boundary. NEVER raises. For the Jupiter module the timestamp is the LOCAL time
    this process filled its price cache (the v3 response carries no upstream time here), NOT an upstream/exchange time: obs_ts_kind says so.
    Visibility only: no TTL, no refusal, no rule reads these fields. Other routes (caller price_fn, reserves, DexScreener, GeckoTerminal)
    carry no observation time: status "none"."""
    import math
    raw = q.get("obs_ts")
    base = {"obs_ts": None, "obs_age_s": None, "obs_ts_kind": "jup_local_cache_fill_time" if q.get("src") == getattr(_jup, "SOURCE", "\0") else None}
    try:
        if raw is None:
            return dict(base, obs_status="none")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return dict(base, obs_status="invalid:type")
        t = float(raw)
        if not math.isfinite(t) or t <= 0:
            return dict(base, obs_status="invalid:not_finite_or_nonpositive")
        if t > float(now) + 1.0:
            return dict(base, obs_status="invalid:future")
        return dict(base, obs_ts=t, obs_age_s=max(0.0, float(now) - t), obs_status="ok")
    except Exception:
        return dict(base, obs_status="invalid:error")


_JUP_REJECTS = {"n": 0}   # Jupiter observed-price entries rejected per mint at the boundary (visible counter)


def _validate_jup_observed(requested, raw):
    """(prices, obs, rejected). PURE, never raises. `raw` is {mint: (price, observed_ts)} as get_prices_observed returns it. A mint is accepted ONLY
    when it was requested, its value is a 2-sequence, and the price is a finite, strictly positive int/float (not bool). The observation ts is kept
    PASSED THROUGH UNCHANGED (any type): the timestamp is judged downstream by _obs_fields (invalid:* status, obs_ts None), which this function does not
    duplicate. Every other entry is rejected INDIVIDUALLY with a reason, keyed by the FULL mint string (so two mints sharing a prefix are never merged or
    undercounted; any log truncation is display only), so one bad mint cannot discard the others and a bad price is never used (a NaN price would
    defeat every stop comparison)."""
    import math
    prices, obs, rej = {}, {}, {}
    if not isinstance(raw, dict):
        return prices, obs, ({"*": "not_a_dict"} if raw else rej)
    want = set(m for m in (requested or []) if isinstance(m, str))
    for m, v in raw.items():
        if m not in want:
            rej[str(m)] = "unrequested_mint"; continue
        try:
            if not isinstance(v, (tuple, list)) or len(v) != 2:
                rej[str(m)] = "bad_shape"; continue
            px, ts = v
            if isinstance(px, bool) or not isinstance(px, (int, float)) or not math.isfinite(px) or px <= 0:
                rej[str(m)] = "bad_price"; continue
            prices[m] = float(px)
            obs[m] = ts
        except Exception:
            rej[str(m)] = "error"
    return prices, obs, rej


def _poll_exits(price_fn):
    _POLL_SEQ["n"] += 1
    if os.environ.get("AGT_GRAD_ENTRY_INTENT_ENABLED") == "1":
        # Entry-intent recovery (flag on only). Contained: ANY failure here is logged and the exit path below proceeds; this can only keep entries blocked.
        try:
            with _ledger_lock():
                _eib = _ei_recover()
            if _eib:
                logger.warning("grad poll_exits: entry-intent state blocks entries: %s", _eib)
        except Exception as _eie:
            logger.warning("grad poll_exits: entry-intent recovery pass failed (%r); exits continue", _eie)
    # Phase 1: snapshot (locked read). Quotes happen outside the lock.
    with _ledger_lock():
        snapshot = {m: p for m, p in _read()["positions"].items()}
    if not snapshot:
        return []
    # Phase 2: fetch quotes (no lock). Quote hierarchy for exits
    # (2026-11-17, Jupiter v3 first for graduated tokens):
    #   1. a caller price_fn always wins when it prices the mint:
    #      rt_sync's tape and the armed watcher's pushed prices.
    #   2. Jupiter Price v3 (one batched call for the whole open
    #      set, 12s cache in jup_price): free, no key, no Helius.
    #      Every position here is a graduated token by construction
    #      (source "graduation"), so v3 has an entry for it; a mint
    #      v3 does not know is simply absent and falls through.
    #   3. the pre-Jupiter path: ARMED positions (peak >= 1.5x)
    #      probe DIRECT pool/curve reserves first (Helius, exempt
    #      from the SharedGuard), unarmed positions read DS/GT.
    #      That path is unchanged — it is the fallback when v3 is
    #      dark, errors, or has no entry for the mint.
    # Entry quotes are NOT touched: quote_price() below is shared
    # with entry, and this hierarchy lives only in _poll_exits.
    jup_prices = {}
    jup_obs = {}     # mint -> observation ts of the Jupiter price (None = the source gave no timestamp: unknown, never invented)
    if _jup is not None:
        _jm = [m for m, p in snapshot.items() if (p.get("chain") or "solana") == "solana"]
        try:
            if hasattr(_jup, "get_prices_observed"):
                # PER-MINT validation at the source boundary: one malformed entry drops THAT mint only (it then falls through to the existing
                # fallback quote chain), never every mint's Jupiter price, and a non-finite/non-positive/boolean/mistyped price can never become q["px"].
                jup_prices, jup_obs, _rej = _validate_jup_observed(_jm, _jup.get_prices_observed(_jm))
                if _rej:
                    _JUP_REJECTS["n"] += len(_rej)
                    logger.warning("grad poll_exits: %d Jupiter price(s) rejected at the boundary (%s); those mints use the fallback route", len(_rej), sorted(set(_rej.values())))
            else:
                jup_prices = _jup.get_prices(_jm) or {}
                _OBS_UNSUPPORTED["n"] += 1
        except Exception:
            jup_prices, jup_obs = {}, {}
    quotes = {}
    for mint, pos in snapshot.items():
        q = {"px": 0.0, "src": "", "liq": 0.0, "pair": ""}
        try:
            entry = float(pos.get("entry_price") or 0)
            peak = float(pos.get("peak_price") or 0)
            armed = entry > 0 and peak >= 1.5 * entry
            if price_fn:
                q["px"] = float(price_fn(pos["chain"], mint,
                                         pos.get("spot_index")) or 0)
                q["src"] = "external"
                if q["px"] <= 0 and mint in jup_prices:
                    q["px"] = jup_prices[mint]
                    q["src"] = _jup.SOURCE
                    q["obs_ts"] = jup_obs.get(mint)
            elif mint in jup_prices:
                q["px"] = jup_prices[mint]
                q["src"] = _jup.SOURCE
                q["obs_ts"] = jup_obs.get(mint)
            elif armed and _reserves is not None:
                rpx, rsrc = _reserves.price_for_position(pos)
                if rpx > 0:
                    q["px"], q["src"] = rpx, rsrc
                else:
                    q["px"] = quote_price(pos["chain"], mint)
                    q["src"] = "ds/gt (reserves: %s)" % rsrc
                    q["pair"] = _LAST_QUOTE_PAIR
                    q["liq"] = _LAST_QUOTE_LIQ
            else:
                q["px"] = quote_price(pos["chain"], mint)
                q["src"] = "ds/gt"
                q["pair"] = _LAST_QUOTE_PAIR
                q["liq"] = _LAST_QUOTE_LIQ
        except Exception:
            q["px"] = 0.0
        quotes[mint] = q
    # Phase 3: mutate under lock.
    with _ledger_lock():
        d = _read()
        closes = []
        now = time.time()
        for mint in list(d["positions"]):
            # fetch the CURRENT object each iteration: a failure restore replaces d with a fresh view, so objects captured before
            # the loop would be stale (their mutations would never reach d or the ledger)
            pos = d["positions"].get(mint)
            if pos is None:
                continue
            try:
                q = quotes.get(mint) or {}
                px = float(q.get("px") or 0)
                # TWAP positions must carry their slice journal
                # BEFORE any exit path runs: the hard stop, the
                # F2 dark-quote exit, and the unquotable backstop
                # all call _close_remaining, which routes on the
                # twap key — a first-poll stop on a position with
                # no slices yet would otherwise take the ladder
                # branch and mark ladder legs for a TWAP close.
                if TWAP_SLICES is not None:
                    pos.setdefault("twap", {
                        "slices": TWAP_SLICES, "done": 0,
                        "sold_tokens": 0.0, "sold_usd": 0.0})
                _migrate_legacy_tranches(pos)
                _reconcile_credited_legs(pos)
                if pos.get("settlement_pending"):
                    # The economic exit already ran on an earlier poll; only the account settlement is outstanding.
                    # Retry it (the journal may be readable now). No second sale can happen: every leg is done.
                    _sp_rec = _finalize(d, mint, pos, (pos["settlement_pending"] or {}).get("closed_reason") or "settlement_retry")
                    if _sp_rec:
                        closes.append(_sp_rec)
                    continue
                # F5: refresh exit-time liquidity from the quote
                # that priced this eval.
                if float(q.get("liq") or 0) > 0:
                    pos["last_liquidity"] = float(q["liq"])
                # GRADUATION HANDLER: the mint's best pair changed
                # while held (migration / venue move). Invalidate
                # the cached quote source + resolved vaults, adopt
                # the new pair, and journal the transition — exits
                # themselves are NOT paused beyond this re-derivation.
                qpair = str(q.get("pair") or "")
                if qpair and pos.get("pair") and \
                        qpair != pos.get("pair"):
                    old_pair = pos["pair"]
                    pos.setdefault("transitions", []).append({
                        "ts": now, "kind": "pair_changed",
                        "from": old_pair, "to": qpair})
                    pos["pair"] = qpair
                    pos.pop("pool_vaults", None)
                    logger.warning(
                        "grad TRANSITION %s: pair %s -> %s "
                        "(quote re-derived, vaults invalidated)",
                        mint[:12], old_pair[:12], qpair[:12])
                elif qpair and not pos.get("pair"):
                    pos["pair"] = qpair
                # F3: per-eval quote journal (compact, capped).
                if px > 0:
                    pos["last_price_source"] = q.get("src") or ""
                    jq = pos.setdefault("quote_journal", [])
                    jq.append({"ts": now, "px": px,
                               "src": q.get("src") or "",
                               # price observation time (visibility only, no rule): None = the source did not say
                               **_obs_fields(q, now)})
                    del jq[:-40]
                    # SHADOW STOP (2026-11-20, faster-exits item 1c):
                    # journal-only — records the first eval at/below
                    # -30% within 15 min of entry. Never gates an
                    # exit; the live stop below is untouched.
                    if _shadow_stop is not None:
                        try:
                            _shadow_stop.note_quote(pos, px, now)
                        except Exception:
                            pass
                    # LIMIT MODEL + COVER (advanced build, pieces
                    # orig-3 + new-3, 2026-11-21): journal-only.
                    # The limit model notes every quote (what a
                    # resting Jupiter limit order WOULD have filled
                    # at exactly the rung / 0.475x stop / trail);
                    # the cover layer journals what it would do
                    # (TAKE_PROFIT / CUT_COVER / HOLD). Neither
                    # gates an exit; the live ladder below is
                    # untouched. Both ride into the close record
                    # via _finalize.
                    if _limit_exits is not None:
                        try:
                            md = pos.get("limit_model")
                            if not isinstance(md, dict):
                                md = _limit_exits.LimitModel(
                                    entry_price=pos.get("entry_price"),
                                    legs=pos.get("legs"),
                                    hard_stop_mult=0.5,
                                    time_stop_s=86400.0,
                                    entry_ts=pos.get("entry_ts"),
                                    entry_notional_usd=pos.get(
                                        "notional_usd")).to_dict()
                                pos["limit_model"] = md
                            model = _limit_exits.LimitModel.from_dict(md)
                            model.note_quote(px, now)
                            pos["limit_model"] = model.to_dict()
                        except Exception:
                            pass
                    if _cover is not None:
                        try:
                            dec = _cover.cover_decision(pos, px, now)
                            pos["cover_last"] = dec
                            if dec.get("action") != "HOLD":
                                cj = pos.setdefault("cover_journal", [])
                                cj.append({"ts": now,
                                           "action": dec.get("action"),
                                           "reason": dec.get("reason"),
                                           "mult": (dec.get("evidence")
                                                    or {}).get("mult")})
                                del cj[:-20]
                        except Exception:
                            pass
                    # VENDOR CHAIN (advanced build, piece 4,
                    # 2026-11-21): journal-only — which vendor
                    # priced this exit eval. q["src"] is the
                    # quote's own source stamp: "external" is the
                    # caller price_fn (rt_sync tape + armed-watch
                    # push), "jup-v3" is Jupiter Price v3, a
                    # "reserves:..." source is Helius reserves,
                    # "ds/gt" is quote_price() (DexScreener +
                    # tape). Never gates an exit.
                    if _vendors is not None:
                        try:
                            _src = str(q.get("src") or "")
                            _step = None
                            if _src == "external":
                                _step = "caller_price_fn"
                            elif _src == "jup-v3":
                                _step = "jupiter_v3"
                            elif _src.startswith("reserves:"):
                                _step = "helius_reserves"
                            elif _src.startswith("ds/gt"):
                                _step = "ds_gt"
                            if _step is not None:
                                _vendors.annotate(
                                    pos, "exit_prices", _step)
                        except Exception:
                            pass
                if px <= 0:
                    # F2 (2026-10-23): px<=0 here means every source
                    # is dark this cycle (armed positions probed the
                    # direct reserves first in phase 2; a caller
                    # price_fn already folds the tape + the pushed
                    # reserve price). An ARMED position dark for a
                    # sustained stretch exits at its last real
                    # observed price, stamped — instead of waiting
                    # for the first post-rug dust print. Never-armed
                    # positions keep the exact pre-F2 behavior below.
                    _f2_stamp = _f2.check(pos, now) \
                        if _f2 is not None else None
                    if _f2_stamp is not None:
                        pos["_f2"] = _f2_stamp
                        pos["last_price_source"] = "f2-last-real-price"
                        if _vendors is not None:
                            try:
                                _vendors.annotate(
                                    pos, "exit_prices",
                                    "f2_last_real_price")
                            except Exception:
                                pass
                        _close_remaining(
                            pos, _f2_stamp["last_real_price"],
                            _f2.F2_REASON)
                        closes.append(_finalize(
                            d, mint, pos, _f2.F2_REASON))
                        continue
                    # Unknown price: skip this cycle — but past the
                    # backstop an unquotable token is dead, so
                    # finalize at 0 (same rule as ceo/mirror; TWAP
                    # instances use their own 60min backstop here
                    # too, with its own reason, never the 24h label).
                    _hold_s = TWAP_MAX_HOLD_S \
                        if TWAP_SLICES is not None else TIME_STOP_H * 3600
                    _uq_reason = "twap_time_stop_60min_unquotable" \
                        if TWAP_SLICES is not None else \
                        "time_stop_24h_unquotable"
                    if now - pos["entry_ts"] > _hold_s:
                        pos["last_price_source"] = "unquotable"
                        _close_remaining(pos, 0.0, _uq_reason)
                        closes.append(_finalize(
                            d, mint, pos, _uq_reason))
                    continue
                entry = pos["entry_price"]
                mult = px / entry if entry > 0 else 0

                # Phantom-print guard 2 (2026-10-24): an absurd upside
                # print (>50x entry — the audit's ledger-sanity bound,
                # shared with f2.F2_MAX_LAST_MULT) is a bad data row,
                # not a moon. The 2026-10-24 phantom banked legs A/B as
                # "2x"/"3x" on a 21.7M-x print (1.027e-4 vs a 4.7e-12
                # fiction entry). Never bank a leg at it and never let
                # it corrupt the tracked peak: journal the refusal and
                # skip the cycle; the position retries next poll. (In a
                # genuine gradual moon legs A/B bank at 2x/3x long
                # before 50x; only a single-poll vertical gap — which is
                # indistinguishable from bad data — is affected.)
                _max_mult = getattr(_f2, "F2_MAX_LAST_MULT", 50.0) \
                    if _f2 is not None else 50.0
                if entry > 0 and mult > _max_mult:
                    _jq = pos.setdefault("quote_journal", [])
                    _jq.append({"ts": now, "px": px,
                               "src": q.get("src") or "",
                               "skipped": "absurd print %.1fx entry "
                               "(>%.0fx sanity bound, phantom guard)"
                               % (mult, _max_mult)})
                    del _jq[:-40]
                    logger.warning("grad %s: absurd print $%s (%.1fx "
                                   "entry) skipped (phantom guard)",
                                   (mint or "?")[:16], px, mult)
                    # Backstop (2026-11-08): a sustained absurd
                    # print is no more usable than no print at
                    # all — past the hold, finalize like
                    # unquotable instead of skipping forever.
                    # Two grad_select positions sat open 56-71h
                    # on absurd prints because this branch
                    # `continue`d past the 24h time stop below.
                    _hold_s = TWAP_MAX_HOLD_S \
                        if TWAP_SLICES is not None else TIME_STOP_H * 3600
                    _uq_reason = "twap_time_stop_60min_unquotable" \
                        if TWAP_SLICES is not None else \
                        "time_stop_24h_unquotable"
                    if now - pos["entry_ts"] > _hold_s:
                        _close_remaining(pos, 0.0, _uq_reason)
                        closes.append(_finalize(
                            d, mint, pos, _uq_reason))
                    else:
                        _write(d)
                    continue

                # F6 phantom-print guard (2026-10-23): a grad-lane
                # position is ALWAYS a graduated token — a bonding-
                # curve price is meaningless for it (the curve holds
                # no post-graduation state; a print from it is
                # fiction, as in the 2026-10-23 phantom). Never bank
                # a leg at it: journal the attempt and skip the
                # cycle; the position retries next poll.
                if str(q.get("src") or "").startswith("reserves:curve"):
                    _jq = pos.setdefault("quote_journal", [])
                    _jq.append({"ts": now, "px": px,
                                "src": q.get("src") or "",
                                "skipped": "curve print on graduated "
                                           "token (phantom guard)"})
                    del _jq[:-40]
                    logger.warning("grad %s: untradable curve print "
                                   "$%s skipped (phantom guard)",
                                   (mint or "?")[:16], px)
                    # Backstop (2026-11-08): same stuck-forever
                    # shape as the absurd-print branch — a
                    # sustained curve print is no more usable
                    # than no print at all, so past the hold
                    # finalize like unquotable instead of
                    # skipping forever. The guard itself is
                    # unchanged: while young it still banks no
                    # legs and still journals/skips.
                    _hold_s = TWAP_MAX_HOLD_S \
                        if TWAP_SLICES is not None else TIME_STOP_H * 3600
                    _uq_reason = "twap_time_stop_60min_unquotable" \
                        if TWAP_SLICES is not None else \
                        "time_stop_24h_unquotable"
                    if now - pos["entry_ts"] > _hold_s:
                        _close_remaining(pos, 0.0, _uq_reason)
                        closes.append(_finalize(
                            d, mint, pos, _uq_reason))
                    else:
                        _write(d)
                    continue

                # Position-level peak tracking (same as ceo/mirror):
                # fold the quote into the running peak BEFORE exits
                # are evaluated; pre-tracking positions are flagged
                # partial (their close reports a lower bound).
                if "peak_price" not in pos:
                    pos["peak_price"] = 0.0
                    pos["peak_partial"] = True
                if px > float(pos.get("peak_price") or 0):
                    pos["peak_price"] = px
                    pos["peak_ts"] = now  # F3: peak timing persists

                # Hard stop first: closes everything immediately. The
                # effective threshold is per-engine (see
                # _effective_stop): default -50%; PROTECT_AFTER_A
                # moves it to breakeven once leg A has banked.
                stop_mult, stop_reason = _effective_stop(pos)
                if mult <= stop_mult:
                    _close_remaining(pos, px, stop_reason)
                    closes.append(_finalize(d, mint, pos,
                                            stop_reason))
                    continue
                # TWAP EXIT (mimic_edge): the ladder below is
                # replaced by fixed-fraction time slices — one per
                # poll until the position is empty. The hard stop
                # above still guards the unsold remainder, and the
                # TWAP max-hold below replaces the 24h backstop
                # for these positions (a dead coin must not sit
                # 24h when the real exit finishes in minutes).
                if TWAP_SLICES is not None:
                    if TWAP_MAX_HOLD_S is not None and \
                            now - pos["entry_ts"] > TWAP_MAX_HOLD_S:
                        _close_remaining(pos, px,
                                         "twap_time_stop_60min")
                        closes.append(_finalize(
                            d, mint, pos, "twap_time_stop_60min"))
                        continue
                    tw = pos.setdefault("twap", {
                        "slices": TWAP_SLICES, "done": 0,
                        "sold_tokens": 0.0, "sold_usd": 0.0})
                    if tw["done"] >= TWAP_SLICES:
                        closes.append(_finalize(
                            d, mint, pos, "twap_complete"))
                        continue
                    # Exit rate: several slices per poll instead
                    # of one — a decaying newborn loses less when
                    # the position empties in ~80s than in ~5min
                    # (edge's TWAP completes averaged ~0.93x: the
                    # decay during the window ate the position).
                    # Below the acceleration multiple the fall is
                    # accelerating, so dump faster still; the -25%
                    # stop below still guards any remainder.
                    _per = TWAP_SLICES_PER_POLL or 1
                    if TWAP_ACCEL_BELOW_MULT is not None and \
                            TWAP_ACCEL_SLICES_PER_POLL and \
                            mult < TWAP_ACCEL_BELOW_MULT:
                        _per = TWAP_ACCEL_SLICES_PER_POLL
                    for _ in range(int(_per)):
                        if pos["twap"]["done"] >= TWAP_SLICES:
                            break
                        _close_twap_slice(pos, px)
                    if pos["twap"]["done"] >= TWAP_SLICES:
                        closes.append(_finalize(
                            d, mint, pos, "twap_complete"))
                    else:
                        _write(d)
                    continue
                # Time stop: 24h hygiene backstop.
                if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                    _close_remaining(pos, px, "time_stop_24h")
                    closes.append(_finalize(d, mint, pos,
                                            "time_stop_24h"))
                    continue
                # Leg A: 50% at 2x.
                la = pos["legs"]["a"]
                if not la["done"] and mult >= LEG_A_TARGET:
                    _close_leg(pos, "a", px, "leg_a_2x")
                # Leg B: 25% at 3x.
                lb = pos["legs"]["b"]
                if not lb["done"] and mult >= LEG_B_TARGET:
                    _close_leg(pos, "b", px, "leg_b_3x")
                # Leg C: 25% on 30% trailing stop from peak.
                lc = pos["legs"]["c"]
                if not lc["done"]:
                    if px > lc["peak"]:
                        lc["peak"] = px
                    elif px <= lc["peak"] * (1 - LEG_C_TRAIL):
                        _close_leg(pos, "c", px, "leg_c_trail_30pct")
                # All legs done -> finalize.
                if all(pos["legs"][k]["done"] for k in ("a", "b", "c")):
                    closes.append(_finalize(d, mint, pos,
                                            "ladder_complete"))
                else:
                    # a legacy position whose legs just sold on THIS poll is migrated in the same poll, so marks/gates
                    # never observe the deferred full-qty account position between polls
                    _migrate_legacy_tranches(pos)
                    _write(d)
            except Exception as e:
                logger.warning("grad poll_exits %s failed: %s", mint, e, exc_info=True)
                _tok = _call_failure_hook(_FAILURE_BEGIN_HOOK, str(mint), repr(e)[:200])     # write-ahead marker BEFORE the restore I/O (K1 window)
                _restored = _persist_poll_failure(d, mint, pos, e)
                if _restored:
                    _call_failure_hook(_FAILURE_END_HOOK, str(mint), _tok)
                if not _restored:
                    # the in-memory ledger may hold half-mutated state and could not be replaced by a fresh persisted view: FAIL CLOSED,
                    # abort the rest of this cycle so no later write can persist it. Positions not yet visited are retried next poll.
                    _POLL_ABORTS["count"] += 1; _POLL_ABORTS.update(last_ts=time.time(), last_mint=str(mint), last_error=repr(e)[:200])
                    if _ABORT_HOOK is not None:
                        try:
                            _ABORT_HOOK(str(mint), repr(e)[:200])
                        except Exception:
                            pass
                    _warn_throttled(("poll_abort", str(mint)), "grad poll_exits: cycle ABORTED after %s failed and its failure state could not be restored; no further writes this cycle (aborts so far: %d; see recon_health poll_aborts)", str(mint)[:12], _POLL_ABORTS["count"])
                    break
                continue
        return [c for c in closes if c]  # None = settlement pending (position kept open)


def open_positions():
    """{mint: position} currently open. Never raises."""
    try:
        return _load()["positions"]
    except Exception:
        return {}
