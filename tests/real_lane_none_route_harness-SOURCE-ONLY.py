#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED. NEVER RUN WITHOUT main's explicit clearance for this exact blob. Isolated runtime test of the price_fn=None (LANE DEFAULT price route) path on the
REAL grad.py engine: main_cycle with price_fn None must still price and run the exits through the engine's own default route (Jupiter v3 batch stub -> jup_prices), and the
v2 behaviour (run_cycle with price_fn None and NO price_route) is reproduced as a negative control on the real run_cycle. Fake Jupiter/feed/fill stubs PLUS an explicit stub of the REAL exit-pricing seam (grad._jup.get_prices_observed returning {mint: (3x entry price, now)}, every call recorded), temp HOME, no trader
script, no thread, no kill; both children install a socket tripwire AFTER the engine boots and report its count. Reuses the BOOT/PRE2 child programs and the bounded child runner
of tests/real_ledger_hook_harness-SOURCE-ONLY.py (loaded as a module; import runs no scenario). 3 children (X4 with phase 1 = partial ladder and phase 2 = runner close, X5, X6 control at 1.0x), rc 0 each, JSON on stdout, a reached-site sentinel field in each.
  X4 flag ON, price_fn None, position at 3x (the stub Jupiter price): main_cycle. Expect the legs-a/b exit sales journaled BEFORE the scan (fills at scan start > before), legacy
     never ran, record done, steps exits ok with n_closed == 0 (full closes only; leg C stays open), fills f0+2, legs a and b done, c not done, 25% of the tokens left, 2 account credits, and no "no_price_route" anywhere in the record.
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
# the engine's REAL pricing seam: grad._jup is jup_price; _poll_exits calls _jup.get_prices_observed(mints) -> {mint: (price, observed_ts)} (validated by _validate_jup_observed). FakeJup (the filler's
# quote stub) does NOT price exits, so stub this explicitly: price = 3x the position's entry price, observation ts = now, and RECORD every call (mints requested).
import daemon.launch_scanner.jup_price as _JP
JUPCALLS = []
def _gpo(mints, now=None):
    JUPCALLS.append(sorted(mints or []))
    out = {}
    for m in (mints or []):
        pos = GR.open_positions().get(m) or {}
        e = float(pos.get("entry_price") or 0)
        if e > 0: out[m] = (e * JUPMULT["m"], time.time())
    return out
JUPMULT = {"m": 3.0}
_JP.get_prices_observed = _gpo; GR._jup = _JP
EMPTY = {"stream": 0, "opened": 0, "select_rejected": 0, "select_reasons": {}}
r1 = open_first(); T.register(GR)
order = []
f0 = len(raw_fills())
P0 = dict(list(GR.open_positions().values())[0]); C0 = len(raw_credits()[0])
def acct_qty(mint):
    snap = PA.snapshot() or {"lanes": {}}
    return sum(float(p["qty"]) for info in snap["lanes"].values() for p in info["positions"] if p["mint"] == mint)
MINT0 = P0["mint"]
def posinfo():
    ps = list(GR.open_positions().values()); q = ps[0] if ps else None
    return None if q is None else {"tokens": float(q.get("tokens") or 0), "a_done": bool(((q.get("legs") or {}).get("a") or {}).get("done")), "b_done": bool(((q.get("legs") or {}).get("b") or {}).get("done")), "c_done": bool(((q.get("legs") or {}).get("c") or {}).get("done"))}
def scan_fn():
    order.append(["scan", len(raw_fills())]); return dict(EMPTY)
def legacy():
    order.append(["legacy", len(raw_fills())]); return dict(EMPTY), 0
