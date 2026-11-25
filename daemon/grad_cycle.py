"""SIM grad-lane cycle driver (DRAFT SOURCE, OFF BY DEFAULT, NOT STARTED BY ANYTHING).

One cycle = time exits, price exits, then (only when it is safe) one entry scan. Pure function `run_cycle` plus a thin `GradCycleDaemon`
(Daemon subclass). Nothing imports this module, `daemon.supervisor.DAEMONS` does not list it, no unit is installed or enabled. It constructs
NO live executor and refuses unless the lane is in DEMO (simulated) mode. `grad` is the lane ENGINE module (grad or a grad_select engine);
all state below is keyed by that engine's ledger path.

Price contract (one statement): price exits need an EXPLICIT route: price_fn, or price_route="lane_default" (the lane's own route when
price_fn is None: Jupiter v3 batch, then reserves/DexScreener/GeckoTerminal, then its unquotable branches). With neither, the price-exit step
is not run and the cycle is recorded degraded. Time exits never need a price.

Gates (exits are NEVER gated): entries only run when ALL hold: no unresolved integration fault, journal intent durable, lane ledger and
recon_health readable with no pending/settlement/migration marker, account snapshot readable and not degraded, exit steps raised nothing.
Unresolved integration fault (durable, keyed by ledger path): recorded when an exit step aborts (abort count rose, or raised), when an abort
happened BETWEEN cycles (count above the last journaled count of the same boot), when a prior cycle is found still "running" (it was
interrupted mid exit, a real crash window) or its journal is unreadable, or when the baseline is unreadable. It clears ONLY through
clear_fault(): explicit cross-source evidence taken under the lane ledger lock, with a durable clear record. Not restart, not time, not clean
polls. If the fault file cannot be written the prior journal record is LEFT as is (still "running"), so a restart still finds the evidence.
Cycle journal `<ledger>.cycle.json` (atomic): "running" before the steps, "done" (with aborts_end) after. A journal write failure refuses NEW
entries; exits still run. No threads or processes are started. A fresh process is not a kill/restart proof (separate crash harness)."""
import json
import logging
import os
import threading
import time

logger = logging.getLogger("forex_agent.daemon.grad_cycle")
ENV_FLAG = "AGT_GRAD_CYCLE_ENABLED"       # must be exactly "1"; default off
_BOOT = {"id": "%d-%d" % (os.getpid(), int(time.time())), "seq": 0}
_MEM = {}      # ledger path -> {"since": ts, "file_written": bool}: in-process mirror, keyed per lane engine
# Facts, not thresholds: a cash conservation difference above this is a REAL difference (journal amounts are rounded to 6 dp).
_CASH_EPS = 1e-5


def enabled():
    return os.environ.get(ENV_FLAG) == "1"


def _journal_path(grad):
    return grad.LEDGER_PATH + ".cycle.json"


def _fault_path(grad):
    return grad.LEDGER_PATH + ".cycle-fault.json"


def _fault_log_path(grad):
    return grad.LEDGER_PATH + ".cycle-fault-log.jsonl"


def _cycle_lock_path(grad):
    return grad.LEDGER_PATH + ".cycle.lock"


def _clear_log_path(grad):
    return grad.LEDGER_PATH + ".cycle-fault-clears.jsonl"


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as e:
        return {"unreadable": repr(e)[:200]}


def _atomic(path, rec):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh, default=str); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_journal(grad):
    return _read_json(_journal_path(grad))


def _write_journal(grad, rec, out):
    """Best effort atomic write. Returns True only when the record is durable."""
    try:
        _atomic(_journal_path(grad), rec)
        return True
    except Exception as e:
        out["journal_write_failure"] = repr(e)[:200]
        logger.warning("grad_cycle: journal not written: %r", e)
        return False


def _read_fault(grad):
    """None = no fault recorded; dict = recorded (or {"unreadable": ...}: unknown, treated as unresolved)."""
    return _read_json(_fault_path(grad))


def _clear_records(grad):
    out = []
    try:
        with open(_clear_log_path(grad)) as fh:
            for line in fh:
                try: out.append(json.loads(line))
                except Exception: out.append({"unreadable": True})
    except FileNotFoundError:
        pass
    return out


CLEAR_COMMIT_ENABLED = False   # clear_fault GATHERS evidence but never commits (no record, no removal) while False. It stays False: the commit is
                               # not atomic against the lane poll, the account writers and the fill journal (no common lock exists). No caller exists.


_TORN_TAIL = "__fault_log_torn_tail__"


def _log_tail_torn(grad):
    try:
        with open(_fault_log_path(grad), "rb") as fh:
            data = fh.read()
        return bool(data) and not data.endswith(b"\n")
    except Exception:
        return False


def _REPAIR_HOOK(point):
    """Test seam only (no-op): lets a test raise at a crash point inside the repair."""
    return None


def _repair_log_tail(grad):
    """Called before every fault-log append (the caller holds the cycle lock). A log not ending in a newline would make the next append
    concatenate onto the torn fragment and poison the whole log forever. The fragment is PRESERVED in <log>.torn-tails.jsonl, the log is
    truncated back to its last newline, and a MARKER fault with its own fault_id is appended: the unknown fault stays ACTIVE until a clear record
    names it. Returns True when the log is append-safe."""
    path = _fault_log_path(grad)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return True
    except Exception:
        return False
    if not data or data.endswith(b"\n"):
        return True
    import uuid  # noqa: PLC0415
    cut = data.rfind(b"\n") + 1
    frag = data[cut:]
    try:
        with open(path + ".torn-tails.jsonl", "a") as th:
            th.write(json.dumps({"ts": time.time(), "fragment": frag.decode("utf-8", "replace")[:2000], "bytes": len(frag)}) + "\n"); th.flush(); os.fsync(th.fileno())
        _REPAIR_HOOK("after_preserve_before_marker")
        mark = (json.dumps({"fault_id": "torn_tail_" + uuid.uuid4().hex, "since": time.time(),
                            "entry": {"reason": "fault_log_torn_tail_repaired", "fragment_bytes": len(frag)}}) + "\n").encode()
        with open(path, "r+b") as fh:
            # CRASH ORDER: the marker is written OVER the fragment and fsynced FIRST, then the file is cut to cut+len(marker). A kill between the
            # two leaves the marker followed by leftover fragment bytes with no newline: that reads as a torn tail again (still ACTIVE).
            # Truncating first would leave a clean log and lose the unknown fault.
            fh.seek(cut); fh.write(mark); fh.flush(); os.fsync(fh.fileno())
            _REPAIR_HOOK("after_marker_before_truncate")
            fh.truncate(cut + len(mark)); fh.flush(); os.fsync(fh.fileno())
        return True
    except Exception:
        return False


