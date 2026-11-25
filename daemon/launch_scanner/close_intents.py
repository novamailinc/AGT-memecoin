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
            if os.path.exists(path):
                # ALWAYS classify the whole journal under the append lock (a newline-terminated mid-file garbage line is as unreadable as a torn one).
                _e, _st, _tail = _read(path)
                if _st == "torn":
                    logger.error("close_intents: journal not cleanly readable (torn); refusing append (%s)", event.get("ev"))
                    return False
                if _st == "torn_tail" and _tail is not None:
                    # Provable crash-mid-append signature (exactly one unparseable LAST record): truncate it like recover_lane before writing.
                    _truncate(path, _tail)
                    logger.error("close_intents: torn tail truncated at %s before append (%s)", _tail, path)
                else:
                    with open(path, "rb") as _f:
                        _f.seek(0, 2)
                        _size = _f.tell()
                        if _size:
                            _f.seek(_size - 1)
                            if _f.read(1) != b"\n":
                                prefix = "\n"   # complete last record that merely lacks its newline
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


def _read_consistent(path: str):
    """_read taken under the same per-path lock as every append/truncate, so a concurrent writer's in-flight line cannot be mistaken for a torn tail."""
    with _append_lock(path):
        return _read(path)


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


def _op_family(op_id) -> str:
    """Base operation id: grad's ":wd" write-down companion belongs to its close op."""
    o = str(op_id)
    return o[:-3] if o.endswith(":wd") else o


