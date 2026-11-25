#!/usr/bin/env python3
"""SOURCE ONLY. NEVER RUN without main's explicit clearance for this exact blob (it spawns child processes that exit ABRUPTLY, which the current run
authorization does not permit). UNRUN, never executed.

Evidence class if it is ever run: an ABRUPT PROCESS EXIT (os._exit(1): no cleanup, no atexit, no finally) at a chosen point of the REAL grad sim-sale path,
followed by a FRESH process on the same durable files. It is NOT an external SIGKILL, NOT a power loss (no fsync/torn-page loss is modeled), NOT E2E, NOT the
real lane's feed, and NOT grad ownership proof for the shared account beyond what the grad lane's own receipts show. Kept separate from the fault-log harness.

Accounting kill windows (per sold leg n = 1 -> leg a, n = 2 -> leg b; each leg sells through _close_leg -> _demo_sell -> _sim_settle -> _credit_st -> lane commit):
  W1 after_fill_journaled        : _demo_sell returned (DemoFiller journaled the fill), nothing credited
  W2 before_account_credit       : settle done, the account credit not yet called
  W3 after_account_credit        : the account credit APPLIED (receipt journaled), the lane has not booked the leg
  W4 first_write_after_nth_credit : the FIRST lane _write after the nth credit returned True is cut off (whatever it would have persisted). Durably this may be the same
                                    state as W3 if no lane write happens between the credit and that write; it is NOT claimed to be a distinct window
  W0 control (no exit).
Oracle, in the FRESH process, in this order:
  RAW (read BEFORE any snapshot/heal; snapshot() and _load_locked heal on access): the lane ledger JSON, the DemoFiller fill file, the account journal entries
      (PA._journal_read). Facts per window are asserted: fills for the killed leg == 1; receipts for the killed leg == (0 for W1/W2, 1 for W3/W4);
      the lane's leg.done is False in the persisted ledger for the killed leg. For n=2 the earlier leg a is asserted too: raw fill 1, receipt 1 (credited in the account), lane_done False (UNBOOKED in the lane), per the reviewer's reconciliation; leg b is not asserted before its window.
  REPLAY: poll_exits again (same prices) TWICE, then the final close. After replay and again after the second replay:
    E1 each leg: exactly ONE fill (qty == leg tokens) and exactly ONE credit receipt (qty == leg tokens, proceeds - fee == fill gross - fee within the
       technical tolerance used by the code); no second sale (fill count unchanged by replays).
    E2 cash conservation derived from the JOURNAL RECEIPTS, not from the lane: cash_end - cash_at_entry == sum(receipt proceeds - fee), and equals the
       fixture's independent closed-form TOTAL (the fixture economics: Q, PX1, FEE copied from tests/test_grad_fixtures_remaining.py, same limits).
    E3 lane: the position is closed, sum(leg realized) == sum(receipt net), account lane position consumed (open basis ~ 0, no open positions).
    E4 idempotence: the second replay changes neither cash, nor receipt count, nor fill count.
KNOWN GAPS: partial-credit intent fixtures (B3) and a real DemoFiller sale of a position opened by TWAP are not here; the account's own torn-journal windows are
covered by tests/test_paper_account_crash.py, not this file; the clear path still heals through snapshot/_load_locked (the raw reads above deliberately precede it)."""
import json, os, subprocess, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: FAILS.append(name)

