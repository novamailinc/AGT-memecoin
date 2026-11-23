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

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
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


class _ledger_lock:
    """Exclusive inter-process lock spanning a read-modify-write section."""

    def __enter__(self):
        self._lf = open(_lock_path(), "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
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
        logger.warning("grad ledger created: bankroll $%.2f, verdict "
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
        if float(d["bankroll"]) < _stake:
            return _skip(mint, "bankroll exhausted (cash $%.2f)"
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
        d = _read()
        if float(d["bankroll"]) < _stake:
            if d["state"] != "bankroll_exhausted":
                d["state"] = "bankroll_exhausted"
                _write(d)
            return _skip(mint, "bankroll exhausted (cash $%.2f)"
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
            _qty = _stake / float(pos["entry_price"])
            # LANE (not a literal) keys the op_id: engine-loaded lane
            # variants (grad_raw, grad_select, tranche, ...) run this
            # same code under their own LANE override, and two lanes
            # entering the SAME pool must never collapse onto one
            # op_id — the retry dedupe would hand the second lane the
            # first lane's outcome without debiting.
            _op = "%s:entry:%s:%d" % (LANE, mint, int(_born * 1000))
            _ok, _why, _pid = _pa.try_debit(LANE, mint, _stake, _qty,
                                            _op)
            if not _ok:
                return _skip(mint, "paper account: " + _why, False)
            # Pin the open instance so the close can never settle
            # a later reopen of the same mint.
            pos["pa_pos_id"] = _pid
        d["bankroll"] = round(float(d["bankroll"]) - _stake, 6)
        d["positions"][mint] = pos
        d["watchlist"].pop(mint, None)
        _write(d)
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
    logger.warning("grad OPEN %s %s @ %s ($1, pool age %.0fs, liq $%.0f, "
                   "mcap $%.0f, cash now $%.2f)",
                   symbol, mint[:12], filled, age, liquidity, mcap,
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


def _close_leg(pos, leg_key, exit_price, reason, pkind=None):
    """Close one leg at an honest exit. Returns realized USD for the leg."""
    leg = pos["legs"][leg_key]
    if leg["done"]:
        return 0.0
    leg_tokens = pos["tokens"] * leg["frac"]
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
    leg.update(done=True, exit_price=filled_exit,
               realized_usd=round(net, 6), exit_reason=reason,
               exit_price_kind=_kind)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


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
    net = gross - gas_out
    tw["done"] += 1
    if _pk is not None:
        _pks = set(tw.get("price_kinds") or [])
        _pks.add(_pk.CURRENT)
        tw["price_kinds"] = sorted(_pks)
    tw["sold_tokens"] = round(tw["sold_tokens"] + slice_tokens, 6)
    tw["sold_usd"] = round(tw["sold_usd"] + net, 6)
    tw["last_exit_price"] = filled_exit
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _close_remaining(pos, exit_price, reason, pkind=None):
    """Close all remaining legs as ONE combined exit (one tx cost) —
    same rule as ceo.py/mirror.py."""
    if pos.get("twap") is not None:
        # TWAP positions never touch the ladder legs: close ONLY
        # the tokens the slices have not sold yet, as one exit.
        # (Using the legs here would re-sell already-sold tokens.)
        tw = pos["twap"]
        remaining = pos["tokens"] - float(tw.get("sold_tokens") or 0)
        if remaining <= 0:
            return 0.0
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
    if not open_legs:
        return 0.0
    total_tokens = sum(pos["tokens"] * pos["legs"][k]["frac"]
                       for k in open_legs)
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
    d["verdict"] = {
        "ts": time.time(),
        "n_closes": len(closes),
        "expectancy": round(exp, 6),
        "net_usd": round(sum(float(c.get("pnl_usd") or 0)
                             for c in closes), 4),
        "action": ("HALT — sample expectancy <= 0" if halt else
                   "SURVIVED — sample expectancy > 0"),
        "sample_closes": VERDICT_SAMPLE_CLOSES,
    }
    d["verdict"].update(_verdict_visibility(closes))
    logger.warning("grad VERDICT at %d closes: expectancy %.4f -> %s",
                   len(closes), exp, d["verdict"]["action"])
    return halt


def _finalize(d, mint, pos, closed_reason):
    """Move an open position to closes, settle cash, record the lane
    trade, evaluate the pre-registered verdict. Must be called with
    _ledger_lock held; writes via _write."""
    _recover_close_intents()
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
                      "amount": realized, "qty": _sold_qty,
                      "price_kind": _sold_kind, "pos_id": _pos_id},
                     {"kind": "write_down", "op_id": _op + ":wd",
                      "mint": mint, "pos_id": _pos_id,
                      "reason": closed_reason}]
        else:
            # Full close (qty=None settles all remaining units).
            # A non-current aggregate kind routes the WHOLE credit
            # to the quarantined estimated bucket.
            _plan = [{"kind": "credit", "op_id": _op, "mint": mint,
                      "amount": realized, "qty": None,
                      "price_kind": _close_kind,
                      "pos_id": _pos_id}]
        _ci = _close_intents()
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
            return closes
    except Exception as e:
        logger.warning("grad poll_time_exits failed: %s", e)
        return []


def poll_exits(price_fn=None):
    """Evaluate exits for all open grad positions. price_fn(chain,
    mint, spot_index) -> float override for tests / rt_sync. Returns
    close records. Never raises."""
    try:
        return _poll_exits(price_fn)
    except Exception as e:
        logger.warning("grad poll_exits failed: %s", e)
        return []


def _poll_exits(price_fn):
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
    if _jup is not None:
        try:
            jup_prices = _jup.get_prices(
                [m for m, p in snapshot.items()
                 if (p.get("chain") or "solana") == "solana"]) or {}
        except Exception:
            jup_prices = {}
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
            elif mint in jup_prices:
                q["px"] = jup_prices[mint]
                q["src"] = _jup.SOURCE
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
        for mint, pos in list(d["positions"].items()):
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
                               "src": q.get("src") or ""})
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
                    _write(d)
            except Exception as e:
                logger.warning("grad poll_exits %s failed: %s", mint, e)
                continue
        return closes


def open_positions():
    """{mint: position} currently open. Never raises."""
    try:
        return _load()["positions"]
    except Exception:
        return {}
