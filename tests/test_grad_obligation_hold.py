#!/usr/bin/env python3
"""Grad obligation-hold suite (source-only, NOT RUN). Real grad entry (consider_candidate), real _close_leg, real poll_exits, real close_intents journal and real paper_account
marker; the same patched veto/safety/quote/tape inputs as test_close_intents_grad. Fixtures (all labelled): DIVERGENCE = the debited position removed from the account lane;
STALE GENERATION = account ledger files wiped after a real entry; INJECTED MARKER = the real paper_account._record_unresolved called for a chosen pos_id;
CORRUPT MARKER = a garbage line appended to the marker file. Modelled-fill mode (the harness pops AGT_EXECUTION_MODE); quote-sim is a separate obligation.
Sections: H0 positive control (healthy stop closes once, no hold); H1 divergence-frozen tranche then stop polls (no close intent, position open, hold kind known_frozen,
no journal growth per held poll); H1M labelled MUTANT (gate disabled) compared with H1; H2 stale-generation tranche refusal (account marker, no journal intent) then stop polls;
H3 own-close exclusion (a refused whole-position close keeps its existing settlement_pending semantics and is NOT reported as a foreign hold); E1 marker of ANOTHER pos_id does
not hold; E2 marker of the SAME pos_id holds a new leg sale and a stop; E3 corrupt marker file: unreadable (account-wide) holds a new sale and finalize, never reported as frozen; E4 exact own-id exclusion; E5 helper exception fails closed in finalize; E6 TWAP slice new sale held.
Run: python3 tests/test_grad_obligation_hold.py
"""
import importlib
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests (mandatory-guard migration, disclosed)

TMPD = tempfile.mkdtemp(prefix="ci-grad-test-")
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(
    prefix="ci-grad-home-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
_SUITE_HOME = os.environ["FOREX_AGENT_HOME"]  # the suite-owned mkdtemp home (custody anchor for the mirror reset)

import daemon.launch_scanner.grad as G
import daemon.launch_scanner.paper_account as PA
_gfb.set_limits_env(); _gfb.install_price_feed(at_cost=True)  # mandatory-guard migration (disclosed): gates on + TEST-FIXTURE limits, mechanics-only marks
import daemon.launch_scanner.close_intents as CI

G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class FakeKS:
    def __init__(self):
        self.halted = set()
        self.recorded = []

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        self.halted.add(lane)
        return {"lane": lane, "action": "HALTED"}


FAKE_KS = FakeKS()
SAFETY_OK = {"passed": True, "unknown": False, "score": 7,
             "flags": [], "fails": [], "source": "rugcheck"}
TAPE_OK = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
           "chg_m5": 3.0, "price_usd": 0.00007,
           "tape_source": "dexscreener"}


