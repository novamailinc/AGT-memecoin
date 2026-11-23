"""ONE shared paper-trading account for every memecoin paper lane.

Why this exists (2026-11-22, user-approved shared demo account):
before this module every lane kept a PRIVATE bankroll constant
(grad $100, ceo $100, steady $100, ...) and its own ledger, so the
demo could be "spending" $1,000+ across lanes that were presented
as one account — the same paper capital was counted several times
over, and nothing could answer "what is the account worth right
now". This module is the single source of truth for paper capital:

  * one cash balance, configurable start ($10.00 engineering
    default — the owner approved a shared $10 example on
    2026-11-22; it is a knob, not a promise),
  * lanes DEBIT on entry and CREDIT on close; a debit that the
    cash balance cannot cover is refused (fail-closed), so two
    lanes can never spend the same dollars,
  * account-level exposure = cash + open allocated cost basis;
    per-lane, per-position and daily P/L are tracked here so
    account-level risk enforcement (Stage 2: daily-loss / drawdown
    gates) has verified numbers to gate on,
  * every mutation is journaled append-only BEFORE the ledger
    write, so a crashed writer never silently drops money.

Persistence follows the repo's hardened conventions (live10
mirror / killswitch, 2026-11-11): exclusive flock around every
read-modify-write (two processes race after a watchdog restart),
per-pid tmp file + fsync + os.replace + directory fsync, a .bak
shadow after every save, and tmp-file sweeps. A corrupt live file
falls back to the shadow; if both are unreadable, debits are
REFUSED (fail-closed) while credits are journaled so no realized
money is lost.

Accounting contract (Stage 1.3, 2026-11-22 review):

  * POSITIONS track quantity and ALLOCATED cost basis (average
    cost), never stake-minus-proceeds: entry records qty and cost;
    each sale (partial or final) allocates basis in proportion to
    the units sold, realizes P/L = (gross proceeds - fees) minus
    the allocated basis, and credits NET proceeds to cash. A
    profitable tranche that recovers more than the original stake
    realizes the gain immediately; remaining basis is per-unit
    allocated and can never go negative. Tracked separately per
    position: remaining qty, remaining basis, cumulative realized
    P/L, cumulative net proceeds, cumulative fees.
  * CONSERVATION INVARIANT (no external flows): after any sale,
    cash + remaining basis == prior cash + prior basis + net
    realized P/L of the sale. Tests pin this against hand-computed
    examples, including profitable and losing partial sales.
  * OPERATION IDs. Every mutation carries a stable caller-supplied
    op_id (distinct from the WAL seq): an uncertain response must
    be retryable, so a repeated op_id returns the ORIGINAL outcome
    without re-applying — even after the position was closed and
    reopened (the retry must not attach to the new instance). The
    same op_id with different parameters is a caller bug and is
    refused. Each journaled mutation also gets an immutable event
    id (ev#<seq>) so repeated tranches of one position are
    distinct in the audit trail.
  * POSITION IDs. Every open instance carries an immutable pos_id
    "lane/mint#<debit-seq>". A mint can be closed and REOPENED
    (ceo has a 24h cooldown, not a ban); lane+mint alone cannot
    tell instance 1 from instance 2, so journal replay matches
    credits to the exact instance they settled. A replayed credit
    whose pos_id disagrees with the open instance is a DIVERGENCE:
    replay stops there and the account goes degraded rather than
    settling the wrong instance.
  * TORN JOURNAL. A crash mid-append leaves a partial LAST line;
    exactly one unparseable trailing record is tolerated and
    truncated under the lock (its money effect, if any, is lost
    with the crashed writer, which never saved state for it). Any
    other unparseable record, or a hole/inversion in the seq
    chain, means the WAL cannot be trusted: the account is
    DEGRADED — debits are refused, credits are journaled for
    manual reconciliation but not applied, and replay is skipped
    (an incomplete WAL must not drive state).
  * EQUITY BASELINES. Day-start and peak equity are captured only
    from trustworthy marks (marks_status "ok") and persisted. At a
    rollover with no trustworthy mark the previous baseline
    CARRIES and gate inputs report unavailable (fail-closed); a
    baseline is NEVER reset to a later favorable value on the same
    day. A mark's value is the liquidation value of the REMAINING
    units of the position (the lane owns units; net proceeds of
    past tranches are already in cash, so marked equity can
    neither lose nor double-count them).

PAPER ONLY. This account is simulated dollars; nothing here
touches, custodies, or prices real funds.

Env knobs (all optional; defaults are conservative engineering
defaults chosen for a $10 demo, flagged here as agent-chosen):
  PAPER_ACCOUNT_START          starting cash (default 10.00)
  PAPER_ACCOUNT_LANE_CAP       max simultaneous open cost basis
                               per lane (default: no cap)
  PAPER_ACCOUNT_MARK_TTL_S     max age of an accepted mark
                               (default 120s)
"""

from __future__ import annotations

import contextlib
import fcntl
import glob
import json
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HOME = os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser(
    "~/.forex-agent")
ACCOUNT_DIR = os.path.join(HOME, "paper_account")
LEDGER_PATH = os.path.join(ACCOUNT_DIR, "ledger.json")
JOURNAL_PATH = os.path.join(ACCOUNT_DIR, "events.jsonl")
LOCK_FILE = os.path.join(ACCOUNT_DIR, ".lock")

DEFAULT_START = 10.00  # owner-approved $10 example (2026-11-22)
DEFAULT_MARK_TTL_S = 120.0

_MUTATION_EVENTS = ("debit", "credit", "write_down")

# Cash split (2026-11-22, quarantine design v4 Stage A): proceeds are
# spendable ONLY when the tranche's exit price was a current observed
# quote (exit_provenance.CURRENT). Stale-price estimates, write-downs,
# mixed closes and unknown/legacy kinds are QUARANTINED in
# cash_estimated: visible, reported, never spendable for new entries.
# Unknown kinds fail closed into the estimated bucket.
_SPENDABLE_KINDS = frozenset({"current_price_simulated_fill"})
_EPS = 1e-9