def begin(lane: str, mint: str, pos_id: Optional[str],
          settle_ops: List[Dict[str, Any]],
          close_info: Dict[str, Any], stable_id: bool = False, supersedes: Optional[str] = None) -> str:
    """Open an intent. settle_ops is the ordered list of account
    operations this close performs, each {kind: credit|write_down,
    op_id, amount?, qty?, price_kind?, reason?}; close_info carries
    what the lane ledger needs to re-book the close (realized_usd,
    entry_notional_usd, closed_reason). Returns the intent id.
    Raises IntentNotDurable when the intent event could not be durably appended (fail closed: no account op may run without it).
    stable_id=True: idempotent per OPERATION KEY (lane:mint:pos:close_ts), see below; the intent id itself is always unique.
    supersedes=<frozen intent id>: the ONLY way a plain begin may open a new intent for an operation whose earlier intent is FROZEN (frozen is terminal and
    never revives). The caller must have frozen that intent with a journaled reason first; the new intent event records the previous id. A plain begin on
    a frozen operation without it raises IntentNotDurable (fail closed: the position stays pending and visible)."""
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
    if stable_id and supersedes:
        raise IntentNotDurable("stable_id and supersedes cannot be combined: refusing (no silent drop of the supersede)")
    op_key = intent_id if stable_id else None
    intent_id = "%s:%s" % (intent_id, uuid.uuid4().hex[:24])
    ev = {"ev": "intent", "intent_id": intent_id, "lane": lane, "mint": mint, "pos_id": pos_id,
          "settle_ops": settle_ops, "close_info": dict(close_info)}
    if op_key is None:
        with _op_key_lock(lane):
            ents, st, _ = _read_consistent(journal_path(lane))
            if st == "torn_tail":
                # the append below repairs a provable torn tail; re-read after to prove the journal is clean
                pass
            elif st is not None:
                raise IntentNotDurable("journal not cleanly readable (%s): refusing to open an intent" % st)
            _ids = sorted(str(o.get("op_id")) for o in settle_ops)
            _stt = _state(ents)

            def _same_pos(e0):
                return e0 is not None and e0.get("mint") == mint and e0.get("pos_id") == pos_id and e0.get("lane", lane) == lane

            def _same_op(e0):
                return _same_pos(e0) and sorted(str(o.get("op_id")) for o in e0.get("settle_ops") or []) == _ids
            _fam = {_op_family(o.get("op_id")) for o in settle_ops}

            def _overlaps(e0):
                # independent per-leg operations of ONE position (distinct op ids) never block each other; a re-plan of the same close shares the base op
                # id (grad's ":wd" write-down belongs to the base op), so overlapping operations are exclusive
                return _same_pos(e0) and bool(_fam & {_op_family(o.get("op_id")) for o in e0.get("settle_ops") or []})
            _succ = {}   # superseded intent id -> ids of intents recorded as its successors (journal ancestry)
            for e in ents:
                if e.get("ev") == "intent" and e.get("supersedes"):
                    _succ.setdefault(e["supersedes"], []).append(e.get("intent_id"))
            _pend_pos = [i0 for i0, s0 in _stt.items() if _overlaps(s0["intent"]) and not s0["applied"] and s0["frozen"] is None]
            _pend = [i0 for i0 in _pend_pos if _same_op(_stt[i0]["intent"])]
            if len(_pend_pos) != len(_pend):
                # POSITION-WIDE exclusivity: one position never carries two pending intents (a different op set on a pending position is refused,
                # with or without supersedes); the pending one must be finalized or frozen first
                raise IntentNotDurable("position %s/%s already has pending intent(s) %s with an OVERLAPPING different op set: refusing a second pending intent"
                                       % (mint, pos_id, ",".join(sorted(set(_pend_pos) - set(_pend)))))
            if _pend:
                # same logical operation still pending (crash before its terminal event): RESUME it, never open a second intent. The caller's
                # lineage must match the recorded one (a plain begin resumes a plain intent; supersedes=X resumes the successor of X): an unknown
                # or foreign supersedes, or a lineage mismatch, is refused and nothing is appended.
                iid0 = _pend[0]; e0 = _stt[iid0]["intent"]
                if len(_pend) != 1 or (e0.get("supersedes") or None) != (supersedes or None):
                    raise IntentNotDurable("operation already has pending intent(s) %s with recorded parent %r: supplied supersedes %r does not match, refusing"
                                           % (",".join(sorted(_pend)), e0.get("supersedes"), supersedes))
                if json.dumps(e0.get("settle_ops"), sort_keys=True) != json.dumps(settle_ops, sort_keys=True):
                    raise IntentNotDurable("operation %s already pending with a DIFFERENT plan" % iid0)
                return iid0
            # frozen-unsuperseded exclusivity covers OVERLAPPING operations of the position: a frozen intent blocks a new overlapping one unless named
            _frozen_open = [i0 for i0, s0 in _stt.items() if _overlaps(s0["intent"]) and not s0["applied"] and s0["frozen"] is not None and i0 not in _succ]
            if supersedes:
                # explicit supersede: must name an EXISTING, FROZEN, UNAPPLIED intent of this very position that has no successor yet (an applied or
                # already-replaced chain is never re-opened, nothing foreign is accepted), validated even when no frozen intent of this op set exists
                s_ = _stt.get(supersedes)
                if not (s_ and _overlaps(s_["intent"]) and s_["frozen"] is not None and not s_["applied"] and supersedes not in _succ):
                    raise IntentNotDurable("supersedes=%s is not a frozen, unapplied, not-yet-replaced intent of this operation: refusing" % supersedes)
            if _frozen_open and set(_frozen_open) != {supersedes}:
                raise IntentNotDurable("operation already has FROZEN intent(s) %s: a frozen intent never revives; a new intent needs an explicit supersedes=<that id>"
                                       % ",".join(sorted(_frozen_open)))
            if supersedes:
                ev["supersedes"] = supersedes
            if not _append(journal_path(lane), ev):
                raise IntentNotDurable("close intent %s could not be journaled" % intent_id)
            if _read_consistent(journal_path(lane))[1] is not None:
                raise IntentNotDurable("journal not cleanly readable after append")
        return intent_id
    ev["op_key"] = op_key
    with _op_key_lock(lane):
        ents, st, _ = _read_consistent(journal_path(lane))
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
    ents, st, _ = _read_consistent(journal_path(lane))
    if st is not None:
        raise IntentNotDurable("journal not cleanly readable (%s)" % st)
    s_ = _state(ents).get(intent_id)
    if not s_ or s_["intent"] is None:
        return None
    return "applied" if s_["applied"] else ("frozen" if s_["frozen"] is not None else "open")