def _patch_all(price=0.00007):
    p1 = patch.object(G, "_veto")
    p2 = patch.object(G, "_safety")
    p3 = patch.object(G, "_ks", FAKE_KS)
    p4 = patch.object(G, "quote_price", return_value=price)
    p5 = patch.object(G, "_fresh_tape", return_value=dict(TAPE_OK))
    p6 = patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)
    m_veto, m_safety = p1.start(), p2.start()
    for p in (p3, p4, p5, p6):
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = dict(SAFETY_OK)
    return (p1, p2, p3, p4, p5, p6), m_veto, m_safety


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.00007):
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
        import shutil as _shl; _shl.rmtree(PA.ACCOUNT_DIR, ignore_errors=True)  # DISCLOSED TEST EDIT: reset the account with the lane ledger (see test_regime)
        _mir = os.path.join(os.path.dirname(PA.ACCOUNT_DIR), "lane_registry.%s.mirror.jsonl" % os.path.basename(PA.ACCOUNT_DIR))  # DISCLOSED FIXTURE EDIT (v6): the PARENT-dir lane-registry mirror must be reset with the account dir, else the next entry is refused fail-closed ("lane registry lost, its mirror remains"); same derivation as paper_account.py
        _sh = os.path.realpath(_SUITE_HOME)
        if os.path.realpath(_mir).startswith(_sh + os.sep) and os.path.basename(_sh).startswith("ci-grad-home-"):  # custody: only a file INSIDE this suite's own mkdtemp home is ever removed
            if os.path.exists(_mir): os.remove(_mir)
        else:
            raise RuntimeError("mirror path outside the suite-owned home, refusing to delete: %r" % (_mir,))
        # DISCLOSED FIXTURE EDIT (v6): the grad intent JOURNAL is reset with the account/registry (same suite-owned-home custody; precedent: test_close_intents_grad line 347). A frozen intent left by an earlier section would otherwise block every later ENTRY lane-wide (seen in test_paper_account_grad's native log), and the registry reset above removes the "journal existed" evidence, so no intent-journal-lost blocker is created.
        _jp = CI.journal_path("grad")
        if os.path.realpath(_jp).startswith(_sh + os.sep) and os.path.basename(_jp) == "close_intents_grad.jsonl":
            if os.path.exists(_jp): os.remove(_jp)
        else:
            raise RuntimeError("journal path outside the suite-owned home, refusing to delete: %r" % (_jp,))
        # PREFLIGHT (v6b): after a consistent reset NO leftover evidence may remain (registry primary+mirror, grad journal, ledger .bak inside the removed account dir, account marker rows, registry blockers). A survivor is a labelled fixture ERROR here, not a later KeyError. Runs only after a reset, never during a deliberate frozen scenario.
        _left = [p for p in PA._registry_paths() if os.path.exists(p)] + ([_jp] if os.path.exists(_jp) else []) + ([PA.ACCOUNT_DIR] if os.path.exists(PA.ACCOUNT_DIR) else [])
        if _left or PA.unresolved_refusals() or PA._registry_blockers():
            raise RuntimeError("fixture reset left evidence behind: %r markers=%r blockers=%r" % (_left, PA.unresolved_refusals(), PA._registry_blockers()))
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    ps, m_veto, m_safety = _patch_all(price=price)
    G.ensure_ledger()
    return ps, m_veto, m_safety


def _cand(mint="AAA111pump", age_s=60.0, dex="pumpswap", **kw):
    c = {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
         "chain": "solana", "dex": dex,
         "created_ts": time.time() - age_s,
         "pool_address": "POOL1", "reserve_usd": 6_000.0,
         "fdv_usd": 69_000.0, "price_usd": 0.00007,
         "source": "gecko-new-pool"}
    c.update(kw)
    return c


def _close_open(mint, mult=2.0):
    with G._ledger_lock():
        d = G._read()
        pos = d["positions"][mint]
        exit_price = pos["entry_price"] * mult
        G._close_leg(pos, "a", exit_price, "test")
        G._close_leg(pos, "b", exit_price, "test")
        G._close_leg(pos, "c", exit_price, "test")
        G._finalize(d, mint, pos, "test")
        G._write(d)


def _events():
    path = CI.journal_path("grad")
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]



def _snap(mint):
    return G._read()["positions"].get(mint)
def _jevents():
    return _events()
_CUR = [None]
def _ints():
    return [e for e in _jevents() if e.get("ev") == "intent" and (_CUR[0] is None or e.get("mint") == _CUR[0])]   # per-mint: other scenarios' intents never count
def _stop_polls(mint, n=1, mult=0.3):
    ent = float(G._read()["positions"][mint]["entry_price"])
    for _ in range(n):
        G.poll_exits(price_fn=lambda chain, m, si=None: ent * mult)
def _enter(mint):
    _CUR[0] = mint
    ps, _mv, _ms = _fresh(); G._ENTRY_BLOCKED = False
    o, w, _f = G.consider_candidate(_cand(mint=mint))
    return ps, o
def _diverge(mint):
    with PA._lock():
        led, _h, _dg = PA._load_locked(); led["lanes"]["grad"]["open"].pop(mint, None); PA._save_locked(led)
def _wipe_generation():
    for n in ("ledger.json", "ledger.json.bak", "events.jsonl"):
        p = os.path.join(PA.ACCOUNT_DIR, n)
        if os.path.exists(p): os.remove(p)
def _leg(mint, key, mult):
    with G._ledger_lock():
        d = G._read(); pos = d["positions"][mint]
        r = G._close_leg(pos, key, pos["entry_price"] * mult, "hold_suite"); G._write(d)
    return r