def _env_f(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("paper_account: bad %s=%r, using default %s",
                       name, raw, default)
        return default


def start_balance() -> float:
    return _env_f("PAPER_ACCOUNT_START", DEFAULT_START)


def lane_cap() -> Optional[float]:
    return _env_f("PAPER_ACCOUNT_LANE_CAP", None)


def mark_ttl_s() -> float:
    return _env_f("PAPER_ACCOUNT_MARK_TTL_S", DEFAULT_MARK_TTL_S)


# ---- Stage 2 account-level risk gates -------------------------------
# PROVISIONAL, INACTIVE BY DEFAULT (PAPER_ACCOUNT_GATES_ENABLED=1
# opts in, on top of PAPER_ACCOUNT_ENABLED). The default thresholds
# below are agent-chosen engineering placeholders for the $10 demo
# account, NOT owner-approved operational limits; the owner sets real
# numbers before any runtime enabling.

def gates_enabled() -> bool:
    return os.environ.get("PAPER_ACCOUNT_GATES_ENABLED") == "1"


def _central_marks() -> Dict[str, Dict[str, Any]]:
    """Marks from the central marker (account_marks.collect_marks),
    collected by try_debit BEFORE the account lock when the caller
    supplies none. Caller-supplied marks always win; this is only
    the default. Any failure returns an empty map, which the view
    treats as untrusted whenever positions are open (fail-closed)
    and as fully marked on an empty account. Never raises: a broken
    marker must degrade gating, not crash a lane's entry path.
    """
    try:
        try:
            from daemon.launch_scanner import account_marks
        except ImportError:
            import account_marks  # sibling import context (scripts)
        marks = account_marks.collect_marks()
        return marks if isinstance(marks, dict) else {}
    except Exception:
        return {}


def max_daily_loss_usd() -> float:
    return _env_f("PAPER_ACCOUNT_MAX_DAILY_LOSS_USD", 5.00)  # provisional


def max_drawdown_pct() -> float:
    return _env_f("PAPER_ACCOUNT_MAX_DRAWDOWN_PCT", 50.0)  # provisional


def max_exposure_pct() -> float:
    return _env_f("PAPER_ACCOUNT_MAX_EXPOSURE_PCT", 60.0)  # provisional


def _gate_check_locked(led: Dict[str, Any], view: Dict[str, Any],
                       new_stake: float) -> Optional[str]:
    """Account-level entry gate, evaluated UNDER the account lock as
    part of the debit's critical section: the evaluation and the
    reservation are atomic, so two lanes can never each pass on the
    same available capacity. Returns None when entry is allowed,
    else the refusal reason. Fail-closed: anything untrusted blocks
    new risk. Exits never pass through here (risk reduction is
    always allowed; credit() enforces ownership, quantity, op
    idempotency and ledger integrity instead).

    Gates (all thresholds provisional, see above):
      * degraded WAL (handled by the caller before this),
      * marks: with any position open, every mark must be present,
        fresh, finite and provenanced (marks_status "ok"), else the
        equity figure is untrusted and entries block; an empty
        account is fully marked by definition (equity == cash),
      * daily loss vs the persisted day-start equity baseline,
      * drawdown vs the persisted peak equity,
      * total exposure after this entry vs marked equity.
    book_value is NEVER a gate input (cost basis hides unrealized
    losses).
    """
    if view["marks_status"] != "ok":
        return ("gate: marks not trustworthy (%s); entry blocked "
                "fail-closed" % view["marks_status"])
    me = view["marked_equity_conservative"]
    dl = view.get("daily_loss")
    if dl is None or view.get("drawdown_pct") is None:
        return ("gate: no trustworthy day-start/peak baseline yet; "
                "entry blocked fail-closed")
    if dl > max_daily_loss_usd():
        return ("gate: daily loss $%.2f > limit $%.2f (provisional)"
                % (dl, max_daily_loss_usd()))
    if view["drawdown_pct"] > max_drawdown_pct():
        return ("gate: drawdown %.1f%% > limit %.1f%% (provisional)"
                % (view["drawdown_pct"], max_drawdown_pct()))
    if me <= 0:
        return "gate: marked equity $%.2f; no capacity" % me
    after = view["cost_exposure"] + new_stake
    if after > max_exposure_pct() / 100.0 * me:
        return ("gate: exposure $%.2f after entry > %.0f%% of marked "
                "equity $%.2f (provisional)"
                % (after, max_exposure_pct(), me))
    return None


def _new_ledger() -> Dict[str, Any]:
    now = time.time()
    return {
        "created_ts": now,
        "seq": 0,
        "start_balance": start_balance(),
        "cash": start_balance(),
        "cash_spendable": start_balance(),
        "cash_estimated": 0.0,
        # pos_id -> {lane, mint, qty, basis, entry_qty, entry_cost,
        #            reason, ts, status}: write-down register.
        # Units moved here are HELD UNRESOLVED: not sold (no fee,
        # no proceeds, no realized loss recognized), carrying value
        # zero, cost preserved for later evidence-led resolution
        # (design v4 Stage B). Never re-sellable.
        "unresolved": {},
        "realized_pnl": 0.0,
        "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
        "day_realized_pnl": 0.0,
        "lanes": {},
        # op_id -> {"event": kind, "lane":, "mint":, "pos_id":,
        #           "event_id":, params...}: caller-retry dedupe.
        "ops": {},
        # Equity baselines: captured only from trustworthy marks,
        # never reset to a later favorable value (see docstring).
        "day_baseline": None,   # {"day":, "value":, "ts":}
        "peak_equity": None,    # {"value":, "ts":}
    }


def _lane(led: Dict[str, Any], lane: str) -> Dict[str, Any]:
    lanes = led.setdefault("lanes", {})
    if lane not in lanes:
        lanes[lane] = {"open": {}, "realized_pnl": 0.0,
                       "n_entries": 0, "n_closes": 0}
    return lanes[lane]


def _pos_id(lane: str, mint: str, seq: int) -> str:
    """Immutable ID of one open instance: unique per debit, stable
    across reopens of the same mint."""
    return "%s/%s#%d" % (lane, mint, seq)


def _pos_basis(pos: Dict[str, Any]) -> float:
    """Remaining allocated cost basis (v2) or full stake (legacy
    Stage-1 record, which has no quantity tracking)."""
    if "basis" in pos:
        return float(pos["basis"])
    return float(pos["stake"])


def _pos_qty(pos: Dict[str, Any]) -> Optional[float]:
    q = pos.get("qty")
    return float(q) if isinstance(q, (int, float)) else None


def _valid_amount(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


@contextlib.contextmanager
def _lock():
    """Exclusive inter-process lock for the account ledger. Same
    pattern as the live10 mirror / killswitch locks: two processes
    can exist after a watchdog restart, and the fd is released
    automatically if the holder dies."""
    os.makedirs(ACCOUNT_DIR, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    """Per-pid tmp + fsync + os.replace + directory fsync (the
    shared-tmp race is the prime suspect for the five 0-byte ledger
    incidents, 2026-11-04..11)."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        pass


def _journal(event: Dict[str, Any]) -> None:
    """Append-only event journal, written BEFORE the ledger save:
    if the process dies mid-save the money movement is still
    recoverable from the journal."""
    event = dict(event)
    event["ts"] = event.get("ts") or time.time()
    try:
        with open(JOURNAL_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        logger.exception("paper_account: journal write failed")


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _journal_read() -> Tuple[List[Dict[str, Any]], Optional[str],
                             Optional[int]]:
    """Parse the WAL. Returns (entries, status, tail_offset).

    status is None when the journal is clean, "torn_tail" when
    exactly one unparseable record sits at the END of the file
    (the signature of a crash mid-append; tail_offset is the byte
    the caller truncates to, under the lock), and "torn" for any
    other unparseable content — mid-file corruption or multiple
    bad records, which no automatic repair can justify.
    """
    entries: List[Dict[str, Any]] = []
    bad: List[int] = []
    last_off: Optional[int] = None
    try:
        with open(JOURNAL_PATH, "rb") as f:
            off = 0
            for raw in f:
                cur = off
                off += len(raw)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                last_off = cur
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict):
                        entries.append(ev)
                except ValueError:
                    bad.append(cur)
    except FileNotFoundError:
        return [], None, None
    except Exception:
        return [], "torn", None
    if not bad:
        return entries, None, None
    if len(bad) == 1 and bad[0] == last_off:
        return entries, "torn_tail", bad[0]
    return entries, "torn", None


def _journal_truncate(offset: int) -> None:
    """Drop a torn tail record. Caller must hold _lock(). The torn
    bytes are a partial write whose state was never saved, so no
    money movement is lost with them."""
    with open(JOURNAL_PATH, "r+") as f:
        f.truncate(offset)
        f.flush()
        os.fsync(f.fileno())


def _seq_violation(entries: List[Dict[str, Any]]
                   ) -> Optional[Tuple[Any, Any]]:
    """Mutation events carry a strictly +1 seq chain. A gap means
    a journaled mutation was lost (replay would skip money); a
    duplicate or inversion means the file was tampered with or
    two uncoordinated writers raced. Either way: untrusted."""
    seqs = [ev.get("seq") for ev in entries
            if ev.get("event") in _MUTATION_EVENTS
            and isinstance(ev.get("seq"), int)]
    for a, b in zip(seqs, seqs[1:]):
        if b != a + 1:
            return (a, b)
    return None


def _apply_debit(led: Dict[str, Any], lane: str, mint: str,
                 stake: float, qty: Optional[float], pid: str,
                 ts: Optional[float]) -> None:
    """Shared debit mutation. Caller holds the lock and has
    validated input and cash."""
    led["cash"] = round(float(led["cash"]) - stake, 6)
    led["cash_spendable"] = round(
        float(led.get("cash_spendable", led["cash"] + stake))
        - stake, 6)
    st = _lane(led, lane)
    st["open"][mint] = {
        "pos_id": pid,
        "qty": qty,                 # remaining units
        "basis": stake,             # remaining allocated cost
        "entry_qty": qty,
        "entry_cost": stake,
        "realized_pnl": 0.0,
        "proceeds": 0.0,            # cumulative NET proceeds
        "fees": 0.0,
        "ts": ts or time.time(),
    }
    st["n_entries"] += 1


def _apply_credit(led: Dict[str, Any], lane: str, mint: str,
                  qty_sold: Optional[float], net: float, fee: float,
                  final: bool,
                  price_kind: Optional[str] = None
                  ) -> Tuple[Optional[float], str]:
    """Shared credit mutation. Returns (realized_delta, "") or
    (None, reason) leaving state untouched. Average-cost
    allocation; a partial sale realizes P/L against the allocated
    basis of the units sold. Caller holds the lock."""
    st = _lane(led, lane)
    pos = st["open"].get(mint)
    if pos is None:
        return None, "no open position"
    basis = _pos_basis(pos)
    have_qty = _pos_qty(pos)
    if final or qty_sold is None:
        if qty_sold is not None and have_qty is not None \
                and qty_sold < have_qty - _EPS:
            return None, "qty under remaining: not final"
        alloc = basis
        realized = round(net - alloc, 6)
        st["open"].pop(mint)
        pos["basis"] = 0.0
        pos["qty"] = 0.0 if have_qty is not None else None
        closed = True
    elif have_qty is None or "basis" not in pos:
        # Legacy Stage-1 record: no quantity tracking, so no
        # allocation is possible. Full close only.
        return None, "legacy position: full close only"
    else:
        if not _valid_amount(qty_sold) or qty_sold <= 0:
            return None, "bad qty"
        if qty_sold > have_qty + _EPS:
            return None, "qty exceeds remaining units"
        if qty_sold >= have_qty - _EPS:
            # De-facto full close: this tranche consumes every
            # remaining unit (within dust), so it settles ALL the
            # remaining basis — the same accounting as an explicit
            # final close. A tranche that left dust units behind
            # would strand their basis in a zombie position.
            alloc = basis
            realized = round(net - alloc, 6)
            st["open"].pop(mint)
            pos["basis"] = 0.0
            pos["qty"] = 0.0
            closed = True
        else:
            alloc = round(basis * (qty_sold / have_qty), 6)
            alloc = min(alloc, basis)
            realized = round(net - alloc, 6)
            pos["qty"] = round(have_qty - qty_sold, 12)
            pos["basis"] = round(basis - alloc, 6)
            closed = False
    pos["realized_pnl"] = round(
        float(pos.get("realized_pnl") or 0.0) + realized, 6)
    pos["proceeds"] = round(
        float(pos.get("proceeds") or 0.0) + net, 6)
    pos["fees"] = round(float(pos.get("fees") or 0.0) + fee, 6)
    led["cash"] = round(float(led["cash"]) + net, 6)
    if price_kind in _SPENDABLE_KINDS:
        led["cash_spendable"] = round(
            float(led.get("cash_spendable") or 0.0) + net, 6)
    else:
        led["cash_estimated"] = round(
            float(led.get("cash_estimated") or 0.0) + net, 6)
    led["realized_pnl"] = round(
        float(led["realized_pnl"]) + realized, 6)
    led["day_realized_pnl"] = round(
        float(led.get("day_realized_pnl") or 0.0) + realized, 6)
    st["realized_pnl"] = round(
        float(st["realized_pnl"]) + realized, 6)
    if closed:
        st["n_closes"] += 1
    return realized, ""


def _reconcile(led: Dict[str, Any], entries: List[Dict[str, Any]]
               ) -> Tuple[Dict[str, Any], Optional[str]]:
    """Replay journaled mutations the ledger has not reached.

    The journal is the write-ahead log (every mutation is journaled
    BEFORE the state save), so after a crash the ledger can be
    BEHIND the journal — and a stale .bak shadow can be behind by
    more. Without reconciliation, restoring that shadow would
    RESURRECT a settled position, and its later close would credit
    the money a second time. Every mutation carries a monotonic
    seq (the state version AFTER the mutation), an immutable
    event id, the caller's op_id, and the pos_id of the open
    INSTANCE it moved; replay applies journaled entries past the
    ledger's seq, idempotently:

      any event whose op_id is already recorded in the state is
      skipped (caller retry that reached the journal twice, or a
      stale shadow re-seeing an applied op);
      debit  — skipped when the same pos_id is already open,
               applied when the mint is closed; a mint open under
               a DIFFERENT pos_id is a divergence (stop, degraded);
      credit — applied when the matching pos_id is open; skipped
               when the mint is fully closed (already settled); a
               mint open under a different pos_id is a divergence
               (a credit for instance #1 must never settle a
               reopened instance #2). Partial credits replay with
               the journaled qty/fee so tranche math is identical.

    Entries without a seq predate this scheme; reconciliation runs
    only over seq-bearing entries, and anything ambiguous is a
    divergence, never a guess. Returns (ledger, divergence) where
    divergence is None or a human-readable reason; on divergence
    replay STOPS at the offending entry (earlier valid mutations
    are kept) and the caller degrades the account.
    """
    led_seq = led.get("seq")
    if not isinstance(led_seq, int):
        return led, "ledger has no seq"
    ops = led.setdefault("ops", {})
    pending = []
    for ev in entries:
        ev_seq = ev.get("seq")
        if not isinstance(ev_seq, int):
            continue
        if ev_seq > led_seq:
            pending.append((ev_seq, ev))
    if not pending:
        return led, None
    pending.sort(key=lambda t: t[0])
    replayed = 0
    for ev_seq, ev in pending:
        kind = ev.get("event")
        lane, mint = ev.get("lane"), ev.get("mint")
        op_id = ev.get("op_id")
        if kind in _MUTATION_EVENTS and op_id and op_id in ops:
            # Caller op already applied: idempotent skip. The seq
            # still advances — the state includes this mutation.
            led["seq"] = ev_seq
            continue
        if kind == "debit" and lane and mint:
            pid = ev.get("pos_id") or _pos_id(lane, mint, ev_seq)
            st = _lane(led, lane)
            cur = st["open"].get(mint)
            if cur is not None:
                if cur.get("pos_id", _pos_id(lane, mint, ev_seq)) == pid:
                    if op_id:
                        ops[op_id] = {"event": "debit",
                                      "pos_id": pid}
                    led["seq"] = ev_seq
                    continue  # state already has it: idempotent
                return led, ("replay divergence: debit %s but %s "
                             "open as %s"
                             % (pid, mint, cur.get("pos_id")))
            stake = float(ev.get("stake") or 0)
            if stake <= 0 or float(led["cash"]) < stake:
                return led, ("replay cannot apply debit %s "
                             "(bad stake or cash)" % pid)
            _apply_debit(led, lane, mint, stake, ev.get("qty"),
                         pid, ev.get("ts"))
            if op_id:
                ops[op_id] = {"event": "debit", "pos_id": pid,
                              "lane": lane, "mint": mint,
                              "stake": stake, "qty": ev.get("qty")}
            replayed += 1
        elif kind == "credit" and lane and mint:
            st = _lane(led, lane)
            pos = st["open"].get(mint)
            if pos is None:
                led["seq"] = ev_seq
                continue  # already settled: idempotent skip
            pid = ev.get("pos_id")
            if pid and pos.get("pos_id") and pos["pos_id"] != pid:
                return led, ("replay divergence: credit %s but %s "
                             "open as %s"
                             % (pid, mint, pos["pos_id"]))
            realized, why = _apply_credit(
                led, lane, mint, ev.get("qty"),
                float(ev.get("net") if ev.get("net") is not None
                      else (float(ev.get("proceeds") or 0)
                            - float(ev.get("fee") or 0))),
                float(ev.get("fee") or 0),
                bool(ev.get("final", True)),
                price_kind=ev.get("price_kind"))
            if realized is None:
                return led, ("replay cannot apply credit %s/%s: %s"
                             % (lane, mint, why))
            if op_id:
                ops[op_id] = {"event": "credit", "lane": lane,
                              "mint": mint, "pos_id": pid,
                              "realized": realized}
            replayed += 1
        elif kind == "write_down" and lane and mint:
            ok, why = _apply_write_down(
                led, lane, mint, ev.get("pos_id"),
                ev.get("reason"), ev.get("ts"))
            if not ok:
                return led, ("replay cannot apply write_down "
                             "%s/%s: %s" % (lane, mint, why))
            if op_id:
                ops[op_id] = {"event": "write_down", "lane": lane,
                              "mint": mint,
                              "pos_id": ev.get("pos_id")}
            replayed += 1
        # The seq advances only for entries actually processed: on
        # a divergence return above it stays BEHIND the offending
        # entry, so the next load sees the divergence again and
        # keeps the account degraded instead of healing silently.
        led["seq"] = ev_seq
    if replayed:
        logger.warning("paper_account: reconciled %d journaled "
                       "mutation(s) the ledger had not reached",
                       replayed)
    return led, None


def _derive_buckets_locked(led: Dict[str, Any],
                           entries: List[Dict[str, Any]]
                           ) -> Tuple[Dict[str, Any], Optional[str]]:
    """Derive the spendable/estimated cash split for a ledger that
    predates it, by replaying the journal from genesis (design v4
    Stage A). EXACT OR FROZEN: a seq-less mutation, a genesis debit
    the verified bucket could not have funded, an unreconstructable
    position, or any mismatch against the stored state leaves the
    account DEGRADED (entries refused, credits journaled-only)
    rather than clamping the inconsistency into an apparently
    usable balance. Credits journaled without a price_kind route to
    the ESTIMATED bucket (conservative)."""
    unseq = [ev for ev in entries
             if ev.get("event") in _MUTATION_EVENTS
             and not isinstance(ev.get("seq"), int)]
    if unseq:
        return led, ("cash-split migration: %d journaled mutations "
                     "predate the seq scheme; manual reconciliation "
                     "required" % len(unseq))
    gen = _new_ledger()
    gen["start_balance"] = float(led.get("start_balance")
                                 or gen["start_balance"])
    gen["cash"] = gen["cash_spendable"] = gen["start_balance"]
    gen["cash_estimated"] = 0.0
    seqd = sorted((ev for ev in entries
                   if ev.get("event") in _MUTATION_EVENTS),
                  key=lambda e: e["seq"])
    for ev in seqd:
        lane, mint = ev.get("lane"), ev.get("mint")
        if ev["event"] == "debit":
            stake = float(ev.get("stake") or 0)
            if (stake <= 0
                    or float(gen["cash_spendable"]) < stake - _EPS):
                return led, ("cash-split migration: genesis debit "
                             "%s exceeds verified cash (history "
                             "financed by estimated proceeds); "
                             "manual reconciliation required"
                             % ev.get("op_id"))
            _apply_debit(gen, lane, mint, stake, ev.get("qty"),
                         ev.get("pos_id")
                         or _pos_id(lane, mint, int(ev["seq"])),
                         ev.get("ts"))
        elif ev["event"] == "write_down":
            ok, why = _apply_write_down(
                gen, lane, mint, ev.get("pos_id"),
                ev.get("reason"), ev.get("ts"))
            if not ok:
                return led, ("cash-split migration: write_down "
                             "%s: %s; manual reconciliation "
                             "required" % (ev.get("op_id"), why))
        else:  # credit
            pos = _lane(gen, lane)["open"].get(mint)
            if pos is None:
                return led, ("cash-split migration: orphan credit "
                             "%s (position not open in genesis "
                             "replay); manual reconciliation "
                             "required" % ev.get("op_id"))
            realized, why = _apply_credit(
                gen, lane, mint, ev.get("qty"),
                float(ev.get("net") if ev.get("net") is not None
                      else (float(ev.get("proceeds") or 0)
                            - float(ev.get("fee") or 0))),
                float(ev.get("fee") or 0),
                bool(ev.get("final", True)),
                price_kind=ev.get("price_kind"))
            if realized is None:
                return led, ("cash-split migration: credit %s: %s"
                             % (ev.get("op_id"), why))
    if abs(float(gen["cash"]) - float(led["cash"])) > 1e-6:
        return led, ("cash-split migration: replayed cash $%.6f "
                     "!= stored $%.6f; manual reconciliation "
                     "required" % (float(gen["cash"]),
                                   float(led["cash"])))
    for lane in set(list((gen.get("lanes") or {}).keys())
                    + list((led.get("lanes") or {}).keys())):
        g = set(((gen["lanes"].get(lane) or {}).get("open")
                 or {}).keys())
        s = set(((led["lanes"].get(lane) or {}).get("open")
                 or {}).keys())
        if g != s:
            return led, ("cash-split migration: open positions "
                         "diverge in lane %s; manual "
                         "reconciliation required" % lane)
    led["cash_spendable"] = gen["cash_spendable"]
    led["cash_estimated"] = gen["cash_estimated"]
    logger.warning("paper_account: cash split derived from journal "
                   "replay: spendable $%.6f, estimated $%.6f",
                   gen["cash_spendable"], gen["cash_estimated"])
    return led, None


def _load_locked() -> Tuple[Optional[Dict[str, Any]], bool,
                            Optional[str]]:
    """Read the ledger under the lock. Returns (ledger, healthy,
    degraded). First run creates it; a corrupt live file falls
    back to the .bak shadow; both unreadable -> (None, False, ...)
    so callers fail closed. A loaded ledger is reconciled against
    the journal (write-ahead log) so a crash or stale shadow can
    never resurrect spendable cash or a settled position. Journal
    damage beyond one torn trailing record, a seq hole, or a
    replay divergence leaves the account DEGRADED: debits are
    refused and credits are journaled-only until a human
    reconciles the WAL. Caller must hold _lock()."""
    degraded: Optional[str] = None
    entries, jstatus, tail_off = _journal_read()
    if jstatus == "torn_tail":
        logger.warning("paper_account: truncating torn journal "
                       "tail at byte %d", tail_off)
        try:
            _journal_truncate(tail_off)  # type: ignore[arg-type]
        except Exception:
            logger.exception("paper_account: torn-tail truncate "
                             "failed")
            degraded = "journal tail torn and truncation failed"
    elif jstatus == "torn":
        degraded = ("journal torn (unparseable record mid-file); "
                    "manual WAL reconciliation required")
    if degraded is None:
        hole = _seq_violation(entries)
        if hole is not None:
            degraded = ("journal seq violation %s -> %s; manual "
                        "WAL reconciliation required" % hole)
    if not os.path.exists(LEDGER_PATH):
        led = _new_ledger()
        _journal({"event": "create", "start_balance":
                  led["start_balance"]})
        _save_locked(led)
        return led, True, degraded
    led = _read_json(LEDGER_PATH)
    if led is None:
        logger.error("paper_account: %s unreadable, trying shadow",
                     LEDGER_PATH)
        led = _read_json(LEDGER_PATH + ".bak")
        if led is None:
            logger.error("paper_account: shadow also unreadable; "
                         "failing closed (debits refused)")
            return None, False, degraded or "ledger unreadable"
        logger.warning("paper_account: restored from shadow copy")
    led.setdefault("ops", {})
    if degraded is None:
        # Only a clean WAL may drive replay: with a torn journal or
        # a seq hole the entries cannot be trusted to be complete,
        # so the state is left exactly as read (debits are refused
        # anyway; see the degraded paths in try_debit/credit).
        seq_before = led.get("seq")
        led, divergence = _reconcile(led, entries)
        if divergence is not None:
            degraded = divergence
            logger.error("paper_account: %s", divergence)
        if led.get("seq") != seq_before:
            # Persist the reconciled state: otherwise the on-disk
            # file stays behind the journal and the NEXT load
            # replays the same mutations over the same stale state
            # (a credit would count twice). On divergence the
            # partial replay up to the offending entry is valid
            # and is kept.
            _save_locked(led)
    if degraded is None and "cash_spendable" not in led:
        # Bucket migration (design v4 Stage A): derive the split
        # from a genesis journal replay; exact or frozen.
        led, degraded = _derive_buckets_locked(led, entries)
        if degraded is None:
            _save_locked(led)
        else:
            logger.error("paper_account: %s", degraded)
    # Day rollover: reset the daily counter, keep the cumulative.
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if led.get("day") != today:
        led["day"] = today
        led["day_realized_pnl"] = 0.0
    return led, True, degraded


def _save_locked(led: Dict[str, Any]) -> None:
    """Caller must hold _lock(). Live file, then shadow (best
    effort), then sweep stale tmp files."""
    _atomic_write(LEDGER_PATH, led)
    try:
        _atomic_write(LEDGER_PATH + ".bak", led)
    except Exception as e:
        logger.warning("paper_account: shadow write failed: %s", e)
    try:
        cutoff = time.time() - 3600
        for pattern in (LEDGER_PATH + ".tmp.*",
                        LEDGER_PATH + ".bak.tmp.*"):
            for stale in glob.glob(pattern):
                try:
                    if os.path.getmtime(stale) < cutoff:
                        os.unlink(stale)
                except OSError:
                    pass
    except Exception:
        pass


def _op_lookup(led: Dict[str, Any], op_id: str, kind: str,
               lane: str, mint: str, params: Dict[str, Any]
               ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Caller-retry dedupe. Returns (record, None) when op_id was
    already applied with the SAME shape (caller should get the
    original outcome, never a re-apply), or (None, reason) when
    the op_id was used for something else (caller bug), or
    (None, None) when the op_id is new."""
    rec = led.get("ops", {}).get(op_id)
    if rec is None:
        return None, None
    if (rec.get("event") != kind or rec.get("lane") != lane
            or rec.get("mint") != mint):
        return None, ("op_id %r already used for %s %s/%s"
                      % (op_id, rec.get("event"), rec.get("lane"),
                         rec.get("mint")))
    for k, v in params.items():
        if rec.get(k) is not None and rec.get(k) != v:
            return None, ("op_id %r parameter conflict on %s"
                          % (op_id, k))
    return rec, None


def try_debit(lane: str, mint: str, stake: float, qty: float,
              op_id: str, ts: Optional[float] = None,
              marks: Optional[Dict[str, Dict[str, Any]]] = None
              ) -> Tuple[bool, str, Optional[str]]:
    """Reserve `stake` paper dollars for `lane` buying `qty` units
    of `mint`. op_id is the caller's stable operation ID: an
    uncertain response may be retried with the SAME op_id and gets
    the original outcome back without re-applying — even if the
    position was closed and reopened meanwhile.

    Returns (True, "ok", pos_id) or (False, reason, None). The
    pos_id immutably names this open instance; pass it back to
    credit() so a close can never settle a later reopen of the
    same mint. Fail-closed: any corruption, degradation or
    inconsistency refuses the debit — a refused paper entry costs
    nothing, a doubled one quietly invents money.

    When PAPER_ACCOUNT_GATES_ENABLED=1 (PROVISIONAL, default OFF),
    `marks` must carry the caller's current liquidation marks and
    the account-level risk gates run INSIDE the same lock as the
    reservation (see _gate_check_locked): evaluation and debit are
    one atomic section, so two lanes cannot each pass on the same
    available capacity. When `marks` is None and gates are enabled,
    the account collects them centrally (account_marks) BEFORE
    taking the lock; a position it cannot price blocks further
    entries fail-closed.
    """
    if not _valid_amount(stake) or stake <= 0:
        return False, "stake must be a positive finite number", None
    if not _valid_amount(qty) or qty <= 0:
        return False, "qty must be a positive finite number", None
    stake = float(stake)
    qty = float(qty)
    if not lane or not mint:
        return False, "lane and mint are required", None
    if not op_id or not isinstance(op_id, str):
        return False, "caller op_id is required", None
    if gates_enabled() and marks is None:
        marks = _central_marks()
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return (False, "account ledger unreadable "
                    "(fail-closed)", None)
        if degraded:
            return False, "account degraded: %s" % degraded, None
        rec, conflict = _op_lookup(led, op_id, "debit", lane, mint,
                                   {"stake": stake, "qty": qty})
        if conflict:
            return False, conflict, None
        if rec is not None:
            return True, "ok (retry of applied op)", rec["pos_id"]
        cash = float(led["cash"])
        spendable = float(led.get("cash_spendable", 0.0))
        if spendable < stake:
            return (False, ("account spendable cash $%.2f < stake "
                            "$%.2f (total $%.2f incl. quarantined "
                            "estimated proceeds)"
                            % (spendable, stake, cash)), None)
        st = _lane(led, lane)
        if mint in st["open"]:
            return (False, ("%s already has an open %s position"
                            % (lane, mint)), None)
        cap = lane_cap()
        if cap is not None:
            open_basis = sum(_pos_basis(p)
                             for p in st["open"].values())
            if open_basis + stake > cap:
                return (False, ("lane cap $%.2f would be exceeded "
                                "($%.2f open + $%.2f new)"
                                % (cap, open_basis, stake)), None)
        if gates_enabled():
            view = _view_locked(led, marks, degraded)
            _update_baselines_locked(led, view)
            view.update(_gate_fields(led, view))
            refusal = _gate_check_locked(led, view, stake)
            if refusal is not None:
                return False, refusal, None
        led["seq"] = int(led.get("seq") or 0) + 1
        pid = _pos_id(lane, mint, led["seq"])
        _apply_debit(led, lane, mint, stake, qty, pid, ts)
        led["ops"][op_id] = {"event": "debit", "lane": lane,
                             "mint": mint, "pos_id": pid,
                             "stake": stake, "qty": qty}
        _journal({"event": "debit", "event_id": "ev#%d" % led["seq"],
                  "op_id": op_id, "lane": lane, "mint": mint,
                  "stake": stake, "qty": qty,
                  "cash_after": led["cash"],
                  "seq": led["seq"], "pos_id": pid, "ts": ts})
        _save_locked(led)
        return True, "ok", pid


def credit(lane: str, mint: str, proceeds: float, op_id: str,
           ts: Optional[float] = None, pos_id: Optional[str] = None,
           qty: Optional[float] = None, fee: float = 0.0,
           price_kind: Optional[str] = None
           ) -> Tuple[bool, str]:
    """Settle a sale of `qty` units of `mint` (None = ALL remaining
    units, i.e. a full close) for `proceeds` gross paper dollars
    with `fee` in fees. Net proceeds (gross - fee) credit the
    shared cash immediately — tranche receipts are never held
    outside the account. Average-cost allocation: the sold units
    carry basis proportionally, and realized P/L = net - allocated
    basis is booked per sale (a profitable tranche that recovers
    more than the original stake realizes the gain while units
    remain; remaining basis is per-unit allocated and never goes
    negative).

    op_id is the caller's stable operation ID for THIS settlement
    (each tranche gets its own): retries return the original
    outcome without re-applying. When pos_id is given it must
    match the open instance; a mismatch means the caller holds a
    stale close (the mint was reopened since) and the credit is
    refused + journaled rather than settling the wrong instance.
    Returns (True, "ok") or (False, reason); a False return means
    nothing was credited — the event is still journaled so the
    money movement is recoverable by hand. On a DEGRADED account
    credits are journaled-only (never applied): a suspect ledger
    must not silently gain money.
    """
    if not _valid_amount(proceeds) or proceeds < 0:
        return False, "proceeds must be a nonnegative finite number"
    if not _valid_amount(fee) or fee < 0:
        return False, "fee must be a nonnegative finite number"
    proceeds = float(proceeds)
    fee = float(fee)
    if fee > proceeds:
        return False, "fee exceeds gross proceeds"
    if qty is not None and (not _valid_amount(qty) or qty <= 0):
        return False, "qty must be a positive finite number"
    if not lane or not mint:
        return False, "lane and mint are required"
    if not op_id or not isinstance(op_id, str):
        return False, "caller op_id is required"
    net = round(proceeds - fee, 6)
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            # Never drop realized money silently: journal it even
            # when the ledger cannot be updated.
            _journal({"event": "credit_dropped", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pos_id,
                      "reason": "ledger unreadable"})
            return False, "account ledger unreadable (journaled)"
        if degraded:
            _journal({"event": "credit_degraded", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pos_id,
                      "reason": degraded})
            return False, ("account degraded: %s (journaled for "
                           "manual reconciliation)" % degraded)
        rec, conflict = _op_lookup(led, op_id, "credit", lane, mint,
                                   {})
        if conflict:
            return False, conflict
        if rec is not None:
            return True, "ok (retry of applied op)"
        st = _lane(led, lane)
        pos = st["open"].get(mint)
        if pos is None:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pos_id})
            return False, ("no open %s position for %s (journaled)"
                           % (mint, lane))
        if pos_id and pos.get("pos_id") and pos["pos_id"] != pos_id:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pos_id,
                      "reason": "pos_id mismatch (open is %s)"
                                % pos["pos_id"]})
            return False, ("stale pos_id %s: %s is open as %s "
                           "(journaled, not applied)"
                           % (pos_id, mint, pos["pos_id"]))
        pid = pos.get("pos_id")
        realized, why = _apply_credit(led, lane, mint, qty, net,
                                      fee, qty is None,
                                      price_kind=price_kind)
        if realized is None:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pid,
                      "reason": why})
            return False, "%s (journaled, not applied)" % why
        led["seq"] = int(led.get("seq") or 0) + 1
        led["ops"][op_id] = {"event": "credit", "lane": lane,
                             "mint": mint, "pos_id": pid,
                             "realized": realized}
        _journal({"event": "credit", "event_id": "ev#%d" % led["seq"],
                  "op_id": op_id, "lane": lane, "mint": mint,
                  "qty": qty, "proceeds": proceeds, "fee": fee,
                  "net": net, "realized": realized,
                  "final": mint not in st["open"],
                  "cash_after": led["cash"],
                  "price_kind": price_kind,
                  "seq": led["seq"], "pos_id": pid, "ts": ts})
        _save_locked(led)
        return True, "ok"


