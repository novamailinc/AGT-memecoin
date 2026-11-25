"""UNEXECUTED SOURCE (R1/R2 v3). Routes the grad-select-trader MAIN LOOP and its entry thread through daemon/grad_cycle, behind the SAME flag
(AGT_GRAD_CYCLE_ENABLED must be exactly "1"; default OFF). Flag off, or an explicitly non-DEMO engine: every function here calls the caller's ORIGINAL
code unchanged. Nothing here opens a ledger, starts a thread, or runs at import.

JOURNAL OWNERSHIP (v3, fixes the v2 two-writer defect). The per-ledger cycle journal (<ledger>.cycle.json) has ONE writer: grad_cycle.run_cycle/scan_phase
on the main loop. Its deferred state spans two lock holds (running/awaiting_scan, release, scan_phase), so a second writer between them could overwrite
that state and hide a main-loop death. The entry thread therefore NEVER touches it. It owns its own record, <ledger>.entry-scan.json, and its own lock,
<ledger>.entry-scan.lock (not the cycle lock), so it also cannot make the main loop's scan_phase see "busy". Liveness = the entry lock: flock is released by
the kernel when the process dies, so "record says running AND the lock is free" means that scan did not finish (same boot: an escaped exception; new boot:
the process died). That is a durable fault (interrupted_entry_scan) and refuses entries until cleared. A held lock means a live scan, never a fault.
Both the entry thread and the main loop call _entry_check() before scanning, so either one finds a dead entry scan.

ABORT ORDERING. grad.py counts a poll abort INSIDE the Phase-3 ledger lock and (v3 seam _ABORT_HOOK) calls the hook before releasing it. The entry commit gate
(entry_guard, called from grad.consider_candidate's authoritative section) runs under the SAME lane ledger lock. So for any abort in this process, the count
and the hook's durable fault both happen-before any later entry commit, and an entry commit cannot overlap an abort. The hook writes the fault file and the
fsynced append-only fault log through grad_cycle._record_fault, so the fault survives process death. commit_refusal refuses on (a) an active or unreadable
fault, (b) an in-memory abort count above the acknowledged baseline, (c) a hook that failed to record. Residual gaps, stated: an abort in ANOTHER process
is invisible here; a kill between the count and the end of the hook (inside the lock, a short window) leaves only the ledger-level heal; _record_fault is not
internally thread-safe, (v3b) every fault writer, including main-cycle calls, now serialises on grad_cycle's cross-thread/cross-process fault lock (leaf lock; a timeout means NOT recorded, fail closed).
"""
import logging
import os
import threading
import time

logger = logging.getLogger("grad_trader_cycle")

FLAG = "AGT_GRAD_CYCLE_ENABLED"
_EMPTY_SCAN = {"stream": 0, "opened": 0, "select_rejected": 0, "select_reasons": {}}
_ENGINES = {}          # lane -> engine, filled by register()
_BASELINE = {}         # lane -> acknowledged abort count
_HOOK_FAILED = {}      # lane -> True when the abort hook could not record a fault


def _gc():
    from daemon import grad_cycle  # noqa: PLC0415
    return grad_cycle


# ---------------------------------------------------------------- mode ---

def mode_info(engine):
    """(mode, detail). mode is 'off' (flag not exactly '1', or the engine is explicitly NOT DEMO: legacy path, the cycle never drives a non-simulated lane),
    'on' (flag on and DEMO) or 'refuse' (flag on but the cycle module or DEMO state is unreadable: exits only, NO entries, fail closed). detail separates
    the two 'off' causes ('flag_off' vs 'off_non_demo') and gives the refuse reason, so startup can show which one applies. AGT_GRAD_CYCLE_LANES does NOT
    change the mode: it only decides which UNREGISTERED lanes commit_refusal blocks."""
    if os.environ.get(FLAG) != "1":
        return "off", "flag_off"
    try:
        gc = _gc()
        if not gc.enabled():
            return "refuse", "grad_cycle_disabled"
        return ("on", "") if bool(engine._demo_mode()) else ("off", "off_non_demo")
    except Exception as e:
        return "refuse", "mode_unreadable:%r" % (e,)


def mode(engine):
    return mode_info(engine)[0]


def active(engine):
    return mode(engine) == "on"


# ------------------------------------------------------ registration ---