def _closes(): return len(G._load()["closes"])
def _jbytes(): return open(CI.journal_path("grad"), "rb").read() if os.path.exists(CI.journal_path("grad")) else b""
def _hold(mint):
    p = _snap(mint); return (p or {}).get("obligation_hold") or {}

# ---- RUNNER (reviewer v3 fix): every section below is executed in its own try/except so a fixture or setup exception is a labelled
# "FAIL <section> ERROR" (a fixture failure, NOT a product result) and the remaining sections still run (the BASE run must reach them all).
# Top-level defs run first; each section's statements share the module globals. The section text below is also the source that is executed.
import ast as _ast, traceback as _tb
_SRC = open(__file__).read()
_BS = _SRC.index("\n# ---- H0 positive control\n") + 1; _BE = _SRC.index("\n# ---- END-OF-SECTIONS\n") + 1
_BODY = _SRC[_BS:_BE]; _OFF = _SRC[:_BS].count("\n")
_MARK = [(i + 1, l[7:].strip()[:40]) for i, l in enumerate(_BODY.split("\n")) if l.startswith("# ---- ")]
_TREE = _ast.parse(_BODY); _GROUPS = {}; _DEFS = []
for _n in _TREE.body:
    if isinstance(_n, (_ast.FunctionDef, _ast.ClassDef)):
        _DEFS.append(_n); continue
    _k = max([m for m in _MARK if m[0] <= _n.lineno], key=lambda m: m[0])
    _GROUPS.setdefault(_k, []).append(_n)
def _run(nodes, label):
    for _n in nodes:
        _n2 = _ast.Module(body=[_n], type_ignores=[]); _ast.increment_lineno(_n2, _OFF)
        exec(compile(_n2, __file__, "exec"), globals())
_run(_DEFS, "defs")
for _k in sorted(_GROUPS):
    try:
        _run(_GROUPS[_k], _k[1])
    except BaseException as _e:
        if isinstance(_e, KeyboardInterrupt): raise
        _l = "SECTION %s ERROR %s: %s (fixture/setup exception, not a product verdict)" % (_k[1], type(_e).__name__, str(_e)[:200])
        print("FAIL " + _l); FAILS.append(_l); _tb.print_exc(file=sys.stdout)
        try: _unpatch(ps)
        except BaseException: pass
print("TEST-EMITTED ROOTS (provenance for post-run recovery; not archived by the launcher): %s %s" % (TMPD, os.environ["FOREX_AGENT_HOME"]))
if FAILS:
    print("FAILS: %d" % len(FAILS))
    sys.exit(1)
print("FAILS: 0")
sys.exit(0)  # the runner above already executed every section once; the physical section text below is the runner's SOURCE and must NOT execute a second time
# ---- H0 positive control
M0 = "H0POS11pump"
ps, ok = _enter(M0); check("H0 fixture: real entry opened", ok)
c0 = _closes(); _stop_polls(M0, 1)
check("H0 control: a healthy stop poll closes the position ONCE and no hold is recorded (closes +%d, position open %r)" % (_closes() - c0, _snap(M0) is not None),
      _closes() - c0 == 1 and _snap(M0) is None)
_unpatch(ps)

# ---- H1 divergence-frozen tranche then stop polls
def scenario_h1(mutant):
    M = "H1MUT11pump" if mutant else "H1FRZ11pump"
    ps, ok = _enter(M); _diverge(M)
    pm = patch.object(G, "_obligation_hold", lambda *a, **k: None, create=True) if mutant else None
    if pm: pm.start()
    try:
        _leg(M, "a", 2.5)
        j1 = _jbytes(); i1 = len(_ints()); c = _closes(); cash1 = PA.snapshot()["cash"]; mk1 = len(PA.unresolved_refusals() or [])
        _stop_polls(M, 1); j2 = _jbytes(); i2 = len(_ints())
        _stop_polls(M, 2); j3 = _jbytes()
        out = {"intents_after_leg": i1, "intents_after_poll1": i2, "journal_grew_poll2_3": j3 != j2, "journal_grew_poll1": j2 != j1,
               "close_ops": [o.get("op_id") for e in _ints() for o in e.get("settle_ops") or [] if ":close:" in str(o.get("op_id"))],
               "account_cash_unchanged": PA.snapshot()["cash"] == cash1, "marker_rows_unchanged": len(PA.unresolved_refusals() or []) == mk1,
               "open": _snap(M) is not None, "new_closes": _closes() - c, "hold": dict(_hold(M)), "pending_exit": (_snap(M) or {}).get("pending_exit")}
    finally:
        if pm: pm.stop()
        _unpatch(ps)
    return out
