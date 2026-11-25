"""Fixture-only controlled test for the tranche buyback commit gate + fault-lock barrier (WRITTEN, NOT RUN until main clears an exact commit+blob via pins.sh).
Fake engine, in-memory ledger, tempdir ledger path (lock/fault files), NO network (tripwire), no executor, no activation. Both tranche.py and tranche_protect.py.
MODULE IDENTITY: every module is imported by the SAME qualified path the code under test uses (daemon.launch_scanner.entry_guard, daemon.grad_cycle), and
identity is asserted, so the guard stub is the object the buyback calls. Each refusal check asserts the REASON (unique stub text in the warning log) and that the
stub was called exactly once with the engine lane, so a refusal for a different reason cannot pass.
F1 flag ON control: guard clear -> one buyback.   F2 guard refuses -> none, ledger unchanged, reason logged, stub called once with LANE.
F3 guard raises -> refused fail-closed, reason logged.   F4 RACE with a real fault-lock holder: another thread holds the fault lock (as _record_fault does) and
then writes the fault file; the buyback must wait for the lock, read the fault, and refuse (reason logged).   F5 SERIALIZATION: a fault writer started during the
buyback's ledger write cannot record before that write returns.   F6 REENTRANCY: a guard that itself takes the fault lock does not deadlock.   F7 flag OFF: no barrier,
no lock file, old behaviour.   F8 lock TIMEOUT: fault lock held past the retry window -> nothing written, failure logged, lock free afterwards.
F9 SENSITIVITY: with commit_barrier replaced by a no-op the F4 scenario executes the buyback (so the barrier, not luck, causes the refusal).   F0 tripwire: no socket connect/DNS."""
import copy, importlib, logging, os, socket, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FAILS = []; NET = []
def check(name, ok):
    print(("PASS: " if ok else "FAIL: ") + name)
    if not ok: FAILS.append(name)
def _trip(*a, **k): NET.append(a); raise OSError("network tripwire")
socket.socket.connect = _trip; socket.create_connection = _trip; socket.getaddrinfo = _trip
os.environ["AGT_GRAD_CYCLE_ENABLED"] = "1"
from daemon.launch_scanner import entry_guard as eg
from daemon import grad_cycle as gc
check("F0a entry_guard identity is the qualified module the buyback imports", sys.modules["daemon.launch_scanner.entry_guard"] is eg)
check("F0b grad_cycle identity is the qualified module commit_barrier imports", sys.modules["daemon.grad_cycle"] is gc)

class _Cap(logging.Handler):
    def __init__(self): super().__init__(logging.WARNING); self.msgs = []
    def emit(self, r): self.msgs.append(r.getMessage())
CAP = _Cap(); logging.getLogger().addHandler(CAP); logging.getLogger().setLevel(logging.WARNING)
def logged(s): return any(s in m for m in CAP.msgs)

class _KS:
    def gate(self, lane): return True
class _Fills:
    def honest_entry(self, q, size, liq): return q
TMP = tempfile.mkdtemp(prefix="gbb_")
_N = [0]
class Eng:
    def __init__(self, lane):
        _N[0] += 1
        self.LANE = lane; self.LEDGER_PATH = os.path.join(TMP, "%s-%d.json" % (lane, _N[0])); self._ks = _KS(); self._fills = _Fills(); self._lk = threading.Lock()
        self.d = {"positions": {"M1": {"mint": "M1", "symbol": "T", "chain": "solana", "tokens": 1000.0, "entry_price": 1.0, "peak_price": 2.0,
                  "entry_ts": time.time(), "realized_usd": 5000.0, "legs": {"a": {"done": True}, "b": {"done": False}}}}}
        self.writes = 0; self.on_write = None
    def _ledger_lock(self): return self._lk
    def _read(self): return copy.deepcopy(self.d)
    def _write(self, d):
        if self.on_write: self.on_write()
        self.d = copy.deepcopy(d); self.writes += 1
    def open_positions(self): return list(self.d["positions"].values())
    def quote_price(self, chain, mint, spot_index=None): return 1.2

