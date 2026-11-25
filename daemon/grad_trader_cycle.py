"""UNEXECUTED SOURCE (R1/R2). Routes the grad-select-trader MAIN LOOP and its entry thread through daemon/grad_cycle, behind the SAME flag
(AGT_GRAD_CYCLE_ENABLED must be exactly "1"; default OFF). Flag off, or a non-DEMO engine, or a cycle driver import failure: every function here calls
the caller's ORIGINAL code unchanged, so the running trader is byte-identical in behavior. Nothing here opens a ledger, starts a thread, or runs at import.

Behavior when ON (DEMO engine only):
 * main loop: grad_cycle.run_cycle(defer_scan=True) runs time exits then exits under the per-ledger cycle lock and journals them; then
   grad_cycle.scan_phase runs the entry scan under the same lock after re-checking the scan gate. ORDER CHANGE vs the old loop: exits now run BEFORE
   the scan (the old loop scanned first). Exits are never conditional on a fault, a gate, or a journal failure.
 * cycle lock busy (a clear is committing, or another cycle holds it): the exits STILL run through the caller's original direct calls this tick, the
   scan is skipped and says so. A risk-reducing exit is never skipped for bookkeeping.
 * entry thread: consults scan_gate before each fresh-only scan; a fault or an unreadable state refuses the scan. Its scans are NOT journaled (a kill
   during an entry-thread scan relies on the ledger-level heal, not the cycle journal): an uncovered window, stated, not hidden.
 * push-price exits (armed watcher) and the ExitPoller thread stay DIRECT and unjournaled on purpose: they are latency-critical and fire per tick. Any
   abort they cause is still detected by the cycle via the per-ledger poll_aborts counter (abort_between_cycles), which blocks ENTRIES only.
 * the unit lock is NOT re-entered: nothing here takes a ledger lock, and the cycle lock is non-blocking, so there is no lock-order cycle.
"""
import logging
import os
import time

logger = logging.getLogger("grad_trader_cycle")

_EMPTY_SCAN = {"stream": 0, "opened": 0, "select_rejected": 0, "select_reasons": {}}


def _gc():
    from daemon import grad_cycle  # noqa: PLC0415
    return grad_cycle


FLAG = "AGT_GRAD_CYCLE_ENABLED"
_ENGINES = {}      # lane -> engine, filled by the wired trader at startup; read by the commit-time check in entry_guard


def register(engine):
    """Record the engine so the commit-time entry check (entry_guard) can find its ledger path by lane. No I/O."""
    try:
        _ENGINES[str(engine.LANE)] = engine
    except Exception:
        pass


def mode(engine):
    """'off' (flag not exactly '1', or the engine is explicitly NOT DEMO: legacy path, the cycle never drives a non-simulated lane),
    'on' (flag on and DEMO), or 'refuse' (flag on but the cycle module or the DEMO state is unreadable: exits only, NO entries, fail closed)."""
    if os.environ.get(FLAG) != "1":
        return "off"
    try:
        gc = _gc()
        if not gc.enabled():
            return "refuse"
        return "on" if bool(engine._demo_mode()) else "off"
    except Exception:
        return "refuse"


def active(engine):
    return mode(engine) == "on"


def commit_refusal(lane):
    """Called by entry_guard under the lane ledger lock, right before the debit. Flag off: None. Flag on: a registered lane with an active or unreadable
    fault refuses; an UNregistered lane (not wired) is not affected. Reads fault files only: takes no ledger lock, so it cannot self-deadlock."""
    if os.environ.get(FLAG) != "1":
        return None
    eng = _ENGINES.get(str(lane))
    if eng is None:
        return None
    try:
        act, why = _gc().fault_active(eng)
    except Exception as e:
        return "cycle fault state unreadable (%r): entries blocked fail-closed" % (e,)
    return ("grad_cycle fault unresolved (%s): entries blocked" % why) if act else None