h1 = scenario_h1(False)
print("  OBSERVED H1 (fixed):", {k: v for k, v in h1.items() if k != "pending_exit"}, (h1["pending_exit"] or {}).get("why"))
check("H1 no grad:close intent opens beside the frozen tranche intent (%r)" % (h1["close_ops"],), not h1["close_ops"] and h1["intents_after_leg"] == 1)
check("H1 position stays open, nothing booked (open %r, new closes %d)" % (h1["open"], h1["new_closes"]), h1["open"] and h1["new_closes"] == 0)
check("H1 hold kind known_frozen with the tranche op id (%r)" % (h1["hold"].get("kind"),),
      h1["hold"].get("kind") == "known_frozen" and any(":leg:a:" in o for k in h1["hold"].get("known", []) for o in k["ops"]))
check("H1 repeated held polls append NOTHING to the journal and open no intent", not h1["journal_grew_poll2_3"] and h1["intents_after_poll1"] == 1)
check("H1 held polls changed neither the account cash nor the marker rows (%r, %r)" % (h1["account_cash_unchanged"], h1["marker_rows_unchanged"]), h1["account_cash_unchanged"] and h1["marker_rows_unchanged"])
check("H1 the tranche diagnostic pointer is preserved (pending_exit why %r)" % ((h1["pending_exit"] or {}).get("why"),), (h1["pending_exit"] or {}).get("why") in ("tranche_credit_divergence", "tranche_credit_divergence_held"))
h1m = scenario_h1(True)
print("  OBSERVED H1M (labelled MUTANT: gate disabled):", {k: v for k, v in h1m.items() if k != "pending_exit"})
# NOTE: H1M disables the WHOLE gate (aggregate mutant): it shows the gate as a whole is causal for this fixture, not that each gate site is separately necessary.
check("H1M MUTANT differs from the fixed run in an observed field (close ops / new closes / open) so the gate is causal", (h1m["close_ops"], h1m["new_closes"], h1m["open"]) != (h1["close_ops"], h1["new_closes"], h1["open"]))

# ---- H2 stale-generation tranche refusal (account marker, NO journal intent)
M2 = "H2GEN11pump"
ps, ok = _enter(M2); _wipe_generation()
_leg(M2, "a", 2.5)
pe2 = (_snap(M2) or {}).get("pending_exit") or {}
mk = [r for r in (PA.unresolved_refusals() or []) if r.get("pos_id") == (_snap(M2) or {}).get("pa_pos_id")]
i_pre = len(_ints())
check("H2 setup: tranche generation refusal recorded the durable marker and journaled NO intent (why %r, markers %d, intents %d)" % (pe2.get("why"), len(mk), i_pre),
      pe2.get("why") == "tranche_credit_generation_refused" and len(mk) >= 1 and i_pre == 0)
c = _closes(); jb = _jbytes(); cash2 = PA.snapshot()["cash"]; mk2 = len(PA.unresolved_refusals() or []); _stop_polls(M2, 3)
h2 = _hold(M2)
check("H2 stop polls: position open, no booking, hold kind account_marker (kind %r, open %r, new closes %d)" % (h2.get("kind"), _snap(M2) is not None, _closes() - c),
      _snap(M2) is not None and _closes() == c and h2.get("kind") == "account_marker")
check("H2 held polls opened no intent, appended nothing to the journal, left the account cash and marker rows unchanged", len(_ints()) == i_pre and _jbytes() == jb and PA.snapshot()["cash"] == cash2 and len(PA.unresolved_refusals() or []) == mk2)
_unpatch(ps)