def fault_file(e): return gc._fault_path(e)
for modname in ("tranche", "tranche_protect"):
    m = importlib.import_module("daemon.launch_scanner." + modname)
    real_guard, real_barrier = eg.mandatory_entry_refusal, eg.commit_barrier
    def fresh():
        e = Eng(m.LANE); m._LAST_BUYBACK_CHECK = 0.0; CAP.msgs.clear(); return e
    calls = []
    def stub(reason):
        def g(lane): calls.append(lane); return reason
        return g
    try:
        e = fresh(); calls.clear(); eg.mandatory_entry_refusal = stub(None); b = copy.deepcopy(e.d); out = m.maybe_buyback(e)
        check("%s F1 flag on, guard clear: one buyback, ledger changed, guard called once with the lane" % modname, len(out) == 1 and e.d != b and calls == [m.LANE])
        e = fresh(); calls.clear(); eg.mandatory_entry_refusal = stub("FIXTURE-REASON-F2"); b = copy.deepcopy(e.d); out = m.maybe_buyback(e)
        check("%s F2 guard refuses: none, ledger unchanged, no write, reason logged, guard called once with the lane" % modname, out == [] and e.d == b and e.writes == 0 and logged("FIXTURE-REASON-F2") and calls == [m.LANE])
        def boom(lane): raise RuntimeError("FIXTURE-GUARD-BROKEN")
        e = fresh(); eg.mandatory_entry_refusal = boom; b = copy.deepcopy(e.d); out = m.maybe_buyback(e)
        check("%s F3 guard raises: refused fail-closed, unchanged, reason logged" % modname, out == [] and e.d == b and e.writes == 0 and logged("FIXTURE-GUARD-BROKEN"))
        def filegrd(e):
            def g(lane): return "FIXTURE-FAULT-FILE-PRESENT" if os.path.exists(fault_file(e)) else None
            return g
        def race(e, barrier=True):
            started = threading.Event(); eg.mandatory_entry_refusal = filegrd(e)
            def writer():
                with gc._fault_lock(e):
                    started.set(); time.sleep(0.4)
                    open(fault_file(e), "w").write("{}")
            t = threading.Thread(target=writer, daemon=True); t.start(); assert started.wait(5)
            b = copy.deepcopy(e.d); out = m.maybe_buyback(e); t.join(5); return out, b
        e = fresh(); out, b = race(e)
        check("%s F4 fault writer holds the fault lock then writes the fault: buyback waits, reads it, refuses (reason logged), ledger unchanged" % modname, out == [] and e.d == b and e.writes == 0 and logged("FIXTURE-FAULT-FILE-PRESENT"))
        e = fresh(); seen = {}; eg.mandatory_entry_refusal = stub(None); wdone = threading.Event()
        def late_writer():
            with gc._fault_lock(e):
                seen["wrote_after_ledger_write"] = e.writes == 1; wdone.set()
        def kick():
            threading.Thread(target=late_writer, daemon=True).start(); time.sleep(0.4); seen["writer_blocked_during_write"] = not wdone.is_set()
        e.on_write = kick; out = m.maybe_buyback(e); wdone.wait(5)
        check("%s F5 a fault writer started during the ledger write is blocked until the write returned (serialised), then records" % modname, len(out) == 1 and seen.get("writer_blocked_during_write") is True and seen.get("wrote_after_ledger_write") is True)
        e = fresh()
        def reent(lane):
            with gc._fault_lock(e): return None
        eg.mandatory_entry_refusal = reent; res = {}
        def go(): res["out"] = m.maybe_buyback(e)
        t = threading.Thread(target=go, daemon=True); t.start(); t.join(8)
        check("%s F6 a guard that re-enters the fault lock does not deadlock and the buyback completes" % modname, not t.is_alive() and len(res.get("out", [])) == 1)
        os.environ["AGT_GRAD_CYCLE_ENABLED"] = "0"
        try:
            e = fresh(); eg.mandatory_entry_refusal = stub(None); out = m.maybe_buyback(e)
            check("%s F7 flag off: buyback as before, no fault-lock file created" % modname, len(out) == 1 and not os.path.exists(gc._fault_lock_path(e)))
        finally:
            os.environ["AGT_GRAD_CYCLE_ENABLED"] = "1"
        e = fresh(); eg.mandatory_entry_refusal = stub(None)
        started = threading.Event()
        def holder():
            with gc._fault_lock(e): started.set(); time.sleep(1.5)                  # held PAST the buyback's own bounded wait (entry_guard.ENTRY_FAULT_WAIT_S, 0.5 s); the module-global 5 s retry window is NOT what bounds the buyback, so it is not patched
        t = threading.Thread(target=holder, daemon=True); t.start(); assert started.wait(5)
        b = copy.deepcopy(e.d); t0 = time.monotonic(); out = m.maybe_buyback(e); el = time.monotonic() - t0; t.join(5)
        with gc._fault_lock(e): freed = True
        check("%s F8 fault lock held past the buyback's bounded wait (%.2fs, ENTRY_FAULT_WAIT_S=%s): nothing written, failure logged, lock free afterwards" % (modname, el, eg.ENTRY_FAULT_WAIT_S), out == [] and e.d == b and e.writes == 0 and logged("buyback") and freed and el < 1.2)
        e = fresh()
        eg.commit_barrier = lambda engine, wait_s=None: __import__("contextlib").nullcontext()   # same 2-argument call shape production uses (engine, ENTRY_FAULT_WAIT_S)
        try: out, b = race(e)
        finally: eg.commit_barrier = real_barrier
        check("%s F9 SENSITIVITY: barrier removed -> the F4 scenario executes the buyback (the barrier is what refuses)" % modname, len(out) == 1 and e.d != b)
    finally:
        eg.mandatory_entry_refusal = real_guard; eg.commit_barrier = real_barrier
check("F0c network tripwire never fired", NET == [])
print("RESULT: %d failures" % len(FAILS))
if FAILS: print("FAILS:", FAILS)
os._exit(1 if FAILS else 0)