def _open_logged_faults(grad):
    """fault_ids in the append-only fault log with no clear record naming them. None = a log line or the log is unreadable."""
    ids = []
    try:
        with open(_fault_log_path(grad)) as fh:
            for line in fh:
                try: ids.append(json.loads(line)["fault_id"])
                except Exception:
                    if line.endswith("\n"):
                        return None            # a COMPLETE unparseable line is damage
                    # a final line without its newline is a crash mid-append: the fault it carried is UNKNOWN (it may exist nowhere else when the fault
                    # file write also failed). It is an ACTIVE sentinel, never clean, and is repaired (preserved + a marker fault) by the next append.
                    ids.append(_TORN_TAIL)
    except FileNotFoundError:
        return []
    except Exception:
        return None
    cleared = set()
    for r in _clear_records(grad):
        cleared.add(r.get("fault_id")); cleared.update(r.get("fault_ids") or [])
    if any(r.get("unreadable") for r in _clear_records(grad)):
        return None
    return sorted({i for i in ids if i not in cleared})


def fault_active(grad):
    """(active, why). The durable file is authoritative. A file-less in-process fault is dropped ONLY when a durable clear record names its
    fault_id (so another process clearing it is honored, and a lost fault file is not read as a clear)."""
    f = _read_fault(grad)
    if f is not None:
        return True, "unreadable_record" if f.get("unreadable") else "recorded"
    logged = _open_logged_faults(grad)
    if logged is None or logged:
        return True, "fault_log_unreadable" if logged is None else "logged_fault_without_clear_record"
    m = _MEM.get(grad.LEDGER_PATH)
    if m is None:
        return False, None
    if m.get("file_written"):
        if any(r.get("fault_id") == m.get("fault_id") for r in _clear_records(grad)):
            _MEM.pop(grad.LEDGER_PATH, None); return False, None
        return True, "fault_file_missing_without_clear_record"
    return True, "fault_file_never_written"


_FAULT_LOCK_TIMEOUT_S = 5.0
_FLT = threading.local()


class FaultLockTimeout(OSError):
    pass


def _fault_lock_path(grad):
    return grad.LEDGER_PATH + ".cycle-fault.lock"


class _fault_lock:
    """Cross-thread AND cross-process serialisation of every fault-state writer (fault file, fault log, clear log, fault-file removal, torn-tail repair,
    _MEM). A flock on its own file (separate open file description per acquisition, so threads of one process also exclude each other), REENTRANT per
    thread via a depth counter. LOCK ORDER: this is a LEAF lock. Callers may hold the cycle lock and/or the ledger lock when they take it (cycle -> ledger
    -> fault, and the poll-abort hook runs ledger -> fault); nothing that holds it may take the cycle or ledger lock (the held sections do bare file I/O
    only). Acquisition is bounded (non-blocking retries up to _FAULT_LOCK_TIMEOUT_S) so a stuck holder cannot hang a thread that holds the ledger lock; a
    timeout raises FaultLockTimeout and the caller treats the fault as NOT recorded (fail closed). The kernel frees it on process death."""
    def __init__(self, grad):
        self.path = _fault_lock_path(grad); self.fh = None

    def __enter__(self):
        import fcntl  # noqa: PLC0415
        depth = getattr(_FLT, "depth", None)
        if depth is None:
            depth = _FLT.depth = {}
        if depth.get(self.path, 0) > 0:
            depth[self.path] += 1; return self
        fh = open(self.path, "a")
        end = time.time() + float(_FAULT_LOCK_TIMEOUT_S)
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.time() >= end:
                    fh.close(); raise FaultLockTimeout("fault lock busy")
                time.sleep(0.01)
        self.fh = fh; depth[self.path] = 1
        return self

    def __exit__(self, *a):
        depth = _FLT.depth
        depth[self.path] -= 1
        if depth[self.path] == 0:
            del depth[self.path]
            try: self.fh.close()
            except Exception: pass
        return False


def _record_fault(grad, out, reason, **detail):
    """Serialised wrapper (see _fault_lock). A lock timeout/IO failure means the fault is NOT durable: in-process flag set, returns False."""
    try:
        with _fault_lock(grad):
            return _record_fault_locked(grad, out, reason, **detail)
    except OSError as e:
        out["fault_lock_failure"] = repr(e)[:200]
        _MEM[grad.LEDGER_PATH] = dict(_MEM.get(grad.LEDGER_PATH) or {}, since=(_MEM.get(grad.LEDGER_PATH) or {}).get("since", time.time()),
                                      fault_id=(_MEM.get(grad.LEDGER_PATH) or {}).get("fault_id") or "lockfail", file_written=False)
        return False