# ---- H3 own-close exclusion: a refused whole-position close keeps its EXISTING semantics
M3 = "H3OWN111pump"
ps, ok = _enter(M3); _wipe_generation(); c = _closes(); _stop_polls(M3, 2)
p3 = _snap(M3) or {}
_cp3 = "grad:close:%s:%s" % (M3, p3.get("pa_pos_id"))
_m3 = [r.get("op_id") for r in (PA.unresolved_refusals() or []) if r.get("pos_id") == p3.get("pa_pos_id")]
check("H3 isolation: every marker row of this position is the position's OWN close op (%r), so the absence of obligation_hold is the own-id exclusion at work, not an absent marker" % (_m3,),
      len(_m3) >= 1 and all(o in (_cp3, _cp3 + ":wd") for o in _m3))
check("H3 refused whole-position close: position open, settlement_pending set (%r), NOT reported as a foreign obligation_hold (%r), nothing booked"
      % ((p3.get("settlement_pending") or {}).get("why"), p3.get("obligation_hold")),
      bool(p3) and bool(p3.get("settlement_pending")) and not p3.get("obligation_hold") and _closes() == c)
_unpatch(ps)

# ---- E1 marker of ANOTHER position does not hold
M4 = "E1OTHER1pump"
ps, ok = _enter(M4)
PA._record_unresolved("grad:leg:a:OTHER:otherpos", "grad", "OTHER", "pos_other_not_this", "injected marker for ANOTHER position (labelled fixture)")
c = _closes(); _stop_polls(M4, 1)
check("E1 a marker of another pos_id does NOT hold this position: stop closes it (closes +%d)" % (_closes() - c), _closes() - c == 1 and _snap(M4) is None)
_unpatch(ps)

# ---- E2 marker of the SAME position holds a new sale and a stop
M5 = "E2SAME11pump"
ps, ok = _enter(M5); pid5 = _snap(M5)["pa_pos_id"]
PA._record_unresolved("grad:leg:a:%s:%s" % (M5, pid5), "grad", M5, pid5, "injected marker for THIS position (labelled fixture)")
r5 = _leg(M5, "a", 2.5); c = _closes(); _stop_polls(M5, 2)
check("E2 same-pos marker: leg sale held (returned %r, leg a done %r), stop held (open %r, new closes %d), kind %r"
      % (r5, _snap(M5)["legs"]["a"]["done"] if _snap(M5) else None, _snap(M5) is not None, _closes() - c, _hold(M5).get("kind")),
      r5 == 0.0 and _snap(M5) is not None and _snap(M5)["legs"]["a"]["done"] is False and _closes() == c and _hold(M5).get("kind") == "account_marker")
_unpatch(ps)

# ---- E3 corrupt marker file: unreadable, account-wide, never reported as frozen
M6 = "E3CORR11pump"
ps, ok = _enter(M6)
with open(PA._unresolved_path(), "a") as _f: _f.write("{not json\n")
r6 = _leg(M6, "a", 2.5); c = _closes(); _stop_polls(M6, 1); h6 = _hold(M6)
check("E3 corrupt marker: new sale held, finalize held, kind unreadable (account-wide wording), position open, nothing booked (kind %r, new closes %d)" % (h6.get("kind"), _closes() - c),
      r6 == 0.0 and _snap(M6) is not None and _closes() == c and h6.get("kind") == "unreadable" and any("account-wide" in u for u in h6.get("unknown", [])) and not h6.get("known"))
_unpatch(ps)

# ---- E4 exact own-id exclusion: a frozen intent whose op merely STARTS WITH the position's close op id is NOT the position's own close, so finalize holds
M7 = "E4EXACT1pump"
ps, ok = _enter(M7); p7 = _snap(M7); pid7 = p7["pa_pos_id"]
_cp7 = "grad:close:%s:%s" % (M7, pid7)
_iid7 = CI.begin("grad", M7, pid7, [{"kind": "credit", "op_id": _cp7 + ":extra", "mint": M7, "amount": 1.0, "qty": 1.0, "price_kind": "unknown", "pos_id": pid7,
                                       "expected_account_id": p7.get("pa_account_id"), "entry_op_id": p7.get("pa_entry_op")}],
                 {"realized_usd": 0.0, "close_ts": 1, "closed_reason": "labelled_fixture"}, stable_id=True, account_id=p7.get("pa_account_id"))