def chain_tip(lane: str, intent_id: str) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """Follow the journal's supersede chain from intent_id to its TIP. Returns (tip_id, status, intent_event) where status is 'applied' | 'frozen' |
    'open' (None when the id is absent). Raises IntentNotDurable when the journal is not cleanly readable or the chain is ambiguous (two successors, a
    loop): never an empty fallback. A caller whose own state still names an old frozen parent after a crash uses the tip to reconcile."""
    ents, st, _ = _read_consistent(journal_path(lane))
    if st is not None:
        raise IntentNotDurable("journal not cleanly readable (%s)" % st)
    stt = _state(ents)
    succ: Dict[str, List[str]] = {}
    for e in ents:
        if e.get("ev") == "intent" and e.get("supersedes"):
            succ.setdefault(e["supersedes"], []).append(e.get("intent_id"))
    cur, seen = intent_id, set()
    _root = (stt.get(intent_id) or {}).get("intent") or None
    if _root is None and intent_id in succ:
        raise IntentNotDurable("supersede chain starts at UNKNOWN intent %s (successors recorded for an absent parent): refusing" % intent_id)
    while cur in succ:
        if len(succ[cur]) != 1 or cur in seen:
            raise IntentNotDurable("ambiguous supersede chain at %s: %r" % (cur, succ[cur]))
        seen.add(cur)
        nxt = succ[cur][0]
        _ne = (stt.get(nxt) or {}).get("intent")
        if _root is not None and (_ne is None or _ne.get("mint") != _root.get("mint") or _ne.get("pos_id") != _root.get("pos_id")):
            raise IntentNotDurable("supersede chain leaves the position at %s -> %s: refusing" % (cur, nxt))
        cur = nxt
    s_ = stt.get(cur)
    if not s_ or s_["intent"] is None:
        return cur, None, None
    return cur, ("applied" if s_["applied"] else ("frozen" if s_["frozen"] is not None else "open")), s_["intent"]


def _mark_once(lane: str, intent_id: str, kind: str) -> bool:
    """Append one marker event for an intent exactly once. Read-check-append runs under the lane lock (the same _op_key_lock begin and recover use, then the
    per-path append lock inside _append), so two writers (recovery plus the resumed re-close, or two processes) cannot both add it. A marker already
    durably present returns True without a second row. A journal that is not cleanly readable cannot prove absence, so a write failure propagates as False
    exactly as _append would (the caller sees the failure)."""
    path = journal_path(lane)
    with _op_key_lock(lane):
        ents, st, _ = _read_consistent(path)
        if st is None:
            if kind != "frozen" and any(e.get("ev") == "frozen" and e.get("intent_id") == intent_id for e in ents):
                logger.error("close_intents: intent %s is FROZEN; refusing marker %s", intent_id, kind)
                return False
            for e in ents:
                if e.get("ev") == kind and e.get("intent_id") == intent_id:
                    return True
        # st == torn_tail: _append repairs it (provable) before writing; any other unreadable state makes _append refuse (False)
        return bool(_append(path, {"ev": kind, "intent_id": intent_id}))


def mark_account_applied(lane: str, intent_id: str) -> bool:
    """Returns False when the marker was not durably written. The intent then stays open: recovery re-applies by the idempotent op ids
    (a lost marker never loses an account op, it only delays convergence), so callers continue but may surface the False. Idempotent per intent."""
    return _mark_once(lane, intent_id, "account_applied")


