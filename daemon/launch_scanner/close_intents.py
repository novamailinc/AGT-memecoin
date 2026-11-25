#!/usr/bin/env python3
"""Cross-ledger close-intent journal (2026-11-22, Stage C).

A lane close touches TWO durable ledgers: the shared paper account
(credit / write_down) and the lane's own ledger (bankroll, closes).
Today the lane settles the account and then writes its ledger; a
process death between those writes leaves the account credited and
the lane position still open (or, when the account settle fails,
the lane closed and the account position held forever). Each side
is individually crash-safe (atomic writes, idempotent op_ids), but
nothing records that a close SPANS both, so no startup can prove
the two ledgers converged.

This module is the missing intent record, per design v4:
append-only events with a durable payload sufficient to rebuild
BOTH sides, and recovery that re-applies whatever is missing.

Event flow for one close (fsync after every append):

    intent            payload: lane, mint, pos_id, the settle ops
                      (kind/amount/qty/price_kind/op_id each) and
                      the close essentials (realized, entry
                      notional, reason). Written BEFORE the first
                      account op.
    account_applied   every settle op returned ok or duplicate
                      (idempotent replay counts - the op_id is the
                      dedupe key on the account side).
    lane_applied      the lane ledger write completed.
    applied           terminal: both sides durable.
    frozen            recovery found a contradiction it cannot
                      repair (exact-or-freeze, same rule as the
                      account migration).

Recovery (recover_lane) runs at the lane's first settle after
start: every intent without a terminal `applied` is re-driven -
account ops re-applied (idempotent via op_id), the lane side
re-booked from the payload when the lane ledger lacks the close,
then `applied`. A settle op the account refuses outright freezes
the intent: the ledgers diverge and no automatic rewrite can
justify inventing the state.

One journal file per lane (lanes are separate processes; per-lane
files keep appends single-writer). Torn-tail handling mirrors
paper_account: exactly one unparseable record at the end is a
crash mid-append and is truncated under the lane lock; anything
else is corruption and freezes recovery.
"""

import fcntl
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _home() -> str:
    return os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser(
        "~/.forex_agent")


def journal_path(lane: str) -> str:
    return os.path.join(_home(), "close_intents_%s.jsonl" % lane)