CI.freeze("grad", _iid7, "labelled fixture: frozen intent with an op id that only shares the close-op prefix")
_fz7 = CI.frozen_for_position("grad", M7, pid7)
check("E4 setup: the hand-built intent is frozen for this position with the prefix-only op id (%r)" % (_fz7,), bool(_fz7) and _fz7[0][0] == _iid7 and _fz7[0][2] == [_cp7 + ":extra"])
c = _closes(); _stop_polls(M7, 1)
check("E4 prefix-only op id is NOT own: finalize held (kind %r, open %r, new closes %d)" % (_hold(M7).get("kind"), _snap(M7) is not None, _closes() - c),
      _hold(M7).get("kind") == "known_frozen" and _snap(M7) is not None and _closes() == c)
_unpatch(ps)

# ---- E5 helper failure is fail-closed in finalize (labelled fault injection into the REAL journal reader)
M8 = "E5FAIL11pump"
ps, ok = _enter(M8)
def _boom(*a, **k): raise RuntimeError("injected reader failure (labelled)")
with patch.object(CI, "frozen_for_position", _boom):
    c = _closes(); _stop_polls(M8, 1)
check("E5 an exception inside the hold check holds finalize as unreadable, never as no-hold (kind %r, open %r, new closes %d)" % (_hold(M8).get("kind"), _snap(M8) is not None, _closes() - c),
      _hold(M8).get("kind") == "unreadable" and _snap(M8) is not None and _closes() == c)
_unpatch(ps)

# ---- E6 TWAP slice: a NEW slice sale is held by a same-position marker (G.TWAP_SLICES is set only by the mimic_edge engines; labelled)
M9 = "E6TWAP11pump"
ps, ok = _enter(M9); pid9 = _snap(M9)["pa_pos_id"]
PA._record_unresolved("grad:leg:a:%s:%s" % (M9, pid9), "grad", M9, pid9, "injected marker for THIS position (labelled fixture)")
_sv = G.TWAP_SLICES; G.TWAP_SLICES = 4
try:
    with G._ledger_lock():
        d = G._read(); pos = d["positions"][M9]; r9 = G._close_twap_slice(pos, pos["entry_price"] * 2.0); done9 = (pos.get("twap") or {}).get("done"); G._write(d)
finally:
    G.TWAP_SLICES = _sv
check("E6 TWAP slice sale held and the position was NOT mutated (returned %r, twap block present %r, hold %r)" % (r9, "twap" in (_snap(M9) or {}), _hold(M9).get("kind")),
      r9 == 0.0 and "twap" not in (_snap(M9) or {}) and _hold(M9).get("kind") == "account_marker")
_unpatch(ps)

def _persist_off_leg(mint, key, mult):
    """CRASH WINDOW fixture (same disclosed pattern as test_close_intents_grad PEND11): the account credit lands, the lane write never does."""
    _ow = G._write; G._write = lambda d: False
    try:
        with G._ledger_lock():
            d = G._read(); pos = d["positions"][mint]; G._close_leg(pos, key, pos["entry_price"] * mult, "crash_window")
    finally:
        G._write = _ow
def _hand_intent(mint, ops, why):
    p = _snap(mint); pid = p["pa_pos_id"]
    iid = CI.begin("grad", mint, pid, [{"kind": "credit", "op_id": o, "mint": mint, "amount": 1.0, "qty": 1.0, "price_kind": "unknown", "pos_id": pid,
                                          "expected_account_id": p.get("pa_account_id"), "entry_op_id": p.get("pa_entry_op")} for o in ops],
                   {"realized_usd": 0.0, "close_ts": 2, "closed_reason": "labelled_fixture"}, stable_id=True, account_id=p.get("pa_account_id"))
    CI.freeze("grad", iid, why); return iid