"""
CHILD_X4 = H.PRE2 + COMMON + r"""
FakeJup.mult = 3.0
try: res, n, rec = T.main_cycle(GR, None, scan_fn, legacy)
finally: FakeJup.mult = 1.0
PH1 = ({"reached": True, "order": order, "f0": f0, "f_end": len(raw_fills()), "n": n, "status": (rec or {}).get("status"), "exits_step": ((rec or {}).get("steps") or {}).get("exits"), "rec_text": json.dumps(rec, default=str), "net": len(_NET), "jupcalls": JUPCALLS, "open_mints": sorted(GR.open_positions()), "pos0": {"tokens": float(P0.get("tokens") or 0)}, "pos_end": posinfo(), "credits0": C0, "credits_end": len(raw_credits()[0]), "acct_qty_end": acct_qty(MINT0), "ledger_closes_end": len(raw_ledger().get("closes") or [])})
# PHASE 2 (full close of the 25% runner): the leg-C peak was set to 3.0x entry in phase 1 (grad.py: `if px > lc["peak"]: lc["peak"] = px`), the trailing stop is peak*(1-LEG_C_TRAIL) = 2.1x entry, so a 2.0x price triggers "leg_c_trail_30pct"
JUPMULT["m"] = 2.0
order[:] = []; calls_before = len(JUPCALLS); f_p1 = len(raw_fills()); c_p1 = len(raw_credits()[0])
FakeJup.mult = 2.0
try: res2, n2, rec2 = T.main_cycle(GR, None, scan_fn, legacy)
finally: FakeJup.mult = 1.0
cl = raw_ledger().get("closes") or []
PH2 = {"order": order, "status": (rec2 or {}).get("status"), "exits_step": ((rec2 or {}).get("steps") or {}).get("exits"), "rec_text": json.dumps(rec2, default=str), "f_p1": f_p1, "f_end": len(raw_fills()), "c_p1": c_p1, "credits_end": len(raw_credits()[0]), "open_mints": sorted(GR.open_positions()), "acct_qty_end": acct_qty(MINT0), "n_closes": len(cl), "last_close": ({"mint": cl[-1].get("mint"), "closed_reason": cl[-1].get("closed_reason"), "legs_done": {k: bool(v.get("done")) for k, v in (cl[-1].get("legs") or {}).items()}} if cl else None), "jupcalls_new": JUPCALLS[calls_before:], "net": len(_NET)}
print(json.dumps(dict(PH1, ph2=PH2)))
"""
CHILD_X6 = H.PRE2 + COMMON + r"""
JUPMULT["m"] = 1.0                                       # CONTROL: the same main_cycle path, price at ENTRY (1.0x): no ladder leg is due
FakeJup.mult = 1.0
res, n, rec = T.main_cycle(GR, None, scan_fn, legacy)
print(json.dumps({"reached": True, "order": order, "f0": f0, "f_end": len(raw_fills()), "status": (rec or {}).get("status"), "exits_step": ((rec or {}).get("steps") or {}).get("exits"), "rec_text": json.dumps(rec, default=str), "net": len(_NET), "jupcalls": JUPCALLS, "pos_end": posinfo(), "pos0": {"tokens": float(P0.get("tokens") or 0)}, "credits0": C0, "credits_end": len(raw_credits()[0])}))
"""
CHILD_X5 = H.PRE2 + COMMON + r"""
FakeJup.mult = 3.0
try: rec = GC.run_cycle(grad=GR, price_fn=None, scan_fn=scan_fn, price_route=None)
finally: FakeJup.mult = 1.0
print(json.dumps({"reached": True, "order": order, "f0": f0, "f_end": len(raw_fills()), "status": (rec or {}).get("status"), "rec_text": json.dumps(rec, default=str), "net": len(_NET), "jupcalls": JUPCALLS}))
"""
def main():
    h = H.newhome(); cp, j = H.run(CHILD_X4, h)
    check("X4 child ran (rc=%s, expected 0) and reached the site" % cp.returncode, cp.returncode == 0 and j is not None and j.get("reached") is True)
    if j:
        j["scans_ok"] = True; scans = [o for o in j["order"] if o[0] == "scan"]; leg = [o for o in j["order"] if o[0] == "legacy"]
        ex = j.get("exits_step") or {}
        check("X4 price_fn None, flag on: exits priced via the lane default route (fills %s -> %s), scan after them %s, legacy none %s, status %s, exits step %s (n_closed counts FULL closes only: at 3x the ladder sells leg A 50%% @2x and leg B 25%% @3x, two sell fills, and leaves leg C 25%% on its trailing stop, so the position stays open: expect fills f0+2, a and b done, c not done, position tokens %s (the lane keeps the ORIGINAL quantity; legs carry fractions, _close_leg sells tokens*frac) of %s, account credits %s -> %s), no no_price_route, no network (%s), the REAL jup pricing seam was called (%s)" % (j["f0"], j["f_end"], scans, leg, j["status"], ex, j["pos_end"] and j["pos_end"]["tokens"], j["pos0"]["tokens"], j["credits0"], j["credits_end"], j["net"], j["jupcalls"]),
              j["f_end"] > j["f0"] and len(scans) == 1 and scans[0][1] > j["f0"] and leg == [] and str(j["status"]).startswith("done") and ex.get("ok") and int(ex.get("n_closed") or 0) == 0 and j["f_end"] == j["f0"] + 2 and j["scans_ok"] and j["pos_end"] and j["pos_end"]["a_done"] and j["pos_end"]["b_done"] and not j["pos_end"]["c_done"] and j["pos_end"]["tokens"] == j["pos0"]["tokens"] and abs(j["acct_qty_end"] - 0.25 * j["pos0"]["tokens"]) <= 1e-6 * j["pos0"]["tokens"] and j["credits_end"] == j["credits0"] + 2 and "no_price_route" not in j["rec_text"] and j["net"] == 0 and len(j["jupcalls"]) >= 1 and any(c for c in j["jupcalls"]) and all(set(c) <= set(j["open_mints"]) for c in j["jupcalls"]))
    if j and j.get("ph2"):
        q = j["ph2"]; ex2 = q.get("exits_step") or {}; lc = q.get("last_close") or {}
        scans2 = [o for o in q["order"] if o[0] == "scan"]
        check("X4b PHASE 2 runner close (price 2.0x entry <= leg-C trail 0.70 * peak 3.0x = 2.1x): exits step %s (n_closed counts FULL closes: expect 1), fills %s -> %s (exactly one more sale: leg C), account credits %s -> %s (exactly one more: leg C; the final close credits NOTHING because every sold leg was credited per tranche), position gone (open %s), account qty %s (all units settled), one close record with closed_reason %s and legs done %s, scan after exits %s, status %s, real seam called again (%s), no no_price_route, no network (%s)" % (ex2, q["f_p1"], q["f_end"], q["c_p1"], q["credits_end"], q["open_mints"], q["acct_qty_end"], lc.get("closed_reason"), lc.get("legs_done"), scans2, q["status"], q["jupcalls_new"], q["net"]),
              ex2.get("ok") and int(ex2.get("n_closed") or 0) == 1 and q["f_end"] == q["f_p1"] + 1 and q["credits_end"] == q["c_p1"] + 1 and q["open_mints"] == [] and abs(q["acct_qty_end"]) <= 1e-9 and lc.get("closed_reason") == "ladder_complete" and lc.get("legs_done") == {"a": True, "b": True, "c": True} and q["n_closes"] == j["ledger_closes_end"] + 1 and len(scans2) == 1 and scans2[0][1] >= q["f_end"] and str(q["status"]).startswith("done") and len(q["jupcalls_new"]) >= 1 and "no_price_route" not in q["rec_text"] and q["net"] == 0)
    h = H.newhome(); cp, j2 = H.run(CHILD_X5, h)
    check("X5 child ran (rc=%s, expected 0) and reached the site" % cp.returncode, cp.returncode == 0 and j2 is not None and j2.get("reached") is True)
    if j2:
        check("X5 NEGATIVE CONTROL (v2 behaviour): price_route None -> no_price_route in the record, no exit sales (fills %s == %s), scan not run (%s), no network (%s), jup pricing never called (%s)" % (j2["f_end"], j2["f0"], j2["order"], j2["net"], j2["jupcalls"]),
              "no_price_route" in j2["rec_text"] and j2["f_end"] == j2["f0"] and j2["order"] == [] and j2["net"] == 0 and j2["jupcalls"] == [])
    h = H.newhome(); cp, j6 = H.run(CHILD_X6, h)
    check("X6 child ran (rc=%s, expected 0) and reached the site" % cp.returncode, cp.returncode == 0 and j6 is not None and j6.get("reached") is True)
    if j6:
        ex6 = j6.get("exits_step") or {}
        check("X6 CONTROL (causal: the sales in X4 are due to the 3x price, not to the route itself): price_fn None via main_cycle with the stub price at 1.0x entry: the real pricing seam is called (%s), exits step %s, NO sale (fills %s -> %s), no ladder leg done (%s), tokens unchanged (%s of %s), credits unchanged (%s -> %s), scan runs after exits (%s), no no_price_route, no network (%s)" % (j6["jupcalls"], ex6, j6["f0"], j6["f_end"], j6["pos_end"], j6["pos_end"] and j6["pos_end"]["tokens"], j6["pos0"]["tokens"], j6["credits0"], j6["credits_end"], j6["order"], j6["net"]),
              len(j6["jupcalls"]) >= 1 and ex6.get("ok") and int(ex6.get("n_closed") or 0) == 0 and j6["f_end"] == j6["f0"] and j6["pos_end"] and not j6["pos_end"]["a_done"] and not j6["pos_end"]["b_done"] and j6["pos_end"]["tokens"] == j6["pos0"]["tokens"] and j6["credits_end"] == j6["credits0"] and len(j6["order"]) == 1 and j6["order"][0][0] == "scan" and "no_price_route" not in j6["rec_text"] and j6["net"] == 0)
    print("elapsed %.1fs of %.0fs budget; FAILS: %d; stop=%s" % (H.time.time() - H._T0, H.BUDGET_S, len(FAILS), H._STOP["why"])); return 1 if (FAILS or H._STOP["why"]) else 0
if __name__ == "__main__":
    sys.exit(main())