def _apply_write_down(led: Dict[str, Any], lane: str, mint: str,
                      pid: str, reason: Optional[str],
                      ts: Optional[float]) -> Tuple[bool, str]:
    """Move an open position to the unresolved register (write-down
    of unquotable units). No cash moves, no fee, no realized P/L is
    recognized: a write-down is not a sale. Caller holds the lock.
    Idempotent per pos_id: a replay or retry of an applied
    write-down is a no-op."""
    reg = led.setdefault("unresolved", {})
    if pid in reg:
        return True, "already unresolved"
    st = _lane(led, lane)
    pos = st["open"].get(mint)
    if pos is None:
        return False, "no open position"
    if pos.get("pos_id") != pid:
        return False, ("pos_id mismatch (open is %s)"
                       % pos.get("pos_id"))
    reg[pid] = {"lane": lane, "mint": mint,
                "qty": pos.get("qty"),
                "basis": pos.get("basis"),
                "entry_qty": pos.get("entry_qty"),
                "entry_cost": pos.get("entry_cost"),
                "reason": reason, "ts": ts or time.time(),
                "status": "held_unresolved"}
    st["open"].pop(mint)
    return True, "ok"


def write_down(lane: str, mint: str, op_id: str,
               ts: Optional[float] = None,
               pos_id: Optional[str] = None,
               reason: Optional[str] = None
               ) -> Tuple[bool, str]:
    """Write a position down to zero carrying value: units and their
    original cost move to the unresolved register as
    held_unresolved (design v4 Stage B). NOT a sale: no fee, no
    proceeds, no realized loss recognized; the register keeps the
    original cost so a later evidence-led resolution can reconcile
    exactly. Same durability contract as credit(): journaled with
    seq before the state write, idempotent on op_id, pinned to the
    pos_id instance."""
    if not isinstance(lane, str) or not lane:
        return False, "bad lane"
    if not isinstance(mint, str) or not mint:
        return False, "bad mint"
    if not isinstance(op_id, str) or not op_id:
        return False, "bad op_id"
    ts = time.time() if ts is None else ts
    with _lock():
        led, healthy, degraded = _load_locked()
        if led is None:
            return False, "ledger unreadable"
        prior = led["ops"].get(op_id)
        if prior is not None:
            return True, "already applied"
        if degraded is not None:
            _journal({"event": "write_down_intent", "lane": lane,
                      "mint": mint, "op_id": op_id, "pos_id": pos_id,
                      "reason": reason, "ts": ts,
                      "note": "account degraded: intent journaled, "
                              "not applied"})
            return False, ("account degraded (%s): write-down "
                           "journaled, not applied" % degraded)
        pid = pos_id
        pos = _lane(led, lane)["open"].get(mint)
        if pid is None and pos is not None:
            pid = pos.get("pos_id")
        if pid is None:
            return False, "no open position"
        ok, why = _apply_write_down(led, lane, mint, pid, reason, ts)
        if not ok:
            _journal({"event": "write_down_refused", "lane": lane,
                      "mint": mint, "op_id": op_id, "pos_id": pid,
                      "reason": why, "ts": ts})
            return False, "%s (journaled, not applied)" % why
        led["seq"] = int(led.get("seq") or 0) + 1
        led["ops"][op_id] = {"event": "write_down", "lane": lane,
                             "mint": mint, "pos_id": pid}
        _journal({"event": "write_down",
                  "event_id": "ev#%d" % led["seq"],
                  "op_id": op_id, "lane": lane, "mint": mint,
                  "pos_id": pid, "reason": reason,
                  "qty": led["unresolved"][pid]["qty"],
                  "basis": led["unresolved"][pid]["basis"],
                  "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok"


def _mark_value(mark: Any, now: float, ttl: float,
                want_qty: Optional[float],
                want_pos_id: Optional[str]
                ) -> Tuple[Optional[float], Optional[str]]:
    """Validate one mark: {value, ts, source, qty, pos_id} with a
    finite, nonnegative value, an observation timestamp within the
    TTL in BOTH directions (future-dated sources are rejected, not
    trusted), a qty matching the position's CURRENT remaining
    units, and a pos_id matching the CURRENT open instance (the
    mark is collected before the account lock; a tranche close or
    a close/reopen in between must not let a stale-sized or
    stale-instance value through - same-qty reopens get a NEW
    pos_id precisely so their old marks die). ts is the source's
    observation time, so a cached price older than the TTL is
    stale even when the marker just wrapped it. Returns
    (value, None) or (None, problem). A mark that cannot prove it
    is fresh, well-formed and bound to the current position
    instance is not a mark."""
    if not isinstance(mark, dict):
        return None, ("malformed (need {value, ts, source, qty, "
                      "pos_id})")
    v = mark.get("value")
    if (not isinstance(v, (int, float)) or isinstance(v, bool)
            or not math.isfinite(v) or v < 0):
        return None, "invalid value %r" % (v,)
    mts = mark.get("ts")
    if (not isinstance(mts, (int, float))
            or isinstance(mts, bool) or not math.isfinite(mts)):
        return None, "no usable timestamp"
    if not mark.get("source") or not isinstance(mark["source"], str):
        return None, "no provenance"
    if abs(now - mts) > ttl:
        return None, "stale (%.0fs old, ttl %.0fs)" % (now - mts, ttl)
    if want_qty is None:
        return None, "position qty unknown (cannot bind mark)"
    mq = mark.get("qty")
    if (not isinstance(mq, (int, float)) or isinstance(mq, bool)
            or not math.isfinite(mq) or mq < 0):
        return None, "no qty binding"
    if abs(float(mq) - want_qty) > 1e-6 * max(1.0, abs(want_qty)):
        return None, ("qty drift (mark %.8g vs current %.8g - "
                      "position changed since collection)"
                      % (float(mq), want_qty))
    if not want_pos_id:
        return None, "position id unknown (cannot bind mark)"
    if mark.get("pos_id") != want_pos_id:
        return None, ("pos_id drift (mark %r vs current %r - "
                      "instance changed since collection)"
                      % (mark.get("pos_id"), want_pos_id))
    return float(v), None


def _view_locked(led: Dict[str, Any],
                 marks: Optional[Dict[str, Dict[str, Any]]],
                 degraded: Optional[str]) -> Dict[str, Any]:
    """Account view from an already-loaded ledger. Caller holds the
    lock. Shared by snapshot() and the gated debit path so both see
    IDENTICAL numbers (the gate never evaluates a different view
    than the one an observer would read)."""
    out_lanes = {}
    exposure = 0.0
    open_positions = []
    for name, st in led.get("lanes", {}).items():
        basis = 0.0
        positions = []
        for mint, p in st.get("open", {}).items():
            pb = _pos_basis(p)
            basis += pb
            open_positions.append((name, mint, _pos_qty(p),
                                   p.get("pos_id")))
            positions.append({"mint": mint, "qty": _pos_qty(p),
                              "basis": round(pb, 6),
                              "pos_id": p.get("pos_id")})
        exposure += basis
        out_lanes[name] = {
            "open_count": len(st.get("open", {})),
            "open_basis": round(basis, 6),
            "realized_pnl": st.get("realized_pnl", 0.0),
            "n_entries": st.get("n_entries", 0),
            "n_closes": st.get("n_closes", 0),
            "positions": positions,
        }
    cash = float(led["cash"])
    out = {
        "start_balance": led["start_balance"],
        "cash": round(cash, 6),
        "unresolved": sorted(
            (dict(v) for v in (led.get("unresolved")
                               or {}).values()),
            key=lambda v: v.get("ts") or 0),
        "unresolved_cost": round(sum(
            float(v.get("basis") or 0.0)
            for v in (led.get("unresolved") or {}).values()), 6),
        "cash_spendable": round(float(led.get("cash_spendable")
                                      or 0.0), 6),
        "cash_estimated": round(float(led.get("cash_estimated")
                                      or 0.0), 6),
        "cost_exposure": round(exposure, 6),
        "book_value": round(cash + exposure, 6),
        "realized_pnl": led.get("realized_pnl", 0.0),
        "day": led.get("day"),
        "day_realized_pnl": led.get("day_realized_pnl", 0.0),
        "degraded": degraded,
        "lanes": out_lanes,
    }
    marked = None
    problems = []
    by_source: Dict[str, float] = {}
    if not open_positions:
        # Nothing to mark: equity is exactly cash, fully trusted.
        marked = 0.0
    elif marks:
        total = 0.0
        by_source: Dict[str, float] = {}
        now = time.time()
        ttl = mark_ttl_s()
        for lane, mint, want_qty, want_pos_id in open_positions:
            m = (marks.get(lane) or {}).get(mint)
            if m is None:
                problems.append("%s/%s: no mark" % (lane, mint))
                continue
            v, problem = _mark_value(m, now, ttl, want_qty,
                                     want_pos_id)
            if problem is not None:
                problems.append("%s/%s: %s" % (lane, mint, problem))
            else:
                total += v  # type: ignore[operator]
                src = str(m.get("source"))
                by_source[src] = round(by_source.get(src, 0.0) + v,
                                       6)
        if not problems:
            marked = round(total, 6)
    if marked is not None:
        out["marks_status"] = "ok"
        out["marked_exposure"] = marked
        # Per-source split of the trusted marked value, so risk
        # evidence shows how much of "equity" is a curve SPOT
        # estimate (not a conservative liquidation value: it
        # ignores the price impact of selling the position back
        # into the curve) versus a Jupiter-listed price.
        out["marked_by_source"] = by_source
        out["marked_equity"] = round(cash + marked, 6)
        # CONSERVATIVE SERIES (quarantine acceptance, 2026-11-22):
        # the equity series every risk input consumes is
        # cash_SPENDABLE + marked exposure. Quarantined estimated
        # proceeds are excluded by construction, so a stale-price
        # tranche can neither boost risk headroom (baseline/peak
        # captured without it) nor mask a drawdown (the series is
        # internally consistent: estimates never enter it at any
        # point in time). marked_equity above stays the gross
        # diagnostic (total cash incl. quarantined).
        out["marked_equity_conservative"] = round(
            float(led.get("cash_spendable") or 0.0) + marked, 6)
        out["unrealized_pnl"] = round(marked - exposure, 6)
    elif marks or open_positions:
        out["marks_status"] = ("; ".join(problems) if problems
                               else "unavailable")
        out["marked_exposure"] = None
        out["marked_equity"] = None
        out["marked_equity_conservative"] = None
        out["unrealized_pnl"] = None
    else:
        out["marks_status"] = "unavailable"
        out["marked_exposure"] = None
        out["marked_equity"] = None
        out["marked_equity_conservative"] = None
        out["unrealized_pnl"] = None
    return out


def _update_baselines_locked(led: Dict[str, Any],
                             view: Dict[str, Any]) -> bool:
    """Capture day-start / peak equity from a trustworthy view.
    NEVER resets a baseline to a later favorable value on the same
    day; at a rollover with no trustworthy mark the previous
    baseline carries (the gate fields report stale and fail
    closed). Caller holds the lock. Returns True when changed."""
    if view["marks_status"] != "ok":
        return False
    if view.get("degraded") is not None \
            or "cash_spendable" not in led:
        # A degraded or pre-split account must not capture equity
        # baselines: the conservative series would read collapsed.
        return False
    me = view["marked_equity_conservative"]
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    changed = False
    baseline = led.get("day_baseline")
    if not isinstance(baseline, dict) or baseline.get("day") != today:
        led["day_baseline"] = {"day": today, "value": me, "ts": now}
        changed = True
    peak = led.get("peak_equity")
    if not isinstance(peak, dict)             or not _valid_amount(peak.get("value"))             or me > float(peak["value"]):
        led["peak_equity"] = {"value": me, "ts": now}
        changed = True
    return changed


def _gate_fields(led: Dict[str, Any], view: Dict[str, Any]
                 ) -> Dict[str, Any]:
    """daily_loss / drawdown_pct gate inputs: None unless BOTH the
    baseline/peak and the current equity are trustworthy (a stale
    baseline or missing marks = fail-closed inputs, never a silent
    fallback to book value)."""
    out = {"day_start_equity": None, "peak_equity": None,
           "daily_loss": None, "drawdown_pct": None}
    baseline = led.get("day_baseline")
    if isinstance(baseline, dict):
        out["day_start_equity"] = dict(baseline)
    peak = led.get("peak_equity")
    if isinstance(peak, dict):
        out["peak_equity"] = dict(peak)
    if view["marks_status"] != "ok":
        return out
    me = view["marked_equity_conservative"]
    today = view["day"]
    if (isinstance(baseline, dict)
            and _valid_amount(baseline.get("value"))
            and baseline.get("day") == today):
        out["daily_loss"] = round(
            max(0.0, float(baseline["value"]) - me), 6)
    if (isinstance(peak, dict) and _valid_amount(peak.get("value"))
            and float(peak["value"]) > 0):
        out["drawdown_pct"] = round(
            max(0.0, (float(peak["value"]) - me)
                / float(peak["value"]) * 100.0), 6)
    return out


def snapshot(marks: Optional[Dict[str, Dict[str, Any]]] = None
             ) -> Optional[Dict[str, Any]]:
    """Read-only account view. None when the ledger is unreadable.
    Includes "degraded": reason when the WAL is untrusted.

    ACCOUNTING CONTRACT (2026-11-22 review): cash + open allocated
    basis is BOOK value, NOT liquidation equity — a position down
    80% still carries its full remaining cost in book_value, so a
    drawdown gate reading book_value would miss the unrealized
    loss. The four quantities are kept explicitly separate:

      cash            uncommitted dollars (including net proceeds
                      of partial tranches already settled)
      cost_exposure   open positions at remaining allocated basis
      book_value      cash + cost_exposure (accounting identity,
                      never a drawdown input)
      realized_pnl    settled P/L only (cumulative + daily)

    `marks` optionally supplies current liquidation values of the
    REMAINING units as
    {lane: {mint: {"value": usd, "ts": epoch, "source": str}}}
    (the lanes own honest-fill marks; this module never fabricates
    them). Every mark is validated: finite, nonnegative, provenance
    present, and no older than PAPER_ACCOUNT_MARK_TTL_S. Only when
    EVERY open position has a valid fresh mark (or no position is
    open at all — equity is then exactly cash) does the view add
    marked_exposure / marked_equity / unrealized_pnl with
    marks_status "ok". Otherwise the marked fields are None and
    marks_status names the failure ("stale", "invalid", "no
    mark", "unavailable") — a stale or missing mark must degrade a
    gate to fail-closed, never silently fall back to cost.

    EQUITY BASELINES (gate inputs for Stage 2): on every snapshot
    with marks_status "ok" the marked equity is recorded as the
    day-start baseline when none exists for the current UTC day,
    and folded into the all-time peak. A baseline is NEVER reset
    to a later favorable value on the same day; when no
    trustworthy mark exists after a rollover the previous baseline
    carries with its original day visible, and daily_loss /
    drawdown_pct report None so a gate fails closed. Both are None
    until the first trustworthy mark.
    """
    try:
        with _lock():
            led, healthy, degraded = _load_locked()
            if not healthy or led is None:
                return None
            out = _view_locked(led, marks, degraded)
            if _update_baselines_locked(led, out):
                _save_locked(led)
            out.update(_gate_fields(led, out))
            return out
    except Exception:
        logger.exception("paper_account: snapshot failed")
        return None
