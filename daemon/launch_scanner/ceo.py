#!/usr/bin/env python3
"""CEO account (2026-10-20): a $100 paper bankroll with real-money discipline.

The user handed the bot a $100 account and CEO authority for two months:
trade the fired-card bus like it's real money, because this ledger is the
evidence base for any future live-mode decision. So the discipline is
real even though the money is paper:

- Bankroll starts at exactly $100.00. NO resets, NO top-ups, ever.
- Every entry is exactly $1.00 of bankroll (the user's $1/shot rule),
  debited from cash at entry; leg proceeds return to cash only when the
  position fully closes (cash settles at finalize).
- If cash < $1.00 the account stops entering: state "bankroll_exhausted".
  Losses are final — the record is never rewritten.
- Honest fills (fills.py) and per-chain tx costs (chain_costs.py) are
  charged exactly like the mirror, including the pre-registered 25%
  cost gate — Ethereum is COST-BLOCKED at $1 notional, and that is a
  finding of this account, not a bug.
- Exits are the user's ladder, identical constants to mirror.py:
  50% @ 2.0x, 25% @ 3.0x, final 25% on a 30% trailing stop, -50% hard
  stop, 24h time stop (unquotable past 24h finalizes at 0).

Entries come from the signal bus (signal_bus.py schema v1): every card
fired by any feed publishes there. The daemon tails the bus from a
byte-offset watermark persisted in the ledger, plus a bounded set of
processed line hashes so a bus rotation (the bus is bounded and
rewritten by publish()) can never replay an old card into a new entry.

Killswitch lane "ceo": pre-registered halt — negative rolling expectancy
after 15 closes halts entries (same rule as the mirror lanes). A halt
stops entries only; open positions keep exiting and the bus is untouched.

Paper only. Nothing here touches real money, keys, or wallets.
Free-only: stdlib HTTP (DexScreener, Hyperliquid public APIs).
"""
import copy
import hashlib
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.ceo")

try:
    from daemon.launch_scanner import fills as _fills
    from daemon.launch_scanner import risk_controls as _risk
    from daemon.launch_scanner import chain_costs as _costs
    from daemon.launch_scanner import killswitch as _ks
    from daemon.launch_scanner import veto as _veto
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import signal_bus as _bus
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
        import signal_bus as _bus
        import sources as _sources
        _HAS_DEPS = True
    except Exception:
        _HAS_DEPS = False

# Tamper-evident verdict ledger (2026-10-23): fail-soft import —
# journaling must never break trading.
try:
    from daemon.launch_scanner import verdict_ledger as _verdict_ledger
except Exception:
    try:
        import verdict_ledger as _verdict_ledger  # type: ignore
    except Exception:
        _verdict_ledger = None  # type: ignore

# F2 sustained-unquotability exit (2026-10-23, user-approved) +
# the direct-reserve cross-probe it needs: fail-soft imports — a
# missing module leaves the pre-F2 behavior byte-identical.
try:
    from daemon.launch_scanner import f2 as _f2
    from daemon.launch_scanner import reserves as _reserves
except Exception:
    try:
        import f2 as _f2  # type: ignore
        import reserves as _reserves  # type: ignore
    except Exception:
        _f2 = None  # type: ignore
        _reserves = None  # type: ignore

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

# Decision memory (2026-11-17): fail-soft import — a missing
# module changes nothing. Lessons are advisory context for CEO
# auto-tune only: ``lessons_context(lane)`` returns the top
# lessons as a text block. They NEVER gate entries or exits.
try:
    from daemon.launch_scanner import lessons as _lessons
except Exception:
    try:
        import lessons as _lessons  # type: ignore
    except Exception:
        _lessons = None  # type: ignore


def lessons_context(lane="grad_select", n=5):
    """Top decision-memory lessons for ``lane`` as a text block,
    for CEO auto-tune to read. Advisory only — never a filter,
    never a gate. Never raises."""
    try:
        if _lessons is not None:
            return _lessons.context_block(lane, n=n)
    except Exception:
        pass
    return "Decision-memory lessons for %s: unavailable." % lane


def _verdict_event(lane, kind, payload):
    """Append one verdict-ledger record. Never raises. The chain path
    is derived from this module's LEDGER_PATH directory (== HOME in
    production; a temp ledger in tests isolates the chain with it)."""
    try:
        if _verdict_ledger is not None:
            _p = os.path.join(
                os.path.dirname(os.path.abspath(LEDGER_PATH)),
                "verdict_ledger", "events.jsonl")
            _verdict_ledger.append(lane, kind, payload, path=_p)
    except Exception:
        pass


HOME = (os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser("~/.forex-agent"))
LEDGER_PATH = os.path.join(HOME, "ceo_ledger.json")
BUS_PATH = os.path.join(HOME, "launch_scanner", "signal_bus.jsonl")
MAX_CLOSES = 500
SEEN_TTL_S = 72 * 3600  # processed-line hashes kept 72h (bus holds ~days)

BANKROLL_START = 100.00
NOTIONAL_USD = 1.00       # the user's $1/shot rule


def _paper_account():
    """The shared cross-lane paper account (2026-11-22), or None
    when disabled. Same opt-in and fail-open-import semantics as
    grad's: PAPER_ACCOUNT_ENABLED=1 makes the shared account the
    authoritative capital gate for entries and the settlement
    target for exits; default OFF preserves historical behavior.
    A defensive import failure disables the gate for the call
    rather than breaking the lane."""
    if os.environ.get("PAPER_ACCOUNT_ENABLED") != "1":
        return None
    try:
        # Absolute with sibling fallback (see grad's note): engine
        # loader variants exec lane modules outside the package.
        try:
            from daemon.launch_scanner import paper_account
        except ImportError:
            import paper_account
        return paper_account
    except Exception:
        logger.exception("ceo: paper account unavailable; shared "
                         "gate disabled for this call")
        return None


def _close_intents():
    """The cross-ledger close-intent journal (Stage C, 2026-11-22),
    or None when the shared account is off. Same engine-loading
    caveats as _paper_account."""
    if _paper_account() is None:
        return None
    try:
        try:
            from daemon.launch_scanner import close_intents
        except ImportError:
            import close_intents
        return close_intents
    except Exception:
        logger.exception("ceo: close-intent journal unavailable")
        return None


# Intents whose account side is durable but whose lane-ledger write
# has not yet been confirmed; tuples (intent_id, pos_id, mint,
# suffix). _write flushes one only when the persist actually
# carries its tranche/close.
_PENDING_CLOSE_INTENTS = []
_RECOVERY_DONE = False
# A FROZEN intent blocks every entry (parent review 2026-11-22):
# contradictory lane/account capital must not fund new risk.
_ENTRY_BLOCKED = False


def _block_entries():
    global _ENTRY_BLOCKED
    _ENTRY_BLOCKED = True


_GEN_REFUSED = []   # reasons of generation refusals raised during the CURRENT close attempt (see _preserve_unresolved)


def _preserve_unresolved(fn):
    """A live account generation refusal must leave the position UNRESOLVED: the lane does not book its own close over an account it could not settle.
    The pre-attempt position is restored in place and marked, so the caller persists an OPEN position (which stays visibly unresolved) and returns 0.0.
    The durable account marker + frozen intent block entries."""
    def wrapper(pos, *a, **k):
        _saved = copy.deepcopy(pos)
        del _GEN_REFUSED[:]
        r = fn(pos, *a, **k)
        if _GEN_REFUSED:
            _why = _GEN_REFUSED[0]
            del _GEN_REFUSED[:]
            pos.clear()
            pos.update(_saved)
            pos["pa_settle_refused"] = _why
            return 0.0
        return r
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    return wrapper


def _live_refusal(pend, why, mint):
    """LIVE account generation refusal (journaled by the account): block entries IN-PROCESS now and freeze the intent durably when the close-intent journal is
    available. The account side stays unresolved; nothing is credited, rebound or compensated. With the journal unavailable only the in-process block exists."""
    if "generation refused" not in str(why):
        return
    _GEN_REFUSED.append(str(why)[:140])
    _block_entries()
    _frozen = False
    try:
        _ci = _close_intents()
        if _ci is not None and pend is not None:
            _frozen = bool(_ci.freeze(LANE, pend[0], "live account generation refusal: " + str(why)[:140]))
    except Exception:
        logger.exception("%s: could not freeze intent after a live generation refusal for %s", LANE, mint)
    logger.error("%s: LIVE account generation refusal for %s (%s): ENTRIES BLOCKED in-process; intent frozen=%s (False = journal unavailable or freeze failed; "
                 "no account credit was applied)", LANE, mint, str(why)[:140], _frozen)


def _unresolved_positions(_lane=None):
    """Restart/entry admission evidence from PERSISTED state (shared account generation vs the lane ledger, lane ledger availability). Falls back to the
    unbound-only scan when the shared account is disabled. See paper_account.restart_admission_blockers."""
    _pa = _paper_account()
    _ln = _lane if _lane is not None else LANE
    if _pa is not None and hasattr(_pa, "restart_admission_blockers"):
        _ij = None
        try:
            from daemon.launch_scanner import close_intents as _ci
        except ImportError:
            import close_intents as _ci
        try:
            _ij = _ci.journal_path(_ln)
        except Exception:
            _ij = None
        _ok = _pa.register_lane(_ln, LEDGER_PATH, _ij)
        _b = list(_pa.restart_admission_blockers(_ln, LEDGER_PATH))
        if not _ok:
            _b.append("lane-registry-unwritable")
        return _b
    return [str(m) for m, p in ((_read().get("positions") or {}).items()) if isinstance(p, dict) and p.get("lane", _ln) == _ln and p.get("pa_pos_id") and not p.get("pa_account_id")]


