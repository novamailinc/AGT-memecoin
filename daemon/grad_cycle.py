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


def fault_active(grad):
    """(active, why). The durable file is authoritative. A file-less in-process fault is dropped ONLY when a durable clear record names it
    (so another process clearing it is honored, and a lost fault file is not read as a clear)."""
    f = _read_fault(grad)
    if f is not None:
        return True, "unreadable_record" if f.get("unreadable") else "recorded"
    m = _MEM.get(grad.LEDGER_PATH)
    if m is None:
        return False, None
    if m.get("file_written"):
        if any(r.get("fault_since") == m.get("since") for r in _clear_records(grad)):
            _MEM.pop(grad.LEDGER_PATH, None); return False, None
        return True, "fault_file_missing_without_clear_record"
    return True, "fault_file_never_written"


def _record_fault(grad, out, reason, **detail):
    """Make the fault durable. When the file write fails the in-process flag stays AND the prior cycle journal is left untouched."""
    since = out.get("ts") if out.get("ts") is not None else time.time()
    rec = dict({"since": since, "reason": reason, "boot_id": _BOOT["id"], "cycle_seq": out.get("cycle_seq")}, **detail)
    try:
        _atomic(_fault_path(grad), rec); written = True
    except Exception as e:
        written = False; out["fault_write_failure"] = repr(e)[:200]
    _MEM[grad.LEDGER_PATH] = {"since": since, "file_written": written}
    out.setdefault("faults", []).append(rec)
    return written


def _cash_conservation(pa, snap):
    """Independent of the lane: replay the ACCOUNT's own write-ahead log. Expected cash = start_balance + sum(credit net) - sum(debit stake).
    Returns (ok_or_None, detail): None = not evaluable (any event kind that moves cash in another way, or an unreadable journal): never a pass."""
    try:
        entries, status, _ = pa._journal_read()
    except Exception as e:
        return None, "journal_unreadable:%r" % (e,)
    if status:
        return None, "journal_status:%s" % status
    net = 0.0
    for e in entries:
        k = e.get("event")
        if k == "debit": net -= float(e.get("stake") or 0.0)
        elif k == "credit": net += float(e.get("net") or 0.0)
        elif k in ("hold", "hold_released", "fee", "pending_sol", "settle_sol"):
            return None, "cash_event_not_modelled:%s" % k
    exp = float(snap.get("start_balance") or 0.0) + net
    got = float(snap.get("cash") or 0.0)
    return abs(exp - got) <= _CASH_EPS, {"expected": round(exp, 6), "account": round(got, 6)}


