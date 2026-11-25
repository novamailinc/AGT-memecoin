"""DemoFiller (REAL daemon.launch_scanner.demo_fill + REAL SimulationExecutor) impact-unit boundary, stamp/raw propagation and old-journal replay.
SOURCE ONLY, WRITTEN NOT RUN. Canned Jupiter quotes in the DOCUMENTED ratio form (fixtures, NOT real-provider acceptance; no network, no signing).
run: python3 tests/test_demo_fill_impact_units.py"""
import os, sys, json, time, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from daemon.launch_scanner import demo_fill as DF

SOLM = "So11111111111111111111111111111111111111112"
MINT = "TokenMint1111111111111111111111111111111111"
FAILS = 0
def check(n, ok, d=""):
    global FAILS
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else " | " + str(d)), flush=True); FAILS += (not ok)

class Jup:
    def __init__(self, impact): self.impact = impact
    def get_quote(self, inp, out, amount, slippage_bps=100):
        o = int(amount / 1e9 / 0.00001 * 1e6) if inp == SOLM else int(amount / 1e6 * 0.00001 * 1e9)
        return {"outAmount": str(o), "priceImpactPct": self.impact, "routePlan": [{"swapInfo": {"label": "FakeAMM"}}]}

def filler(impact, path):
    return DF.DemoFiller(Jup(impact), sol_usd_fn=lambda: (100.0, time.time()), decimals_fn=lambda m: 6, journal_path=path, seed=1, hourly_vol=0.0,
                         fx_age_cap_fn=lambda: 100000.0)

def skip_reason(f, op, cap=2.0):
    try:
        f.buy(op, MINT, 1.0, cap)
    except DF.DemoSkip as e:
        return e.reason if hasattr(e, "reason") else str(e), getattr(e, "detail", None)
    return None, None

def main():
    d = tempfile.mkdtemp(prefix="dfunits-")
    jp = os.path.join(d, "j.json")
    f = filler("0.02", jp); r = f.buy("op-eq", MINT, 1.0, 2.0)
    check("boundary: ratio 0.02 (2.0%) at cap 2.0 fills (strict >)", r.get("status") == "filled", r.get("status"))
    check("fill record: price_impact_pct is true percent (2.0)", abs(r["provenance"]["price_impact_pct"] - 2.0) < 1e-9, r["provenance"].get("price_impact_pct"))
    check("fill record: raw quote value kept verbatim", r["provenance"].get("price_impact_raw") == "0.02", r["provenance"].get("price_impact_raw"))
    check("fill record: unit stamp percent_v2", r["provenance"].get("price_impact_unit") == "percent_v2", r["provenance"].get("price_impact_unit"))
    j = json.load(open(jp))["fills"]["op-eq"]
    check("journal on disk carries the same pct/raw/unit", j["provenance"].get("price_impact_unit") == "percent_v2" and j["provenance"].get("price_impact_raw") == "0.02")
    s = f.sell("op-sell", MINT, r["qty_tokens"], 2.0)
    sp = s["provenance"]
    check("sell record (real sim sell block) carries unit stamp and raw", sp.get("price_impact_unit") == "percent_v2" and sp.get("price_impact_raw") == "0.02" and abs(sp["price_impact_pct"] - 2.0) < 1e-9, {k: sp.get(k) for k in ("price_impact_pct", "price_impact_raw", "price_impact_unit")})
    reason, det = skip_reason(filler("0.0201", os.path.join(d, "j2.json")), "op-over")
    check("boundary: ratio 0.0201 (2.01%) over cap 2.0 refused price_impact_over_cap", reason == "price_impact_over_cap", reason)
    f3 = filler("0.0201", os.path.join(d, "j3.json")); skip_reason(f3, "op-over3")
    check("refused buy journals NO fill", f3.get_fill("op-over3") is None)
    for imp, want in (("5.0", "price_impact_out_of_documented_range"), ("1.5", "price_impact_out_of_documented_range"), ("-0.01", "price_impact_negative_unsupported"), ("zz", "price_impact_unknown")):
        reason, det = skip_reason(filler(imp, os.path.join(d, "jx%s.json" % imp)), "op-x")
        check("quote %r refused as %s with raw value in detail" % (imp, want), reason == want and (det or {}).get("price_impact_raw") == imp[:64], (reason, det))
    # percent-form 0.4 is now read as 40%: refused at cap 2.0 (the unit regression this change closes), buy and sell
    reason, det = skip_reason(filler("0.4", os.path.join(d, "j40.json")), "op-40")
    check("quote ratio 0.4 (=40%) refused price_impact_over_cap at cap 2.0 (buy)", reason == "price_impact_over_cap", reason)
    fs = filler("0.0201", os.path.join(d, "jsell.json"))
    try:
        fs.sell("op-sell-over", MINT, 100.0, 2.0); sreason = None
    except DF.DemoSkip as e:
        sreason = e.reason
    check("sell: ratio 0.0201 over cap 2.0 refused price_impact_over_cap, no fill journaled", sreason == "price_impact_over_cap" and fs.get_fill("op-sell-over") is None, sreason)
    # old-record replay: the OLD record is produced by the REAL writer (DemoFiller._save) from a real fill with the new keys removed and the
    # pre-fix semantics (ratio under price_impact_pct), then replayed by a fresh DemoFiller on the same journal. We compare the actual returned fill.
    p4 = os.path.join(d, "old.json")
    fo = filler("0.02", p4)
    real = dict(fo.buy("op-seed", MINT, 1.0, 2.0))
    import copy
    old = copy.deepcopy(real)               # impact fields live in fill["provenance"] (demo_fill._prov), not at the top level
    for k in ("price_impact_unit", "price_impact_raw"):
        old["provenance"].pop(k, None)
    old["provenance"]["price_impact_pct"] = 0.02   # pre-fix stored the ratio under the _pct name
    fo._save("op-old", old)
    stored = dict(filler("0.02", p4).get_fill("op-old"))
    r4 = dict(filler("0.02", p4).buy("op-old", MINT, 1.0, 2.0))
    check("old record: replay returns the stored fill exactly (no executor call, nothing recomputed)", r4 == stored, {k: (r4.get(k), stored.get(k)) for k in set(r4) | set(stored) if r4.get(k) != stored.get(k)})
    check("old record: carries NO unit stamp and NO raw value after replay", "price_impact_unit" not in r4["provenance"] and "price_impact_raw" not in r4["provenance"])
    check("old record: stored pct still the old ratio value, never multiplied", r4["provenance"].get("price_impact_pct") == 0.02)
    check("a NEW fill on the same journal is stamped while the old one stays unstamped", filler("0.02", p4).buy("op-new", MINT, 1.0, 2.0)["provenance"].get("price_impact_unit") == "percent_v2"
          and dict(filler("0.02", p4).get_fill("op-old")) == stored)
    print("FAILS: %d" % FAILS); return 1 if FAILS else 0

if __name__ == "__main__":
    sys.exit(main())