def _recover_close_intents():
    """Re-drive incomplete close intents once per process, before
    this process's first entry/close. Account ops re-apply
    idempotently (op_id dedupe); a still-open position is left to
    its natural re-fire; a missing tranche on a closed position is
    NOT reconstructed (operator territory) - it freezes."""
    global _RECOVERY_DONE, _ENTRY_BLOCKED
    _ci = _close_intents()
    if _ci is None:
        _RECOVERY_DONE = True
        # Close-intent journal OFF: no intent exists to freeze (none is claimed). The unbound-position block does not depend on it: it is derived from the
        # persisted lane ledger in every process, so entries stay blocked while any account-opened position has no bound generation.
        try:
            _unb0 = _unresolved_positions()
        except Exception:
            _unb0 = ["unreadable-lane-ledger"]
        if _unb0:
            _block_entries()
            logger.error("ceo: close-intent journal OFF; open position(s) with NO bound account generation %s: ENTRIES BLOCKED (no intent freeze exists)", ",".join(_unb0))
            return True
        return False
    if _RECOVERY_DONE:
        # Not once-and-forget: re-run while the journal is unreadable or any intent is still pending (a torn tail that appeared later, an
        # intent left for the natural re-close). A clean, fully-terminal journal short-circuits.
        try:
            if (_ci._read(_ci.journal_path(LANE))[1] is None
                    and not _ci.pending(LANE)):
                return _ENTRY_BLOCKED
        except Exception:
            pass
    _RECOVERY_DONE = True

    def account_apply(op):
        _pa = _paper_account()
        if _pa is None:
            return False, "paper account disabled"
        if op.get("kind") == "write_down":
            ok, why = _pa.write_down(
                LANE, op.get("mint"), op.get("op_id"),
                pos_id=op.get("pos_id"), qty=op.get("qty"),
                reason=op.get("reason") or "recovered",
                expected_account_id=op.get("expected_account_id"), entry_op_id=op.get("entry_op_id"))
        else:
            ok, why = _pa.credit(
                LANE, op.get("mint"), op.get("amount"),
                op.get("op_id"), pos_id=op.get("pos_id"),
                qty=op.get("qty"), fee=op.get("fee") or 0.0,
                price_kind=op.get("price_kind"),
                expected_account_id=op.get("expected_account_id"), entry_op_id=op.get("entry_op_id"))
        if not ok and _pa.op_outcome(op.get("op_id")) == "orphan" \
                and not _pa.pos_ever_existed(
                    pos_id=op.get("pos_id"), lane=LANE,
                    mint=op.get("mint")):
            return True, "orphan (converged, epoch-proven)"
        return ok, why

    def lane_has_close(intent):
        info = intent.get("close_info") or {}
        return _ci.tranche_lane_done(_read(), intent.get("pos_id"),
                                     intent.get("mint"),
                                     info.get("leg_suffix"))

    def lane_apply(intent):
        # An open position re-fires the tranche naturally (account
        # dedupes by op_id). A tranche lost from a CLOSED position
        # is genuine divergence: never reconstruct it silently.
        d = _read()
        pos = (d.get("positions") or {}).get(intent.get("mint"))
        if pos is not None and (
                not intent.get("pos_id")
                or pos.get("pa_pos_id") == intent.get("pos_id")):
            return None
        return False

    frozen = _ci.recover_lane(LANE, account_apply, lane_has_close,
                              lane_apply)
    if not frozen:
        _un = _pending_unusable()
        if _un:
            for _iid, _w in _un:
                logger.error("ceo: pending intent %s has an unusable recorded op (%s): freezing, ENTRIES BLOCKED", _iid, _w)
                _fzok = False
                try:
                    if _iid:
                        _fzok = bool(_ci.freeze(LANE, _iid, "unusable recorded op: " + _w))   # best effort; the block below does not depend on this write
                except Exception:
                    logger.exception("ceo: could not freeze intent %s", _iid)
                if not _fzok:
                    logger.error("ceo: freeze event for %s NOT written; entries stay blocked from the durable record alone", _iid)
            frozen = [str(i or "unreadable-pending") for i, _w in _un]
    try:
        _unb = _unresolved_positions()
    except Exception:
        _unb = ["unreadable-lane-ledger"]
    if _unb:
        frozen = list(frozen) + ["unbound-position:" + m for m in _unb]
        logger.error("ceo: open position(s) with NO bound account generation %s: ENTRIES BLOCKED (no automatic rebinding)" % ",".join(_unb))
    if frozen:
        _ENTRY_BLOCKED = True
        logger.error("ceo: %d close intent(s) FROZEN - ledgers "
                     "diverge, ENTRIES BLOCKED until operator "
                     "resolution: %s", len(frozen), ",".join(frozen))
    return _ENTRY_BLOCKED


def _intent_open(lane_suffix, pos, ops, reason):
    """begin() + return the pending tuple, or None when the journal
    is off."""
    _ci = _close_intents()
    if _ci is None:
        return None
    mint = pos["mint"]
    pa_pos_id = pos.get("pa_pos_id")
    iid = _ci.begin(LANE, mint, pa_pos_id, ops,
                    {"realized_usd": ops[0].get("amount") or 0.0,
                     "closed_reason": reason,
                     "close_ts": time.time(),
                     "leg_suffix": lane_suffix},
                    account_id=pos.get("pa_account_id"))
    return (iid, pa_pos_id, mint, lane_suffix)


def _intent_gate(pos, key):
    """FAIL CLOSED before anything is sold or booked: when the shared account is on for this position and the close-intent journal cannot be
    written, the leg/close is NOT executed (no sale, no lane close/bankroll/verdict, no account op). The position shows pending_exit
    close_intent_not_durable and the exit is retried on later polls, once storage returns."""
    if not pos.get("pa_pos_id"):
        return True
    _ci = _close_intents()
    if _ci is None:
        return True
    try:
        _ok = _ci.journal_writable(LANE)
    except Exception:
        _ok = False
    if _ok:
        return True
    logger.error("ceo: close intent journal NOT writable: exit %s not executed (pending_exit close_intent_not_durable), retried next poll", key)
    _hold_not_durable(pos, key)
    return False


def _pa_settle(pos, qty, gross, fee, suffix, price_kind=None):
    """Credit the shared account for one settlement tranche, when
    this position was opened through it. `qty` units sold for
    `gross` paper dollars with `fee` in fees; the account books
    net proceeds immediately and allocates basis average-cost, so
    partial legs shrink the position's remaining units and basis
    while their receipts land in shared cash (never held outside
    the account, never double-counted at finalize). The op_id is
    stable per tranche (pinned position instance + suffix), so a
    retried close after an uncertain response cannot double-credit.
    Positions without a pinned pa_pos_id (opened with the flag
    off) are skipped: their stake never left the account, and a
    lane+mint match could settle a LATER reopen of the same mint
    after the 24h cooldown. Failures are journaled by the account
    module and never block the lane's own settle."""
    pa_pos_id = pos.get("pa_pos_id")
    if not pa_pos_id:
        return
    _pa = _paper_account()
    if _pa is None:
        return
    mint = pos["mint"]
    _op = "%s:%s:%s:%s" % (LANE, suffix, mint, pa_pos_id)
    # Stage C: durable intent BEFORE the account op.
    # closed_reason is the TRIGGER reason of this close (the guard's exit meta), recorded durably with the intent so a retry under a different trigger can
    # keep the original provenance. Falls back to the op suffix (the legacy value) when no trigger reason is available.
    _pend = _intent_open(suffix, pos,
                         [{"kind": "credit", "op_id": _op,
                           "mint": mint, "amount": gross, "qty": qty,
                           "fee": fee, "price_kind": price_kind,
                           "pos_id": pa_pos_id,
                           "expected_account_id": pos.get("pa_account_id"), "entry_op_id": pos.get("pa_entry_op")}],
                         (pos.get("_exit_meta") or {}).get("reason") or suffix)
    _ok, _why = _pa.credit(LANE, mint, gross, _op, pos_id=pa_pos_id,
                           qty=qty, fee=fee, price_kind=price_kind,
                           expected_account_id=pos.get("pa_account_id"), entry_op_id=pos.get("pa_entry_op"))
    if not _ok and _pa.op_outcome(_op) == "orphan" \
            and not _pa.pos_ever_existed(pos_id=pa_pos_id,
                                         lane=LANE, mint=mint):
        _ok, _why = True, ""
    if _ok:
        if _pend is not None:
            _ci2 = _close_intents()
            _ci2.mark_account_applied(LANE, _pend[0])
            _PENDING_CLOSE_INTENTS.append(_pend)
    else:
        _live_refusal(_pend, _why, pos.get("mint"))
        logger.warning("ceo: shared paper account credit for %s "
                       "(%s) failed: %s (journaled, intent open "
                       "for recovery)", mint, suffix, _why)
def _pa_write_down(pos, reason):
    """Move the shared-account position to the unresolved register
    for a zero-price write-down (design v4 Stage B): the units were
    never sold, so there is no fee and no proceeds - the account
    keeps the original cost as held_unresolved instead of refusing
    a zero-gross credit and diverging from the lane. Same pinning
    and failure rules as _pa_settle."""
    pa_pos_id = pos.get("pa_pos_id")
    if not pa_pos_id:
        return
    _pa = _paper_account()
    if _pa is None:
        return
    mint = pos["mint"]
    _op = "%s:writedown:%s:%s" % (LANE, mint, pa_pos_id)
    _pend = _intent_open("writedown", pos,
                         [{"kind": "write_down", "op_id": _op,
                           "mint": mint, "pos_id": pa_pos_id,
                           "reason": reason,
                           "expected_account_id": pos.get("pa_account_id"), "entry_op_id": pos.get("pa_entry_op")}], reason)
    _ok, _why = _pa.write_down(LANE, mint, _op, pos_id=pa_pos_id,
                               reason=reason,
                               expected_account_id=pos.get("pa_account_id"), entry_op_id=pos.get("pa_entry_op"))
    if not _ok and _pa.op_outcome(_op) == "orphan" \
            and not _pa.pos_ever_existed(pos_id=pa_pos_id,
                                         lane=LANE, mint=mint):
        _ok, _why = True, ""
    if _ok:
        if _pend is not None:
            _ci2 = _close_intents()
            _ci2.mark_account_applied(LANE, _pend[0])
            _PENDING_CLOSE_INTENTS.append(_pend)
    else:
        _live_refusal(_pend, _why, pos.get("mint"))
        logger.warning("paper account write-down for %s failed: "
                       "%s (journaled, intent open for recovery)",
                       mint, _why)

MAX_OPEN = 50              # bankroll binds first by design (see header)
LANE = "watcher"

MAX_OPEN = 10
COOLDOWN_S = 24 * 3600    # a closed mint rests 24h before re-entry
LANE = "ceo"

# The user's ladder — identical constants to mirror.py (post-revert).
LEG_A_FRAC, LEG_A_TARGET = 0.50, 2.0   # sell half at 2x
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0   # half the remainder at 3x
LEG_C_FRAC, LEG_C_TRAIL = 0.25, 0.30   # final quarter on 30% trailing stop
HARD_STOP_MULT = 0.50                  # hard exit at -50%
TIME_STOP_H = 24                       # hygiene backstop

CHAINS = ("solana", "base", "ethereum", "hyperliquid")
DS_CHAIN = {"solana": "solana", "base": "base", "ethereum": "ethereum"}
HL_INFO = "https://api.hyperliquid.xyz/info"

# Farmed-pack identification (CEO review 2026-10-21): the 6-wallet
# single-operator pack in the smart-wallet registry (labels
# grad-sniper-1/2/3, pack-sniper-1/2/3) sourced 13 of the CEO account's
# first 17 entries, and every one of those coins died 10-30 minutes
# after the card fired (registry record for all six wallets: 0 wins).
# This began as a hard exclusion; the user's order later the same day
# ("trade the highest signal") superseded it — KIMCHI was a pack card
# AND the day's top signal. The pack set is now used for LOGGING only:
# the entry decision belongs to the runner gate (_runner_gate), which
# judges the live tape instead of the wallet name.
_PACK_PREFIXES = frozenset(
    {"Fn9n3f", "Fs9RN3", "27HFmP", "3KcPSJ", "5zWxvq", "7HVGmP"})


def _is_pack_convergence(sig):
    """True when a smart-watcher convergence card names a pack wallet.

    smart-watcher publishes evidence.wallet_names as a comma-joined string
    of 6-char wallet prefixes (scripts/smart-watcher). Never raises.
    """
    try:
        ev = sig.get("evidence") or {}
        names = str(ev.get("wallet_names") or "")
        parts = {p.strip() for p in names.replace(",", " ").split()}
        return bool(parts & _PACK_PREFIXES)
    except Exception:
        return False


# Runner gate at entry (user order 2026-10-21, supersedes the pack
# exclusion above as the ENTRY rule): the CEO account's day-1 bleed came
# from entering convergence cards at birth size — the coins rugged in
# minutes, before any exit rung could matter. The pack exclusion tried
# to fix that by wallet name, but it also blocks the pack's genuine
# runners (KIMCHI: pack-bought at graduation, carded at $6.05M mcap /
# ~$320k liq, +2.3x after the card) — and the user's standing order is
# to trade the highest signals. The gate judges the TAPE, not the name:
# a Solana card is entered only when the LIVE tape at entry moment is
# runner-grade per the standing rule ($25k liq / $50k mcap / rising
# 5m). Pack cards that reach runner grade are admitted and logged, so
# the ledger can judge pack-at-grade on its own record.
RUNNER_MIN_LIQ_USD = 25_000.0
RUNNER_MIN_MCAP_USD = 50_000.0


_LAST_TAPE_SOURCE = ""  # venue of the last _fresh_tape read, for logs
_LAST_QUOTE_STAMP = None  # request-start/receipt of the last priced quote
_LAST_GATE_TAPE = None  # last runner-gate tape read (journaling only)