# ---- R1 receipt replay under a hold: (a) an APPLIED receipt replays (bookkeeping is never held); (b) a REFUSED replay never begins or freezes an intent
MA = "R1RCPT11pump"
ps, ok = _enter(MA); pidA = _snap(MA)["pa_pos_id"]
_persist_off_leg(MA, "a", 2.5)
check("R1 fixture: crash window left leg a NOT done on disk while the account holds its receipt (leg done %r)" % (_snap(MA)["legs"]["a"]["done"],),
      _snap(MA)["legs"]["a"]["done"] is False and G._tranche_receipt(_snap(MA), "a") is not None)
PA._record_unresolved("grad:leg:b:%s:%s" % (MA, pidA), "grad", MA, pidA, "injected marker for THIS position (labelled fixture)")
i_before = len(_ints())
ra = _leg(MA, "a", 2.5)
check("R1a under a same-position marker the APPLIED-receipt replay still completes leg a (returned %r, leg done %r)" % (ra, _snap(MA)["legs"]["a"]["done"]),
      _snap(MA)["legs"]["a"]["done"] is True and len(_ints()) == i_before)
_unpatch(ps)
MB = "R1RFSD11pump"
ps, ok = _enter(MB); pidB = _snap(MB)["pa_pos_id"]
_persist_off_leg(MB, "a", 2.5)
PA._record_unresolved("grad:leg:b:%s:%s" % (MB, pidB), "grad", MB, pidB, "injected marker for THIS position (labelled fixture)")
i_before = len(_ints()); jb = _jbytes()
# LABELLED FAULT INJECTION at the account API boundary: the replayed credit is refused as a divergence (position absent, op orphan, position ever existed)
with patch.object(PA, "credit", lambda *a, **k: (False, "position not held (labelled injected refusal)")), \
     patch.object(PA, "op_outcome", lambda *a, **k: "orphan"), patch.object(PA, "pos_ever_existed", lambda *a, **k: True):
    rb = _leg(MB, "a", 2.5)
pb = _snap(MB)
check("R1b refused replay under a hold: leg stays not done, NO intent begun or frozen, journal unchanged, visible why (%r), hold kind %r"
      % ((pb.get("pending_exit") or {}).get("why"), (pb.get("obligation_hold") or {}).get("kind")),
      pb["legs"]["a"]["done"] is False and len(_ints()) == i_before and _jbytes() == jb and (pb.get("pending_exit") or {}).get("why") == "tranche_credit_divergence_held")
_unpatch(ps)

# ---- R2 mixed single intent (own close op + a tranche op) holds finalize: only an intent whose ops are ALL own is skipped
MC = "R2MIXED1pump"
ps, ok = _enter(MC); pidC = _snap(MC)["pa_pos_id"]
_hand_intent(MC, ["grad:close:%s:%s" % (MC, pidC), "grad:leg:a:%s:%s" % (MC, pidC)], "labelled fixture: mixed own+tranche frozen intent")
c = _closes(); _stop_polls(MC, 2)
check("R2 mixed own+tranche frozen intent holds finalize (kind %r, open %r, new closes %d)" % (_hold(MC).get("kind"), _snap(MC) is not None, _closes() - c),
      _hold(MC).get("kind") == "known_frozen" and _snap(MC) is not None and _closes() == c)
_unpatch(ps)

# ---- R3 all legs receipted and done, an UNRELATED same-position marker: the whole-position settlement stays visibly unresolved, nothing booked
MD = "R3ALLDON1pump"
ps, ok = _enter(MD); pidD = _snap(MD)["pa_pos_id"]
_leg(MD, "a", 2.5); _leg(MD, "b", 3.5); _leg(MD, "c", 1.0)
ld = {k: _snap(MD)["legs"][k]["done"] for k in "abc"}
PA._record_unresolved("grad:leg:zz:%s:%s" % (MD, pidD), "grad", MD, pidD, "injected UNRELATED marker for THIS position (labelled fixture)")
bk = G._read()["bankroll"]; c = _closes(); _stop_polls(MD, 2, mult=1.0)
check("R3 all legs done (%r) + unrelated marker: finalize held, position open, nothing booked (kind %r, bankroll unchanged %r, new closes %d)"
      % (ld, _hold(MD).get("kind"), G._read()["bankroll"] == bk, _closes() - c),
      all(ld.values()) and _hold(MD).get("kind") == "account_marker" and _snap(MD) is not None and _closes() == c and G._read()["bankroll"] == bk)
