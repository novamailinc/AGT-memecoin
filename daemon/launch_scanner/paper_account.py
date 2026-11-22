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

PAPER ONLY. This account is simulated dollars; nothing here
touches, custodies, or prices real funds.

Env knobs (all optional; defaults are conservative engineering
defaults chosen for a $10 demo, flagged here as agent-chosen):
  PAPER_ACCOUNT_START          starting cash (default 10.00)
  PAPER_ACCOUNT_LANE_CAP       max simultaneous open cost basis
                               per lane (default: no cap)
"""

from __future__ import annotations

import contextlib
import fcntl
import glob
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

HOME = os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser(
    "~/.forex-agent")
ACCOUNT_DIR = os.path.join(HOME, "paper_account")
LEDGER_PATH = os.path.join(ACCOUNT_DIR, "ledger.json")
JOURNAL_PATH = os.path.join(ACCOUNT_DIR, "events.jsonl")
LOCK_FILE = os.path.join(ACCOUNT_DIR, ".lock")

DEFAULT_START = 10.00  # owner-approved $10 example (2026-11-22)


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


def _journal_entries() -> list:
    try:
        with open(JOURNAL_PATH) as f:
            out = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
            return out
    except Exception:
        return []


def _reconcile(led: Dict[str, Any]) -> Dict[str, Any]:
    """Replay journaled mutations the ledger has not reached.

    The journal is the write-ahead log (every mutation is journaled
    BEFORE the state save), so after a crash the ledger can be BEHIND
    the journal — and a stale .bak shadow can be behind by more.
    Without reconciliation, restoring that shadow would RESURRECT a
    settled position, and its later close would credit the money a
    second time. Every mutation carries a monotonic seq (the state
    version AFTER the mutation); replay applies journaled entries
    past the ledger's seq, idempotently:

      debit  — skipped when the position is already open (the state
               already has it), applied otherwise;
      credit — skipped when the position is NOT open (already
               settled), applied otherwise;
      orphan/dropped credit records and the create marker carry no
      state effect and are never replayed.

    Entries without a seq predate this scheme and cannot be ordered
    against the state; reconciliation runs only when BOTH sides
    carry seqs, and anything ambiguous is logged, never guessed.
    """
    led_seq = led.get("seq")
    if not isinstance(led_seq, int):
        return led
    pending = []
    for ev in _journal_entries():
        ev_seq = ev.get("seq")
        if not isinstance(ev_seq, int):
            continue
        if ev_seq > led_seq:
            pending.append((ev_seq, ev))
    if not pending:
        return led
    pending.sort(key=lambda t: t[0])
    replayed = 0
    for ev_seq, ev in pending:
        kind = ev.get("event")
        lane, mint = ev.get("lane"), ev.get("mint")
        if kind == "debit" and lane and mint:
            st = _lane(led, lane)
            if mint in st["open"]:
                continue  # state already has it: idempotent skip
            stake = float(ev.get("stake") or 0)
            if stake <= 0 or float(led["cash"]) < stake:
                logger.error("paper_account: reconcile cannot replay "
                             "debit %s/%s (bad stake or cash); "
                             "stopping replay", lane, mint)
                break
            led["cash"] = round(float(led["cash"]) - stake, 6)
            st["open"][mint] = {"stake": stake,
                                "ts": ev.get("ts") or time.time()}
            st["n_entries"] += 1
            replayed += 1
        elif kind == "credit" and lane and mint:
            st = _lane(led, lane)
            pos = st["open"].pop(mint, None)
            if pos is None:
                continue  # already settled: idempotent skip
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
    return led


def _load_locked() -> Tuple[Optional[Dict[str, Any]], bool]:
    """Read the ledger under the lock. Returns (ledger, healthy).
    First run creates it; a corrupt live file falls back to the
    .bak shadow; both unreadable -> (None, False) so callers fail
    closed. A loaded ledger is reconciled against the journal
    (write-ahead log) so a crash or stale shadow can never
    resurrect spendable cash or a settled position. Caller must
    hold _lock()."""
    if not os.path.exists(LEDGER_PATH):
        led = _new_ledger()
        _journal({"event": "create", "start_balance":
                  led["start_balance"]})
        _save_locked(led)
        return led, True
    led = _read_json(LEDGER_PATH)
    if led is None:
        logger.error("paper_account: %s unreadable, trying shadow",
                     LEDGER_PATH)
        led = _read_json(LEDGER_PATH + ".bak")
        if led is None:
            logger.error("paper_account: shadow also unreadable; "
                         "failing closed (debits refused)")
            return None, False
        logger.warning("paper_account: restored from shadow copy")
    seq_before = led.get("seq")
    led = _reconcile(led)
    if led.get("seq") != seq_before:
        # Persist the reconciled state: otherwise the on-disk file
        # stays behind the journal and the NEXT load replays the
        # same mutations over the same stale state (a credit would
        # count twice).
        _save_locked(led)
    # Day rollover: reset the daily counter, keep the cumulative.
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if led.get("day") != today:
        led["day"] = today
        led["day_realized_pnl"] = 0.0
    return led, True


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
              ts: Optional[float] = None) -> Tuple[bool, str]:
    """Reserve `stake` paper dollars for `lane` buying `mint`.

    Returns (True, "ok") or (False, reason). Fail-closed: any
    corruption or inconsistency refuses the debit — a refused paper
    entry costs nothing, a doubled one quietly invents money.
    """
    try:
        stake = float(stake)
    except (TypeError, ValueError):
        return False, "bad stake %r" % (stake,)
    if not (stake > 0):
        return False, "stake must be positive"
    if not lane or not mint:
        return False, "lane and mint are required"
    with _lock():
        led, healthy = _load_locked()
        if not healthy or led is None:
            return False, "account ledger unreadable (fail-closed)"
        cash = float(led["cash"])
        if cash < stake:
            return False, ("account cash $%.2f < stake $%.2f"
                           % (cash, stake))
        st = _lane(led, lane)
        if mint in st["open"]:
            return False, ("%s already has an open %s position"
                           % (lane, mint))
        cap = lane_cap()
        if cap is not None:
            open_basis = sum(float(p["stake"])
                             for p in st["open"].values())
            if open_basis + stake > cap:
                return False, ("lane cap $%.2f would be exceeded "
                               "($%.2f open + $%.2f new)"
                               % (cap, open_basis, stake))
        led["cash"] = round(cash - stake, 6)
        st["open"][mint] = {"stake": stake, "ts": ts or time.time()}
        st["n_entries"] += 1
        led["seq"] = int(led.get("seq") or 0) + 1
        _journal({"event": "debit", "lane": lane, "mint": mint,
                  "stake": stake, "cash_after": led["cash"],
                  "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok"


def credit(lane: str, mint: str, proceeds: float,
           ts: Optional[float] = None) -> Tuple[bool, str]:
    """Return `proceeds` paper dollars (stake + realized P/L) from
    closing lane's position in mint. P/L is computed against the
    recorded stake. Returns (True, "ok") or (False, reason); a
    False return means NO open position was found and nothing was
    credited — the event is still journaled so the money movement
    is recoverable by hand."""
    try:
        proceeds = float(proceeds)
    except (TypeError, ValueError):
        return False, "bad proceeds %r" % (proceeds,)
    if not (proceeds >= 0):
        return False, "proceeds cannot be negative"
    if not lane or not mint:
        return False, "lane and mint are required"
    with _lock():
        led, healthy = _load_locked()
        if not healthy or led is None:
            # Never drop realized money silently: journal it even
            # when the ledger cannot be updated.
            _journal({"event": "credit_dropped", "lane": lane,
                      "mint": mint, "proceeds": proceeds, "ts": ts,
                      "reason": "ledger unreadable"})
            return False, "account ledger unreadable (journaled)"
        st = _lane(led, lane)
        pos = st["open"].pop(mint, None)
        if pos is None:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds, "ts": ts})
            return False, ("no open %s position for %s (journaled)"
                           % (mint, lane))
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
                  "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok"


def snapshot(marks: Optional[Dict[str, Dict[str, float]]] = None
             ) -> Optional[Dict[str, Any]]:
    """Read-only account view. None when the ledger is unreadable.

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
    {lane: {mint: value_usd}} (the lanes own honest-fill marks;
    this module never fabricates them). With complete marks the
    view adds marked_exposure, marked_equity and unrealized_pnl
    and marks_status "ok". With any open position unmarked the
    marked fields are None and marks_status says "partial" — a
    stale or missing mark must degrade a gate to fail-closed,
    never silently fall back to cost.
    """
    try:
        with _lock():
            led, healthy = _load_locked()
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
                "lanes": out_lanes,
            }
            marked = None
            missing = []
            if marks:
                total = 0.0
                for lane, mint in open_positions:
                    v = (marks.get(lane) or {}).get(mint)
                    if v is None:
                        missing.append("%s/%s" % (lane, mint))
                    else:
                        total += float(v)
                if not missing:
                    marked = round(total, 6)
            if marked is not None:
                out["marks_status"] = "ok"
                out["marked_exposure"] = marked
                out["marked_equity"] = round(cash + marked, 6)
                out["unrealized_pnl"] = round(marked - exposure, 6)
            elif marks:
                out["marks_status"] = ("partial: no mark for %s"
                                       % ", ".join(missing))
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