BOOT = r'''
import importlib, os, sys, time, json
home = sys.argv[1]
sys.path.insert(0, os.environ["AGT_REPO_ROOT"]); sys.path.insert(0, os.path.join(os.environ["AGT_REPO_ROOT"], "daemon", "launch_scanner"))
import tests._guard_fixture as G; G.block_network()            # BEFORE any repo import
os.environ["FOREX_AGENT_HOME"] = home
for k in list(os.environ):
    if k.startswith("PAPER_ACCOUNT"): os.environ.pop(k)
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; os.environ["PAPER_ACCOUNT_START"] = "10"
os.environ["PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT"] = "2.0"      # TEST-FIXTURE, same as the fixtures file
os.environ["AGT_EXECUTION_MODE"] = "jupiter_quote_sim"
import daemon.launch_scanner.paper_account as PA
G.install(PA); os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "60"   # TEST-FIXTURE, after install(), same order as fresh()
from daemon.launch_scanner import demo_fill as DF, grad as GR
SOLM = "So11111111111111111111111111111111111111112"
class FakeJup:
    mult = 1.0
    def get_quote(self, inp, out, amount, slippage_bps=100):
        if inp == SOLM: o = int(amount / 1e9 / 0.00001 * 1e6)
        else: o = int(amount / 1e6 * 0.00001 * 1e9 * FakeJup.mult)
        return {"outAmount": str(o), "priceImpactPct": 0.4, "routePlan": [{"swapInfo": {"label": "FakeAMM"}}]}
JUP = FakeJup()
os.makedirs(os.path.join(home, "j"), exist_ok=True); os.makedirs(os.path.join(home, "led"), exist_ok=True)
FILL_PATH = os.path.join(home, "j", "j.json")
F = DF.DemoFiller(JUP, sol_usd_fn=lambda: (100.0, time.time()), decimals_fn=lambda m: 6, journal_path=FILL_PATH, seed=1, hourly_vol=0.0)
class KS:
    def is_halted(self, lane): return False
    def gate(self, lane): return True
    def record_trade(self, lane, pnl, ts=None): pass
    def check_lane(self, lane): return {"lane": lane}
class Veto:
    def veto_check(self, mint, chain, symbol="", safety_report=None, run_bundle=False, lane="unknown"):
        return {"passed": True, "vetoes": [], "flags": [], "details": {}}
class GSafe:
    def check_token(self, chain, mint, timeout=10): return {"passed": True, "unknown": False, "score": 7, "flags": [], "fails": [], "source": "rugcheck"}
GR.LEDGER_PATH = os.path.join(home, "led", "grad.json")
if not os.path.exists(GR.LEDGER_PATH): GR.ensure_ledger()
GR._demo_mode = lambda: True; GR._demo_filler = lambda F=F: F
GR._ks = KS(); GR._veto = Veto(); GR.QUOTE_RETRY_GAP_S = 0.0; GR.MAX_OPEN = 50; GR._safety = GSafe()
def _gt(m):
    n = time.time(); return {"usd_liquidity": 8000.0, "usd_market_cap": 69000.0, "chg_m5": 3.0, "price_usd": 0.00007, "tape_source": "dexscreener", "fetched_at": n, "received_at": n}
GR._fresh_tape = _gt
GR.quote_price = G.stamped_quote(GR, lambda chain, mint, spot_index=None: 0.00007)
def gpf(mult): return lambda chain, mint, spot_index=None: float((GR.open_positions().get(mint) or {}).get("entry_price") or 0) * mult
def sell_ab():
    FakeJup.mult = 3.0
    try: GR.poll_exits(price_fn=gpf(3.0))
    finally: FakeJup.mult = 1.0
def finish():
    FakeJup.mult = 2.0
    try: GR.poll_exits(price_fn=gpf(2.0))
    finally: FakeJup.mult = 1.0
def raw_ledger():
    return json.load(open(GR.LEDGER_PATH))
def raw_fills():
    try: return json.load(open(FILL_PATH)).get("fills", {})
    except FileNotFoundError: return {}
def raw_credits():
    ents, status, _ = PA._journal_read()
    return [e for e in ents if e.get("event") == "credit"], status
'''
SETUP = BOOT + r'''
cand = {"mint": "GradMint1pump", "symbol": "TKN", "name": "TKN / SOL", "chain": "solana", "dex": "pumpswap", "created_ts": time.time() - 60.0,
        "pool_address": "POOL1", "reserve_usd": 6000.0, "fdv_usd": 69000.0, "price_usd": 0.00007, "source": "gecko-new-pool"}
# the entry quote must be near 0.001 like the fixtures (FakeJup prices 1e-5 SOL at $100): the stub above is only the quote_price fallback; entry uses the sim fill
r = GR.consider_candidate(cand, time.time())
snap0 = PA.snapshot()
pos = list(GR.open_positions().values())
print(json.dumps({"ok": bool(r[0]), "why": r[1], "n_pos": len(pos), "cash0": snap0["cash"], "mint": pos[0]["mint"] if pos else None,
                  "pa_pos_id": pos[0].get("pa_pos_id") if pos else None, "tokens": pos[0]["tokens"] if pos else None}))
'''
CHILD = BOOT + r'''
window, nth = sys.argv[2], int(sys.argv[3])
cnt = {"sell": 0, "credit": 0, "write_armed": False}
def die(): os._exit(1)     # ABRUPT process exit: no cleanup, no atexit, no finally
_ds, _cs, _tc, _wr = GR._demo_sell, GR._credit_st, GR._tranche_credit, GR._write
def ds(*a, **k):
    r = _ds(*a, **k)
    if r is not None:
        cnt["sell"] += 1
        if window == "W1" and cnt["sell"] == nth: die()
    return r
def cs(*a, **k):
    cnt["credit"] += 1
    if window == "W2" and cnt["credit"] == nth: die()
    ok = _cs(*a, **k)
    if ok and cnt["credit"] == nth:
        if window == "W3": die()
        if window == "W4": cnt["write_armed"] = True
    return ok
def wr(d):
    if cnt["write_armed"]: die()
    return _wr(d)
GR._demo_sell, GR._credit_st, GR._write = ds, cs, wr
sell_ab()
print("CHILD_COMPLETED_WITHOUT_EXIT")
'''
VERIFY = BOOT + r'''
cash0 = float(sys.argv[2]); mint = sys.argv[3]; pid = sys.argv[4]; window = sys.argv[5]; nth = int(sys.argv[6])
TOL = lambda x: max(1e-6, 1e-4 * abs(float(x)))
def legs_ops(): return {k: "%s:leg:%s:%s:%s" % (GR.LANE, k, mint, pid) for k in ("a", "b", "c")}
def facts(with_cash=False):
    led = raw_ledger(); pos = (led.get("positions") or {}).get(mint) or {}
    fills = raw_fills(); cr, st = raw_credits(); ops = legs_ops()
    out = {"journal_status": st, "legs": {}}
    for k, op in ops.items():
        fk = GR._fill_key(pos, k) if pos else None
        rcs = [e for e in cr if e.get("op_id") == op]
        out["legs"][k] = {"fills": 1 if (fk in fills) else 0, "receipts": len(rcs), "receipt_qty": [e.get("qty") for e in rcs],
                          "receipt_net": [float(e.get("proceeds") or 0) - float(e.get("fee") or 0) for e in rcs],
                          "fill_qty": (fills.get(fk) or {}).get("qty_tokens"), "fill_net": ((fills.get(fk) or {}).get("gross_usd") or 0) - ((fills.get(fk) or {}).get("fee_usd") or 0) if fk in fills else None,
                          "lane_done": bool(((pos.get("legs") or {}).get(k) or {}).get("done")), "lane_realized": ((pos.get("legs") or {}).get(k) or {}).get("realized_usd")}
    out["position_open"] = bool(pos); out["tokens"] = pos.get("tokens")
    if with_cash: out["cash"] = PA.snapshot()["cash"]    # snapshot heals: only called AFTER the raw pre-heal capture
    return out
rawpre = facts()                       # RAW, before ANY snapshot/heal
sell_ab(); mid1 = facts(True)
sell_ab(); mid2 = facts(True)
finish(); end = facts(True)
sell_ab(); finish(); post = facts(True)      # extra replays AFTER the position is closed
snap = PA.snapshot(); gl = (snap["lanes"].get("grad") or {"positions": [], "open_basis": 0.0})
print(json.dumps({"rawpre": rawpre, "after_replay1": mid1, "after_replay2": mid2, "end": end, "postclose": post, "cash_end": snap["cash"], "cash0": cash0,
                  "acct_open": len(gl["positions"]), "acct_basis": gl["open_basis"], "degraded": snap.get("degraded")}, default=str))
'''
import time, signal
BUDGET_S = 100.0          # WHOLE-harness budget, below the 120 s wrapper limit; each child gets min(60, remaining); early stop when exhausted
_T0 = time.time(); _STOP = {"why": None}
def _remaining(): return BUDGET_S - (time.time() - _T0)
def py(code, *args, timeout=60):
    """Bounded child: own process group, killed (whole group) and reaped on timeout or any exception; never left running. The children start no descendants."""
    rem = _remaining()
    if rem <= 1.0:
        _STOP["why"] = "budget_exhausted"; raise TimeoutError("harness budget exhausted")
    env = dict(os.environ, AGT_REPO_ROOT=REPO)
    p = subprocess.Popen([sys.executable, "-c", code] + [str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, process_group=0)
    try:
        out, err = p.communicate(timeout=min(timeout, rem))
    except BaseException:
        try: os.killpg(p.pid, signal.SIGKILL)
        except Exception: pass
        try: p.wait(timeout=5)
        except Exception: pass
        _STOP["why"] = "child_timeout_or_error"; raise
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)