_unpatch(ps)

# ---- R4 pointer loss is recoverable: lane pointers (pending_exit, obligation_hold) dropped from the lane ledger (labelled fault injection), the journal still holds the position
ME = "R4PTRLS1pump"
ps, ok = _enter(ME); _diverge(ME); _leg(ME, "a", 2.5)
with G._ledger_lock():
    d = G._read(); pe = d["positions"][ME]; pe.pop("pending_exit", None); pe.pop("obligation_hold", None); pe.pop("settlement_pending", None); G._write(d)
i_before = len(_ints()); c = _closes(); _stop_polls(ME, 2)
check("R4 after the lane pointers were dropped, a stop poll is still held from the JOURNAL (kind %r, intents %d -> %d, open %r, new closes %d)"
      % (_hold(ME).get("kind"), i_before, len(_ints()), _snap(ME) is not None, _closes() - c),
      _hold(ME).get("kind") == "known_frozen" and len(_ints()) == i_before and _snap(ME) is not None and _closes() == c)
_unpatch(ps)

# ---- E7 finalize fails closed DIRECTLY with all legs done (labelled injected reader failure in the REAL journal reader)
MF = "E7DONE11pump"
ps, ok = _enter(MF); _leg(MF, "a", 2.5); _leg(MF, "b", 3.5); _leg(MF, "c", 1.0)
check("E7 setup: all three legs done", all(_snap(MF)["legs"][k]["done"] for k in "abc"))
bk = G._read()["bankroll"]; c = _closes()
with patch.object(CI, "frozen_for_position", _boom):
    _stop_polls(MF, 2, mult=1.0)
check("E7 helper failure with all legs done: finalize held as unreadable, nothing booked (kind %r, bankroll unchanged %r, new closes %d)" % (_hold(MF).get("kind"), G._read()["bankroll"] == bk, _closes() - c),
      _hold(MF).get("kind") == "unreadable" and _snap(MF) is not None and G._read()["bankroll"] == bk and _closes() == c)
_unpatch(ps)

# ---- R5 a refused replay of ANOTHER leg never clobbers a frozen leg's pointer (direct call of the REAL _tranche_credit; labelled fault injection at the account API)
MG = "R5PTRKP1pump"
ps, ok = _enter(MG); _diverge(MG); _leg(MG, "a", 2.5)
pa0 = dict((_snap(MG) or {}).get("pending_exit") or {})
check("R5 setup: leg a pointer carries the frozen tranche intent id (%r)" % (pa0.get("intent_id"),), pa0.get("leg") == "a" and bool(pa0.get("intent_id")) and pa0.get("why") == "tranche_credit_divergence")
i_before = len(_ints())
with G._ledger_lock():
    d = G._read(); posg = d["positions"][MG]
    with patch.object(PA, "credit", lambda *a, **k: (False, "position not held (labelled injected refusal)")), \
         patch.object(PA, "op_outcome", lambda *a, **k: "orphan"), patch.object(PA, "pos_ever_existed", lambda *a, **k: True):
        rg = G._tranche_credit(posg, "b", 1.0, 1.0, 0.0, None)
    peg = dict(posg.get("pending_exit") or {}); G._write(d)
check("R5 refused replay of leg b: returned %r, leg a pointer UNCHANGED (leg %r, intent %r), no new intent (%d -> %d), diagnostic recorded in obligation_hold (%r)"
      % (rg, peg.get("leg"), peg.get("intent_id"), i_before, len(_ints()), (_snap(MG).get("obligation_hold") or {}).get("kind")),
      rg is False and peg.get("leg") == "a" and peg.get("intent_id") == pa0.get("intent_id") and len(_ints()) == i_before and bool((_snap(MG).get("obligation_hold") or {}).get("kind")))
_unpatch(ps)

# ---- END-OF-SECTIONS
if FAILS:
    print("FAILS: %d" % len(FAILS))
    sys.exit(1)
print("FAILS: 0")
