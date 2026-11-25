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
import time

logger = logging.getLogger("grad_trader_cycle")

_EMPTY_SCAN = {"stream": 0, "opened": 0, "select_rejected": 0, "select_reasons": {}}


def _gc():
    from daemon import grad_cycle  # noqa: PLC0415
    return grad_cycle


def active(engine):
    """True only when the flag is exactly '1' AND the engine reports DEMO. Anything unreadable is False (legacy path)."""
    try:
        return bool(_gc().enabled() and engine._demo_mode())
    except Exception:
        return False


def startup_report(engine):
    """Read-only snapshot for the startup log. Does not heal, clear, or write. Returns a small dict."""
    if not active(engine):
        return {"active": False}
    try:
        act, why = _gc().fault_active(engine)
    except Exception as e:
        act, why = True, "fault_state_unreadable:%r" % (e,)
    return {"active": True, "fault_active": bool(act), "why": why}


def entry_allowed(engine):
    """(ok, why). Flag off or non-DEMO: (True, None), the old behavior. On: scan_gate; any exception refuses (entries only, never exits)."""
    if not active(engine):
        return True, None
    try:
        return _gc().scan_gate(engine)
    except Exception as e:
        return False, "scan_gate_error:%r" % (e,)


def main_cycle(engine, price_fn, scan_fn, legacy):
    """One main-loop tick. scan_fn() runs the entry scan and returns the scan_once result dict. legacy() runs the ORIGINAL scan+exits code and returns
    (res, n_closes). Returns (res, n_closes, record_or_None). Never raises past what legacy itself would."""
    if not active(engine):
        res, n = legacy()
        return res, n, None
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