# fixture closed-form economics (copied from tests/test_grad_fixtures_remaining.py; same Q/PX1/FEE; A,B sold at 3x, C at 2x)
Q = 998.5; FEE = 15000 / 1e9 * 100.0; PX1 = 1e-5 * 100.0
def leg(frac, m): return frac * Q * PX1 * m - FEE
TOTAL = leg(0.5, 3) + leg(0.25, 3) + leg(0.25, 2)

def evaluate(j, tag, window, nth):
    """PURE oracle over a captured verify result `j` (no I/O). Returns [(name, ok)]. Also used by --selfcheck negative controls on mutated captures."""
    R = []
    k = "ab"[nth - 1]; raw = j["rawpre"]["legs"][k]
    if window != "W0":
        want_receipts = 1 if window in ("W3", "W4") else 0
        R.append(("%s RAW pre-heal: killed leg has exactly 1 journaled fill (%s)" % (tag, raw["fills"]), raw["fills"] == 1))
        R.append(("%s RAW pre-heal: account receipts for the killed leg == %d (got %d)" % (tag, want_receipts, raw["receipts"]), raw["receipts"] == want_receipts))
        R.append(("%s RAW pre-heal: lane leg not done in the persisted ledger" % tag, raw["lane_done"] is False))
    for stage in ("after_replay1", "after_replay2", "end"):
        L = j[stage]["legs"]
        for lk in ("a", "b"):
            lg = L[lk]
            ok = (lg["fills"] == 1 and lg["receipts"] == 1 and len(lg["receipt_qty"]) == 1 and lg["fill_qty"] is not None
                  and abs(float(lg["receipt_qty"][0]) - float(lg["fill_qty"])) <= max(1e-9, 1e-6 * float(lg["fill_qty"]))
                  and abs(lg["receipt_net"][0] - lg["fill_net"]) <= TOL(lg["fill_net"]))
            R.append(("%s %s leg %s: exactly one fill and one receipt, qty equal to fill qty, net within tolerance" % (tag, stage, lk), ok))
    if nth == 2 and window != "W0":
        ra = j["rawpre"]["legs"]["a"]
        R.append(("%s RAW pre-heal n=2: leg a fill 1, receipt 1, lane_done False (credited, UNBOOKED; got %s/%s/%s)" % (tag, ra["fills"], ra["receipts"], ra["lane_done"]),
                  ra["fills"] == 1 and ra["receipts"] == 1 and ra["lane_done"] is False))
    R.append(("%s E4b cash invariant replay1 -> replay2 (%.6f -> %.6f)" % (tag, j["after_replay1"]["cash"], j["after_replay2"]["cash"]), abs(j["after_replay1"]["cash"] - j["after_replay2"]["cash"]) < 1e-9))
    pc, en = j["postclose"], j["end"]
    R.append(("%s E5 post-close replays: zero fill/receipt/cash delta" % tag,
              abs(pc["cash"] - en["cash"]) < 1e-9 and all(pc["legs"][x]["fills"] == en["legs"][x]["fills"] and pc["legs"][x]["receipts"] == en["legs"][x]["receipts"] for x in "abc") and pc["position_open"] is False))
    a1, a2 = j["after_replay1"]["legs"], j["after_replay2"]["legs"]
    R.append(("%s E4 second replay changes no fill/receipt counts" % tag, all(a1[x]["fills"] == a2[x]["fills"] and a1[x]["receipts"] == a2[x]["receipts"] for x in "ab")))
    E = j["end"]["legs"]; net_r = sum(sum(E[x]["receipt_net"]) for x in "abc")
    R.append(("%s E2 cash delta %.6f == journal receipt net %.6f == closed-form TOTAL %.6f" % (tag, j["cash_end"] - j["cash0"], net_r, TOTAL),
              abs((j["cash_end"] - j["cash0"]) - net_r) < 1e-5 and abs(net_r - TOTAL) < 1e-2))
    R.append(("%s E3 position closed (open=%s), account lane consumed (open %s, basis %s), not degraded (%s)" % (tag, j["end"]["position_open"], j["acct_open"], j["acct_basis"], j["degraded"]),
              not j["end"]["position_open"] and j["acct_open"] == 0 and abs(float(j["acct_basis"])) < 1e-9 and not j["degraded"]))
    return R