def _fresh_tape(chain, mint):
    """Fresh tape stats dict for the entry gate (DexScreener, with a
    GeckoTerminal fallback per mint when DS is dark). None = unknown.
    The returned dict carries ``tape_source``; the last source is also
    journaled in _LAST_TAPE_SOURCE for skip/open log lines."""
    global _LAST_TAPE_SOURCE
    try:
        stats = _sources.tape_batch_stats([mint],
                                          chain=chain or "solana")
        st = (stats or {}).get(mint) or None
        _LAST_TAPE_SOURCE = str((st or {}).get("tape_source") or "")
        return st
    except Exception:
        _LAST_TAPE_SOURCE = ""
        return None


# Consecutive confirmation (2026-10-21, warp-id pattern adopted from
# the GitHub ecosystem sweep): a single DexScreener print can be a
# stale or spike read — the pool that looks runner-grade on one fetch
# is mid-rug on the next. The gate therefore requires the tape to pass
# on RUNNER_CONFIRM_READS consecutive fresh reads, one rt_sync cadence
# apart. Fail-closed throughout: a degraded or unknown confirmation
# read blocks exactly like a failed first read.
RUNNER_CONFIRM_READS = 2
RUNNER_CONFIRM_GAP_S = 5.0


def _runner_gate_check(mint):
    """One runner-grade read of the live tape. Returns the bare failure
    reason ("" when the tape is runner-grade right now)."""
    global _LAST_GATE_TAPE
    st = _fresh_tape("solana", mint)
    # Journaling only: retain the tape this gate read so an entry can
    # stamp the REAL entry-time tape values onto its position. Some
    # card sources publish placeholder entry fields (smart-watcher
    # cards carry a constant score and no buys/sells/age at all), so
    # the tape the lane itself measured is the only real record.
    _LAST_GATE_TAPE = st if isinstance(st, dict) else None
    if not st:
        return "no live tape"
    liq = float(st.get("usd_liquidity") or 0)
    mcap = float(st.get("usd_market_cap") or 0)
    chg = float(st.get("chg_m5") or 0)
    if liq < RUNNER_MIN_LIQ_USD:
        return "liq $%.0f < $25k" % liq
    if mcap < RUNNER_MIN_MCAP_USD:
        return "mcap $%.0f < $50k" % mcap
    if chg <= 0:
        return "not rising (chg_m5 %.1f%%)" % chg
    return ""


def _runner_gate(chain, mint):
    """(ok, why). Fail-closed on unknown tape. Solana only: the bleed
    was Solana birth-size entries; other chains keep their existing
    gates (feeds enforce the rule at fire time, ETH is cost-blocked).

    Tradeoff, stated plainly: the confirmation read BLOCKS this caller
    for ~RUNNER_CONFIRM_GAP_S on the pass path. ceo-trader consumes
    sparse bus cards serially (~100/day), so the queue cost is nil and
    the +5s re-verification is the entire point. (A deferred/async
    confirm queue would restructure the bus loop for no gain here.)"""
    global _LAST_GATE_TAPE
    _LAST_GATE_TAPE = None  # journaling: only THIS card's gate reads count
    if chain != "solana":
        return True, ""
    why = _runner_gate_check(mint)
    if why:
        return False, "runner gate: " + why
    for _ in range(RUNNER_CONFIRM_READS - 1):
        time.sleep(RUNNER_CONFIRM_GAP_S)
        why = _runner_gate_check(mint)
        if why:
            via = (" [via %s]" % _LAST_TAPE_SOURCE) if _LAST_TAPE_SOURCE \
                else ""
            return False, "runner gate unconfirmed: " + why + via
    via = (" via %s" % _LAST_TAPE_SOURCE) if _LAST_TAPE_SOURCE else ""
    logger.warning("ceo runner gate confirmed x%d %s%s",
                   RUNNER_CONFIRM_READS, (mint or "?")[:16], via)
    return True, ""


def _gate_tape_stamp():
    """Journaling-only: the confirming runner-gate tape read as a
    compact entry_features sub-record — the REAL entry-time tape
    values (m5 buys/sells/volume, momentum, liquidity, mcap, venue)
    this lane itself measured when it decided to enter. Only fields
    the tape actually carries are stamped: the GeckoTerminal
    fallback rows carry None for the txn windows, and those are
    omitted, never zero-filled. None when no gate tape was read for
    this entry (non-Solana chains skip the gate)."""
    st = _LAST_GATE_TAPE
    if not isinstance(st, dict):
        return None
    out = {}
    for key, src in (("tape_source", "tape_source"),
                     ("liquidity", "usd_liquidity"),
                     ("mcap", "usd_market_cap"),
                     ("chg_m5", "chg_m5"),
                     ("buys_m5", "buys_m5"),
                     ("sells_m5", "sells_m5"),
                     ("vol_m5", "vol_m5")):
        v = st.get(src)
        if v is not None:
            out[key] = v
    return out or None


# ---- ledger -------------------------------------------------------------
# Same inter-process locking pattern as mirror.py (audit 2026-10-18):
# every read-modify-write section holds an exclusive flock on a lock file
# next to the ledger, and writes are atomic tmp+rename. The lock path is
# derived from LEDGER_PATH at call time so tests can isolate the ledger.

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


def _blank():
    return {
        "bankroll_start": BANKROLL_START,
        "bankroll": BANKROLL_START,
        "positions": {},
        "closes": [],
        "created_ts": time.time(),
        "state": "active",
        "watermark_offset": 0,
        "seen_cards": {},
    }


def _read():
    """Bare ledger read. Hold _ledger_lock if you will modify+write."""
    try:
        with open(LEDGER_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict):
            b = _blank()
            for k, v in b.items():
                d.setdefault(k, v)
            if not isinstance(d.get("positions"), dict):
                d["positions"] = {}
            if not isinstance(d.get("closes"), list):
                d["closes"] = []
            if not isinstance(d.get("seen_cards"), dict):
                d["seen_cards"] = {}
            return d
    except Exception:
        pass
    return _blank()


def _write(d):
    """Bare ledger write (atomic tmp+rename, fsync'd). Hold _ledger_lock.

    Flush+fsync the tmp file before the rename and fsync the
    directory after it (best-effort): this VM hard-resets, and an
    un-fsync'd rename can come back hollow/stale (2026-10-24
    grad_select ledger loss)."""
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
        # Stage C: flush lane-side intent marks ONLY for intents
        # whose tranche/close THIS persist actually carries.
        if _PENDING_CLOSE_INTENTS:
            _ciw = _close_intents()
            if _ciw is not None:
                _keep = []
                for _iid, _pid, _mint, _suffix in \
                        _PENDING_CLOSE_INTENTS:
                    if _ciw.tranche_lane_done(d, _pid, _mint,
                                              _suffix):
                        _ciw.mark_lane_applied(LANE, _iid)
                    else:
                        _keep.append((_iid, _pid, _mint, _suffix))
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
    """Create the ledger on first run. The watermark starts at the
    CURRENT bus end: the CEO account trades cards fired from now on,
    never the historical backlog. Returns the ledger dict."""
    with _ledger_lock():
        if os.path.exists(LEDGER_PATH):
            return _read()
        d = _blank()
        try:
            d["watermark_offset"] = os.path.getsize(BUS_PATH)
        except Exception:
            d["watermark_offset"] = 0
        _write(d)
        logger.warning("ceo ledger created: bankroll $%.2f, bus watermark "
                       "at byte %d", d["bankroll"], d["watermark_offset"])
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
            "created_ts": d["created_ts"],
        }
    except Exception:
        return {}


# ---- price feeds (same routes as mirror.py) ------------------------------

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


# Entry-quote repair (2026-10-21 autopsy): cards were passing the
# consecutive-confirmation runner gate and then skipping "no live
# quote" ~0.4s later (Grok in BOTH lanes, Speed in mirror — Speed's
# quote then worked 60s later in ceo). Two defects vs the gate's tape
# read (sources.dexscreener_batch_stats):
#   1. quote_price read pairs[0] blindly, while the gate selects the
#      HIGHEST-LIQUIDITY pair — a fresh graduation's first-listed pair
#      can be the stale/unpriced one.
#   2. _http_json is a single bare-urllib attempt (no retry, no curl),
#      while the gate's fetcher is curl-first with retries/backoff — a
#      transient 429/proxy drop was treated as a final "no quote".
# The quote path now selects pairs exactly like the gate, falls back
# to the gate's own tape read, and retries the pairs fetch once after
# a short gap. Every attempt is journaled into _LAST_QUOTE_DETAIL so a
# skip names what was tried and what each lookup returned.
QUOTE_RETRY_GAP_S = 2.0
_LAST_QUOTE_DETAIL = ""


def _ds_pairs_price(pairs):
    """(price, note): price of the highest-liquidity pair that carries
    one — the same selection rule as the gate's tape (sources.py)."""
    if not isinstance(pairs, list) or not pairs:
        return 0.0, "no pairs returned"
    best_px, best_liq, priced = 0.0, -1.0, 0
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
    if best_px > 0:
        return best_px, "%d pair(s), %d priced" % (len(pairs), priced)
    return 0.0, "%d pair(s), none priced" % len(pairs)


def _tape_quote_stamp(st):
    """Stamp for a quote priced from a tape row: only the row's own
    stamps (source_asof/fetched_at + received_at). Missing -> None, so
    the entry age gate refuses; never invented."""
    if not isinstance(st, dict):
        return None
    out = {}
    for k in ("source_asof", "fetched_at", "received_at"):
        v = st.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
    if "received_at" not in out or not (
            "source_asof" in out or "fetched_at" in out):
        return None
    return out


def _quote_detail(chain, mint, spot_index=None):
    """(price, trail): quote via, in order — best-pair selection on the
    tokens/v1 fetch, the gate's tape read as fallback, one bounded
    retry of the pairs fetch. Trail is plain-language, for skip logs."""
    global _LAST_QUOTE_DETAIL, _LAST_QUOTE_STAMP
    chain = (chain or "").lower()
    _qstart = time.time()
    _LAST_QUOTE_STAMP = None
    if chain == "hyperliquid":
        px = _hl_mark_price(spot_index)
        if px > 0:
            _LAST_QUOTE_STAMP = {"fetched_at": _qstart,
                                 "received_at": time.time()}
        _LAST_QUOTE_DETAIL = "hl mark price: %s" % (
            "ok" if px > 0 else "unavailable")
        return px, _LAST_QUOTE_DETAIL
    ds = DS_CHAIN.get(chain)
    if not ds:
        _LAST_QUOTE_DETAIL = "no dexscreener chain for %s" % chain
        return 0.0, _LAST_QUOTE_DETAIL
    # tokens/v1/<chain>/<mint> — the /latest/dex/tokens/ route drops
    # through this VM's egress proxy (same note as mirror.py).
    url = "https://api.dexscreener.com/tokens/v1/%s/%s" % (ds, mint)
    trail = []
    _rs = time.time()
    px, note = _ds_pairs_price(_http_json(url))
    if px > 0:
        # Stamp only from the observed request start/receipt (DexScreener
        # gives no source time); no price -> no stamp -> age gate refuses.
        _LAST_QUOTE_STAMP = {"fetched_at": _rs, "received_at": time.time()}
    trail.append("pairs fetch: " + note)
    if px <= 0:
        # Tape fallback: the runner gate just priced this mint through
        # sources.py (curl + retries, max-liquidity pair). If the tape
        # carries a price, the entry quote uses it — fills.py still
        # applies adverse slippage vs it, nothing is fabricated.
        st = _fresh_tape(chain, mint)
        tpx = float((st or {}).get("price_usd") or 0) if st else 0.0
        if tpx > 0:
            trail.append("tape fallback: priced via %s"
                         % ((st or {}).get("tape_source") or "unknown"))
            logger.warning("ceo quote via tape fallback %s",
                           (mint or "?")[:16])
            px = tpx
            _LAST_QUOTE_STAMP = _tape_quote_stamp(st)
        else:
            trail.append("tape fallback: "
                         + ("no tape" if not st else "tape unpriced"))
    if px <= 0:
        time.sleep(QUOTE_RETRY_GAP_S)
        _rs2 = time.time()
        px2, note2 = _ds_pairs_price(_http_json(url))
        trail.append("pairs retry: " + note2)
        if px2 > 0:
            px = px2
            _LAST_QUOTE_STAMP = {"fetched_at": _rs2,
                                 "received_at": time.time()}
    _LAST_QUOTE_DETAIL = "; ".join(trail)
    return px, _LAST_QUOTE_DETAIL


