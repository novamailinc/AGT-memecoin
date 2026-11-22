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
  * account-level exposure = cash + open cost basis, per-lane and
    daily P/L are tracked here so account-level risk enforcement
    (Stage 2: daily-loss / drawdown gates) has verified numbers to
    gate on,
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

Crash-journal contract (Stage 1.2, 2026-11-22 review):

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
    manual reconciliation but not applied, so a suspect ledger
    never silently gains or resurrects money.
  * PARTIAL CLOSES. The shared account only ever credits a FULL
    close. A lane scaling out in tranches keeps the unsold
    remainder in its own ledger; the shared stake stays reserved
    until the position fully closes. Conservative on purpose: the
    account never under-reserves.

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

_MUTATION_EVENTS = ("debit", "credit")


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


def _new_ledger() -> Dict[str, Any]:
    now = time.time()
    return {
        "created_ts": now,
        "seq": 0,
        "start_balance": start_balance(),
        "cash": start_balance(),
        "realized_pnl": 0.0,
        "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
        "day_realized_pnl": 0.0,
        "lanes": {},
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


def _reconcile(led: Dict[str, Any], entries: List[Dict[str, Any]]
               ) -> Tuple[Dict[str, Any], Optional[str]]:
    """Replay journaled mutations the ledger has not reached.

    The journal is the write-ahead log (every mutation is journaled
    BEFORE the state save), so after a crash the ledger can be
    BEHIND the journal — and a stale .bak shadow can be behind by
    more. Without reconciliation, restoring that shadow would
    RESURRECT a settled position, and its later close would credit
    the money a second time. Every mutation carries a monotonic
    seq (the state version AFTER the mutation) and an immutable
    pos_id (the open INSTANCE it moved); replay applies journaled
    entries past the ledger's seq, idempotently:

      debit  — skipped when the same pos_id is already open,
               applied when the mint is closed; a mint open under
               a DIFFERENT pos_id is a divergence (stop, degraded);
      credit — applied when the matching pos_id is open; skipped
               when the mint is fully closed (already settled); a
               mint open under a different pos_id is a divergence
               (a credit for instance #1 must never settle a
               reopened instance #2).

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
        if kind == "debit" and lane and mint:
            pid = ev.get("pos_id") or _pos_id(lane, mint, ev_seq)
            st = _lane(led, lane)
            cur = st["open"].get(mint)
            if cur is not None:
                if cur.get("pos_id", _pos_id(lane, mint, ev_seq)) == pid:
                    led["seq"] = ev_seq
                    continue  # state already has it: idempotent
                return led, ("replay divergence: debit %s but %s "
                             "open as %s"
                             % (pid, mint, cur.get("pos_id")))
            stake = float(ev.get("stake") or 0)
            if stake <= 0 or float(led["cash"]) < stake:
                return led, ("replay cannot apply debit %s "
                             "(bad stake or cash)" % pid)
            led["cash"] = round(float(led["cash"]) - stake, 6)
            st["open"][mint] = {"stake": stake,
                                "ts": ev.get("ts") or time.time(),
                                "pos_id": pid}
            st["n_entries"] += 1
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
            pos = st["open"].pop(mint)
            proceeds = float(ev.get("proceeds") or 0)
            stake = float(pos["stake"])
            pnl = round(proceeds - stake, 6)
            led["cash"] = round(float(led["cash"]) + proceeds, 6)
            led["realized_pnl"] = round(
                float(led["realized_pnl"]) + pnl, 6)
            led["day_realized_pnl"] = round(
                float(led.get("day_realized_pnl") or 0.0) + pnl, 6)
            st["realized_pnl"] = round(
                float(st["realized_pnl"]) + pnl, 6)
            st["n_closes"] += 1
            replayed += 1
        led["seq"] = ev_seq
    if replayed:
        logger.warning("paper_account: reconciled %d journaled "
                       "mutation(s) the ledger had not reached",
                       replayed)
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
    else:
        seq_before = led.get("seq")
    if led.get("seq") != seq_before:
        # Persist the reconciled state: otherwise the on-disk file
        # stays behind the journal and the NEXT load replays the
        # same mutations over the same stale state (a credit would
        # count twice). On divergence the partial replay up to the
        # offending entry is valid and is kept.
        _save_locked(led)
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


def try_debit(lane: str, mint: str, stake: float,
              ts: Optional[float] = None
              ) -> Tuple[bool, str, Optional[str]]:
    """Reserve `stake` paper dollars for `lane` buying `mint`.

    Returns (True, "ok", pos_id) or (False, reason, None). The
    pos_id immutably names this open instance; pass it back to
    credit() so a close can never settle a later reopen of the
    same mint. Fail-closed: any corruption, degradation or
    inconsistency refuses the debit — a refused paper entry costs
    nothing, a doubled one quietly invents money.
    """
    try:
        stake = float(stake)
    except (TypeError, ValueError):
        return False, "bad stake %r" % (stake,), None
    if not (stake > 0):
        return False, "stake must be positive", None
    if not lane or not mint:
        return False, "lane and mint are required", None
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return (False, "account ledger unreadable "
                    "(fail-closed)", None)
        if degraded:
            return False, "account degraded: %s" % degraded, None
        cash = float(led["cash"])
        if cash < stake:
            return (False, ("account cash $%.2f < stake $%.2f"
                            % (cash, stake)), None)
        st = _lane(led, lane)
        if mint in st["open"]:
            return (False, ("%s already has an open %s position"
                            % (lane, mint)), None)
        cap = lane_cap()
        if cap is not None:
            open_basis = sum(float(p["stake"])
                             for p in st["open"].values())
            if open_basis + stake > cap:
                return (False, ("lane cap $%.2f would be exceeded "
                                "($%.2f open + $%.2f new)"
                                % (cap, open_basis, stake)), None)
        led["seq"] = int(led.get("seq") or 0) + 1
        pid = _pos_id(lane, mint, led["seq"])
        led["cash"] = round(cash - stake, 6)
        st["open"][mint] = {"stake": stake, "ts": ts or time.time(),
                            "pos_id": pid}
        st["n_entries"] += 1
        _journal({"event": "debit", "lane": lane, "mint": mint,
                  "stake": stake, "cash_after": led["cash"],
                  "seq": led["seq"], "pos_id": pid, "ts": ts})
        _save_locked(led)
        return True, "ok", pid


def credit(lane: str, mint: str, proceeds: float,
           ts: Optional[float] = None,
           pos_id: Optional[str] = None) -> Tuple[bool, str]:
    """Return `proceeds` paper dollars (stake + realized P/L) from
    closing lane's position in mint. FULL closes only: a lane
    scaling out in tranches tracks the remainder in its own
    ledger and credits the shared account when the position is
    fully closed (see module docstring).

    When pos_id is given it must match the open instance; a
    mismatch means the caller holds a stale close (the mint was
    reopened since) and the credit is refused + journaled rather
    than settling the wrong instance. P/L is computed against the
    recorded stake. Returns (True, "ok") or (False, reason); a
    False return means nothing was credited — the event is still
    journaled so the money movement is recoverable by hand. On a
    DEGRADED account credits are journaled-only (never applied):
    a suspect ledger must not silently gain money.
    """
    try:
        proceeds = float(proceeds)
    except (TypeError, ValueError):
        return False, "bad proceeds %r" % (proceeds,)
    if not (proceeds >= 0):
        return False, "proceeds cannot be negative"
    if not lane or not mint:
        return False, "lane and mint are required"
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            # Never drop realized money silently: journal it even
            # when the ledger cannot be updated.
            _journal({"event": "credit_dropped", "lane": lane,
                      "mint": mint, "proceeds": proceeds, "ts": ts,
                      "pos_id": pos_id,
                      "reason": "ledger unreadable"})
            return False, "account ledger unreadable (journaled)"
        if degraded:
            _journal({"event": "credit_degraded", "lane": lane,
                      "mint": mint, "proceeds": proceeds, "ts": ts,
                      "pos_id": pos_id, "reason": degraded})
            return False, ("account degraded: %s (journaled for "
                           "manual reconciliation)" % degraded)
        st = _lane(led, lane)
        pos = st["open"].get(mint)
        if pos is None:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds, "ts": ts,
                      "pos_id": pos_id})
            return False, ("no open %s position for %s (journaled)"
                           % (mint, lane))
        if pos_id and pos.get("pos_id") and pos["pos_id"] != pos_id:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds, "ts": ts,
                      "pos_id": pos_id,
                      "reason": "pos_id mismatch (open is %s)"
                                % pos["pos_id"]})
            return False, ("stale pos_id %s: %s is open as %s "
                           "(journaled, not applied)"
                           % (pos_id, mint, pos["pos_id"]))
        pos = st["open"].pop(mint)
        stake = float(pos["stake"])
        pnl = round(proceeds - stake, 6)
        led["cash"] = round(float(led["cash"]) + proceeds, 6)
        led["realized_pnl"] = round(
            float(led["realized_pnl"]) + pnl, 6)
        led["day_realized_pnl"] = round(
            float(led.get("day_realized_pnl") or 0.0) + pnl, 6)
        st["realized_pnl"] = round(
            float(st["realized_pnl"]) + pnl, 6)
        st["n_closes"] += 1
        led["seq"] = int(led.get("seq") or 0) + 1
        _journal({"event": "credit", "lane": lane, "mint": mint,
                  "stake": stake, "proceeds": proceeds, "pnl": pnl,
                  "cash_after": led["cash"],
                  "seq": led["seq"],
                  "pos_id": pos.get("pos_id"), "ts": ts})
        _save_locked(led)
        return True, "ok"


def _mark_value(mark: Any, now: float, ttl: float
                ) -> Tuple[Optional[float], Optional[str]]:
    """Validate one mark: {value, ts, source} with a finite,
    nonnegative value and a timestamp within the TTL. Returns
    (value, None) or (None, problem). A mark that cannot prove it
    is fresh and well-formed is not a mark."""
    if not isinstance(mark, dict):
        return None, "malformed (need {value, ts, source})"
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
    return float(v), None


def snapshot(marks: Optional[Dict[str, Dict[str, Any]]] = None
             ) -> Optional[Dict[str, Any]]:
    """Read-only account view. None when the ledger is unreadable.
    Includes "degraded": reason when the WAL is untrusted.

    ACCOUNTING CONTRACT (2026-11-22 review): cash + open cost basis
    is BOOK value, NOT liquidation equity — a position down 80%
    still carries its full cost in book_value, so a drawdown gate
    reading book_value would miss the unrealized loss. The four
    quantities are kept explicitly separate:

      cash            uncommitted dollars
      cost_exposure   open positions at entry cost
      book_value      cash + cost_exposure (accounting identity,
                      never a drawdown input)
      realized_pnl    settled P/L only (cumulative + daily)

    `marks` optionally supplies current executable values as
    {lane: {mint: {"value": usd, "ts": epoch, "source": str}}}
    (the lanes own honest-fill marks; this module never fabricates
    them). Every mark is validated: finite, nonnegative, provenance
    present, and no older than PAPER_ACCOUNT_MARK_TTL_S. Only when
    EVERY open position has a valid fresh mark does the view add
    marked_exposure / marked_equity / unrealized_pnl with
    marks_status "ok". Otherwise the marked fields are None and
    marks_status names the failure ("stale", "invalid", "partial",
    "unavailable") — a stale or missing mark must degrade a gate
    to fail-closed, never silently fall back to cost.
    """
    try:
        with _lock():
            led, healthy, degraded = _load_locked()
            if not healthy or led is None:
                return None
            out_lanes = {}
            exposure = 0.0
            open_positions = []
            for name, st in led.get("lanes", {}).items():
                basis = 0.0
                for mint, p in st.get("open", {}).items():
                    basis += float(p["stake"])
                    open_positions.append((name, mint))
                exposure += basis
                out_lanes[name] = {
                    "open_count": len(st.get("open", {})),
                    "open_basis": round(basis, 6),
                    "realized_pnl": st.get("realized_pnl", 0.0),
                    "n_entries": st.get("n_entries", 0),
                    "n_closes": st.get("n_closes", 0),
                }
            cash = float(led["cash"])
            out = {
                "start_balance": led["start_balance"],
                "cash": round(cash, 6),
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
            if marks:
                total = 0.0
                now = time.time()
                ttl = mark_ttl_s()
                for lane, mint in open_positions:
                    m = (marks.get(lane) or {}).get(mint)
                    if m is None:
                        problems.append("%s/%s: no mark"
                                        % (lane, mint))
                        continue
                    v, problem = _mark_value(m, now, ttl)
                    if problem is not None:
                        problems.append("%s/%s: %s"
                                        % (lane, mint, problem))
                    else:
                        total += v  # type: ignore[operator]
                if not problems:
                    marked = round(total, 6)
            if marked is not None:
                out["marks_status"] = "ok"
                out["marked_exposure"] = marked
                out["marked_equity"] = round(cash + marked, 6)
                out["unrealized_pnl"] = round(marked - exposure, 6)
            elif marks:
                out["marks_status"] = "; ".join(problems)
                out["marked_exposure"] = None
                out["marked_equity"] = None
                out["unrealized_pnl"] = None
            else:
                out["marks_status"] = "unavailable"
                out["marked_exposure"] = None
                out["marked_equity"] = None
                out["unrealized_pnl"] = None
            return out
    except Exception:
        logger.exception("paper_account: snapshot failed")
        return None