TOL = lambda x: max(1e-6, 1e-4 * abs(float(x)))

def scenario(window, nth):
    tag = "%s/leg%d" % (window, nth)
    home = tempfile.mkdtemp(prefix="gacct_")
    s = py(SETUP, home)
    try: sj = json.loads(s.stdout.strip().splitlines()[-1])
    except Exception: print(s.stdout, s.stderr); check("%s setup produced an open position" % tag, False); return
    check("%s setup opened exactly one grad position (%s)" % (tag, sj.get("why")), sj.get("ok") and sj.get("n_pos") == 1)
    c = py(CHILD, home, window, nth)
    if window == "W0": check("%s control child completed without exit" % tag, "CHILD_COMPLETED_WITHOUT_EXIT" in c.stdout)
    else: check("%s child ended by abrupt process exit rc=%s (not completed)" % (tag, c.returncode), c.returncode == 1 and "CHILD_COMPLETED_WITHOUT_EXIT" not in c.stdout)
    v = py(VERIFY, home, sj["cash0"], sj["mint"], sj["pa_pos_id"], window, nth)
    try: j = json.loads(v.stdout.strip().splitlines()[-1])
    except Exception: print(v.stdout, v.stderr); check("%s verify produced a result" % tag, False); return
    print("RECORDED (not asserted) other-leg raw state %s: %s" % (tag, json.dumps(j["rawpre"]["legs"])))
    for n, ok in evaluate(j, tag, window, nth): check(n, ok)

