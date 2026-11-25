"""Fixture-only controlled test for the commit-time buyback gate (WRITTEN, NOT RUN until main clears an exact commit+blob via pins.sh).
Fake engine, in-memory ledger, no network, no executor, no activation. Covers BOTH tranche.py and tranche_protect.py.
B1 guard clear: buyback executes (control, flag-off behaviour preserved).
B2 guard refuses at commit: no buyback, ledger byte-identical.
B3 guard raises: refused fail-closed, ledger unchanged.
B4 RACE: a fault latches while another thread holds the ledger lock; the guard must be evaluated AFTER the lock is acquired, so the buyback is refused
   (red against any version that checks only before the lock)."""
import copy, importlib, os, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "daemon", "launch_scanner")); sys.path.insert(0, ROOT)
FAILS = []
def check(name, ok):
    print(("PASS: " if ok else "FAIL: ") + name)
    if not ok: FAILS.append(name)

class _KS:
    def gate(self, lane): return True
class _Fills:
    def honest_entry(self, q, size, liq): return q
class Eng:
    def __init__(self, lane):
        self.LANE = lane; self._ks = _KS(); self._fills = _Fills(); self._lk = threading.Lock()
        self.d = {"positions": {"M1": {"mint": "M1", "symbol": "T", "chain": "solana", "tokens": 1000.0, "entry_price": 1.0, "peak_price": 2.0,
                  "entry_ts": time.time(), "realized_usd": 5000.0, "legs": {"a": {"done": True}, "b": {"done": False}}}}}
        self.writes = 0
    def _ledger_lock(self): return self._lk
    def _read(self): return copy.deepcopy(self.d)
    def _write(self, d): self.d = copy.deepcopy(d); self.writes += 1
    def open_positions(self): return list(self.d["positions"].values())
    def quote_price(self, chain, mint, spot_index=None): return 1.2     # >= entry, <= peak*(1-retrace): due

for modname in ("tranche", "tranche_protect"):
    m = importlib.import_module(modname)
    import entry_guard as eg
    real = eg.mandatory_entry_refusal
    try:
        def run(stub, eng=None, pre=None):
            eng = eng or Eng(m.LANE); m._LAST_BUYBACK_CHECK = 0.0
            eg.mandatory_entry_refusal = stub
            if pre: pre(eng)
            before = copy.deepcopy(eng.d)
            out = m.maybe_buyback(eng)
            return eng, out, before
        e, out, b = run(lambda lane: None)
        check("%s B1 guard clear: one buyback, ledger changed" % modname, len(out) == 1 and e.d != b and e.d["positions"]["M1"].get("buyback_used") is True)
        e, out, b = run(lambda lane: "mandatory entry guard: fixture refusal")
        check("%s B2 guard refuses at commit: none done, ledger unchanged, no write" % modname, out == [] and e.d == b and e.writes == 0)
        def boom(lane): raise RuntimeError("guard broken")
        e, out, b = run(boom)
        check("%s B3 guard raises: refused fail-closed, ledger unchanged" % modname, out == [] and e.d == b and e.writes == 0)
        latched = {"v": False}
        eng = Eng(m.LANE); started = threading.Event()
        def setter():
            with eng._ledger_lock():
                started.set(); time.sleep(0.3); latched["v"] = True
        t = threading.Thread(target=setter, daemon=True); t.start(); assert started.wait(5)
        m._LAST_BUYBACK_CHECK = 0.0
        eg.mandatory_entry_refusal = lambda lane: ("fault latched" if latched["v"] else None)
        b = copy.deepcopy(eng.d); out = m.maybe_buyback(eng); t.join(5)
        check("%s B4 fault latched under the ledger lock before the buyback acquired it: refused, ledger unchanged" % modname, out == [] and eng.d == b and eng.writes == 0)
    finally:
        eg.mandatory_entry_refusal = real
print("RESULT: %d failures" % len(FAILS))
if FAILS: print("FAILS:", FAILS)
os._exit(1 if FAILS else 0)
