#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED. NEVER RUN WITHOUT main's explicit clearance for this exact blob. Isolated runtime test of the price_fn=None (LANE DEFAULT price route) path on the
REAL grad.py engine: main_cycle with price_fn None must still price and run the exits through the engine's own default route (Jupiter v3 batch stub -> jup_prices), and the
v2 behaviour (run_cycle with price_fn None and NO price_route) is reproduced as a negative control on the real run_cycle. Fake Jupiter/feed/fill stubs, temp HOME, no trader
script, no thread, no kill; both children install a socket tripwire AFTER the engine boots and report its count. Reuses the BOOT/PRE2 child programs and the bounded child runner
of tests/real_ledger_hook_harness-SOURCE-ONLY.py (loaded as a module; import runs no scenario). 2 children, rc 0 each, JSON on stdout, a reached-site sentinel field in each.
  X4 flag ON, price_fn None, position at 3x (the stub Jupiter price): main_cycle. Expect the legs-a/b exit sales journaled BEFORE the scan (fills at scan start > before), legacy
     never ran, record done, steps exits ok with n_closed>=1, and no "no_price_route" anywhere in the record.
  X5 NEGATIVE CONTROL (the v2 defect, on the real run_cycle): GC.run_cycle(price_fn None, price_route None): Expect NO exit sales (fills unchanged), "no_price_route" in the record, and
     the scan is not run. X4 vs X5 differ only by the price_route argument that main_cycle now passes.
CLAIM LIMITS: stubbed Jupiter v3 batch only; the DexScreener/GeckoTerminal/reserves fallbacks of the default route are NOT exercised (no network); one real engine in one process; not
the trader scripts, threads, push or ExitPoller callbacks; no kill. A pass shows the default route is reached through main_cycle for a Jupiter-priced position, not full pricing parity."""
import importlib.util, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("hook_base", os.path.join(HERE, "real_ledger_hook_harness-SOURCE-ONLY.py"))
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)
FAILS = H.FAILS; check = H.check
COMMON = r"""
import socket as _sk
_NET = []
def _trip(*a, **k):
    _NET.append(1); raise OSError("network tripwire")
_sk.socket.connect = _trip; _sk.create_connection = _trip; _sk.getaddrinfo = _trip
EMPTY = {"stream": 0, "opened": 0, "select_rejected": 0, "select_reasons": {}}
r1 = open_first(); T.register(GR)
order = []
f0 = len(raw_fills())
def scan_fn():
    order.append(["scan", len(raw_fills())]); return dict(EMPTY)
def legacy():
    order.append(["legacy", len(raw_fills())]); return dict(EMPTY), 0
"""
CHILD_X4 = H.PRE2 + COMMON + r"""
FakeJup.mult = 3.0
try: res, n, rec = T.main_cycle(GR, None, scan_fn, legacy)
finally: FakeJup.mult = 1.0
print(json.dumps({"reached": True, "order": order, "f0": f0, "f_end": len(raw_fills()), "n": n, "status": (rec or {}).get("status"), "exits_step": ((rec or {}).get("steps") or {}).get("exits"), "rec_text": json.dumps(rec, default=str), "net": len(_NET)}))
"""
CHILD_X5 = H.PRE2 + COMMON + r"""
FakeJup.mult = 3.0
try: rec = GC.run_cycle(grad=GR, price_fn=None, scan_fn=scan_fn, price_route=None)
finally: FakeJup.mult = 1.0
print(json.dumps({"reached": True, "order": order, "f0": f0, "f_end": len(raw_fills()), "status": (rec or {}).get("status"), "rec_text": json.dumps(rec, default=str), "net": len(_NET)}))
"""
def main():
    h = H.newhome(); cp, j = H.run(CHILD_X4, h)
    check("X4 child ran (rc=%s, expected 0) and reached the site" % cp.returncode, cp.returncode == 0 and j is not None and j.get("reached") is True)
    if j:
        scans = [o for o in j["order"] if o[0] == "scan"]; leg = [o for o in j["order"] if o[0] == "legacy"]
        ex = j.get("exits_step") or {}
        check("X4 price_fn None, flag on: exits priced via the lane default route (fills %s -> %s), scan after them %s, legacy none %s, status %s, exits step %s, no no_price_route, no network (%s)" % (j["f0"], j["f_end"], scans, leg, j["status"], ex, j["net"]),
              j["f_end"] > j["f0"] and len(scans) == 1 and scans[0][1] > j["f0"] and leg == [] and str(j["status"]).startswith("done") and ex.get("ok") and int(ex.get("n_closed") or 0) >= 1 and "no_price_route" not in j["rec_text"] and j["net"] == 0)
    h = H.newhome(); cp, j2 = H.run(CHILD_X5, h)
    check("X5 child ran (rc=%s, expected 0) and reached the site" % cp.returncode, cp.returncode == 0 and j2 is not None and j2.get("reached") is True)
    if j2:
        check("X5 NEGATIVE CONTROL (v2 behaviour): price_route None -> no_price_route in the record, no exit sales (fills %s == %s), scan not run (%s), no network (%s)" % (j2["f_end"], j2["f0"], j2["order"], j2["net"]),
              "no_price_route" in j2["rec_text"] and j2["f_end"] == j2["f0"] and j2["order"] == [] and j2["net"] == 0)
    print("elapsed %.1fs of %.0fs budget; FAILS: %d; stop=%s" % (H.time.time() - H._T0, H.BUDGET_S, len(FAILS), H._STOP["why"])); return 1 if (FAILS or H._STOP["why"]) else 0
if __name__ == "__main__":
    sys.exit(main())