def _good(window="W3", nth=1):
    """Synthetic CAPTURE shaped exactly like a verify result and consistent with the oracle (no engine involved). Only the evaluator is exercised."""
    def lg(done, rc, fills=1):
        return {"fills": fills, "receipts": rc, "receipt_qty": [100.0] * rc, "receipt_net": [10.0] * rc, "fill_qty": 100.0, "fill_net": 10.0, "lane_done": done, "lane_realized": None}
    full = {"a": lg(True, 1), "b": lg(True, 1), "c": lg(True, 1)}
    for x in "abc": full[x]["receipt_net"] = [leg_n[x]]
    raw = {"a": lg(False, 1 if window in ("W3", "W4") else 0), "b": lg(False, 0), "c": lg(False, 0)}
    if nth == 2: raw["a"] = lg(False, 1); raw["b"] = lg(False, 1 if window in ("W3", "W4") else 0)
    st = lambda: {"legs": json.loads(json.dumps(full)), "position_open": False, "cash": 10.0 + TOTAL}
    return {"rawpre": {"legs": raw}, "after_replay1": st(), "after_replay2": st(), "end": st(), "postclose": st(), "cash0": 10.0, "cash_end": 10.0 + TOTAL, "acct_open": 0, "acct_basis": 0.0, "degraded": None}
leg_n = {"a": leg(0.5, 3), "b": leg(0.25, 3), "c": leg(0.25, 2)}
def _fix_fill_nets(j):
    for st in ("after_replay1", "after_replay2", "end"):
        for x in "abc": j[st]["legs"][x]["fill_net"] = j[st]["legs"][x]["receipt_net"][0] if j[st]["legs"][x]["receipt_net"] else 10.0
    return j