def _record_fault_locked(grad, out, reason, **detail):
    """Make the fault durable. The FIRST record keeps its identity (fault_id) and age (since): later reasons are APPENDED to `reasons`, never
    replacing the original. When the file write fails the in-process flag stays AND the prior cycle journal is left untouched."""
    now = out.get("ts") if out.get("ts") is not None else time.time()
    entry = dict({"reason": reason, "at": now, "boot_id": _BOOT["id"], "cycle_seq": out.get("cycle_seq")}, **detail)
    prev = _read_fault(grad)
    mem = _MEM.get(grad.LEDGER_PATH) or {}
    if prev and not prev.get("unreadable"):
        rec = dict(prev); rec["reasons"] = list(prev.get("reasons") or []) + [entry]; rec["reason"] = prev.get("reason") or reason
    else:
        import uuid  # noqa: PLC0415
        rec = {"fault_id": mem.get("fault_id") or uuid.uuid4().hex, "since": mem.get("since", now), "reason": reason, "reasons": [entry],
               "boot_id": _BOOT["id"], "cycle_seq": out.get("cycle_seq")}
    try:
        _atomic(_fault_path(grad), rec); written = True
    except Exception as e:
        written = False; out["fault_write_failure"] = repr(e)[:200]
    try:      # append-only log: survives deletion of the fault file; active = a logged fault_id with no clear record naming it
        if not _repair_log_tail(grad):
            raise OSError("fault log tail could not be repaired")
        with open(_fault_log_path(grad), "a") as fh:
            fh.write(json.dumps({"fault_id": rec["fault_id"], "since": rec["since"], "entry": entry}, default=str) + "\n"); fh.flush(); os.fsync(fh.fileno())
        written = written or None
    except Exception as e:
        out["fault_log_write_failure"] = repr(e)[:200]
    written = bool(written)
    _MEM[grad.LEDGER_PATH] = {"since": rec["since"], "fault_id": rec["fault_id"], "file_written": written}
    out.setdefault("faults", []).append(dict(entry, fault_id=rec["fault_id"]))
    return written


def _journal_internal_consistency(pa, snap):
    """INTERNAL consistency only, NOT independent conservation: paper_account.verify_replay() (READ ONLY: no truncation, creation, .bak restore or
    reconcile) replays the account's own WAL and compares cash total, buckets, assumed_sold keys and open-position key sets. It does NOT compare
    holds, pending_sol or per-position qty/basis. Independent conservation is NOT implemented. Returns (ok_or_None, detail): None = refuse."""
    fn = getattr(pa, "verify_replay", None)
    if fn is None:
        return None, "account_has_no_verify_replay"
    try:
        ok, why = fn()
    except Exception as e:
        return None, "verify_replay_error:%r" % (e,)
    return ok, why


def _account_op_index(pa):
    """Read-only index of the account's WAL: {op_id: pos_id} for debits and the set of pos_ids with a FINAL credit. None when unreadable."""
    try:
        entries, status, _ = pa._journal_read()
    except Exception:
        return None
    if status:
        return None
    debits, settled, refused, dups = {}, set(), set(), set()
    for e in entries:
        if e.get("event") == "debit_refused" and e.get("op_id"):
            refused.add(e["op_id"])
        if e.get("event") == "debit" and e.get("op_id"):
            if e["op_id"] in debits: dups.add(e["op_id"])          # two debits for ONE op id: the index would keep only the last pos_id
            debits[e["op_id"]] = e.get("pos_id")
        elif e.get("event") == "credit" and e.get("final") is True and e.get("pos_id"):
            settled.add(e["pos_id"])
    return {"debits": debits, "settled": settled, "refused": refused, "dups": dups}


def _fills_ownership(grad, jj, d, acct):
    """Ownership of this lane's fill-journal keys. Returns (problems, notes). Keys of other lanes are skipped; a key with an unknown lane-like kind
    blocks. ENTRY fills are anchored on the ACCOUNT, not on the lane's retained records (the lane caps `closes` at 500 and recovered closes carry no
    entry_op): a DEMO entry journals its fill BEFORE the post-quote refusals and a refusal leaves the fill with NO account debit, so
      - no account debit for the op: an uncommitted/refused entry (benign, counted in notes);
      - a debit whose pos_id a lane position/close owns, or whose key equals a lane execution.entry_op: owned;
      - a debit whose pos_id has a FINAL account credit but no lane record: settled on the account, record trimmed or never kept (benign, counted);
      - a debit, still open on the account, with no lane owner: ORPHAN (block).
    An open entry intent for this lane blocks (in flight or crashed: unknown). EXIT fills: owned mint within the retained window; a pa_pos_id
    suffix must be owned; a bare key on a mint with more than one owned instance is ambiguous; an exit fill whose mint left the retained `closes`
    window (cap 500) blocks (fail closed, stated, not guessed benign)."""
    lane = grad.LANE; pos = (d.get("positions") or {}); closes = d.get("closes") or []
    recs = list(pos.values()) + [c for c in closes if isinstance(c, dict)]
    entry_ops = {str(((r.get("execution") or {}).get("entry_op"))) for r in recs}
    pids = {str(r.get("pa_pos_id")) for r in recs if r.get("pa_pos_id")}
    per_mint = {}
    for r in recs:
        per_mint.setdefault(r.get("mint"), set()).add(str(r.get("pa_pos_id") or r.get("entry_op") or id(r)))
    bad, notes = [], {"entry_refused_no_debit": 0, "entry_settled_on_account_unowned": 0}
    fills = jj.get("fills") or {}
    for k, op in (jj.get("intents") or {}).items():
        if not str(k).startswith(lane + "|"):
            continue
        # classified from the account WAL read-only (never filler._intent_state, which calls _load_locked): a DemoSkip entry leaves its intent open BY DESIGN
        if acct is None:
            bad.append("open_entry_intent_unclassifiable:%s" % k); continue
        if op in acct["dups"]:
            bad.append("op_has_multiple_debits:%s" % op); continue
        if op not in acct["debits"] and op not in acct["refused"]:
            notes["intent_fresh_no_account_event"] = notes.get("intent_fresh_no_account_event", 0) + 1; continue
        if op in acct["refused"]:
            notes["intent_stale_refused"] = notes.get("intent_stale_refused", 0) + 1; continue
        pid = acct["debits"].get(op)
        if pid is not None and (str(pid) in pids or str(op) in entry_ops):
            notes["intent_resume_owned_by_lane"] = notes.get("intent_resume_owned_by_lane", 0) + 1; continue
        if pid is not None and pid in acct["settled"]:
            notes["intent_stale_settled"] = notes.get("intent_stale_settled", 0) + 1; continue
        bad.append("open_entry_intent_debited_unowned:%s" % k)
    for k in fills:
        if not isinstance(k, str):
            bad.append("fill_key_not_string"); continue
        if not k.startswith(lane + ":"):
            continue
        parts = k.split(":")
        kind = parts[1] if len(parts) > 1 else ""
        if kind == "entry":
            if k in entry_ops:
                continue
            if acct is None:
                bad.append("entry_fill_unclassifiable_account_journal_unreadable:%s" % k); continue
            if k in acct["dups"]:
                bad.append("op_has_multiple_debits:%s" % k); continue
            if k not in acct["debits"]:
                notes["entry_refused_no_debit"] += 1; continue
            pid = acct["debits"][k]
            if pid is not None and str(pid) in pids:
                continue
            if pid is not None and pid in acct["settled"]:
                notes["entry_settled_on_account_unowned"] += 1; continue
            bad.append("entry_fill_debited_unowned:%s" % k)
        elif kind == "exit":
            if len(parts) < 4: bad.append("exit_fill_malformed:%s" % k); continue
            mint = parts[2]
            if mint not in per_mint: bad.append("exit_fill_unowned_mint_or_beyond_retained_closes:%s" % k)
            elif len(parts) >= 5 and parts[4] not in pids: bad.append("exit_fill_unowned_instance:%s" % k)
            elif len(parts) == 4 and len(per_mint[mint]) > 1: bad.append("exit_fill_ambiguous_instance:%s" % k)
        else:
            bad.append("fill_key_unknown_kind:%s" % k)
    return bad, notes