def _abort_count(engine):
    """Direct read of the engine's in-memory counter. NOT grad_cycle._abort_count: that goes through recon_health -> the ledger lock, and this is called
    from commit_refusal, which already runs UNDER the lane ledger lock (a nested lock self-deadlocks)."""
    try:
        return int(engine._POLL_ABORTS["count"])
    except Exception:
        return None


_BEGIN_FAILED = {}  # lane -> True once a write-ahead marker write failed in this process (cleared only by register, i.e. a restart)
_INFLIGHT = {}      # lane -> token of the write-ahead marker for the failure currently being handled (set by BEGIN, used by the abort hook)


def _make_begin_hook(engine):
    lane = str(engine.LANE)

    def _begin(mint, err):
        try:
            tok = _gc().mark_inflight(engine, mint, err)
        except Exception:
            tok = None
        _INFLIGHT[lane] = tok
        if tok is None:
            _BEGIN_FAILED[lane] = True      # write-ahead marker NOT durable for this failure: visible, refuses entries for the life of this process
        return tok
    return _begin


def _make_end_hook(engine):
    def _end(mint, tok):
        _gc().unmark_inflight(engine, tok)
    return _end


def _make_abort_hook(engine):
    lane = str(engine.LANE)

    def _hook(mint, err):
        try:
            ok = _gc()._record_fault(engine, {}, "poll_abort_live", mint=mint, error=err)      # serialised inside grad_cycle by the fault lock
            if not ok:
                _HOOK_FAILED[lane] = True
            else:
                _gc().unmark_inflight(engine, _INFLIGHT.pop(lane, None))      # durable fault recorded: marker superseded. On failure it STAYS (protects after restart)
        except Exception:
            _HOOK_FAILED[lane] = True
    return _hook


_REGISTER_FAILED = {}  # lane (or "?" when the lane itself is unreadable) -> repr of the registration error; refuses entries while the flag is on


def register(engine):
    """Record the engine for the commit-time check, take the abort baseline, and (flag on) install the abort hooks. No ledger I/O. Returns True when registration
    completed. A failure is NEVER silent: it is latched in _REGISTER_FAILED (commit_refusal refuses entries; startup_report shows it) and False is returned."""
    lane = "?"
    try:
        lane = str(engine.LANE)
        _ENGINES[lane] = engine
        c = _abort_count(engine)
        _BASELINE[lane] = c
        _HOOK_FAILED.pop(lane, None)
        _BEGIN_FAILED.pop(lane, None)
        _REGISTER_FAILED.pop(lane, None)
        if os.environ.get(FLAG) == "1":
            engine._ABORT_HOOK = _make_abort_hook(engine)
            engine._FAILURE_BEGIN_HOOK = _make_begin_hook(engine)
            engine._FAILURE_END_HOOK = _make_end_hook(engine)
        return True
    except Exception as e:
        _REGISTER_FAILED[lane] = repr(e)[:200]
        return False


def startup_report(engine):
    """Read-only startup snapshot for the log. Does not heal, clear or write. Keeps the v2 keys (active, why, fault_active) and adds mode/detail."""
    m, detail = mode_info(engine)
    if m == "refuse":
        return {"active": False, "mode": "refuse", "detail": detail, "why": "flag on but cycle module or DEMO state unreadable: entries refused, exits only"}
    if m != "on":
        return {"active": False, "mode": "off", "detail": detail}
    try:
        act, why = _gc().fault_active(engine)
    except Exception as e:
        act, why = True, "fault_state_unreadable:%r" % (e,)
    return {"active": True, "mode": "on", "detail": "", "fault_active": bool(act), "why": why,
            "registered": str(getattr(engine, "LANE", "?")) in _ENGINES, "register_failed": _REGISTER_FAILED.get(str(engine.LANE)) or _REGISTER_FAILED.get("?"), "begin_failed": bool(_BEGIN_FAILED.get(str(engine.LANE))), "hook_failed": bool(_HOOK_FAILED.get(str(engine.LANE))),
            "inflight_marker": _gc()._inflight_state(engine), "abort_baseline": _BASELINE.get(str(engine.LANE)), "entry_scan_state": _entry_state(engine)}


def _advance_baseline(engine, rec):
    """Only after a COMPLETED main cycle: its aborts_end was read after the exit steps, and any abort it saw already became a durable fault before this
    runs, so advancing the baseline never hides one. A later abort stays above the baseline until the next cycle."""
    try:
        if rec and str(rec.get("status") or "").startswith("done") and rec.get("aborts_end") is not None and not rec.get("fault_write_failure") \
                and not rec.get("fault_log_write_failure"):
            _BASELINE[str(engine.LANE)] = int(rec["aborts_end"])
    except Exception:
        pass