def _fingerprint(grad, account=None):
    """Cheap comparable digest of the three stores (lane ledger positions+closes, account snapshot, fill journal keys). Bare reads only (safe inside
    the lane lock). An unreadable store gives a unique token so a clear cannot pass on it."""
    import hashlib
    parts = []
    for name, fn in (("lane", lambda: (lambda d: (sorted((d.get("positions") or {}).keys()), len(d.get("closes") or [])))(grad._read())),
                     ("acct", lambda: (account if account is not None else grad._paper_account()).snapshot()),
                     ("fills", lambda: sorted(grad._demo_filler()._locked(lambda jj: dict(jj["fills"])).keys()))):
        try: parts.append((name, fn()))
        except Exception as e: parts.append((name, "unreadable:%r:%r" % (e, time.time())))
    return hashlib.sha256(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()


def clear_fault(grad, account=None, operator_note=""):
    """EXPLICIT reconciliation. Returns (cleared, evidence). Evidence is read without the lane lock; the commit (clear record, fault removal) runs under it after re-checking that no cycle is "running" and that the store fingerprint did not change. Clears ONLY
    when every check passes (an unevaluable check is a failure, never a pass), and writes a durable clear record BEFORE removing the fault:
    fault record readable; storage I/O probe; lane ledger readable and free of pending/settlement/migration markers; fill journal readable;
    EVERY journaled lane fill maps to an open position or a close record (no orphan fill); account snapshot readable, not degraded, with every
    lane position mapped to an open lane position (no orphan account position) and no pending_sol, holds, unresolved or assumed_sold entries
    for the lane; account cash equals an independent replay of its own journal; and every open lane position diagnoses "consistent" (a
    legacy non-instance position is evidence_unavailable and blocks: a missing contract is never success; explained_residue does not clear).
    An EMPTY lane is not vacuously clean: orphans on the account or in the fill journal block it."""
    ev = {"operator_note": str(operator_note)[:200]}
    lock = getattr(grad, "_ledger_lock", None)
    if lock is None:
        return False, dict(ev, blocked=["no_ledger_lock"])
    fp0 = _fingerprint(grad, account)
    if True:     # evidence is gathered WITHOUT holding the lane lock (the lane's own readers take it; a second flock in this process would self-deadlock)
        f = _read_fault(grad)
        ev["fault_record"] = "none" if f is None else ("unreadable" if f.get("unreadable") else "present")
        if f is not None and f.get("unreadable"):
            return False, dict(ev, blocked=["fault_record_unreadable"])
        j = _read_journal(grad)
        ev["journal_status"] = (j or {}).get("status") if j and not j.get("unreadable") else ("unreadable" if j else "none")
        bad = []
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
        try:
            _d = grad._read() or {}
            op = _d.get("positions") or {}
            closes = _d.get("closes") or []
        except Exception as e:
            op, closes = None, None; bad.append("lane_state_unreadable:%r" % (e,))
        pa = account if account is not None else grad._paper_account()
        try: snap = pa.snapshot() if pa is not None else None
        except Exception: snap = None
        if not snap or snap.get("degraded"):
            ev["account"] = "unreadable_or_degraded"; bad.append("account")
        else:
            ev["account"] = "ok"
            lane = grad.LANE
            lp = [x for x in (((snap.get("lanes") or {}).get(lane) or {}).get("positions") or [])]
            ids = {str(p.get("pa_pos_id")) for p in (op or {}).values()}
            orphans = [x.get("pos_id") for x in lp if str(x.get("pos_id")) not in ids]
            if orphans: bad.append("account_orphan_positions:%s" % orphans)
            for name in ("pending_sol", "unresolved", "assumed_sold"):
                n = [x for x in (snap.get(name) or []) if x.get("lane") == lane]
                if n: bad.append("account_%s_for_lane:%d" % (name, len(n)))
            hl = [k for k, v in (snap.get("holds") or {}).items() if v.get("lane") == lane or str(k).startswith(lane)]
            if hl: bad.append("account_holds_for_lane:%d" % len(hl))
            ok_c, det = _cash_conservation(pa, snap)
            ev["cash_conservation"] = det
            if ok_c is not True: bad.append("cash_conservation_%s" % ("failed" if ok_c is False else "unevaluable"))
        try:
            fills = grad._demo_filler()._locked(lambda jj: dict(jj["fills"])); ev["fill_journal"] = "ok:%d" % len(fills)
            mints = set((op or {}).keys()) | {c.get("mint") for c in (closes or [])}
            own = [k for k in fills if str(k).startswith(grad.LANE + ":exit:") and str(k).split(":")[2] not in mints]
            if own: bad.append("orphan_fills:%d" % len(own))
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
        if bad:
            return False, dict(ev, blocked=bad)
        with lock():     # commit step only: re-verify under the lane lock that nothing moved and no cycle is running, using bare reads only
            j2 = _read_journal(grad)
            if j2 and (j2.get("unreadable") or j2.get("status") == "running"):
                return False, dict(ev, blocked=["cycle_running_at_commit"])
            if _fingerprint(grad, account) != fp0:
                return False, dict(ev, blocked=["state_changed_during_evidence"])
            try:
                with open(_clear_log_path(grad), "a") as fh:
                    fh.write(json.dumps({"ts": time.time(), "fault_since": (f or {}).get("since"), "fault": f, "evidence": ev, "boot_id": _BOOT["id"],
                                         "fingerprint": fp0}, default=str) + "\n")
                    fh.flush(); os.fsync(fh.fileno())
            except Exception as e:
                return False, dict(ev, blocked=["clear_record_not_written:%r" % (e,)])
            try:
                if f is not None: os.remove(_fault_path(grad))
            except Exception as e:
                return False, dict(ev, blocked=["fault_file_not_removed:%r" % (e,)])
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


def run_cycle(grad=None, price_fn=None, scan_fn=None, now=None, price_route=None):
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
    elif not intent_ok:
        out["scan_skipped"] = "cycle_journal_unavailable"      # exits already ran; NEW entries need the durable cycle intent
    else:
        ok, why = scan_gate(grad)
        if not ok:
            out["scan_skipped"] = why
        else:
            try:
                scan_fn(); out["steps"]["scan"] = {"ok": True}
            except Exception as e:
                out["steps"]["scan"] = {"ok": False, "error": repr(e)[:200]}
    out["status"] = "done"
    if fault_written:
        _write_journal(grad, out, out)
    return out


from daemon.common import Daemon as _Daemon       # a broken import FAILS CLOSED (module import error), never a stand-in base class


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
    if j.get("scan_skipped"):
        return {"state": "scan_skipped", "why": j["scan_skipped"], "last": j}
    return {"state": "ok", "last": j}


def make_select_scan_fn(engine, feed=None, gt_rows_fn=None):
    """Adapter for ONE entry scan of a lane engine (grad_select.scan_once). `feed` (a graduation stream) and `gt_rows_fn` (backfill rows) are
    INJECTED: this module opens no stream. With neither, scan_once sees no candidates and opens nothing (an inert scan, by design)."""
    def _scan():
        from daemon.launch_scanner import grad_select  # noqa: PLC0415
        return grad_select.scan_once(engine, feed=feed, gt_rows=(gt_rows_fn() if gt_rows_fn else None))
    return _scan


def main_entry(argv=None):
    if not enabled():
        logger.warning("grad_cycle disabled (%s != 1): exiting without doing anything", ENV_FLAG)
        return 0
    # Exits use the lane's own default read-only price route (jup_price free batch) because price_fn is None.
    # The SELECT lane engine is a SEPARATE module instance with its own ledger (grad_select.make_engine), so the driver is lane-parametric:
    # the engine is the `grad` argument of run_cycle. No graduation feed is opened here (needs a reviewed adapter): entries stay inert.
    from daemon.launch_scanner import grad_select  # noqa: PLC0415
    eng = grad_select.make_engine()
    d = GradCycleDaemon(scan_fn=make_select_scan_fn(eng), price_route="lane_default")
    d.run_once = lambda: run_cycle(grad=eng, price_fn=d.price_fn, scan_fn=d.scan_fn, price_route=d.price_route)
    return d.run_forever()


if __name__ == "__main__":
    import sys
    sys.exit(main_entry(sys.argv[1:]))