def quote_price(chain, mint, spot_index=None):
    """Live quoted price in USD. 0.0 only when NO source can price the
    mint (caller skips). Attempts are journaled in _LAST_QUOTE_DETAIL.
    Also used by exit polling: 0 still means 'unknown — skip this
    cycle', never a fabricated price."""
    try:
        return _quote_detail(chain, mint, spot_index)[0]
    except Exception:
        return 0.0


def _is_graduated_solana(pos):
    """True only for a Solana position known to be a graduated token.

    Jupiter Price v3 lists graduated (Raydium/PumpSwap) tokens; a
    pre-graduation bonding-curve token has no v3 entry, so v3 must
    never be tried for it — the Helius curve/reserves path stays
    the fallback there. Markers, in order: an explicit graduated
    flag, a graduation block in the entry snapshot (the grad lanes'
    shape), or a graduation source. Anything else is NOT treated
    as graduated and keeps the pre-Jupiter path byte-identical.
    """
    try:
        if (pos.get("chain") or "solana") != "solana":
            return False
        if pos.get("graduated") is True:
            return True
        snap = pos.get("entry_snapshot") or {}
        if isinstance(snap, dict) and snap.get("graduation"):
            return True
        return (pos.get("source") or "") == "graduation"
    except Exception:
        return False


def _hl_mark_price(spot_index):
    try:
        d = _http_json(HL_INFO, payload={"type": "spotMetaAndAssetCtxs"})
        if not isinstance(d, list) or len(d) < 2:
            return 0.0
        universe = (d[0] or {}).get("universe") or []
        ctxs = d[1] or []
        for u, c in zip(universe, ctxs):
            try:
                if int(u.get("index")) == int(spot_index):
                    return float((c or {}).get("markPx") or 0)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return 0.0


def _hl_all_marks():
    """{spot_index: markPx} for one poll cycle. Best effort."""
    out = {}
    try:
        d = _http_json(HL_INFO, payload={"type": "spotMetaAndAssetCtxs"})
        if not isinstance(d, list) or len(d) < 2:
            return out
        for u, c in zip((d[0] or {}).get("universe") or [], d[1] or []):
            try:
                out[str(int(u.get("index")))] = float(
                    (c or {}).get("markPx") or 0)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return out


# ---- entry ---------------------------------------------------------------

def _exit_fee_venue(pos):
    """Venue for the exit fee. The entry-stamped venue wins; a legacy
    position without one is charged the conservative pump maximum tier
    on solana (estimates may hurt, never help). None = the chain's own
    model (hyperliquid taker fee) or no venue kwarg."""
    v = pos.get("fee_venue")
    if v is None and (pos.get("chain") or "").lower() == "solana":
        return "pump"
    return v or None


def _veto_ok(chain, mint):
    """Re-verify hard vetoes at entry (same semantics as mirror.py):
    fail-open on exception (vetoes only on positive evidence),
    fail-CLOSED on unknown EVM safety."""
    try:
        if chain == "solana":
            # run_bundle=False: the card pipeline ran the full safety
            # suite seconds ago; re-running the RPC walk here would stall
            # on a slow (not failing) RPC (audit 2026-10-18).
            rep = _veto.veto_check(mint, "solana", run_bundle=False,
                                   lane="ceo")
            return bool(rep.get("passed")), "; ".join(rep.get("vetoes", []))
        if chain in ("base", "ethereum"):
            rep = _safety.check_token(chain, mint, timeout=10)
            if rep.get("unknown"):
                return False, "safety unknown at ceo entry"
            if not rep.get("passed"):
                return False, "; ".join(rep.get("fails", []))[:120]
            return True, ""
        if chain == "hyperliquid":
            return True, ""  # exchange-settled; no contract veto applies
        return False, "unsupported chain"
    except Exception as e:
        return True, "veto unavailable (%s)" % e


def _skip(mint, why):
    logger.warning("ceo SKIP %s: %s", (mint or "?")[:16], why)
    return False, why


def consider_card(sig):
    """Decide one bus card event. Opens a $1 position from the bankroll
    or logs the skip reason. Returns (opened, reason). Never raises."""
    try:
        return _consider_card(sig)
    except Exception as e:
        logger.warning("ceo consider_card failed: %s", e)
        return False, "ceo error: %s" % e


def _entry_notional():
    """Stake for a new entry: the owner-configured EXACT trade size when
    one is set and valid (shared paper account enabled); otherwise the
    lane's own NOTIONAL_USD (the account debit gate still refuses an
    invalid or conflicting configured size). Recorded on the position so
    exits compute PnL from the real notional."""
    _pa = _paper_account()
    if _pa is None:
        return NOTIONAL_USD
    try:
        v = _pa.configured_trade_size()
    except Exception:
        v = None
    return NOTIONAL_USD if v is None else float(v)