def mark_lane_applied(lane: str, intent_id: str) -> bool:
    ok1 = _mark_once(lane, intent_id, "lane_applied")
    # Terminal event in the same pass when both sides are durable: the account side always precedes the lane write by design.
    # The terminal "applied" is written ONLY if the lane marker landed (a terminal state must never outrun its evidence). Idempotent per intent.
    return bool(ok1 and _mark_once(lane, intent_id, "applied"))


def journal_writable(lane: str) -> bool:
    """Durability pre-check for a caller that must not SELL (or book anything) unless it can later make its close intent durable: appends a
    harmless "probe" event (no intent_id, so _state and recovery ignore it) and returns whether it was fsynced. A pass is evidence for NOW only;
    begin() still raises IntentNotDurable if the real intent write fails."""
    if not _append(journal_path(lane), {"ev": "probe", "lane": lane}):
        return False
    return _read_consistent(journal_path(lane))[1] is None


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


JOURNAL_CORRUPT_PREFIX = "journal-corrupt:"   # sentinel id recover_lane returns for a corrupt journal with no visible incomplete intent


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
    entries, status, tail = _read_consistent(path)
    if status == "torn":
        logger.error("close_intents: %s journal corrupt; recovery "
                     "frozen", lane)
        # A corrupt journal must BLOCK entries even when no incomplete intent is visible in the readable part: the unreadable part may hold one. Never an
        # empty list here (an empty list means "fully converged").
        return ([iid for iid, st in _state(entries).items()
                 if st["intent"] is not None and not st["applied"]]
                or [JOURNAL_CORRUPT_PREFIX + lane])
    if status == "torn_tail" and tail is not None:
        try:
            with _op_key_lock(lane), _append_lock(path):
                # re-read under the SAME serialization as begin/_append (no concurrent truncating writers); repair only if still provable
                entries, status, tail = _read(path)
                if status == "torn_tail" and tail is not None:
                    _truncate(path, tail)
                    entries, status, _ = _read(path)
            if status is not None:
                return ([iid for iid, st in _state(entries).items()
                         if st["intent"] is not None]
                        or [JOURNAL_CORRUPT_PREFIX + lane])
        except Exception:
            logger.exception("close_intents: torn-tail truncate "
                             "failed (%s)", lane)
            return ([iid for iid, st in _state(entries).items()
                     if st["intent"] is not None]
                    or [JOURNAL_CORRUPT_PREFIX + lane])
    frozen: List[str] = []
    _succ = {e.get("supersedes") for e in entries if e.get("ev") == "intent" and e.get("supersedes")}
    for iid, st in _state(entries).items():
        intent = st["intent"]
        if intent is not None and not st["applied"] and st["frozen"] and iid not in _succ:
            # A PERSISTED frozen, unapplied, unsuperseded intent is terminal until an operator supersedes it: it keeps the lane's entries blocked in every
            # new process, not only in the process that froze it.
            frozen.append(iid)
            continue
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
                _mark_once(lane, iid, "account_applied")
                st["account_applied"] = True
        if why is None and not st["lane_applied"]:
            if lane_has_close(intent):
                _mark_once(lane, iid, "lane_applied")
                st["lane_applied"] = True
            else:
                res = lane_apply(intent)
                if res is True:
                    _mark_once(lane, iid, "lane_applied")
                    st["lane_applied"] = True
                elif res is False:
                    why = "lane re-book failed"
                # res None: leave pending - the lane's natural
                # re-close path will settle this intent.
        if why is None and not st["lane_applied"]:
            continue
        if why is None and st["account_applied"] \
                and st["lane_applied"]:
            _mark_once(lane, iid, "applied")
        elif why is not None:
            if freeze(lane, iid, why):
                frozen.append(iid)
            else:
                # the freeze event could not be written: the intent stays pending and is retried, but the divergence is real, so it is still a BLOCKER
                frozen.append(iid)
                logger.error("close_intents: recover_lane could not durably freeze %s (%s): it stays pending and is retried", iid, why)
    return frozen