# --------------------------------------------------------- commit check ---

def commit_refusal(lane):
    """Called by entry_guard under the lane ledger lock right before the debit. Flag off: None. Flag on: (1) a lane NAMED in AGT_GRAD_CYCLE_LANES but not
    registered in this process refuses (the operator turned the flag on and chose the lane but this process is not wired); a lane not named is untouched;
    (2) a registered lane refuses on an active or unreadable fault, an abort count above the baseline (or unreadable), or a failed abort hook.
    Reads files and in-memory counters only; takes no lock."""
    if os.environ.get(FLAG) != "1":
        return None
    lane = str(lane)
    try:
        known, unknown = _gc().selected_lanes()
        named = list(known) + list(unknown)       # a lane NAMED in the env but not known to grad_cycle is still named
    except Exception as e:
        return "cycle lane selection unreadable (%r): entries blocked fail-closed" % (e,)
    if _REGISTER_FAILED.get(lane) or _REGISTER_FAILED.get("?"):
        return "cycle hook registration failed (%s): entries blocked fail-closed" % (_REGISTER_FAILED.get(lane) or _REGISTER_FAILED.get("?"))
    eng = _ENGINES.get(lane)
    if eng is None:
        return ("lane %s is selected in AGT_GRAD_CYCLE_LANES but this process did not register it: entries blocked" % lane) if lane in named else None
    try:
        act, why = _gc().fault_active(eng)
    except Exception as e:
        return "cycle fault state unreadable (%r): entries blocked fail-closed" % (e,)
    if act:
        return "grad_cycle fault unresolved (%s): entries blocked" % why
    if _HOOK_FAILED.get(lane):
        return "abort hook could not record a fault: entries blocked"
    if _BEGIN_FAILED.get(lane):
        return "poll-failure write-ahead marker could not be written: entries blocked (durable protection degraded; exits unaffected)"
    base, cur = _BASELINE.get(lane), _abort_count(eng)
    if base is None or cur is None:
        return "abort count unreadable: entries blocked fail-closed"
    if cur > base:
        return "poll abort count %d above acknowledged baseline %d (next cycle records the fault): entries blocked" % (cur, base)
    return None


# ------------------------------------------------------ entry journal ---

def _entry_paths(engine):
    return engine.LEDGER_PATH + ".entry-scan.json", engine.LEDGER_PATH + ".entry-scan.lock"


def _entry_state(engine):
    try:
        jp, _lp = _entry_paths(engine)
        j = _gc()._read_json(jp)
        return None if j is None else ("unreadable" if j.get("unreadable") else j.get("status"))
    except Exception as e:
        return "error:%r" % (e,)


def _entry_check_locked(engine):
    """Caller holds the entry lock. A prior record still 'running' (or unreadable) is a dead scan: record a durable fault. Returns (ok, why)."""
    gc = _gc()
    jp, _lp = _entry_paths(engine)
    j = gc._read_json(jp)
    if j is None:
        return True, None
    if j.get("unreadable"):
        gc._record_fault(engine, {}, "entry_scan_journal_unreadable", detail=str(j.get("unreadable"))[:120])
        return False, "entry_scan_journal_unreadable"
    if j.get("status") == "running":
        gc._record_fault(engine, {}, "interrupted_entry_scan", prior_boot=j.get("boot_id"), same_boot=(j.get("boot_id") == gc._BOOT["id"]))
        try:
            gc._atomic(jp, dict(j, status="interrupted_recorded"))      # the fault is durable; do not record it twice
        except Exception:
            pass
        return False, "interrupted_entry_scan"
    return True, None


def _entry_check(engine):
    """Used by the main loop: finds a dead entry scan without scanning. A held lock = a live scan = fine."""
    import fcntl  # noqa: PLC0415
    jp, lp = _entry_paths(engine)
    try:
        lk = open(lp, "w")
    except Exception as e:
        return False, "entry_lock_io:%r" % (e,)
    try:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, "live_entry_scan"
        return _entry_check_locked(engine)
    finally:
        try: lk.close()
        except Exception: pass


def entry_allowed(engine):
    """(ok, why). off/off_non_demo: (True, None). refuse: False. on: entry check plus scan_gate; any exception refuses."""
    m, why = mode_info(engine)
    if m == "off":
        return True, None
    if m == "refuse":
        return False, "flag_on_cycle_unavailable:" + why
    try:
        ok, w = _entry_check(engine)
        if not ok:
            return False, w
        return _gc().scan_gate(engine)
    except Exception as e:
        return False, "scan_gate_error:%r" % (e,)