def _consider_card(sig):
    if not _HAS_DEPS:
        return _skip(sig.get("mint"), "ceo deps missing")
    chain = (sig.get("chain") or "").lower()
    mint = sig.get("mint") or ""
    source = sig.get("source") or "unknown"
    if chain not in CHAINS:
        return _skip(mint, "unsupported chain %s" % chain)
    if not mint:
        return _skip(mint, "no mint")

    # Killswitch halt stops entries only — never the bus, never exits.
    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state error.
    if not _ks.gate(LANE):
        return _skip(mint, "ceo lane halted by killswitch")

    # Runner gate (2026-10-21): Solana entries must be runner-grade on
    # the live tape RIGHT NOW — this replaces the farmed-pack exclusion
    # as the entry rule (see _runner_gate). Pack cards at runner grade
    # are admitted and logged for their own ledger record.
    ok_gate, why_gate = _runner_gate(chain, mint)
    # Immutable snapshot of THIS entry's gate row for the age gate; later
    # reads of the module global cannot substitute for it.
    _entry_row = (dict(_LAST_GATE_TAPE)
                  if isinstance(_LAST_GATE_TAPE, dict) else None)
    if not ok_gate:
        return _skip(mint, why_gate)
    if _is_pack_convergence(sig):
        logger.warning("ceo pack card admitted at runner grade: %s %s",
                       sig.get("symbol"), mint)

    # Fast dedupe/state checks on a locked snapshot (authoritative
    # re-check happens under the write lock below).
    _N = _entry_notional()
    with _ledger_lock():
        d = _read()
        state = d["state"]
        cash = float(d["bankroll"])
        if state == "bankroll_exhausted" or cash < _N:
            if state != "bankroll_exhausted":
                d["state"] = "bankroll_exhausted"
                _write(d)  # lock held here; the flip is final, like losses
            return _skip(mint, "bankroll exhausted (notional counter $%.2f)" % cash)
        if mint in d["positions"]:
            return _skip(mint, "already open")
        for c in d["closes"]:
            if c.get("mint") == mint and \
                    time.time() - float(c.get("close_ts") or 0) < COOLDOWN_S:
                return _skip(mint, "closed within 24h cooldown")
        if len(d["positions"]) >= MAX_OPEN:
            return _skip(mint, "max open positions (%d)" % MAX_OPEN)

    # Pre-registered cost gate (the productive ETH answer).
    blocked, why = _costs.cost_gate(chain, _N)
    if blocked:
        logger.warning("ceo %s: %s", mint, why)
        return False, why

    extra = sig.get("extra") or {}
    spot_index = extra.get("spot_index")
    global _LAST_QUOTE_STAMP
    _LAST_QUOTE_STAMP = None  # never reuse a previous entry's stamp
    quoted = quote_price(chain, mint, spot_index)
    if quoted <= 0:
        # HL listings carry their mark price on the card event.
        quoted = float(extra.get("mark_px") or 0)
    _ma = extra.get("mark_asof")  # age of the price actually used, or unknown
    if _LAST_QUOTE_STAMP is None and isinstance(_ma, (int, float)):
        _LAST_QUOTE_STAMP = {"source_asof": float(_ma),
                             "fetched_at": float(_ma),
                             "received_at": float(_ma)}
    _quote_stamp_snap = dict(_LAST_QUOTE_STAMP) if _LAST_QUOTE_STAMP else None
    if quoted <= 0:
        why = "no live quote"
        if _LAST_QUOTE_DETAIL:
            why = "no live quote (%s)" % _LAST_QUOTE_DETAIL
        return _skip(mint, why)

    ok, why = _veto_ok(chain, mint)
    if not ok:
        return _skip(mint, "veto at entry: %s" % why)

    # Venue fee grounding (2026-11-22 fee integration): solana entries
    # classify the venue from the runner gate's OWN confirmed tape
    # (_LAST_GATE_TAPE - no extra fetch, and the gate already verified
    # it fresh); hyperliquid uses its own taker-fee model; any other
    # chain or an unmodeled dex REFUSES the entry rather than silently
    # booking zero venue fees. Market cap / quote mint are not threaded
    # (no quote-lamport mcap in-lane), so the fee module charges its
    # maximum tier.
    if (chain or "").lower() == "solana":
        fee_venue, venue_why = _costs.lane_dex_venue(
            chain, (_LAST_GATE_TAPE or {}).get("dex"))
    else:
        fee_venue, venue_why = _costs.lane_dex_venue(chain, None)
    if fee_venue is None:
        return _skip(mint, venue_why)
    blocked, why = _costs.cost_gate(chain, _N,
                                    venue=fee_venue or None)
    if blocked:
        logger.warning("ceo %s: %s", mint, why)
        return False, why

    # The fill and the slippage gate use the lane's OWN gate-verified
    # tape liquidity, never the card's claim: a claimed value can be
    # absent (fail-open) or inflated (diluted gate). The runner gate
    # already refused no-tape candidates, so the verified figure
    # exists on every path that reaches here (2026-11-23).
    gate_liq = float((_LAST_GATE_TAPE or {}).get("usd_liquidity") or 0)
    liquidity = gate_liq or float(sig.get("liquidity") or 0)
    gas_in = _costs.tx_cost_usd(chain, _N,
                                venue=fee_venue or None)
    eff_notional = _N - gas_in
    if eff_notional <= 0:
        return _skip(mint, "costs exceed notional")

    # Declared slippage cap (fills.MAX_SLIPPAGE_PCT, tighten-only via
    # AGT_RISK_MAX_SLIPPAGE_PCT): a fill the model can only price at
    # the cap is not an honest entry. Retryable - liquidity can
    # recover.
    _slip_ok, _slip_why, _ = _risk.check_slippage(
        eff_notional, liquidity or None)
    if not _slip_ok:
        return _skip(mint, "risk: " + _slip_why)
    try:
        filled = _fills.honest_entry(quoted, eff_notional, liquidity or None)
    except Exception:
        filled = quoted
    if filled <= 0:
        return _skip(mint, "bad fill price")
    tokens = eff_notional / filled

    evidence = sig.get("evidence") or {}
    # Journaling only: the gate's confirming tape read (real
    # entry-time buys/sells/liquidity/mcap) rides into
    # entry_features — for sources whose cards publish placeholder
    # fields it is the only real entry record the close will carry.
    gate_tape = _gate_tape_stamp()
    pos = {
        "mint": mint, "symbol": (sig.get("symbol") or "?")[:32],
        "chain": chain, "source": (source or "unknown")[:32], "lane": LANE,
        "score": round(float(sig.get("score") or 0), 1),
        "tier": (sig.get("tier") or "")[:32],
        "entry_ts": time.time(), "quoted_price": quoted,
        "entry_price": filled, "notional_usd": _N,
        # Position-level peak over poll-observed prices, seeded with
        # the entry fill. Persisted on closes as peak_mult so
        # round-tripper leakage is measurable (added 2026-10-20,
        # mirroring mirror.py — closes previously carried no peak).
        "peak_price": filled,
        "tokens": tokens, "gas_in_usd": round(gas_in, 6),
        "legs": {
            "a": {"frac": LEG_A_FRAC, "target": LEG_A_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "b": {"frac": LEG_B_FRAC, "target": LEG_B_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "c": {"frac": LEG_C_FRAC, "trail": LEG_C_TRAIL, "peak": filled,
                  "done": False, "exit_price": 0, "realized_usd": 0},
        },
        "realized_usd": 0.0,
        "status": "open",
        "fee_venue": fee_venue,
        "spot_index": spot_index, "pair": extra.get("pair", ""),
        "entry_snapshot": {
            "score": round(float(sig.get("score") or 0), 1),
            "tier": (sig.get("tier") or "")[:32],
            "mcap": float(sig.get("mcap") or 0),
            "liquidity": liquidity,
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, _N),
            "evidence": dict(evidence),
        },
        "entry_features": {
            "chain": chain, "source": (source or "unknown")[:32],
            "score": round(float(sig.get("score") or 0), 1),
            "tier": (sig.get("tier") or "")[:32],
            "mcap": float(sig.get("mcap") or 0),
            "liquidity": liquidity,
            "card_ts": float(sig.get("ts") or 0),
            "quoted_price": quoted, "filled_price": filled,
            "age_min": float(sig.get("age_min") or 0),
            "buys": int(sig.get("buys") or 0),
            "sells": int(sig.get("sells") or 0),
            "gate_tape": gate_tape,
            "cost_summary": _costs.cost_summary(chain, _N),
        },
    }

    # Authoritative section: dedupe + bankroll + cap re-checked under
    # the same lock that debits cash and inserts the position — a crash
    # or a racing cycle can never double-enter or overdraw.
    with _ledger_lock():
        d = _read()
        if d["state"] == "bankroll_exhausted" or \
                float(d["bankroll"]) < _N:
            if float(d["bankroll"]) < _N and \
                    d["state"] != "bankroll_exhausted":
                d["state"] = "bankroll_exhausted"
                _write(d)
            return _skip(mint, "bankroll exhausted (notional counter $%.2f)"
                         % float(d["bankroll"]))
        if mint in d["positions"]:
            return _skip(mint, "already open (race)")
        for c in d["closes"]:
            if c.get("mint") == mint and \
                    time.time() - float(c.get("close_ts") or 0) < COOLDOWN_S:
                return _skip(mint, "closed within 24h cooldown (race)")
        if len(d["positions"]) >= MAX_OPEN:
            return _skip(mint, "max open positions (%d) (race)" % MAX_OPEN)
        # Shared cross-lane paper account (2026-11-22): when enabled
        # it is the AUTHORITATIVE capital gate — the entry happens
        # only if the shared cash covers the stake, so two lanes can
        # never spend the same dollars. Lock order is lane-ledger ->
        # account (the account never calls back), same as grad; the
        # debit lands immediately before the position insert, which
        # cannot fail. Fail-closed and transient: cash freed by a
        # close re-enables entries. The op_id keys on the pool's
        # durable identity (mint + entry ts) so a retry of an
        # uncertain response returns the original outcome instead of
        # double-spending; a legitimate REOPEN after the 24h cooldown
        # is a new instance with a new entry ts.
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
            return _skip(mint, _why_blocked)
        _pa = _paper_account()
        if _pa is not None:
            # Stage C: reconcile pending close intents BEFORE any
            # entry/capital decision; a FROZEN intent blocks the
            # entry outright (parent review 2026-11-22).
            _recover_close_intents()
            if _ENTRY_BLOCKED:
                return _skip(mint,
                             "close-intent divergence frozen: "
                             "entries blocked until operator "
                             "resolution")
            from daemon.launch_scanner import entry_guard as _eg
            _age_why = _eg.quote_age_refusal(
                _entry_row if chain == "solana" else _quote_stamp_snap,
                _pa.max_data_age_s(), time.time())
            if _age_why:
                return _skip(mint, "risk: " + _age_why)
            _qage_why = _eg.quote_age_refusal(
                _quote_stamp_snap, _pa.max_data_age_s(), time.time())
            if _qage_why:
                return _skip(mint, "risk: priced quote: " + _qage_why)
            _op = "%s:entry:%s:%d" % (LANE, mint,
                                       int(pos["entry_ts"] * 1000))
            if _demo_mode():
                # DEMO: the fill is the Jupiter-quote simulation, NEVER the model fill. No filler
                # or a skip REFUSES the entry (no silent fallback to modelled P&L).
                _df = _demo_filler()
                if _df is None:
                    return _skip(mint, "demo exec: quote-simulation filler unavailable; entry refused")
                try:
                    _op = _df.intent_id(LANE, mint)  # durable intent; stale/unknown state checked against the account journal
                except _demo.DemoSkip as _e:
                    return _skip(mint, "demo exec: %s" % _e.reason)
                try:
                    _prior_fill = _df.get_fill(_op)  # durable journal read BEFORE buy
                    _committed = _prior_fill is not None and _df.op_committed(_op) is True  # replay needs account debit evidence
                    _fill = _df.buy(_op, mint, _N, _pa.max_price_impact_pct())
                except _demo.DemoSkip as _e:
                    return _skip(mint, "demo exec: %s" % _e.reason)
                from daemon.launch_scanner import entry_guard as _eg_pq
                _pq = _eg_pq.post_quote_refusal(_fill, _prior_fill, _committed, _pa.max_data_age_s(), time.time())
                if _pq:
                    try:
                        _df.release_intent(LANE, mint)
                    except Exception as _re:
                        logger.warning("%s: release_intent failed (%r); a retry may replay the original fill", LANE, _re)
                    return _skip(mint, "risk: " + _pq)
                tokens = _fill["qty_tokens"]
                filled = _fill["price_usd"]
                pos.update({"tokens": tokens, "entry_price": filled, "peak_price": filled,
                            "gas_in_usd": round(_fill["fee_usd"], 6),
                            "execution": {"source": _fill["execution_source"], "entry_op": _op,
                                          "signature": _fill["signature"],
                                          "synthetic_signature": _fill["synthetic_signature"],
                                          "provenance": _fill["provenance"]}})
                pos["legs"]["c"]["peak"] = filled
                pos["entry_snapshot"]["filled_price"] = filled
                pos["entry_snapshot"]["cost_summary_basis"] = "MODEL_NOT_EXECUTION"
            _ok, _why, _pid = _pa.try_debit(LANE, mint, _N,
                                            tokens, _op,
                                            durable_refusal=_demo_mode())
            if not _ok:
                if _demo_mode():
                    try:
                        _demo_filler().release_intent(LANE, mint)
                    except Exception:
                        pass
                return _skip(mint, "paper account: " + _why)
            # Pin the open instance so no close can settle a later
            # reopen of the same mint.
            pos["pa_pos_id"] = _pid
            # GENERATION BINDING from the debit's own entry op under the account lock. None = unproven: stored UNBOUND, settlements refused and
            # frozen, entries blocked (no automatic rebinding, no compensation).
            pos["pa_entry_op"] = _op
            pos["pa_account_id"] = _pa.entry_binding(LANE, mint, _op, _pid)
            if not pos["pa_account_id"]:
                pos["pa_binding_failed"] = True
                _block_entries()
                logger.error("ceo: entry %s debit %s succeeded but its account generation could NOT be proven: position stored UNBOUND, ENTRIES BLOCKED" % (mint, _pid))
        d["bankroll"] = round(float(d["bankroll"]) - _N, 6)
        d["positions"][mint] = pos
        _write(d)
        if _demo_mode():
            try:
                _demo_filler().consume_intent(LANE, mint)
            except Exception as _ce:  # safe: a stale intent is detected from the account journal on re-entry
                logger.warning("%s: consume_intent failed (%r); stale intent will be dropped on re-entry", LANE, _ce)
    logger.warning("ceo OPEN %s %s %s @ %s ($%.2f, cash now $%.2f)",
                   chain, pos["symbol"], mint[:12], filled, _N,
                   account_cash())
    return True, "opened"


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


def _demo_sell(pos, leg_tokens, key):
    """Sell through the quote sim. Returns (fill, cap, src) or None (leg stays PENDING with
    diagnostics; nothing invented)."""
    _df = _demo_filler()
    if _df is None:
        pos["pending_exit"] = {"leg": key, "why": "filler unavailable", "ts": time.time()}
        return None
    cap, src = _exit_cap()
    try:
        return _df.sell(_exit_op(pos, key), pos["mint"], leg_tokens,
                        cap, meta=_sell_meta(pos)), cap, src
    except _demo.DemoSkip as e:
        _hold_not_durable(pos, key, e.reason, detail=e.detail)
        logger.warning("ceo demo exit pending %s %s: %s", pos["mint"][:12], key, e.reason)
        return None


def account_cash():
    try:
        return float(_load()["bankroll"])
    except Exception:
        return 0.0


# ---- bus tail --------------------------------------------------------------

def _line_key(line):
    return hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:16]


def process_bus():
    """Consume new bus lines since the persisted watermark.

    Returns {"lines": n_new, "opened": n, "skipped": n}. The watermark
    (byte offset) advances past every consumed line; processed line
    hashes are kept 72h so a bus rewrite/rotation replays nothing.
    Never raises."""
    out = {"lines": 0, "opened": 0, "skipped": 0}
    try:
        return _process_bus(out)
    except Exception as e:
        logger.warning("ceo process_bus failed: %s", e)
        return out