def startup_report(engine):
    """Read-only snapshot for the startup log. Does not heal, clear, or write. Returns a small dict."""
    m = mode(engine)
    if m == "refuse":
        return {"active": False, "mode": "refuse", "why": "flag on but cycle module or DEMO state unreadable: entries refused, exits only"}
    if m != "on":
        return {"active": False}
    try:
        act, why = _gc().fault_active(engine)
    except Exception as e:
        act, why = True, "fault_state_unreadable:%r" % (e,)
    return {"active": True, "fault_active": bool(act), "why": why}


def entry_allowed(engine):
    """(ok, why). Flag off or non-DEMO: (True, None), the old behavior. On: scan_gate; any exception refuses (entries only, never exits)."""
    m = mode(engine)
    if m == "off":
        return True, None
    if m == "refuse":
        return False, "flag_on_cycle_unavailable"
    try:
        return _gc().scan_gate(engine)
    except Exception as e:
        return False, "scan_gate_error:%r" % (e,)


def main_cycle(engine, price_fn, scan_fn, legacy):
    """One main-loop tick. scan_fn() runs the entry scan and returns the scan_once result dict. legacy() runs the ORIGINAL scan+exits code and returns
    (res, n_closes). Returns (res, n_closes, record_or_None). Never raises past what legacy itself would."""
    m = mode(engine)
    if m == "off":
        res, n = legacy()
        return res, n, None
    if m == "refuse":
        # flag on but the cycle cannot be used: NEVER fall back to ungated entries. Exits still run directly.
        try:
            n = len(engine.poll_time_exits() or []) + len(engine.poll_exits(price_fn) or [])
        except Exception:
            logger.exception("grad_trader_cycle: direct exits failed")
            n = 0
        return dict(_EMPTY_SCAN, skipped="flag_on_cycle_unavailable"), n, None
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
    if st.startswith("busy") or st.startswith("cycle_exception") or st in ("disabled",) or st.startswith("refused") or st.startswith("grad_import_failed") or not steps:
        # the cycle did not run the exits: run them directly, skip the scan, say so
        try:
            n = len(engine.poll_time_exits() or []) + len(engine.poll_exits(price_fn) or [])
        except Exception:
            logger.exception("grad_trader_cycle: direct exits failed")
            n = 0
        return dict(_EMPTY_SCAN, skipped=st or "no_steps"), n, rec
    try:
        _gc().scan_phase(engine, _scan, rec)
    except Exception as e:
        rec["scan_skipped"] = "scan_phase_error:%r" % (e,)
    n = 0
    for k in ("time_exits", "exits"):
        s = steps.get(k) or {}
        if s.get("ok"):
            n += int(s.get("n_closed") or 0)
    res = held.get("res")
    if not isinstance(res, dict):
        res = dict(_EMPTY_SCAN, skipped=rec.get("scan_skipped") or "scan_not_run")
    return res, n, rec


def entry_scan(engine, scan_fn):
    """One entry-thread scan. Flag off: scan_fn() directly, as before. On: JOURNALED through scan_phase under the non-blocking cycle lock (gate
    re-checked inside); busy means skipped this attempt (the caller retries). Refuse mode: skipped. Returns (ran, why_or_None, result)."""
    m = mode(engine)
    if m == "off":
        return True, None, scan_fn()
    if m == "refuse":
        return False, "flag_on_cycle_unavailable", None
    gc = _gc()
    held = {}

    def _scan():
        held["res"] = scan_fn()
        return held["res"]
    rec = {"boot_id": gc._BOOT["id"], "cycle_seq": gc._BOOT["seq"], "ts": time.time(), "steps": {}, "scan_deferred": True,
           "aborts_end": gc._abort_count(engine), "kind": "entry_thread"}
    try:
        gc.scan_phase(engine, _scan, rec)
    except Exception as e:
        return False, "scan_phase_error:%r" % (e,), None
    if rec.get("scan_skipped"):
        return False, rec["scan_skipped"], None
    return True, None, held.get("res")