def entry_scan(engine, scan_fn):
    """One entry-thread scan. off/off_non_demo: scan_fn() directly. refuse: skipped. on: under the ENTRY lock (non-blocking) with its OWN journal: check for
    a dead prior scan, re-check the gate, write running, scan, write done. Returns (ran, why_or_None, result)."""
    m, why = mode_info(engine)
    if m == "off":
        return True, None, scan_fn()
    if m == "refuse":
        return False, "flag_on_cycle_unavailable:" + why, None
    import fcntl  # noqa: PLC0415
    gc = _gc()
    jp, lp = _entry_paths(engine)
    try:
        lk = open(lp, "w")
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False, "entry_scan_busy", None
    except Exception as e:
        return False, "entry_lock_io:%r" % (e,), None
    try:
        ok, w = _entry_check_locked(engine)
        if not ok:
            return False, w, None
        ok, w = gc.scan_gate(engine)
        if not ok:
            return False, w, None
        rec = {"boot_id": gc._BOOT["id"], "ts": time.time(), "status": "running", "aborts_start": _abort_count(engine)}
        try:
            gc._atomic(jp, rec)
        except Exception as e:
            return False, "entry_journal_unwritable:%r" % (e,), None      # no durable intent, no scan
        try:
            res = scan_fn()
        except Exception as e:
            try: gc._atomic(jp, dict(rec, status="done", scan_error=repr(e)[:200], ts=time.time()))
            except Exception: pass
            return False, "scan_exception:%r" % (e,), None
        try:
            gc._atomic(jp, dict(rec, status="done", ts=time.time(), aborts_end=_abort_count(engine)))
        except Exception:
            pass       # the record stays 'running': the next check reports it as interrupted (fail closed, possibly spurious, stated)
        return True, None, res
    finally:
        try: lk.close()
        except Exception: pass


# ---------------------------------------------------------- main loop ---

def _direct_exits(engine, price_fn):
    try:
        return len(engine.poll_time_exits() or []) + len(engine.poll_exits(price_fn) or [])
    except Exception:
        logger.exception("grad_trader_cycle: direct exits failed")
        return 0


def main_cycle(engine, price_fn, scan_fn, legacy):
    """One main-loop tick. scan_fn() runs the entry scan and returns the scan_once result dict. legacy() runs the ORIGINAL scan+exits code and returns
    (res, n_closes). Returns (res, n_closes, record_or_None). Exits run in every mode."""
    m, why = mode_info(engine)
    if m == "off":
        res, n = legacy()
        return res, n, None
    if m == "refuse":
        return dict(_EMPTY_SCAN, skipped="flag_on_cycle_unavailable:" + why), _direct_exits(engine, price_fn), None
    try:
        _entry_check(engine)          # finds (and durably records) a dead entry scan before this cycle's scan gate runs
    except Exception:
        pass
    held = {}

    def _scan():
        r = scan_fn()
        held["res"] = r
        return r
    try:
        rec = _gc().run_cycle(grad=engine, price_fn=price_fn, scan_fn=_scan, defer_scan=True)
    except Exception as e:           # run_cycle never raises; an exception is an integration fault: fall back so exits still run
        logger.warning("grad_trader_cycle: run_cycle raised %r; falling back to the direct exit calls", e)
        rec = {"status": "cycle_exception:%r" % (e,)}
    st = str(rec.get("status") or "")
    steps = rec.get("steps") or {}
    if st.startswith("busy") or st.startswith("cycle_exception") or st == "disabled" or st.startswith("refused") or st.startswith("grad_import_failed") or not steps:
        return dict(_EMPTY_SCAN, skipped=st or "no_steps"), _direct_exits(engine, price_fn), rec
    try:
        _gc().scan_phase(engine, _scan, rec)
    except Exception as e:
        rec["scan_skipped"] = "scan_phase_error:%r" % (e,)
    _advance_baseline(engine, rec)
    n = 0
    for k in ("time_exits", "exits"):
        s = steps.get(k) or {}
        if s.get("ok"):
            n += int(s.get("n_closed") or 0)
    res = held.get("res")
    if not isinstance(res, dict):
        res = dict(_EMPTY_SCAN, skipped=rec.get("scan_skipped") or "scan_not_run")
    return res, n, rec