def _process_bus(out):
    ensure_ledger()
    with _ledger_lock():
        d = _read()
        offset = int(d.get("watermark_offset") or 0)
        seen = dict(d.get("seen_cards") or {})
    try:
        size = os.path.getsize(BUS_PATH)
    except Exception:
        return out
    if size < offset:
        # Bus was bounded/rewritten smaller — rescan from 0; the seen
        # hashes + position/cooldown dedupe make replays harmless.
        logger.warning("ceo bus shrank (%d < %d); rescanning from 0",
                       size, offset)
        offset = 0
    if size == offset:
        return out
    with open(BUS_PATH, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    text = chunk.decode("utf-8", "replace")
    lines = text.split("\n")
    # Last element is "" when the chunk ends with a newline (complete);
    # otherwise it is a partial line — hold it back for the next cycle.
    complete = lines[:-1]
    consumed_bytes = 0
    new_seen = {}
    for line in complete:
        consumed_bytes += len(line.encode("utf-8", "replace")) + 1
        if not line.strip():
            continue
        out["lines"] += 1
        key = _line_key(line)
        if key in seen or key in new_seen:
            out["skipped"] += 1
            continue
        new_seen[key] = time.time()
        try:
            sig = json.loads(line)
        except Exception:
            out["skipped"] += 1
            continue
        if _HAS_DEPS:
            ok_v, errs = _bus.validate(sig)
            if not ok_v:
                logger.warning("ceo SKIP bus line (invalid: %s)",
                               ",".join(errs))
                out["skipped"] += 1
                continue
        opened, _why = consider_card(sig)
        if opened:
            out["opened"] += 1
        else:
            out["skipped"] += 1
    # Persist watermark + seen (pruned) even when nothing opened.
    with _ledger_lock():
        d = _read()
        d["watermark_offset"] = offset + consumed_bytes
        merged = dict(d.get("seen_cards") or {})
        merged.update(new_seen)
        cutoff = time.time() - SEEN_TTL_S
        d["seen_cards"] = {k: v for k, v in merged.items() if v >= cutoff}
        _write(d)
    return out


# ---- exits (the user's ladder, mirror-identical constants) -----------------

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

def _recorded_credit_op(pos, suffix):
    """The RECORDED credit op of a still-pending (open, unfrozen, unapplied) close intent for this exact op id of this position instance, or None.
    A process death after the account credit and before the lane write leaves exactly this record; the account side is already done at THESE amounts."""
    _ci = _close_intents()
    if _ci is None or not pos.get("pa_pos_id"):
        return None
    _op_id = "%s:%s:%s:%s" % (LANE, suffix, pos["mint"], pos["pa_pos_id"])
    for _it in _ci.pending(LANE):
        for _op in ((_it.get("intent") or {}).get("settle_ops") or []):
            if _op.get("op_id") == _op_id and _op.get("kind") == "credit":
                return dict(_op, _intent_id=_it.get("intent_id"), _close_info=dict((_it.get("intent") or {}).get("close_info") or {}))
    return None


def _op_defects(op, close_info, qty, suffix=None):
    """Why a RECORDED pending credit op cannot be settled by a retry (None = usable). Pure check of durable data: non-finite/negative amount or fee, a qty
    that is not exactly the exit qty (when qty is given), a missing price_kind, a missing close reason. Used both at the retry and by restart recognition."""
    try:
        _g = float(op["amount"]); _f = float(op.get("fee") or 0.0); _rq = float(op["qty"])
    except Exception:
        return "amount/fee/qty missing or not numeric"
    if not (_g == _g and _f == _f and 0.0 <= _g < float("inf") and 0.0 <= _f < float("inf")):
        return "amount or fee non-finite or negative"
    if qty is not None and not (qty > 0 and _rq == qty):
        return "recorded qty %r is not exactly the exit qty %r" % (op.get("qty"), qty)
    if not op.get("price_kind"):
        return "price_kind not recorded"
    if not (close_info or {}).get("closed_reason"):
        return "close reason not recorded"
    return None


def _pending_unusable():
    """Restart recognition from DURABLE data only (journal + ledger): pending intents whose recorded credit op is unusable for the retry. Judged only for
    an op of THIS ceo position instance (matching mint and pa_pos_id): a foreign or absent position is not judged here. Returns [(intent_id, why)]."""
    _ci = _close_intents()
    if _ci is None:
        return []
    out = []
    try:
        _pos = (_read().get("positions") or {})
        for _it in _ci.pending(LANE):
            _int = _it.get("intent") or {}
            for _op in (_int.get("settle_ops") or []):
                if _op.get("kind") != "credit":
                    continue
                _p = _pos.get(_op.get("mint"))
                if not _p or not _p.get("pa_pos_id") or _p.get("pa_pos_id") != _op.get("pos_id"):
                    continue
                _oid = str(_op.get("op_id") or ""); _pre = "%s:" % LANE
                _parts = _oid[len(_pre):].split(":") if _oid.startswith(_pre) else []
                _q = None
                if _parts and _parts[0] == "leg" and len(_parts) > 1 and _parts[1] in _p.get("legs", {}):
                    _q = _p["tokens"] * _p["legs"][_parts[1]]["frac"]
                elif _parts and _parts[0] == "remaining" and len(_parts) > 1:
                    _lg = [k for k in _parts[1] if k in _p.get("legs", {})]
                    _q = sum(_p["tokens"] * _p["legs"][k]["frac"] for k in _lg) if _lg else None
                _w = _op_defects(_op, _int.get("close_info"), _q)
                if _w:
                    out.append((_it.get("intent_id"), "%s: %s" % (_oid, _w)))
    except Exception:
        logger.exception("ceo: pending-intent recognition failed")
        out.append((None, "pending-intent recognition raised: blocking fail-closed"))
    return out


def _nonsim_figures(pos, qty, exit_price, reason, pkind, suffix):
    """(filled_exit, gross, fee, kind, fee_venue, recorded_reason) of a NON-sim (modelled-fill) exit. When a pending intent already RECORDED a credit for this
    same op id, the retry settles EXACTLY those recorded amounts, qty, price_kind and close reason (the account credited them; a fresh live quote or a new
    trigger reason would be a different plan). A recorded op that cannot be used (qty not exactly equal to this exit's, non-finite/negative amount or fee,
    missing price_kind or closed_reason provenance) FREEZES that intent and BLOCKS entries (the existing freeze + entry-block contract), then refuses visibly.
    Otherwise the live honest_exit price, as before (recorded_reason None). A write-down never reuses a credit."""
    global _ENTRY_BLOCKED
    _kind = _pkind(reason, pkind)
    _fv = _exit_fee_venue(pos)
    if _kind != "write_down_zero":
        _rec = _recorded_credit_op(pos, suffix)
        if _rec is not None:
            _ci = _close_intents()
            _rreason = (_rec.get("_close_info") or {}).get("closed_reason")
            _dw = _op_defects(_rec, _rec.get("_close_info"), qty, suffix)
            _g = float(_rec["amount"]) if _dw is None else None
            _f = float(_rec.get("fee") or 0.0) if _dw is None else None
            _okrec = _dw is None
            if not _okrec:
                _why = ("recorded credit op %s unusable for the retry (recorded qty %r vs exit qty %r, amount %r, fee %r, price_kind %r, closed_reason %r): "
                        "frozen, operator resolution required" % (_rec.get("op_id"), _rec.get("qty"), qty, _rec.get("amount"), _rec.get("fee"),
                                                                    _rec.get("price_kind"), _rreason))
                try:
                    _fzok = bool(_ci.freeze(LANE, _rec.get("_intent_id"), _why))
                except Exception:
                    logger.exception("ceo: could not freeze intent %s", _rec.get("_intent_id")); _fzok = False
                _ENTRY_BLOCKED = True
                if _fzok:
                    logger.error("ceo: %s; ENTRIES BLOCKED", _why)
                else:
                    logger.error("ceo: %s; the freeze event was NOT written (intent stays pending); ENTRIES BLOCKED in this process and by restart recognition of the durable record", _why)
                raise _ci.IntentNotDurable(_why)
            if _rreason == suffix:
                logger.warning("ceo: recorded close reason for %s is the legacy op suffix (original trigger reason NOT recorded): the retry trigger reason is used", _rec.get("op_id"))
                _rreason = None
            return _g / qty, _g, _f, _rec["price_kind"], _fv, _rreason
    try:
        filled_exit = _fills.honest_exit(
            exit_price, qty * exit_price,
            # F5 (2026-10-23): exit-time liquidity beats the entry
            # snapshot — pools drain between entry and exit.
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = qty * filled_exit
    # A write-down is NOT a sale (design v4 Stage B): no fee is
    # booked on units nobody bought.
    gas_out = (0.0 if _kind == "write_down_zero"
               else _costs.tx_cost_usd(pos["chain"], gross, venue=_fv))
    return filled_exit, gross, gas_out, _kind, _fv, None


def _close_leg_impl(pos, leg_key, exit_price, reason, pkind=None):
    """Close one leg at an honest exit. Returns realized USD for the leg."""
    chain = pos["chain"]
    leg = pos["legs"][leg_key]
    if leg["done"]:
        return 0.0
    if not _intent_gate(pos, leg_key):
        return 0.0
    leg_tokens = pos["tokens"] * leg["frac"]
    _sim = (pos.get("execution") or {}).get("source") == "jupiter_quote_sim"
    if _sim and _pkind(reason, pkind) != "write_down_zero":
        _r = _demo_sell(pos, leg_tokens, leg_key)
        if _r is None:
            return 0.0
        _fill, _cap, _src = _r
        pos.pop("pending_exit", None)
        _kind = _pkind(reason, pkind)
        _fv = _exit_fee_venue(pos)
        filled_exit, gross, gas_out = _fill["price_usd"], _fill["gross_usd"], _fill["fee_usd"]
        net = gross - gas_out
        leg.update(done=True, exit_price=filled_exit, realized_usd=round(net, 6),
                   exit_reason=reason, exit_price_kind=_kind, fee_venue=_fv or "none",
                   exit_execution={"source": _fill["execution_source"], "signature": _fill["signature"],
                                   "synthetic_signature": True, "effective_impact_cap_pct": _cap,
                                   "impact_cap_source": _src, "provenance": _fill["provenance"]})
        pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
        _pa_settle(pos, leg_tokens, gross, gas_out, "leg:%s" % leg_key, price_kind=_kind)
        return net
    filled_exit, gross, gas_out, _kind, _fv, _rr = _nonsim_figures(
        pos, leg_tokens, exit_price, reason, pkind, "leg:%s" % leg_key)
    reason = _rr or reason   # a recorded pending credit keeps ITS close reason (never a mixed-provenance new trigger)
    net = gross - gas_out
    leg.update(done=True, exit_price=filled_exit,
               realized_usd=round(net, 6), exit_reason=reason,
               exit_price_kind=_kind, fee_venue=_fv or "none")
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    if _kind == "write_down_zero":
        _pa_write_down(pos, reason)
    else:
        _pa_settle(pos, leg_tokens, gross, gas_out,
                   "leg:%s" % leg_key, price_kind=_kind)
    return net


def _close_remaining_impl(pos, exit_price, reason, pkind=None):
    """Close all remaining legs as ONE combined exit (one tx cost) —
    same rule as mirror.py (audit 2026-10-18)."""
    chain = pos["chain"]
    open_legs = [k for k in ("a", "b", "c") if not pos["legs"][k]["done"]]
    if not open_legs:
        return 0.0
    if not _intent_gate(pos, "remaining:%s" % "".join(open_legs)):
        return 0.0
    total_tokens = sum(pos["tokens"] * pos["legs"][k]["frac"]
                       for k in open_legs)
    _sim = (pos.get("execution") or {}).get("source") == "jupiter_quote_sim"
    if _sim and _pkind(reason, pkind) != "write_down_zero":
        _rkey = "remaining:%s" % "".join(open_legs)
        _r = _demo_sell(pos, total_tokens, _rkey)
        if _r is None:
            return 0.0
        _fill, _cap, _src = _r
        pos.pop("pending_exit", None)
        _kind = _pkind(reason, pkind)
        _fv = _exit_fee_venue(pos)
        filled_exit, gross, gas_out = _fill["price_usd"], _fill["gross_usd"], _fill["fee_usd"]
        net = gross - gas_out
        for key in open_legs:
            leg = pos["legs"][key]
            share = (pos["tokens"] * leg["frac"]) / total_tokens if total_tokens else 0
            leg.update(done=True, exit_price=filled_exit, realized_usd=round(net * share, 6),
                       exit_reason=reason, exit_price_kind=_kind, fee_venue=_fv or "none",
                       exit_execution={"source": _fill["execution_source"],
                                       "signature": _fill["signature"], "synthetic_signature": True,
                                       "effective_impact_cap_pct": _cap, "impact_cap_source": _src,
                                       "provenance": _fill["provenance"]})
        pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
        _pa_settle(pos, total_tokens, gross, gas_out, _rkey, price_kind=_kind)
        return net
    filled_exit, gross, gas_out, _kind, _fv, _rr = _nonsim_figures(
        pos, total_tokens, exit_price, reason, pkind,
        "remaining:%s" % "".join(open_legs))
    reason = _rr or reason   # a recorded pending credit keeps ITS close reason (never a mixed-provenance new trigger)
    net = gross - gas_out
    for key in open_legs:
        leg = pos["legs"][key]
        share = (pos["tokens"] * leg["frac"]) / total_tokens \
            if total_tokens else 0
        leg.update(done=True, exit_price=filled_exit,
                   realized_usd=round(net * share, 6), exit_reason=reason,
                   exit_price_kind=_kind, fee_venue=_fv or "none")
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    if _kind == "write_down_zero":
        _pa_write_down(pos, reason)
    else:
        _pa_settle(pos, total_tokens, gross, gas_out,
                   "remaining:%s" % "".join(open_legs),
                   price_kind=_kind)
    return net


def _peak_fields(pos):
    """Peak price/multiple for the close record, from RECORDED prices
    only — never fabricated. Identical semantics to mirror.py: tracked
    positions report their running poll-observed peak
    (peak_source="tracked"); positions opened before tracking existed
    report the max of their recorded prices (leg C peak + realized
    exit prices) as an honest lower bound
    (peak_source="legacy_lower_bound"); no recorded price at all ->
    nulls with peak_unknown=True.
    """
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
            # F3 (2026-10-23): when the peak happened.
            "peak_ts": pos.get("peak_ts"),
            "peak_source": ("legacy_lower_bound"
                            if pos.get("peak_partial") else "tracked")}



def _guard_close(impl, pos, key, *args, meta=None):
    """Run one leg/close implementation. If the close intent could not be made durable AFTER the sale fill (begin raised IntentNotDurable),
    restore the WHOLE position (leg dict objects in place, so poll-loop aliases stay valid) and hold the exit visibly: no account op ran (begin
    precedes it), the lane persists nothing. The journaled sale fill (deterministic op id "<lane>:exit:<mint>:<key>", carrying the exit
    reason/kind in its own record via sell(meta=)) is what the next poll's journal-derived resume settles; the sale is not repeated."""
    _snap = copy.deepcopy(pos); _live_legs = pos.get("legs"); _live_leg = {k: v for k, v in (_live_legs or {}).items()}
    pos["_exit_meta"] = dict(meta or {}, key=key)
    try:
        return impl(*args)
    except Exception as _e:
        _ci = _close_intents()
        if _ci is None or not isinstance(_e, _ci.IntentNotDurable):
            raise
        pos.clear(); pos.update(_snap)
        # keep the ORIGINAL leg dict objects (the poll loop may hold aliases such as la/lb/lc): restore them in place, then re-attach them
        for _k, _lv in _live_leg.items():
            _lv.clear(); _lv.update(pos["legs"][_k]); pos["legs"][_k] = _lv
        if _live_legs is not None:
            _live_legs.clear(); _live_legs.update(pos["legs"]); pos["legs"] = _live_legs
        pos["_exit_meta"] = dict(meta or {}, key=key)
        logger.error("close intent NOT DURABLE (%s): exit %s rolled back in memory (nothing persisted, no account op)", _e, key)
        _hold_not_durable(pos, key)
        return 0.0
    finally:
        pos.pop("_exit_meta", None)


@_preserve_unresolved
def _close_leg(pos, leg_key, exit_price, reason, pkind=None):
    return _guard_close(_close_leg_impl, pos, leg_key, pos, leg_key, exit_price, reason, pkind,
                        meta={"reason": reason, "pkind": pkind})


@_preserve_unresolved
def _close_remaining(pos, exit_price, reason, pkind=None):
    return _guard_close(_close_remaining_impl, pos, "remaining", pos, exit_price, reason, pkind,
                        meta={"reason": reason, "pkind": pkind})


def _exit_op(pos, key):
    """Deterministic sale op id of THIS position generation: lane:exit:mint:key plus the account position instance id. Without the instance id a
    re-entry of the same mint (after the cooldown) would replay the previous generation's journaled exit fill (the filler returns an existing op id)."""
    _o = "%s:exit:%s:%s" % (LANE, pos["mint"], key)
    if pos.get("pa_pos_id"):
        _o += ":%s" % pos["pa_pos_id"]
    return _o


def _sell_meta(pos):
    """Exit reason/kind recorded INSIDE the journaled fill (atomic with it) so a later resume knows what the sale was for."""
    _m = pos.get("_exit_meta") or {}
    return {"reason": _m.get("reason"), "pkind": _m.get("pkind"), "key": _m.get("key"), "ts": time.time()} if _m else None


def _hold_not_durable(pos, key, why="close_intent_not_durable", detail=None):
    """Visible, DURABLE hold (pending_exit is persisted by the poll's own write): keeps a pre-existing pending exit in full under "prior", counts the
    tries and the first-held time, flags OPERATOR ATTENTION from the 3rd consecutive try, and marks a RISK exit (stop / time stop / dark-position exit)
    as blocked: a risk exit held by storage failure is never silently waived and the log alone is not the escalation."""
    _p0 = pos.get("pending_exit"); _pe = dict(_p0) if isinstance(_p0, dict) else {}
    _was = _pe.get("why") == why
    if _p0 and not _was:
        _pe["prior"] = {k: v for k, v in _p0.items() if k != "prior"}
    _m = pos.get("_exit_meta") or {}
    _rs = _m.get("reason") or _pe.get("reason") or ""
    _tries = int(_pe.get("tries") or 0) + 1 if _was else 1
    _pe.update({"leg": key, "why": why, "ts": time.time(), "tries": _tries,
                "first_ts": _pe.get("first_ts") if _was else time.time(), "reason": _rs,
                "risk_exit_blocked": bool(any(t in str(_rs) for t in ("stop", "f2", "unquotable", "backstop", "trail"))),
                "operator_attention": _tries >= 3})
    if detail is not None:
        _pe["detail"] = detail
    if why == "close_intent_not_durable" or str(why).startswith("unresolved_"):
        # An exit held on UNRESOLVED evidence (storage or an unknown/inflight sale) excludes the position from any qualification, and says how it is
        # resolved: by an operator reconciling the sale record against the fill journal; observed records are never rewritten to clear it.
        _pe["qualification_excluded"] = True
        _pe["resolution"] = "operator: reconcile the sale record against the fill journal; observed evidence is not rewritten to clear this hold"
    pos["pending_exit"] = _pe
    _h = pos.setdefault("exit_hold_history", [])   # append-only provenance, carried into the close record
    _h.append({"ts": _pe["ts"], "key": key, "why": why, "reason": _rs, "tries": _tries, "risk_exit_blocked": _pe["risk_exit_blocked"],
               "qualification_excluded": bool(_pe.get("qualification_excluded"))})
    del _h[:-50]
    if _pe["risk_exit_blocked"]:
        logger.error("RISK EXIT BLOCKED by close-intent storage failure: %s %s reason=%s tries=%d operator_attention=%s",
                     pos.get("mint"), key, _rs, _tries, _pe["operator_attention"])


def _resume_pending_exit(pos):
    """Poll-start journal reconciliation for THIS position generation. SCOPE: only a position with an account instance id (pa_pos_id) whose exit
    executed through the quote sim; a position without them (no shared account, non-sim model exits) has no journaled sale and is never touched.
    ALL exit records of the generation are enumerated by prefix (lane:exit:mint:..., ending in the instance id), so a record cannot hide behind a
    candidate key. Unresolved evidence HOLDS the position visibly and no trigger is evaluated: an unreadable journal, an inflight or UNKNOWN
    record, an unrecognised key, a partially booked or drifted key (the remainder key must equal the CURRENT open legs), and more than one unbooked
    filled record (overlap). Exactly one unbooked filled record is settled from the journal (its own record carries the exit reason and kind).
    None = nothing to resume; "held"; "closed:<reason>" (all legs done); "resumed" (legs remain)."""
    if not pos.get("pa_pos_id") or (pos.get("execution") or {}).get("source") != "jupiter_quote_sim":
        return None
    _df = _demo_filler()
    if _df is None:
        return None
    _open = [k for k in ("a", "b", "c") if not (pos["legs"].get(k) or {}).get("done")]
    if not _open:
        return None
    _pre = _exit_op(pos, "")[:-len(":%s" % pos["pa_pos_id"])]   # "<lane>:exit:<mint>:"
    _suf = ":%s" % pos["pa_pos_id"]
    def _hold(key, why, reason):
        pos["_exit_meta"] = {"reason": reason or why, "key": key}
        try:
            _hold_not_durable(pos, key, why)
        finally:
            pos.pop("_exit_meta", None)
        return "held"
    try:
        _recs = _df.fills_by_prefix(_pre, _suf)
    except Exception:
        return _hold("*", "unresolved_fill_journal", "fill_journal_unreadable")   # unknown evidence is never read as "no fill"
    # LEGACY-KEY QUARANTINE: exit ids written before the generation suffix carry no instance id ("<lane>:exit:<mint>:<key>"). Their generation cannot be
    # proven, so a legacy record that is not provably OLDER than this position's entry is never adopted and never read as "absent": the position is held.
    try:
        _legacy = {k: v for k, v in _df.fills_by_prefix(_pre).items() if not k.endswith(_suf) and k[len(_pre):] in
                   ("a", "b", "c") + tuple("remaining:" + c for c in ("a", "b", "c", "ab", "ac", "bc", "abc"))}
    except Exception:
        return _hold("*", "unresolved_fill_journal", "fill_journal_unreadable")
    for _lk, _lr in sorted(_legacy.items()):
        _lts = (_lr or {}).get("ts")
        try:
            _older = _lts is not None and float(_lts) < float(pos.get("entry_ts") or 0)
        except Exception:
            _older = False
        if not _older:
            return _hold(_lk[len(_pre):], "unresolved_legacy_exit_key", ((_lr or {}).get("intent") or {}).get("reason"))
    _cand = []
    for _op, _rec in sorted(_recs.items()):
        _key = _op[len(_pre):-len(_suf)]
        _st = (_rec or {}).get("status")
        _why_r = ((_rec or {}).get("intent") or {}).get("reason")
        if _st in ("inflight", "unknown"):
            return _hold(_key, "unresolved_inflight" if _st == "inflight" else "unresolved_unknown", _why_r)
        if _st != "filled":
            continue
        _legs = list(_key[len("remaining:"):]) if _key.startswith("remaining:") else ([_key] if _key in ("a", "b", "c") else None)
        if not _legs or any(l not in ("a", "b", "c") for l in _legs):
            return _hold(_key, "unresolved_exit_key", _why_r)
        _still = [l for l in _legs if l in _open]
        if not _still:
            continue   # every leg of this fill is already booked on the lane
        if len(_still) != len(_legs) or (_key.startswith("remaining:") and sorted(_legs) != sorted(_open)):
            return _hold(_key, "unresolved_key_drift", _why_r)   # partially booked, or the remainder no longer equals the open legs
        _cand.append((_key, _legs, _rec))
    if not _cand:
        return None
    if len(_cand) > 1:
        return _hold(_cand[0][0], "unresolved_exit_overlap", ((_cand[0][2] or {}).get("intent") or {}).get("reason"))
    _key, _legs, _rec = _cand[0]
    _it = _rec.get("intent") or {}
    _reason = _it.get("reason") or "journaled_fill_resume"
    _pk = _it.get("pkind")
    if _key.startswith("remaining:"):
        _close_remaining(pos, float(_rec.get("price_usd") or 0.0), _reason, _pk)
    else:
        _close_leg(pos, _key, float(_rec.get("price_usd") or 0.0), _reason, _pk)
    _pe2 = pos.get("pending_exit")
    if isinstance(_pe2, dict) and _pe2.get("why") == "close_intent_not_durable":
        return "held"
    if all((v or {}).get("done") for v in pos["legs"].values()):
        return "closed:%s" % _reason
    return "resumed"


def held_exits():
    """READER: open positions whose exit is held on unresolved evidence ({mint: pending_exit}). The poll's own persisted write carries the state; this
    is how an operator or a monitor reads it (operator_attention from the 3rd try, qualification_excluded, risk_exit_blocked)."""
    try:
        _ps = open_positions() or {}
    except Exception:
        return {}
    return {m: dict(p["pending_exit"]) for m, p in _ps.items() if isinstance(p.get("pending_exit"), dict)
            and (p["pending_exit"].get("operator_attention") or p["pending_exit"].get("qualification_excluded") or p["pending_exit"].get("risk_exit_blocked"))}


def _append_close(closes, rec):
    """_finalize returns None when a position is held open (legs not done); only real close records are collected."""
    if rec is not None:
        closes.append(rec)


def _finalize(d, mint, pos, closed_reason):
    """Move an open position to closes, settle cash, record the lane
    trade. Must be called with _ledger_lock held; writes via _write."""
    _recover_close_intents()
    if not all((v or {}).get("done") for v in pos["legs"].values()):
        # An exit leg is still PENDING (filler unavailable or skipped, or the close intent was not durable and the exit was rolled back): never
        # book a close, bankroll, trade record or verdict for a position that still holds unsold units. It stays open; the exit retries next poll.
        pos.setdefault("pending_exit", {"leg": "close", "why": "legs_not_done", "ts": time.time()})
        _write(d)
        return None
    _N = float(pos.get("notional_usd") or NOTIONAL_USD)  # the real entry notional
    realized = round(float(pos["realized_usd"]), 6)
    pnl = round(realized - _N, 4)
    d["bankroll"] = round(float(d["bankroll"]) + realized, 6)
    if d["bankroll"] < _N:
        d["state"] = "bankroll_exhausted"
    rec = {
        "mint": mint, "symbol": pos["symbol"], "chain": pos["chain"],
        "source": pos["source"], "lane": LANE,
        "entry_ts": pos["entry_ts"], "close_ts": time.time(),
        "entry_price": pos["entry_price"],
        "realized_usd": realized,
        "realized_mult": round(realized / _N, 4),
        "pnl_usd": pnl,
        "closed_reason": closed_reason,
        "legs": {k: {"done": v["done"],
                     "exit_price": v.get("exit_price", 0),
                     "realized_usd": v.get("realized_usd", 0),
                     "exit_reason": v.get("exit_reason", ""),
                     "exit_price_kind": v.get("exit_price_kind")}
                 for k, v in pos["legs"].items()},
        "entry_snapshot": pos["entry_snapshot"],
        "entry_features": pos.get("entry_features", {}),
        # F3/P1 (2026-10-23): quote journal tail + exit liquidity.
        "quote_journal": list(pos.get("quote_journal") or [])[-10:],
        "exit_liquidity": pos.get("last_liquidity"),
        # Stage C: match lane closes to account ops.
        "pa_pos_id": pos.get("pa_pos_id"),
        # Jupiter v3 audit (2026-11-17): which source priced the
        # exit that closed this position ("jup-v3", "external",
        # "ds/gt", "hl-mark", "f2-last-real-price", "unquotable",
        # or "" for legacy closes).
        "price_source": pos.get("last_price_source") or "",
        "transitions": list(pos.get("transitions") or []),
    }
    # The pre-registered verdict is frozen at NOTIONAL_USD. A close at any
    # other notional is a different size candidate: label it, do not
    # silently renormalize the frozen thresholds or evidence.
    rec["notional_usd"] = _N
    rec["frozen_verdict_notional_compatible"] = abs(_N - NOTIONAL_USD) < 1e-9
    rec.update(_peak_fields(pos))
    rec["exit_price_kind"] = (
        _pk.aggregate(v.get("exit_price_kind")
                      for v in pos["legs"].values() if v.get("done"))
        if _pk is not None else "unknown")
    # F2 stamp (2026-10-23): pos["_f2"] is set ONLY by the F2 exit
    # branch — its presence is the unmistakable marker, copied into
    # the record and the verdict-ledger payload below.
    if pos.get("_f2"):
        rec["f2"] = dict(pos["_f2"])
    _hh = list(pos.get("exit_hold_history") or [])
    if _hh:   # the close record says it was held on unresolved evidence; such a close is excluded from any qualification
        rec["exit_hold_history"] = _hh
        rec["qualification_excluded"] = any(x.get("qualification_excluded") for x in _hh)
    d["closes"].append(rec)
    d["closes"] = d["closes"][-MAX_CLOSES:]
    del d["positions"][mint]
    _write(d)  # lock held by caller
    try:
        _ks.record_trade(LANE, pnl)
        _ks.check_lane(LANE)
    except Exception:
        pass
    logger.warning("ceo CLOSE %s %s pnl=$%.2f realized=%.3fx (%s) "
                   "cash=$%.2f", pos["chain"], pos["symbol"], pnl,
                   rec["realized_mult"], closed_reason, d["bankroll"])
    _vp = {"mint": mint, "pnl_usd": pnl,
           "realized_mult": rec["realized_mult"],
           "closed_reason": closed_reason,
           "closes_count": len(d["closes"])}
    if rec.get("f2"):
        _vp["f2"] = rec["f2"]
    _verdict_event(LANE, "close", _vp)
    return rec


def poll_exits(price_fn=None):
    """Evaluate exits for all open CEO positions. price_fn(chain, mint,
    spot_index) -> float override for tests. Returns close records.
    Never raises."""
    try:
        return _poll_exits(price_fn)
    except Exception as e:
        logger.warning("ceo poll_exits failed: %s", e)
        return []


def _poll_exits(price_fn):
    # Recover incomplete close intents (and a provable torn tail) BEFORE this poll touches the journal or the account (once per process).
    _recover_close_intents()
    # Phase 1: snapshot (locked read). Quotes happen outside the lock.
    with _ledger_lock():
        snapshot = {m: p for m, p in _read()["positions"].items()}
    if not snapshot:
        return []
    # Phase 2: fetch quotes (no lock). HL prices reuse mirror's path:
    # one spotMetaAndAssetCtxs fetch per cycle, keyed by spot index.
    # Jupiter v3 (2026-11-17) is the first exit source for GRADUATED
    # Solana tokens only — one batched call for the whole open set,
    # 12s cache in jup_price, no Helius. A caller price_fn still
    # wins when it prices the mint; a mint v3 does not know (a
    # pre-graduation curve token) falls through to the pre-Jupiter
    # path unchanged, including the F2 reserves probe below.
    hl_marks = {}
    if any(p["chain"] == "hyperliquid" for p in snapshot.values()):
        hl_marks = _hl_all_marks()
    jup_prices = {}
    if _jup is not None:
        try:
            jup_prices = _jup.get_prices(
                [m for m, p in snapshot.items()
                 if _is_graduated_solana(p)]) or {}
        except Exception:
            jup_prices = {}
    quotes = {}
    refusals = {}
    quote_src = {}
    liqs = {}
    for mint, pos in snapshot.items():
        try:
            src = ""
            if price_fn:
                px = float(price_fn(pos["chain"], mint,
                                    pos.get("spot_index")) or 0)
                src = "external"
                if px <= 0 and mint in jup_prices:
                    px = jup_prices[mint]
                    src = _jup.SOURCE
            elif pos["chain"] == "hyperliquid":
                px = float(hl_marks.get(str(pos.get("spot_index")), 0) or 0)
                src = "hl-mark"
            elif mint in jup_prices:
                px = jup_prices[mint]
                src = _jup.SOURCE
            else:
                px = quote_price(pos["chain"], mint)
                src = "ds/gt"
                try:  # F5: fresh liquidity for the exit fill model
                    st = _fresh_tape(pos["chain"], mint)
                    if st and float(st.get("usd_liquidity") or 0) > 0:
                        liqs[mint] = float(st["usd_liquidity"])
                except Exception:
                    pass
            quote_src[mint] = src
        except Exception:
            px = 0.0
            quote_src[mint] = ""
        # F2 cross-probe (2026-10-23): aggregator dark on an ARMED
        # Solana position — before anyone may call it unquotable,
        # the direct pool reserves get their say (the same source
        # grad's phase 2 and rt_sync's pushed pricing already use).
        # A live reserve price makes the position quotable and is
        # evaluated normally; both sources dark is what phase 3's
        # F2 check requires. (With a caller price_fn the probe is
        # unnecessary: rt_sync's price_fn already folds the tape
        # and the pushed reserve price into its 0.)
        if px <= 0 and not price_fn and _f2 is not None \
                and _reserves is not None \
                and (pos.get("chain") or "solana") == "solana" \
                and _f2.is_candidate(pos):
            try:
                rpx, _rsrc = _reserves.price_for_position(
                    dict(pos, mint=mint))
                if rpx > 0:
                    px = rpx
                elif _rsrc:
                    refusals[mint] = _rsrc
            except Exception:
                pass
        quotes[mint] = px
    # Phase 3: mutate under lock.
    with _ledger_lock():
        d = _read()
        closes = []
        now = time.time()
        for mint, pos in list(d["positions"].items()):
            try:
                _rr = _resume_pending_exit(pos)
                if _rr == "held":
                    _write(d)
                    continue
                if _rr and _rr.startswith("closed:"):
                    _append_close(closes, _finalize(d, mint, pos, _rr[7:]))
                    continue
                px = quotes.get(mint, 0)
                if px <= 0:
                    # Reserves refusal provenance (2026-11-22): the
                    # refusal DETAIL is journalled so a schema-gate
                    # refusal is distinguishable from ordinary
                    # darkness in the close record.
                    _ref = refusals.get(mint)
                    if _ref:
                        _jq = pos.setdefault("quote_journal", [])
                        _jq.append({"ts": now, "px": None,
                                    "src": "reserves_refused",
                                    "detail": _ref})
                        del _jq[:-40]
                        # Dark positions `continue` below without the
                        # normal per-position write — persist the
                        # provenance now (lock held).
                        _write(d)
                    # F2 (2026-10-23): an ARMED position dark across
                    # every source for a sustained stretch exits at
                    # its last real observed price, stamped — instead
                    # of waiting for the first post-rug dust print.
                    # Never-armed positions keep the exact pre-F2
                    # behavior below (incl. the 24h backstop).
                    _f2_stamp = _f2.check(pos, now) \
                        if _f2 is not None else None
                    if _f2_stamp is not None:
                        pos["_f2"] = _f2_stamp
                        pos["last_price_source"] = "f2-last-real-price"
                        _close_remaining(
                            pos, _f2_stamp["last_real_price"],
                            _f2.F2_REASON)
                        _append_close(closes, _finalize(
                            d, mint, pos, _f2.F2_REASON))
                        continue
                    # Unknown price: skip this cycle — but past the 24h
                    # backstop an unquotable token is dead, so finalize
                    # at 0 (same rule mirror.py adopted 2026-10-19).
                    if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                        pos["last_price_source"] = "unquotable"
                        _close_remaining(pos, 0.0,
                                         "time_stop_24h_unquotable")
                        _append_close(closes, _finalize(
                            d, mint, pos, "time_stop_24h_unquotable"))
                    continue
                entry = pos["entry_price"]
                mult = px / entry if entry > 0 else 0

                # Position-level peak tracking (2026-10-20, same as
                # mirror.py): fold the quote into the running peak
                # BEFORE exits are evaluated; pre-tracking positions
                # are seeded mid-life and flagged partial (their close
                # reports a lower bound, never an invention).
                if "peak_price" not in pos:
                    pos["peak_price"] = 0.0
                    pos["peak_partial"] = True
                if px > float(pos.get("peak_price") or 0):
                    pos["peak_price"] = px
                    pos["peak_ts"] = now  # F3 (2026-10-23)
                if mint in liqs:
                    pos["last_liquidity"] = liqs[mint]  # F5
                # F3: per-eval quote journal (compact, capped).
                jq = pos.setdefault("quote_journal", [])
                jq.append({"ts": now, "px": px,
                           "src": quote_src.get(mint) or "poll"})
                del jq[:-40]
                pos["last_price_source"] = quote_src.get(mint) or ""

                # Hard stop first: -50% closes everything immediately.
                if mult <= HARD_STOP_MULT:
                    _close_remaining(pos, px, "hard_stop_-50pct")
                    _append_close(closes, _finalize(d, mint, pos, "hard_stop_-50pct"))
                    continue
                # Time stop: 24h hygiene backstop.
                if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                    _close_remaining(pos, px, "time_stop_24h")
                    _append_close(closes, _finalize(d, mint, pos, "time_stop_24h"))
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
                    _append_close(closes, _finalize(d, mint, pos, "ladder_complete"))
                else:
                    _write(d)
            except Exception as e:
                logger.warning("ceo poll_exits %s failed: %s", mint, e)
                continue
        return closes


def open_positions():
    """{mint: position} currently open. Never raises."""
    try:
        return _load()["positions"]
    except Exception:
        return {}