def _fingerprint(grad, account=None):
    """Digest of the FULL state of every store the clear reasons about (whole lane ledger, whole account snapshot, whole fill journal, account
    WAL entries, cycle journal and fault file). Bare reads only. An unreadable store gives a unique token so a clear cannot pass on it. This
    narrows the race; it is not atomic proof (there is no common writer lock)."""
    import hashlib  # noqa: PLC0415
    pa = account if account is not None else grad._paper_account()
    parts = []
    for name, fn in (("lane", lambda: grad._read()),
                     ("acct", lambda: pa.snapshot()),
                     ("acct_wal", lambda: pa._journal_read()[:2]),
                     ("fills", lambda: grad._demo_filler()._locked(lambda jj: json.loads(json.dumps(jj, default=str)))),
                     ("cycle", lambda: _read_journal(grad)), ("fault", lambda: _read_fault(grad))):
        try: parts.append((name, fn()))
        except Exception as e: parts.append((name, "unreadable:%r:%r" % (e, time.time())))
    return hashlib.sha256(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()


def clear_fault(grad, account=None, operator_note=""):
    """DISABLED for commit (CLEAR_COMMIT_ENABLED False, no caller, no operator route). Gathers reconciliation evidence and returns (cleared, evidence).
    Every check must pass; an unevaluable check or an unknown identity is a refusal. Checks: fault record readable; cycle journal not "running";
    storage I/O probe; lane ledger readable, no pending/settlement/migration markers; fill journal readable and EVERY lane fill key owned
    (instance-bound, entry and exit kinds; unknown kinds block); account snapshot readable, not degraded; account positions and lane positions
    match in BOTH directions by non-empty pa_pos_id (missing id blocks); pending_sol, holds, unresolved, assumed_sold with a missing lane
    field block (unknown ownership), those for this lane block; account cash agrees with the account's own journal (INTERNAL consistency only,
    whitelisted WAL events); every open position diagnoses "consistent" (evidence_unavailable and explained_residue refuse). An empty lane is
    not vacuously clean. With the commit enabled the commit step re-checks, under grad._ledger_lock and with bare reads only, that no cycle is
    running and the full-state fingerprint is unchanged, appends a durable clear record (fault_id, evidence), then removes the fault file."""
    ev = {"operator_note": str(operator_note)[:200], "commit_enabled": bool(CLEAR_COMMIT_ENABLED)}
    lock = getattr(grad, "_ledger_lock", None)
    if lock is None:
        return False, dict(ev, blocked=["no_ledger_lock"])
    pa = account if account is not None else grad._paper_account()
    # RAW account evidence FIRST: pa.snapshot() (also used by _fingerprint) runs _load_locked, which truncates a torn WAL tail, restores .bak and
    # reconciles the ledger to the WAL, so after it the crash-window states verify_replay exists to see are already healed.
    pre_consistency = _journal_internal_consistency(pa, None) if pa is not None else (None, "no_account")
    pre_index = _account_op_index(pa) if pa is not None else None
    ev["account_evidence_order"] = "verify_replay_and_wal_index_read_BEFORE_snapshot_heal"
    fp0 = _fingerprint(grad, account)
    f = _read_fault(grad)
    ev["fault_record"] = "none" if f is None else ("unreadable" if f.get("unreadable") else "present")
    if f is not None and f.get("unreadable"):
        return False, dict(ev, blocked=["fault_record_unreadable"])
    _lg = _open_logged_faults(grad)
    if _lg is None and f is None:
        return False, dict(ev, blocked=["fault_log_unreadable"])
    if f is None and not _lg:
        return False, dict(ev, blocked=["no_fault"])
    bad = []
    if _lg and _TORN_TAIL in _lg: bad.append("fault_log_torn_tail")
    j = _read_journal(grad)
    ev["journal_status"] = (j or {}).get("status") if j and not j.get("unreadable") else ("unreadable" if j else "none")
    if ev["journal_status"] == "running": bad.append("cycle_running_or_interrupted")
    if ev["journal_status"] == "unreadable": bad.append("cycle_journal_unreadable")
    try:
        probe = os.path.join(os.path.dirname(grad.LEDGER_PATH) or ".", ".cycle-probe-%d" % os.getpid())
        with open(probe, "w") as fh:
            fh.write("probe"); fh.flush(); os.fsync(fh.fileno())
        back = open(probe).read(); os.remove(probe)
        ev["io_probe"] = "ok" if back == "probe" else "readback_mismatch"
    except Exception as e:
        ev["io_probe"] = "failed:%r" % (e,)
    if ev["io_probe"] != "ok": bad.append("io_probe")
    try: h = grad.recon_health()
    except Exception: h = None
    ev["lane_ledger"] = "unreadable" if h is None else "readable"
    ev["markers"] = None if h is None else {k: h.get(k) for k in ("n_pending", "n_settlement_pending", "n_migration_blocked")}
    if h is None or any(ev["markers"].values()): bad.append("lane_ledger_or_markers")
    d = None
    try:
        d = grad._read() or {}
        op = d.get("positions") or {}
    except Exception as e:
        op = None; bad.append("lane_state_unreadable:%r" % (e,))
    try: snap = pa.snapshot() if pa is not None else None
    except Exception: snap = None
    if not snap or snap.get("degraded"):
        ev["account"] = "unreadable_or_degraded"; bad.append("account")
    else:
        ev["account"] = "ok"; lane = grad.LANE
        lp = (((snap.get("lanes") or {}).get(lane) or {}).get("positions") or [])
        acct_ids = [str(x.get("pos_id")) if x.get("pos_id") else None for x in lp]
        if any(i is None for i in acct_ids): bad.append("account_position_without_id")
        if op is not None:
            lane_ids = [str(p.get("pa_pos_id")) if (p or {}).get("pa_pos_id") else None for p in op.values()]
            if any(i is None for i in lane_ids): bad.append("lane_position_without_pa_pos_id")
            orph = [i for i in acct_ids if i is not None and i not in set(x for x in lane_ids if x)]
            if orph: bad.append("account_orphan_positions:%s" % orph)
            miss = [i for i in lane_ids if i is not None and i not in set(x for x in acct_ids if x)]
            if miss: bad.append("lane_positions_missing_on_account:%s" % miss)
        for name in ("pending_sol", "unresolved", "assumed_sold"):
            items = snap.get(name) or []
            if any((not isinstance(x, dict)) or not x.get("lane") for x in items): bad.append("account_%s_unknown_lane" % name)
            n = [x for x in items if isinstance(x, dict) and x.get("lane") == lane]
            if n: bad.append("account_%s_for_lane:%d" % (name, len(n)))
        holds = snap.get("holds") or {}
        if any((not isinstance(v, dict)) or not v.get("lane") for v in holds.values()): bad.append("account_holds_unknown_lane")
        hl = [k for k, v in holds.items() if isinstance(v, dict) and v.get("lane") == lane]
        if hl: bad.append("account_holds_for_lane:%d" % len(hl))
        ok_c, det = pre_consistency
        ev["account_journal_internal_consistency"] = det
        if ok_c is False: ev["account_consistency_note"] = "a False pre-heal verdict may be a ledger BEHIND the WAL (crash window; an account reload heals it) rather than corruption"
        if ok_c is not True: bad.append("account_journal_consistency_%s" % ("failed" if ok_c is False else "unevaluable"))
    try:
        jj = grad._demo_filler()._locked(lambda j_: json.loads(json.dumps({"fills": j_.get("fills") or {}, "intents": j_.get("intents") or {}}, default=str)))
        ev["fill_journal"] = "ok:%d" % len(jj["fills"])
        if d is not None:
            own, notes = _fills_ownership(grad, jj, d, pre_index)
            ev["fill_ownership_notes"] = notes
            if own: bad.append("fill_ownership:%s" % own[:5])
    except Exception as e:
        ev["fill_journal"] = "unreadable:%r" % (e,); bad.append("fill_journal")
    pos = {}
    try:
        for m in (op or {}):
            pos[m] = (grad.diagnose_position(m) or {}).get("verdict")
    except Exception as e:
        pos["_error"] = "%r" % (e,)
    ev["open_positions"] = pos
    for m, v in pos.items():
        if m == "_error" or v != "consistent": bad.append("position:%s:%s" % (m, v))
    if not CLEAR_COMMIT_ENABLED:
        bad.append("commit_disabled_no_common_writer_lock")
    if bad:
        return False, dict(ev, blocked=bad)
    import fcntl  # noqa: PLC0415
    try:
        _cl = open(_cycle_lock_path(grad), "w"); fcntl.flock(_cl, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the SAME lock run_cycle takes: no cycle can start now
    except Exception as e:
        return False, dict(ev, blocked=["cycle_lock_unavailable:%r" % (e,)])
    try:
      with lock(), _fault_lock(grad):     # commit step (fault lock = leaf, taken last): bare reads only, after re-checking no cycle runs and the full state is unchanged
        j2 = _read_journal(grad)
        if j2 and (j2.get("unreadable") or j2.get("status") == "running"):
            return False, dict(ev, blocked=["cycle_running_at_commit"])
        if _fingerprint(grad, account) != fp0:
            return False, dict(ev, blocked=["state_changed_during_evidence"])
        try:
            with open(_clear_log_path(grad), "a") as fh:
                fh.write(json.dumps({"ts": time.time(), "fault_id": (f or {}).get("fault_id"), "fault_ids": sorted(set(filter(None, [(f or {}).get("fault_id")] + list(_open_logged_faults(grad) or [])))), "fault_since": (f or {}).get("since"), "fault": f,
                                     "evidence": ev, "boot_id": _BOOT["id"], "fingerprint": fp0}, default=str) + "\n")
                fh.flush(); os.fsync(fh.fileno())
        except Exception as e:
            return False, dict(ev, blocked=["clear_record_not_written:%r" % (e,)])
        try:
            if f is not None: os.remove(_fault_path(grad))
        except Exception as e:
            return False, dict(ev, blocked=["fault_file_not_removed:%r" % (e,)])
    except FaultLockTimeout as e:
        return False, dict(ev, blocked=["fault_lock_unavailable:%r" % (e,)])
    finally:
        try: _cl.close()
        except Exception: pass
    _MEM.pop(grad.LEDGER_PATH, None)
    return True, ev


def _abort_count(grad):
    try:
        return int(((grad.recon_health() or {}).get("poll_aborts") or {}).get("count") or 0)
    except Exception:
        return None


def scan_gate(grad, account=None):
    """(ok, reason). Entries only; never consulted for exits. Unknown is a refusal."""
    act, why = fault_active(grad)
    if act:
        return False, "unresolved_fault:%s" % why
    try:
        h = grad.recon_health()
    except Exception as e:
        return False, "recon_health_error:%r" % (e,)
    if h is None:
        return False, "ledger_unreadable"
    for k in ("n_pending", "n_settlement_pending", "n_migration_blocked"):
        if h.get(k):
            return False, "%s=%s" % (k, h.get(k))
    pa = account if account is not None else grad._paper_account()
    if pa is None:
        return False, "account_unavailable"
    try:
        snap = pa.snapshot()
    except Exception as e:
        return False, "account_snapshot_error:%r" % (e,)
    if not snap or snap.get("degraded"):
        return False, "account_degraded_or_unreadable"
    return True, None


def _run_cycle_inner(grad=None, price_fn=None, scan_fn=None, now=None, price_route=None, lock_ok=True, defer_scan=False):
    """Never raises. Returns the cycle record (also journaled). See the module docstring for the price and gate contracts."""
    out = {"boot_id": _BOOT["id"], "steps": {}, "scan_skipped": None}
    if not enabled():
        out["status"] = "disabled"; return out
    if grad is None:
        try:
            from daemon.launch_scanner import grad as grad  # noqa: PLC0415
        except Exception as e:
            out["status"] = "grad_import_failed:%r" % (e,); return out
    if not grad._demo_mode():
        out["status"] = "refused:not_demo_mode"; return out      # never drives a non-simulated lane
    _BOOT["seq"] += 1
    out["cycle_seq"] = _BOOT["seq"]; out["ts"] = now if now is not None else time.time()
    aborts0 = _abort_count(grad)
    prior = _read_journal(grad)
    fault_written = True
    if _log_tail_torn(grad) and not _repair_log_tail(grad):
        fault_written = _record_fault(grad, out, "fault_log_torn_tail_unrepairable") and fault_written
    if prior and prior.get("unreadable"):
        fault_written = _record_fault(grad, out, "cycle_journal_unreadable", detail=prior.get("unreadable"))
    elif prior and prior.get("status") == "running":
        # a cycle that began and never finished: the process died (or the journal write failed) mid exit steps. State unknown.
        fault_written = _record_fault(grad, out, "interrupted_prior_cycle", prior_boot=prior.get("boot_id"), prior_seq=prior.get("cycle_seq"))
    elif prior and prior.get("boot_id") == _BOOT["id"] and prior.get("aborts_end") is not None and aborts0 is not None and aborts0 > int(prior["aborts_end"]):
        fault_written = _record_fault(grad, out, "abort_between_cycles", aborts_end=prior["aborts_end"], aborts_now=aborts0)
    if aborts0 is None:
        fault_written = _record_fault(grad, out, "abort_baseline_unreadable") and fault_written
    if not fault_written:
        # fault evidence could not be made durable: leave the prior journal record untouched so a restart still sees it; exits still run
        intent_ok = False; out["scan_skipped"] = "fault_not_durable"
    else:
        intent_ok = _write_journal(grad, dict(out, status="running"), out)
    failed = False
    have_route = price_fn is not None or price_route == "lane_default"
    for name, fn in (("time_exits", lambda: grad.poll_time_exits()), ("exits", lambda: grad.poll_exits(price_fn=price_fn))):
        if name == "exits" and not have_route:
            failed = True; out["degraded"] = "no_price_route"; out["steps"][name] = {"ok": False, "error": "no_price_route", "ran": False}
            continue
        try:
            res = fn(); out["steps"][name] = {"ok": True, "n_closed": len(res or [])}
        except Exception as e:      # the lane functions never raise; an exception here is an integration fault
            failed = True; out["steps"][name] = {"ok": False, "error": repr(e)[:200]}
            _record_fault(grad, out, "exit_step_exception", step=name, error=repr(e)[:200])
    aborts1 = _abort_count(grad)
    if aborts0 is not None and aborts1 is not None and aborts1 > aborts0:
        _record_fault(grad, out, "poll_abort", aborts_before=aborts0, aborts_after=aborts1)
    elif aborts1 is None:
        _record_fault(grad, out, "abort_count_unreadable_after_steps")
    out["aborts_end"] = aborts1
    if out.get("scan_skipped") == "fault_not_durable":
        pass
    elif scan_fn is None:
        out["scan_skipped"] = "no_scan_fn"
    elif failed:
        out["scan_skipped"] = out.get("degraded") or "exit_step_failed"
    elif not lock_ok:
        out["scan_skipped"] = "cycle_lock_io_failure"
    elif not intent_ok:
        out["scan_skipped"] = "cycle_journal_unavailable"      # exits already ran; NEW entries need the durable cycle intent
    elif defer_scan:
        out["scan_deferred"] = True          # exits are done; scan_phase() runs the scan AFTER every lane's exits
    else:
        ok, why = scan_gate(grad)
        if not ok:
            out["scan_skipped"] = why
        else:
            try:
                _res = scan_fn(); out["steps"]["scan"] = {"ok": True, "feed_health": (_res or {}).get("feed_health") if isinstance(_res, dict) else None}
            except Exception as e:
                out["steps"]["scan"] = {"ok": False, "error": repr(e)[:200]}
    out["status"] = "done"
    if fault_written:
        # a DEFERRED cycle is not finished until scan_phase completes: the journal stays "running" (phase awaiting_scan) so a kill in between is seen
        _write_journal(grad, dict(out, status="running", phase="awaiting_scan") if out.get("scan_deferred") else out, out)
    return out


from daemon.common import Daemon as _Daemon       # a broken import FAILS CLOSED (module import error), never a stand-in base class


def run_cycle(grad=None, price_fn=None, scan_fn=None, now=None, price_route=None, defer_scan=False):
    """Takes the per-ledger cycle lock (non-blocking flock; the SAME lock clear_fault's commit takes) around the whole cycle. When it cannot be
    taken (a clear is committing, or another cycle runs on this ledger) nothing runs this tick and the record says so; the next tick retries."""
    if grad is None and enabled():
        try:
            from daemon.launch_scanner import grad as grad  # noqa: PLC0415
        except Exception as e:
            return {"status": "grad_import_failed:%r" % (e,), "steps": {}, "scan_skipped": None}
    if grad is None or not enabled():
        return _run_cycle_inner(grad, price_fn, scan_fn, now, price_route)
    import fcntl  # noqa: PLC0415
    try:
        lk = open(_cycle_lock_path(grad), "w"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        return {"status": "busy:cycle_lock_held:%r" % (e,), "steps": {}, "scan_skipped": "busy"}
    except Exception as e:       # an I/O failure (not contention): exits STILL run, NEW entries are refused (no lock, no durable intent)
        return _run_cycle_inner(grad, price_fn, scan_fn, now, price_route, lock_ok=False)      # never defers: entries are refused anyway
    try:
        return _run_cycle_inner(grad, price_fn, scan_fn, now, price_route, defer_scan=defer_scan)
    finally:
        try: lk.close()
        except Exception: pass


def scan_phase(grad, scan_fn, rec):
    """Second phase of a deferred cycle: runs the entry scan for ONE lane, after every lane's exit phase. Takes the same per-ledger cycle lock, re-checks
    the scan gate NOW (fault, markers, account) and whether an abort happened since the exit phase ended. Never raises; exits are not involved.
    EVERY path after the lock is taken finalizes the journal with a truthful status ("done" when the scan ran; "done_scan_skipped" when refused; a
    failed final write is recorded in rec["journal_final_write_failure"] and the journal then stays running, fail closed). When the lock cannot be
    taken the journal is left as the exit phase wrote it (running/awaiting_scan): the next cycle reports it as interrupted (fail closed, possibly
    spurious, stated)."""
    import fcntl  # noqa: PLC0415
    if not rec.get("scan_deferred") or scan_fn is None:
        return rec
    rec["scan_deferred"] = False
    try:
        lk = open(_cycle_lock_path(grad), "w"); fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception as e:
        rec["scan_skipped"] = "busy_or_lock_failure:%r" % (e,); return rec
    try:
        try:
            now_ab = _abort_count(grad)
            if now_ab is None or (rec.get("aborts_end") is not None and now_ab > int(rec["aborts_end"])):
                _record_fault(grad, rec, "abort_between_exit_phase_and_scan", aborts_end=rec.get("aborts_end"), aborts_now=now_ab)
                rec["scan_skipped"] = "abort_before_scan"
            else:
                ok, why = scan_gate(grad)
                if not ok:
                    rec["scan_skipped"] = why
                elif not _write_journal(grad, dict(rec, status="running", phase="scan"), rec):
                    rec["scan_skipped"] = "scan_intent_not_durable"      # no durable "running" scan record: the entry crash window would be invisible
                else:
                    try:
                        _res = scan_fn(); rec.setdefault("steps", {})["scan"] = {"ok": True, "feed_health": (_res or {}).get("feed_health") if isinstance(_res, dict) else None}
                    except Exception as e:
                        rec.setdefault("steps", {})["scan"] = {"ok": False, "error": repr(e)[:200]}
        except Exception as e:
            rec["scan_skipped"] = "scan_phase_exception:%r" % (e,)
        rec["status"] = "done_scan_skipped" if rec.get("scan_skipped") else "done"
        try:
            _atomic(_journal_path(grad), dict(rec))
        except Exception as e:
            rec["journal_final_write_failure"] = repr(e)[:200]
        return rec
    finally:
        try: lk.close()
        except Exception: pass


def run_all(engines, now=None):
    """engines = [(lane, grad, scan_fn)]. ALL lanes' exit phases run first; only then each lane's scan (so a slow scan on one lane cannot delay another
    lane's exits). Returns {lane: record}."""
    recs = {}
    for lane, eng, sf in engines:
        recs[lane] = run_cycle(grad=eng, price_fn=None, scan_fn=sf, now=now, price_route="lane_default", defer_scan=True)
    for lane, eng, sf in engines:
        try: scan_phase(eng, sf, recs[lane])
        except Exception as e: recs[lane]["scan_skipped"] = "scan_phase_error:%r" % (e,)
    return recs


class GradCycleDaemon(_Daemon):
    name = "grad_cycle"
    interval = 60.0     # engineering cadence: above the 12s jup_price cache (a faster poll would only re-read cached prices) and the Daemon default; a
                        # reviewer may tune it. The status consumer takes its own max_age_s, so the two are not coupled by a hidden constant.

    def __init__(self, price_fn=None, scan_fn=None, interval=None, price_route=None):
        _Daemon.__init__(self, interval)
        self.price_fn, self.scan_fn = price_fn, scan_fn
        self.price_route = price_route

    def run_once(self):
        run_cycle(price_fn=self.price_fn, scan_fn=self.scan_fn, price_route=self.price_route)


def status(grad, max_age_s, now=None):
    """Health CONSUMER (read-only). `max_age_s` is the consumer's own poll cadence (supplied, never invented here). Returns {"state", ...}:
    "unknown" (no or unreadable journal), "stale" (last cycle older than max_age_s: the driver stopped, UNKNOWN not clean), "interrupted"
    (journal still 'running' from an earlier cycle), "scan_skipped" (exits ran; entries refused with a reason), "ok"."""
    now = time.time() if now is None else now
    act, why = fault_active(grad)
    if act:
        f = _read_fault(grad) or {}
        since = f.get("since") or (_MEM.get(grad.LEDGER_PATH) or {}).get("since")
        return {"state": "fault_unresolved", "why": why, "reason": f.get("reason"), "fault": f,
                "fault_age_s": None if since is None else now - float(since), "last": _read_journal(grad)}
    j = _read_journal(grad)
    if not j or j.get("unreadable"):
        return {"state": "unknown", "why": (j or {}).get("unreadable") or "no_cycle_journal"}
    ts = j.get("ts")
    if ts is None or (now - float(ts)) > float(max_age_s):
        return {"state": "stale", "age_s": None if ts is None else now - float(ts), "last": j}
    if j.get("status") == "running":
        return {"state": "interrupted", "last": j}
    if j.get("degraded"):
        return {"state": "degraded", "why": j["degraded"], "last": j}
    _fh = (((j.get("steps") or {}).get("scan") or {}).get("feed_health"))
    if _fh:
        return {"state": "feed_error", "why": (_fh or {}).get("error"), "feed_health": _fh, "last": j}      # the lane is inert on a durable feed error: not "ok"
    if j.get("scan_skipped"):
        return {"state": "scan_skipped", "why": j["scan_skipped"], "last": j}
    return {"state": "ok", "last": j}


def make_select_scan_fn(engine, feed=None, gt_rows_fn=None):
    """Adapter for ONE entry scan of a lane engine (grad_select.scan_once). `feed` (a graduation stream) and `gt_rows_fn` (backfill rows) are
    INJECTED: this module opens no stream. With neither, scan_once sees no candidates and opens nothing (an inert scan, by design)."""
    def _scan():
        from daemon.launch_scanner import grad_select  # noqa: PLC0415
        res = grad_select.scan_once(engine, feed=feed, gt_rows=(gt_rows_fn() if gt_rows_fn else None))
        h = feed.health() if feed is not None and hasattr(feed, "health") else None
        return dict(res or {}, feed_health=h)       # an empty feed with feed_health None is genuinely empty; an error is visible in the cycle journal
    return _scan


LANES_ENV = "AGT_GRAD_CYCLE_LANES"       # comma list of lane names; DEFAULT EMPTY = no lane. The user has not selected a lane in anything read.
# Engines that are fresh module copies of grad.py with their own ledger (each one's make_engine() was read: it execs grad.py and overrides
# LEDGER_PATH/LANE/limits). "grad" is the published module itself. Listing a lane here is NOT selecting it. Only grad_select has an entry-scan
# adapter (make_select_scan_fn); every other lane runs EXITS ONLY (no scan_fn, entries stay off) until an adapter is reviewed.
_ENGINES = {"grad": None, "grad_select": "grad_select", "grad_raw": "grad_raw", "tranche": "tranche", "tranche_protect": "tranche_protect",
            "smart_select": "smart_select", "mimic": "mimic", "mimic_edge": "mimic_edge", "moon": "moon_lane"}


def selected_lanes(environ=None):
    """(lanes, unknown). Pure parse of AGT_GRAD_CYCLE_LANES; nothing is chosen on the user's behalf."""
    raw = (environ if environ is not None else os.environ).get(LANES_ENV, "")
    names = [x.strip() for x in raw.split(",") if x.strip()]
    return [n for n in names if n in _ENGINES], [n for n in names if n not in _ENGINES]


def load_engine(lane):
    import importlib  # noqa: PLC0415
    if lane == "grad":
        from daemon.launch_scanner import grad as g  # noqa: PLC0415
        return g
    return importlib.import_module("daemon.launch_scanner." + _ENGINES[lane]).make_engine()


FEED_SPOOL_ENV = "AGT_GRAD_FEED_SPOOL"   # path of the graduation spool (daemon/grad_feed_spool.py); unset = NO feed, entries stay inert


def _spool_feed(lane):
    path = os.environ.get(FEED_SPOOL_ENV)
    if not path:
        return None
    from daemon import grad_feed_spool  # noqa: PLC0415
    return grad_feed_spool.SpoolFeed(path, path + ".cursor-" + lane + ".json")


def main_entry(argv=None):
    if not enabled():
        logger.warning("grad_cycle disabled (%s != 1): exiting without doing anything", ENV_FLAG)
        return 0
    lanes, unknown = selected_lanes()
    if unknown or not lanes:
        logger.error("grad_cycle: no valid lane selected (%s=%r, unknown=%s): refusing to start, no lane is chosen for you", LANES_ENV, os.environ.get(LANES_ENV), unknown)
        return 2
    engines = []
    for lane in lanes:
        try:
            eng = load_engine(lane)
        except Exception as e:
            logger.error("grad_cycle: lane %s engine failed to load (%r): refusing to start", lane, e)
            return 2
        engines.append((lane, eng, make_select_scan_fn(eng, feed=_spool_feed(lane)) if lane == "grad_select" else None))
    # Exits use each lane's own default read-only price route (price_fn None, price_route="lane_default"). Each engine has its OWN ledger, so
    # faults, journals and clear records are per lane. No graduation feed is opened here (needs a reviewed adapter): entries stay inert.
    d = GradCycleDaemon(price_route="lane_default")
    d.run_once = lambda: run_all(engines)
    return d.run_forever()


if __name__ == "__main__":
    import sys
    sys.exit(main_entry(sys.argv[1:]))