def _append(path: str, event: Dict[str, Any]) -> bool:
    """Append one event, flush+fsync. Returns False on failure
    (logged); a lost intent event is safer than a false one - the
    account and lane ledgers still carry their own idempotent
    records, and a missing journal entry reads as an intent that
    never reached the failed side."""
    event = dict(event)
    event["ts"] = event.get("ts") or time.time()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _append_lock(path):
            prefix = ""
            try:
                with open(path, "rb") as _f:
                    _f.seek(0, 2)
                    _size = _f.tell()
                    if _size:
                        _f.seek(_size - 1)
                        _last = _f.read(1)
                    else:
                        _last = b"\n"
            except FileNotFoundError:
                _last = b"\n"
            if _last != b"\n":
                # Unterminated tail: appending would MERGE our record into the half line. Repair only a PROVABLE torn tail (exactly one
                # unparseable last record, the crash-mid-append signature, truncated like recover_lane); a complete last record that merely
                # lacks its newline gets the newline first; any other corruption is never healed and nothing is appended.
                _e, _st, _tail = _read(path)
                if _st is None:
                    prefix = "\n"
                elif _st == "torn_tail" and _tail is not None:
                    _truncate(path, _tail)
                    logger.error("close_intents: torn tail truncated at %s before append (%s)", _tail, path)
                else:
                    logger.error("close_intents: journal not cleanly readable (%s); refusing append (%s)", _st, event.get("ev"))
                    return False
            with open(path, "a") as f:
                f.write(prefix + json.dumps(event) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return True
    except Exception:
        logger.exception("close_intents: append failed (%s)",
                         event.get("ev"))
        return False


_APPEND_TL: Dict[str, "threading.Lock"] = {}


class _append_lock:
    """Serializes the tail check + repair + append across threads and processes (flock on a sidecar file)."""
    def __init__(self, path):
        self.path = path
        self.tl = _APPEND_TL.setdefault(path, threading.Lock())
        self.fh = None

    def __enter__(self):
        self.tl.acquire()
        try:
            import fcntl
            self.fh = open(self.path + ".append.lock", "a")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except Exception:
            self.tl.release()
            raise
        return self

    def __exit__(self, *a):
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        finally:
            self.tl.release()


def _read(path: str) -> Tuple[List[Dict[str, Any]], Optional[str],
                              Optional[int]]:
    """Parse the journal. Returns (entries, status, tail_offset)
    with the paper_account conventions: status None when clean,
    'torn_tail' for exactly one unparseable record at the end
    (crash mid-append; truncate at tail_offset), 'torn' for any
    other unparseable content (freeze - no automatic repair)."""
    entries: List[Dict[str, Any]] = []
    bad: List[int] = []
    last_off: Optional[int] = None
    try:
        with open(path, "rb") as f:
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


def _truncate(path: str, offset: int) -> None:
    with open(path, "r+") as f:
        f.truncate(offset)
        f.flush()
        os.fsync(f.fileno())


def _state(entries: List[Dict[str, Any]]
           ) -> Dict[str, Dict[str, Any]]:
    """intent_id -> {intent, account_applied, lane_applied,
    applied, frozen}."""
    states: Dict[str, Dict[str, Any]] = {}
    for ev in entries:
        iid = ev.get("intent_id")
        if not iid:
            continue
        st = states.setdefault(iid, {"intent": None,
                                     "account_applied": False,
                                     "lane_applied": False,
                                     "applied": False,
                                     "frozen": None})
        kind = ev.get("ev")
        if kind == "intent":
            st["intent"] = ev
        elif kind == "account_applied":
            st["account_applied"] = True
        elif kind == "lane_applied":
            st["lane_applied"] = True
        elif kind == "applied":
            st["applied"] = True
        elif kind == "frozen":
            st["frozen"] = ev.get("why") or "frozen"
    return states


class IntentNotDurable(RuntimeError):
    """The intent (or a freeze) could not be written to the journal. The caller MUST NOT apply the account side without a durable
    plan: recovery can only converge an intent that exists on disk."""


def begin(lane: str, mint: str, pos_id: Optional[str],
          settle_ops: List[Dict[str, Any]],
          close_info: Dict[str, Any], stable_id: bool = False) -> str:
    """Open an intent. settle_ops is the ordered list of account
    operations this close performs, each {kind: credit|write_down,
    op_id, amount?, qty?, price_kind?, reason?}; close_info carries
    what the lane ledger needs to re-book the close (realized_usd,
    entry_notional_usd, closed_reason). Returns the intent id.
    Raises IntentNotDurable when the intent event could not be durably appended (fail closed: no account op may run without it).
    stable_id=True: idempotent per OPERATION KEY (lane:mint:pos:close_ts), see below; the intent id itself is always unique."""
    intent_id = "%s:%s:%s:%d" % (lane, mint, pos_id or "legacy",
                                 int(close_info.get("close_ts")
                                     or time.time() * 1000))
    # The base id has one-second resolution (close_ts is epoch seconds), so a freeze followed by a new begin for the same position in the
    # same second, or two concurrent begins, would reuse an id and the new intent would read as the old terminal one. Uniqueness must not
    # depend on reading the journal (unreadable evidence would silently restore the collision) nor on a read/choose/append sequence
    # (racy across threads and processes): add a random 96-bit suffix. Ids are opaque (nothing parses them) and a begin is a new intent.
    # Intent identity is ALWAYS unique (random suffix). A caller whose retry must not open a second intent for the same logical operation
    # passes stable_id=True: the deterministic base id then serves as a separate OPERATION KEY stored on the intent event, and a retry finds
    # the existing intent by that key (journal read cleanly, under a lock) and returns ITS unique id after comparing the recorded plan.
    op_key = intent_id if stable_id else None
    intent_id = "%s:%s" % (intent_id, uuid.uuid4().hex[:24])
    ev = {"ev": "intent", "intent_id": intent_id, "lane": lane, "mint": mint, "pos_id": pos_id,
          "settle_ops": settle_ops, "close_info": dict(close_info)}
    if op_key is None:
        with _op_key_lock(lane):
            ents, st, _ = _read(journal_path(lane))
            if st == "torn_tail":
                # the append below repairs a provable torn tail; re-read after to prove the journal is clean
                pass
            elif st is not None:
                raise IntentNotDurable("journal not cleanly readable (%s): refusing to open an intent" % st)
            _ids = sorted(str(o.get("op_id")) for o in settle_ops)
            for iid0, st0 in _state(ents).items():
                e0 = st0["intent"]
                if (e0 is not None and not st0["applied"] and not st0["frozen"] and e0.get("mint") == mint and e0.get("pos_id") == pos_id
                        and sorted(str(o.get("op_id")) for o in e0.get("settle_ops") or []) == _ids):
                    # same logical operation still pending (crash before its terminal event): RESUME it, never open a second intent
                    if json.dumps(e0.get("settle_ops"), sort_keys=True) != json.dumps(settle_ops, sort_keys=True):
                        raise IntentNotDurable("operation %s already pending with a DIFFERENT plan" % iid0)
                    return iid0
            if not _append(journal_path(lane), ev):
                raise IntentNotDurable("close intent %s could not be journaled" % intent_id)
            if _read(journal_path(lane))[1] is not None:
                raise IntentNotDurable("journal not cleanly readable after append")
        return intent_id
    ev["op_key"] = op_key
    with _op_key_lock(lane):
        ents, st, _ = _read(journal_path(lane))
        if st is not None:
            raise IntentNotDurable("journal not cleanly readable (%s): cannot prove operation %s is new" % (st, op_key))
        for e in ents:
            if e.get("ev") == "intent" and e.get("op_key") == op_key:
                _ci0 = {k: v for k, v in (e.get("close_info") or {}).items() if k != "close_ts"}
                _ci1 = {k: v for k, v in close_info.items() if k != "close_ts"}
                same = (json.dumps(e.get("settle_ops"), sort_keys=True) == json.dumps(settle_ops, sort_keys=True)
                        and e.get("mint") == mint and e.get("pos_id") == pos_id
                        and json.dumps(_ci0, sort_keys=True) == json.dumps(_ci1, sort_keys=True))   # economics + reason, not only the ops
                if not same:
                    raise IntentNotDurable("operation %s already has intent %s with a DIFFERENT plan: refusing to reuse or replace it"
                                           % (op_key, e.get("intent_id")))
                return e["intent_id"]
        if not _append(journal_path(lane), ev):
            raise IntentNotDurable("close intent %s could not be journaled" % intent_id)
    return intent_id


_THREAD_LOCKS: Dict[str, "threading.Lock"] = {}


class _op_key_lock:
    """Serializes read-check-append of a stable-key begin across threads (a lock) and processes (flock on a sidecar file)."""
    def __init__(self, lane):
        self.path = journal_path(lane) + ".lock"
        self.tl = _THREAD_LOCKS.setdefault(self.path, threading.Lock())
        self.fh = None
    def __enter__(self):
        self.tl.acquire()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.fh = open(self.path, "a")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except Exception as e:
            self.__exit__(None, None, None)
            raise IntentNotDurable("cannot take the intent lock: %r" % (e,))
        return self
    def __exit__(self, *a):
        try:
            if self.fh is not None:
                fcntl.flock(self.fh, fcntl.LOCK_UN); self.fh.close()
        finally:
            self.fh = None
            self.tl.release()


def intent_status(lane: str, intent_id: str) -> Optional[str]:
    """'applied' | 'frozen' | 'open' for an intent id, None when absent. Raises IntentNotDurable when the journal is not cleanly readable."""
    ents, st, _ = _read(journal_path(lane))
    if st is not None:
        raise IntentNotDurable("journal not cleanly readable (%s)" % st)
    s_ = _state(ents).get(intent_id)
    if not s_ or s_["intent"] is None:
        return None
    return "applied" if s_["applied"] else ("frozen" if s_["frozen"] is not None else "open")


def mark_account_applied(lane: str, intent_id: str) -> bool:
    """Returns False when the marker was not durably written. The intent then stays open: recovery re-applies by the idempotent op ids
    (a lost marker never loses an account op, it only delays convergence), so callers continue but may surface the False."""
    return bool(_append(journal_path(lane), {"ev": "account_applied", "intent_id": intent_id}))


def mark_lane_applied(lane: str, intent_id: str) -> bool:
    path = journal_path(lane)
    ok1 = bool(_append(path, {"ev": "lane_applied", "intent_id": intent_id}))
    # Terminal event in the same pass when both sides are durable: the account side always precedes the lane write by design.
    # The terminal "applied" is written ONLY if the lane marker landed (a terminal state must never outrun its evidence).
    return bool(ok1 and _append(path, {"ev": "applied", "intent_id": intent_id}))


def journal_writable(lane: str) -> bool:
    """Durability pre-check for a caller that must not SELL (or book anything) unless it can later make its close intent durable: appends a
    harmless "probe" event (no intent_id, so _state and recovery ignore it) and returns whether it was fsynced. A pass is evidence for NOW only;
    begin() still raises IntentNotDurable if the real intent write fails."""
    if not _append(journal_path(lane), {"ev": "probe", "lane": lane}):
        return False
    return _read(journal_path(lane))[1] is None


def freeze(lane: str, intent_id: str, why: str) -> bool:
    """Append a terminal frozen event. Returns False when it could not be written (callers that must not proceed without it check)."""
    return bool(_append(journal_path(lane), {"ev": "frozen",
                                             "intent_id": intent_id,
                                             "why": why}))


def pending(lane: str) -> List[Dict[str, Any]]:
    """Intents with no terminal applied/frozen, for inspection and
    tests."""
    entries, status, _ = _read(journal_path(lane))
    if status == "torn":
        return []
    return [dict(st, intent_id=iid)
            for iid, st in _state(entries).items()
            if st["intent"] is not None and not st["applied"]
            and st["frozen"] is None]


def tranche_lane_done(d: Dict[str, Any], pos_id: Optional[str],
                      mint: str, suffix: Optional[str]) -> bool:
    """True when lane-ledger dict `d` reflects the lane-side write
    for a TRANCHE-lane intent (watcher/ceo/momentum shape:
    positions with legs a/b/c, closes list). Matching rules:
    a close record carrying this pos_id always counts (the
    position fully closed); otherwise the open position must
    belong to the same account instance and show the intent's
    legs done: 'leg:X' -> leg X done, 'remaining:XYZ' -> all of
    X,Y,Z done, 'writedown' -> any leg done at write_down_zero."""
    for rec in d.get("closes", []):
        if pos_id and rec.get("pa_pos_id") == pos_id:
            return True
        if not pos_id and rec.get("mint") == mint:
            return True
    pos = (d.get("positions") or {}).get(mint)
    if pos is None:
        return False
    if pos_id and pos.get("pa_pos_id") != pos_id:
        return False
    legs = pos.get("legs") or {}
    if suffix and suffix.startswith("leg:"):
        return bool((legs.get(suffix[4:]) or {}).get("done"))
    if suffix and suffix.startswith("remaining:"):
        ks = suffix[10:]
        return bool(ks) and all(
            (legs.get(k) or {}).get("done") for k in ks)
    if suffix == "writedown":
        return any(v.get("done")
                   and v.get("exit_price_kind") == "write_down_zero"
                   for v in legs.values())
    return False


def recover_lane(
    lane: str,
    account_apply: Callable[[Dict[str, Any]], Tuple[bool, str]],
    lane_has_close: Callable[[str, Optional[str]], bool],
    lane_apply: Callable[[Dict[str, Any]], bool],
) -> List[str]:
    """Re-drive every incomplete intent for this lane.

    account_apply(settle_op) re-applies one account operation and
    must be idempotent (the account dedupes by op_id; a duplicate
    is a successful apply). lane_has_close(intent) reports
    whether the lane ledger already reflects this intent's close
    (per-position for whole-close lanes, per-leg for tranche
    lanes - the intent payload carries the markers).
    lane_apply(intent) is tri-state: True = the lane ledger now
    holds the close (re-booked from the durable payload); None =
    leave the intent pending without freezing (the lane's natural
    re-close of a still-open position will book it on the next
    poll); False = contradiction, freeze.

    Returns a list of frozen intent ids (empty = fully converged).
    A torn journal freezes recovery entirely: every incomplete
    intent id is returned without any re-application, because the
    journal's own history is suspect.
    """
    path = journal_path(lane)
    entries, status, tail = _read(path)
    if status == "torn":
        logger.error("close_intents: %s journal corrupt; recovery "
                     "frozen", lane)
        return [iid for iid, st in _state(entries).items()
                if st["intent"] is not None and not st["applied"]]
    if status == "torn_tail" and tail is not None:
        try:
            with _op_key_lock(lane), _append_lock(path):
                # re-read under the SAME serialization as begin/_append (no concurrent truncating writers); repair only if still provable
                entries, status, tail = _read(path)
                if status == "torn_tail" and tail is not None:
                    _truncate(path, tail)
                    entries, status, _ = _read(path)
            if status is not None:
                return [iid for iid, st in _state(entries).items()
                        if st["intent"] is not None]
        except Exception:
            logger.exception("close_intents: torn-tail truncate "
                             "failed (%s)", lane)
            return [iid for iid, st in _state(entries).items()
                    if st["intent"] is not None]
    frozen: List[str] = []
    for iid, st in _state(entries).items():
        intent = st["intent"]
        if intent is None or st["applied"] or st["frozen"]:
            continue
        why = None
        if not st["account_applied"]:
            for op in (intent.get("settle_ops") or []):
                ok, op_why = account_apply(op)
                if not ok:
                    why = ("account re-apply refused: %s"
                           % (op_why or "?"))
                    break
            if why is None:
                _append(path, {"ev": "account_applied",
                               "intent_id": iid})
                st["account_applied"] = True
        if why is None and not st["lane_applied"]:
            if lane_has_close(intent):
                _append(path, {"ev": "lane_applied",
                               "intent_id": iid})
                st["lane_applied"] = True
            else:
                res = lane_apply(intent)
                if res is True:
                    _append(path, {"ev": "lane_applied",
                                   "intent_id": iid})
                    st["lane_applied"] = True
                elif res is False:
                    why = "lane re-book failed"
                # res None: leave pending - the lane's natural
                # re-close path will settle this intent.
        if why is None and not st["lane_applied"]:
            continue
        if why is None and st["account_applied"] \
                and st["lane_applied"]:
            _append(path, {"ev": "applied", "intent_id": iid})
        elif why is not None:
            freeze(lane, iid, why)
            frozen.append(iid)
    return frozen