def selfcheck():
    """NO child process: the evaluator alone. A good capture must pass; each MUTATION of one captured fact must make at least one assertion FAIL (oracle-negative controls)."""
    import copy
    g = _fix_fill_nets(_good("W3", 1)); base = evaluate(g, "ctl", "W3", 1)
    check("CTL0 unmutated synthetic capture passes the evaluator", all(ok for _, ok in base))
    def mut(name, fn, window="W3", nth=1):
        j = _fix_fill_nets(_good(window, nth)); fn(j); r = evaluate(j, "ctl", window, nth)
        check("CTL %s: evaluator REJECTS the altered capture (%d failing assertions)" % (name, sum(1 for _, ok in r if not ok)), any(not ok for _, ok in r))
    mut("duplicate receipt after replay1 (double credit)", lambda j: j["after_replay1"]["legs"]["a"].update(receipts=2, receipt_qty=[100.0, 100.0], receipt_net=[10.0, 10.0]))
    mut("second fill for a leg (double sale)", lambda j: j["end"]["legs"]["b"].update(fills=2))
    mut("receipt qty differs from fill qty", lambda j: j["end"]["legs"]["a"].update(receipt_qty=[99.0]))
    mut("receipt net differs from fill net", lambda j: j["end"]["legs"]["b"].update(fill_net=j["end"]["legs"]["b"]["receipt_net"][0] + 0.5))
    mut("cash off by one cent after close", lambda j: j.update(cash_end=j["cash_end"] + 0.02))
    mut("cash off from receipts (lane/credit mismatch)", lambda j: j["end"]["legs"]["c"].update(receipt_net=[j["end"]["legs"]["c"]["receipt_net"][0] + 0.5], fill_net=j["end"]["legs"]["c"]["receipt_net"][0] + 0.5))
    mut("receipt missing for a leg (credit lost)", lambda j: j["end"]["legs"]["a"].update(receipts=0, receipt_qty=[], receipt_net=[]))
    mut("replay2 changes the receipt count", lambda j: j["after_replay2"]["legs"]["a"].update(receipts=2, receipt_qty=[100.0, 100.0], receipt_net=[10.0, 10.0]))
    mut("killed leg already done in the persisted ledger (raw)", lambda j: j["rawpre"]["legs"]["a"].update(lane_done=True))
    mut("killed leg has no journaled fill (raw)", lambda j: j["rawpre"]["legs"]["a"].update(fills=0))
    mut("W3 raw receipt count wrong (raw)", lambda j: j["rawpre"]["legs"]["a"].update(receipts=0))
    mut("position still open at end", lambda j: j["end"].update(position_open=True))
    mut("account lane not consumed", lambda j: j.update(acct_open=1))
    mut("account degraded", lambda j: j.update(degraded="x"))
    mut("n=2 leg a raw: lane already booked it", lambda j: j["rawpre"]["legs"]["a"].update(lane_done=True), window="W3", nth=2)
    mut("n=2 leg a raw: receipt missing", lambda j: j["rawpre"]["legs"]["a"].update(receipts=0), window="W3", nth=2)
    mut("cash moves between replay1 and replay2", lambda j: j["after_replay2"].update(cash=j["after_replay2"]["cash"] + 0.01))
    mut("post-close replay moves cash", lambda j: j["postclose"].update(cash=j["postclose"]["cash"] + 0.01))
    mut("post-close replay adds a receipt", lambda j: j["postclose"]["legs"]["c"].update(receipts=2))
    mut("post-close replay reopens the position", lambda j: j["postclose"].update(position_open=True))
    mut("W1 raw state claims a receipt exists", lambda j: j["rawpre"]["legs"]["a"].update(receipts=1), window="W1")
    print("FAILS: %d" % len(FAILS)); return 1 if FAILS else 0

def main():
    scenario("W0", 1)
    for w in ("W1", "W2", "W3", "W4"):
        for n in (1, 2):
            if FAILS or _STOP["why"]: print("EARLY STOP before %s/leg%d (fails=%d, stop=%s)" % (w, n, len(FAILS), _STOP["why"])); break
            scenario(w, n)
    print("elapsed %.1fs of %.0fs budget; FAILS: %d; stop=%s" % (time.time() - _T0, BUDGET_S, len(FAILS), _STOP["why"])); return 1 if (FAILS or _STOP["why"]) else 0
if __name__ == "__main__":
    sys.exit(selfcheck() if "--selfcheck" in sys.argv else main())
