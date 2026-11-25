#!/usr/bin/env python3
"""Regression for close_intents.begin() id uniqueness (2026-11-24).

The intent id base has one-second resolution (lane:mint:pos_id:int(close_ts)). A freeze followed by a NEW begin for the same position in the
same second used to reuse the id, so the new intent read as the old terminal (frozen) one and was never recovered. This test FORCES the same
second with an explicit close_ts (no sleeping, no clock dependence) against the REAL close_intents journal in a temp home. Engineering test of
journal logic only: no account, no network, no engine.
Run: python3 -B tests/test_close_intents_unique_id.py
"""
import os, sys, tempfile, subprocess
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="ci-uniq-")
import daemon.launch_scanner.close_intents as ci

FAILS = 0
def check(name, cond):
    global FAILS
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS += 1

def _raises(fn):
    try:
        fn()
    except ci.IntentNotDurable:
        return True
    return False

OPS = [{"kind": "credit", "op_id": "op-1", "amount": 1.0}]
INFO = {"close_ts": 1795593430.2, "realized_usd": 1.0, "entry_notional_usd": 1.0, "closed_reason": "t"}
a = ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO))
ci.freeze("grad", a, "superseded by a changed settle plan")
# FROZEN NEVER REVIVES: a plain begin on an operation whose intent is frozen is refused unless the caller names the frozen intent (explicit supersede).
check("a plain begin of the SAME operation after its intent was frozen is REFUSED (frozen never revives; no new intent appended)",
      _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO, close_ts=1795593430.9)))
      and len([e for e in ci._read(ci.journal_path("grad"))[0] if e.get("ev") == "intent"]) == 1)
check("a begin that names a DIFFERENT intent as supersedes is also refused", _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO, close_ts=1795593430.9), supersedes="grad:nope:1")))
b = ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO, close_ts=1795593430.9), supersedes=a)   # explicit journaled supersede, same integer second
_sup = [e for e in ci._read(ci.journal_path("grad"))[0] if e.get("ev") == "intent" and e.get("intent_id") == b]
check("explicit supersede opens a NEW intent with a different id (%s vs %s), recording supersedes=%s" % (a, b, a), a != b and len(_sup) == 1 and _sup[0].get("supersedes") == a)
check("a PLAIN begin of the same operation while the superseding intent is pending is refused (lineage mismatch: its recorded parent is %s)" % a,
      _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO))))
check("a begin naming a FOREIGN supersedes while the successor is pending is refused", _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO), supersedes="grad:foreign:1")))
b2 = ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO), supersedes=a)
check("the SAME supersedes while the successor is pending RESUMES it (same id %s, still two intent events for this op)" % b2,
      b2 == b and len([e for e in ci._read(ci.journal_path("grad"))[0] if e.get("ev") == "intent" and e.get("pos_id") == "grad/MINT1#1" and e.get("settle_ops") == OPS]) == 2)
# DISTINCT operations (other pos / other op ids) in the same second get distinct ids; the frozen original is not in the way of them
c = ci.begin("grad", "MINT1", "grad/MINT1#2", OPS, dict(INFO))
_OTHER = [{"kind": "credit", "op_id": "op-other", "amount": 1.0}]
_OVER = [{"kind": "credit", "op_id": "op-1", "amount": 1.0}, {"kind": "write_down", "op_id": "op-1:wd", "reason": "r"}]   # shares the base op op-1 (a grad re-plan)
def _ngrad(): return len([e for e in ci._read(ci.journal_path("grad"))[0] if e.get("ev") == "intent"])
_n0 = _ngrad()
check("OVERLAPPING exclusivity: a different op set that shares the base op (a re-plan) on a position with a pending intent is refused, plain or with supersedes, nothing appended (%d -> %d)" % (_n0, _ngrad()),
      _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", _OVER, dict(INFO))) and _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", _OVER, dict(INFO), supersedes=a))
      and _ngrad() == _n0)
d1 = ci.begin("grad", "MINT1", "grad/MINT1#1", _OTHER, dict(INFO))
check("a NON-overlapping independent operation on the same position is allowed while another is pending (%s, %d intents)" % (d1, _ngrad()), bool(d1) and d1 != b and _ngrad() == _n0 + 1)
c2 = ci.begin("grad", "MINT1", "grad/MINT1#3", _OTHER, dict(INFO))
check("distinct POSITIONS in the same second are distinct from each other and from both earlier intents (%s, %s)" % (c, c2), len({a, b, c, c2}) == 4)
ci.freeze("grad", b, "second freeze")
check("overlapping FROZEN exclusivity: with b frozen (unsuperseded) an overlapping different op set is refused unless it names b",
      _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", _OVER, dict(INFO))) and _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", _OVER, dict(INFO), supersedes=a)))
check("after a SECOND freeze the same operation again needs an explicit supersede naming the LATEST frozen intent (plain refused, supersedes=a refused, supersedes=b ok)",
      _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO))) and _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO), supersedes=a))
      and bool(ci.begin("grad", "MINT1", "grad/MINT1#1", OPS, dict(INFO), supersedes=b)))
check("a SAME-operation begin with a DIFFERENT plan (same op ids, other amount) while an intent is pending raises, and appends nothing",
      _raises(lambda: ci.begin("grad", "MINT1", "grad/MINT1#2", [{"kind": "credit", "op_id": "op-1", "amount": 5.0}], dict(INFO)))
      and len([e for e in ci._read(ci.journal_path("grad"))[0] if e.get("ev") == "intent" and e.get("pos_id") == "grad/MINT1#2"]) == 1)
# concurrent explicit supersedes of ONE frozen operation: exactly one new intent, every caller gets its id
_f0 = ci.begin("lanefz", "MF", "pf", OPS, dict(INFO)); ci.freeze("lanefz", _f0, "r")
import threading as _t0
_sid = []; _sl = _t0.Lock()
def _sw():
    x = ci.begin("lanefz", "MF", "pf", OPS, dict(INFO), supersedes=_f0)
    with _sl: _sid.append(x)
_tt = [_t0.Thread(target=_sw) for _ in range(8)]; [t.start() for t in _tt]; [t.join() for t in _tt]
_fe = [e for e in ci._read(ci.journal_path("lanefz"))[0] if e.get("ev") == "intent"]
check("8 concurrent explicit supersedes of one frozen op: 2 intent events in all (%d), ONE new id returned to all 8 (%d distinct of %d), chain recorded" % (len(_fe), len(set(_sid)), len(_sid)),
      len(_fe) == 2 and len(set(_sid)) == 1 and len(_sid) == 8 and _fe[1].get("supersedes") == _f0 and _sid[0] == _fe[1]["intent_id"] and _f0 not in [x["intent_id"] for x in ci.pending("lanefz")])
_cs = ("import sys; sys.path.insert(0, %r); import daemon.launch_scanner.close_intents as ci; "
       "print(ci.begin('lanefp','MF','pf',[{'kind':'credit','op_id':'op-1','amount':1.0}],{'close_ts':1795593430.2,'realized_usd':1.0},supersedes=sys.argv[1]))" % REPO)
_g0 = ci.begin("lanefp", "MF", "pf", OPS, dict(INFO)); ci.freeze("lanefp", _g0, "r")
_pp2 = [subprocess.Popen([sys.executable, "-B", "-c", _cs, _g0], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ)) for _ in range(4)]
_oo = [p.communicate() for p in _pp2]
_fp = [e for e in ci._read(ci.journal_path("lanefp"))[0] if e.get("ev") == "intent"]
check("4 processes concurrently superseding one frozen op: rcs %r, 2 intent events (%d), ONE new id (%d)" % ([p.returncode for p in _pp2], len(_fp), len({o[0].strip() for o in _oo})),
      all(p.returncode == 0 for p in _pp2) and len(_fp) == 2 and len({o[0].strip() for o in _oo}) == 1 and {o[0].strip() for o in _oo} == {_fp[1]["intent_id"]})
pend = [x["intent_id"] for x in ci.pending("grad")]
# ---- supersedes validation (nothing is appended on an invalid request) ----
def _nint(lane): return len([e for e in ci._read(ci.journal_path(lane))[0] if e.get("ev") == "intent"])
check("supersedes of an UNKNOWN id with NO frozen fixture at all is refused and appends nothing",
      _raises(lambda: ci.begin("lanev1", "MV", "pv", OPS, dict(INFO), supersedes="lanev1:nope:1")) and _nint("lanev1") == 0)
_pv = ci.begin("lanev2", "MV", "pv", OPS, dict(INFO))
check("supersedes naming a PENDING intent is refused (only a frozen one can be superseded), nothing appended",
      _raises(lambda: ci.begin("lanev2", "MV", "pv2", OPS, dict(INFO), supersedes=_pv)) and _nint("lanev2") == 1)
check("INVALID supersedes while the same operation is pending is refused and appends nothing (pending resume validates first)",
      _raises(lambda: ci.begin("lanev2", "MV", "pv", OPS, dict(INFO), supersedes="lanev2:nope:1")) and _nint("lanev2") == 1)
ci.mark_account_applied("lanev2", _pv); ci.mark_lane_applied("lanev2", _pv)
check("supersedes naming an APPLIED intent is refused", _raises(lambda: ci.begin("lanev2", "MV", "pv", OPS, dict(INFO), supersedes=_pv)) and _nint("lanev2") == 1)
_fo = ci.begin("lanev3", "MV", "pv-other", OPS, dict(INFO)); ci.freeze("lanev3", _fo, "r")
check("supersedes naming a frozen intent of ANOTHER position/op is refused (foreign), nothing appended",
      _raises(lambda: ci.begin("lanev3", "MV", "pv-mine", OPS, dict(INFO), supersedes=_fo)) and _nint("lanev3") == 1)
_ra = ci.begin("lanev4", "MV", "pv", OPS, dict(INFO)); ci.freeze("lanev4", _ra, "r")
_rb = ci.begin("lanev4", "MV", "pv", OPS, dict(INFO), supersedes=_ra); ci.mark_account_applied("lanev4", _rb); ci.mark_lane_applied("lanev4", _rb)
check("RE-supersede of the ancestor after its successor APPLIED is refused (no second successor), nothing appended",
      _raises(lambda: ci.begin("lanev4", "MV", "pv", OPS, dict(INFO), supersedes=_ra)) and _nint("lanev4") == 2)
check("stable_id together with supersedes is an explicit refusal (never a silent drop), nothing appended",
      _raises(lambda: ci.begin("lanev5", "MV", "pv", OPS, dict(INFO), stable_id=True, supersedes="x")) and _nint("lanev5") == 0)
check("supersedes of a frozen intent by an OVERLAPPING changed op set (re-plan sharing the base op) is allowed and recorded",
      bool(ci.begin("lanev3", "MV", "pv-other", _OVER, dict(INFO), supersedes=_fo)) and _nint("lanev3") == 2)
_fo2 = ci.begin("lanev6", "MV", "pv", OPS, dict(INFO)); ci.freeze("lanev6", _fo2, "r")
check("supersedes of a frozen intent by a NON-overlapping op set is refused (a different operation is not its replacement), nothing appended",
      _raises(lambda: ci.begin("lanev6", "MV", "pv", _OTHER, dict(INFO), supersedes=_fo2)) and _nint("lanev6") == 1)

# ---- per-leg independence (N5): ops of one position with distinct op ids do not block each other; overlapping ones do ----
LA = [{"kind": "credit", "op_id": "pos#1:leg:a", "amount": 1.0}]; LB = [{"kind": "credit", "op_id": "pos#1:leg:b", "amount": 1.0}]
LD = [{"kind": "credit", "op_id": "pos#1:leg:d", "amount": 1.0}]
LAW = [{"kind": "credit", "op_id": "pos#1:leg:a", "amount": 1.0}, {"kind": "write_down", "op_id": "pos#1:leg:a:wd", "reason": "r"}]
n5a = ci.begin("lanen5", "MN", "pos#1", LA, dict(INFO)); n5b = ci.begin("lanen5", "MN", "pos#1", LB, dict(INFO))
check("leg A pending, leg B allowed on the same position (distinct intents %s / %s, 2 intents)" % (n5a, n5b), n5a != n5b and _nint("lanen5") == 2)
check("plain retry of leg B resumes it (same id); an overlapping re-plan of leg A while A is pending is refused", ci.begin("lanen5", "MN", "pos#1", LB, dict(INFO)) == n5b
      and _raises(lambda: ci.begin("lanen5", "MN", "pos#1", LAW, dict(INFO))) and _nint("lanen5") == 2)
ci.freeze("lanen5", n5a, "r")
n5d = ci.begin("lanen5", "MN", "pos#1", LD, dict(INFO))
check("leg A frozen: leg D (non-overlapping) is allowed (%s); the PLAIN retry of leg A is refused; leg B still resumes" % n5d,
      bool(n5d) and _raises(lambda: ci.begin("lanen5", "MN", "pos#1", LA, dict(INFO))) and ci.begin("lanen5", "MN", "pos#1", LB, dict(INFO)) == n5b and _nint("lanen5") == 3)
n5a2 = ci.begin("lanen5", "MN", "pos#1", LAW, dict(INFO), supersedes=n5a)
check("leg A's frozen intent is replaced by the overlapping re-plan only through supersedes (%s, parent recorded), then a SECOND overlapping re-plan while it is pending is refused" % n5a2,
      [e for e in ci._read(ci.journal_path("lanen5"))[0] if e.get("intent_id") == n5a2][0].get("supersedes") == n5a and _raises(lambda: ci.begin("lanen5", "MN", "pos#1", LA, dict(INFO))))

# ---- chain_tip: follow the journal's supersede chain (N3 support) ----
_t1 = ci.begin("lanect", "MT", "pt", OPS, dict(INFO)); ci.freeze("lanect", _t1, "r")
_t2 = ci.begin("lanect", "MT", "pt", OPS, dict(INFO), supersedes=_t1); ci.freeze("lanect", _t2, "r2")
_t3 = ci.begin("lanect", "MT", "pt", OPS, dict(INFO), supersedes=_t2)
check("chain_tip from the root follows frozen A -> frozen B -> open C: tip C, status open, event carries its parent (%s)" % (ci.chain_tip("lanect", _t1)[:2],),
      ci.chain_tip("lanect", _t1)[0] == _t3 and ci.chain_tip("lanect", _t1)[1] == "open" and ci.chain_tip("lanect", _t1)[2].get("supersedes") == _t2 and ci.chain_tip("lanect", _t3)[0] == _t3)
ci.mark_account_applied("lanect", _t3); ci.mark_lane_applied("lanect", _t3)
check("chain_tip reports the tip's status after it applies (applied); an absent id gives (id, None, None)", ci.chain_tip("lanect", _t2)[1] == "applied" and ci.chain_tip("lanect", "lanect:none:1") == ("lanect:none:1", None, None))
_ev_amb = {"ev": "intent", "intent_id": "lanect:amb:2", "lane": "lanect", "mint": "MT", "pos_id": "pt", "settle_ops": OPS, "close_info": {}, "supersedes": _t1, "ts": 1.0}
import json as _json
with open(ci.journal_path("lanect"), "a") as _fh: _fh.write(_json.dumps(_ev_amb) + "\n")
check("an AMBIGUOUS chain (two successors recorded for one parent) raises IntentNotDurable (never a guess)", _raises(lambda: ci.chain_tip("lanect", _t1)))
open(ci.journal_path("lanecu"), "w").write("{not json}\n{also not json}\n")
check("chain_tip on an unreadable journal raises IntentNotDurable (never an empty fallback)", _raises(lambda: ci.chain_tip("lanecu", "x")))
def _raw(lane, ev):
    with open(ci.journal_path(lane), "a") as fh: fh.write(_json.dumps(ev) + "\n")
def _iev(lane, iid, pos, sup=None):
    e = {"ev": "intent", "intent_id": iid, "lane": lane, "mint": "MX", "pos_id": pos, "settle_ops": OPS, "close_info": {}, "ts": 1.0}
    if sup: e["supersedes"] = sup
    return e
_raw("lanecy", _iev("lanecy", "lanecy:A", "p", "lanecy:B")); _raw("lanecy", _iev("lanecy", "lanecy:B", "p", "lanecy:A"))
check("chain_tip NEGATIVE CONTROL: a CYCLE (A supersedes B, B supersedes A) raises", _raises(lambda: ci.chain_tip("lanecy", "lanecy:A")))
_raw("lanexp", _iev("lanexp", "lanexp:X", "p1")); _raw("lanexp", _iev("lanexp", "lanexp:Y", "p2", "lanexp:X"))
check("chain_tip NEGATIVE CONTROL: a successor on ANOTHER position (cross-position chain) raises", _raises(lambda: ci.chain_tip("lanexp", "lanexp:X")))
_raw("laneun", _iev("laneun", "laneun:Z", "p", "laneun:GHOST"))
check("chain_tip NEGATIVE CONTROL: successors recorded for an UNKNOWN (absent) parent raise; the successor itself is a valid root", _raises(lambda: ci.chain_tip("laneun", "laneun:GHOST")) and ci.chain_tip("laneun", "laneun:Z")[:2] == ("laneun:Z", "open"))
_raw("lanedb", _iev("lanedb", "lanedb:P", "p")); _raw("lanedb", _iev("lanedb", "lanedb:Q1", "p", "lanedb:P")); _raw("lanedb", _iev("lanedb", "lanedb:Q2", "p", "lanedb:P"))
check("chain_tip NEGATIVE CONTROL: a DOUBLE successor of one parent raises; and begin(supersedes=<that parent>) refuses without appending",
      _raises(lambda: ci.chain_tip("lanedb", "lanedb:P")) and _raises(lambda: ci.begin("lanedb", "MX", "p", OPS, dict(INFO), supersedes="lanedb:P")) and _nint("lanedb") == 3)

# ---- recover_lane on a supersede chain, with canned callbacks (journal logic only; the callbacks are interface stubs, no account) ----
_ca = ci.begin("lanerc", "MR", "pr", OPS, dict(INFO)); ci.freeze("lanerc", _ca, "account re-apply refused: earlier")
_cb = ci.begin("lanerc", "MR", "pr", OPS, dict(INFO), supersedes=_ca)
_fr = ci.recover_lane("lanerc", lambda op: (True, ""), lambda it: False, lambda it: True)
check("recover_lane on a chain (frozen prior, pending successor): the prior is skipped and stays frozen, the successor is finalized to applied, nothing newly frozen (%r)" % (_fr,),
      _fr == [] and ci.intent_status("lanerc", _ca) == "frozen" and ci.intent_status("lanerc", _cb) == "applied")
_rp = ci.begin("lanerf", "MR", "pf", OPS, dict(INFO))
_real_freeze = ci.freeze
ci.freeze = lambda lane, iid, why: False      # NAMED FAULT INJECTION: the freeze append fails
try:
    _fr1 = ci.recover_lane("lanerf", lambda op: (False, "refused (injected)"), lambda it: False, lambda it: True)
finally:
    ci.freeze = _real_freeze
check("recover_lane when the freeze itself FAILS: the intent is still returned as a BLOCKER (%r) though its status stays open (not frozen) for retry (%s)" % (_fr1, ci.intent_status("lanerf", _rp)),
      _fr1 == [_rp] and ci.intent_status("lanerf", _rp) == "open")   # TEST EDIT (disclosed, BB40): was `_fr1 == []`. Fail-closed contract: an intent whose freeze append FAILED is still returned as a BLOCKER (not reported frozen, status stays open), never dropped
_fr2 = ci.recover_lane("lanerf", lambda op: (False, "refused (injected)"), lambda it: False, lambda it: True)
check("the retry with a working journal freezes it and reports it (%r, status %s)" % (_fr2, ci.intent_status("lanerf", _rp)), _fr2 == [_rp] and ci.intent_status("lanerf", _rp) == "frozen")

check("the new intents are OPEN and the frozen ones stay terminal (pending=%r)" % pend, a not in pend and b not in pend and c in pend and c2 in pend)
d = ci.begin("grad", "MINT2", "grad/MINT2#1", OPS, dict(INFO))
check("id keeps the readable base (lane:mint:pos:second) plus a suffix (%s)" % d, d.startswith("grad:MINT2:grad/MINT2#1:1795593430:") and len(d.rsplit(":", 1)[-1]) == 24)

# FAIL CLOSED: an unwritable journal (path replaced by a directory: read and append both fail) must make begin() RAISE IntentNotDurable and freeze() return
# False, never hand back an id for an intent that is not on disk. The expected-fault logging (logger.exception inside _append) is silenced here so the
# native log carries no traceback of an INJECTED fault (the strict parser rejects any Traceback).
import shutil, logging
jp = ci.journal_path("lanex"); os.makedirs(jp)
class _Cap(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self); self.recs = []
    def emit(self, r):
        self.recs.append(r)
_cap = _Cap(); _lg = logging.getLogger(ci.logger.name); _prop = _lg.propagate; _lg.addHandler(_cap); _lg.propagate = False
try:
    try:
        ci.begin("lanex", "M", "p", OPS, dict(INFO)); _raised = False
    except ci.IntentNotDurable:
        _raised = True
    _fz = ci.freeze("lanex", "grad:M:p:1:abc", "x")
finally:
    _lg.removeHandler(_cap); _lg.propagate = _prop
_rf = [r for r in _cap.recs if r.getMessage().startswith("close_intents: journal not cleanly readable (") and "refusing append" in r.getMessage() and r.exc_info is None]
check("the directory fault is a CLASSIFICATION refusal: begin refuses at the read (no log), freeze logs EXACTLY one 'journal not cleanly readable ... refusing append' record with no exc_info (%d such, %d total: %r)" % (len(_rf), len(_cap.recs), [r.getMessage() for r in _cap.recs]),
      len(_rf) == 1 and len(_cap.recs) == 1)
check("unwritable journal: begin raises IntentNotDurable (no id for a non-durable intent) and freeze returns False (%r, %r)" % (_raised, _fz), _raised is True and _fz is False)
shutil.rmtree(jp)
check("healthy journal again: freeze returns True and begin works", ci.freeze("lanex", "grad:M:p:1:abc", "y") is True and bool(ci.begin("lanex", "M", "p", OPS, dict(INFO))))

# stable_id=True is idempotent per OPERATION KEY while every intent id stays UNIQUE: a retry finds the existing intent by the key and returns ITS id
st1 = ci.begin("lanes", "MS", "ps", OPS, dict(INFO), stable_id=True); st2 = ci.begin("lanes", "MS", "ps", OPS, dict(INFO), stable_id=True)
_ev = [e for e in ci._read(ci.journal_path("lanes"))[0] if e.get("ev") == "intent"]
check("stable_id=True: the retry returns the SAME unique id (%s) with ONE intent event (%d) carrying op_key lanes:MS:ps:1795593430" % (st1, len(_ev)),
      st1 == st2 and len(_ev) == 1 and _ev[0].get("op_key") == "lanes:MS:ps:1795593430" and st1 != "lanes:MS:ps:1795593430" and st1.startswith("lanes:MS:ps:1795593430:"))
check("intent_status: open before the freeze", ci.intent_status("lanes", st1) == "open")
check("a stable begin whose recorded PLAN differs is refused (raises, nothing appended, the old intent untouched)",
      (lambda: [ci.begin("lanes", "MS", "ps", [{"kind": "credit", "op_id": "op-OTHER", "amount": 9.0}], dict(INFO), stable_id=True)])
      and _raises(lambda: ci.begin("lanes", "MS", "ps", [{"kind": "credit", "op_id": "op-OTHER", "amount": 9.0}], dict(INFO), stable_id=True))
      and len([e for e in ci._read(ci.journal_path("lanes"))[0] if e.get("ev") == "intent"]) == 1)
check("freeze returns True, status frozen", ci.freeze("lanes", st1, "terminal") is True and ci.intent_status("lanes", st1) == "frozen")
st3 = ci.begin("lanes", "MS", "ps", OPS, dict(INFO), stable_id=True)
check("stable begin after a FREEZE returns the existing frozen handle and opens NO new intent (still one event, still terminal, not pending)",
      st3 == st1 and len([e for e in ci._read(ci.journal_path("lanes"))[0] if e.get("ev") == "intent"]) == 1 and st1 not in [x["intent_id"] for x in ci.pending("lanes")])
_ma1 = ci.mark_account_applied("lanes", st1); _ml1 = ci.mark_lane_applied("lanes", st1)
check("a FROZEN intent refuses both markers (%r, %r) and its status stays frozen (%s); an absent id is None" % (_ma1, _ml1, ci.intent_status("lanes", st1)),
      _ma1 is False and _ml1 is False and ci.intent_status("lanes", st1) == "frozen" and ci.intent_status("lanes", "nope") is None)
st4 = ci.begin("lanes", "MS", "ps-fresh", OPS, dict(INFO), stable_id=True)
check("a SEPARATE fresh intent still reaches applied through both markers (%s)" % st4, ci.mark_account_applied("lanes", st4) is True and ci.mark_lane_applied("lanes", st4) is True and ci.intent_status("lanes", st4) == "applied")
# serialization: 8 threads begin the SAME operation concurrently -> ONE intent, one distinct id
import threading, subprocess
_ids = []; _l = threading.Lock()
def _w():
    i = ci.begin("lanec", "MC", "pc", OPS, dict(INFO), stable_id=True)
    with _l: _ids.append(i)
_th = [threading.Thread(target=_w) for _ in range(8)]
[t.start() for t in _th]; [t.join() for t in _th]
_n = len([e for e in ci._read(ci.journal_path("lanec"))[0] if e.get("ev") == "intent"])
check("8 concurrent stable begins (threads): ONE intent event (%d), one distinct id returned (%d of %d)" % (_n, len(set(_ids)), len(_ids)), _n == 1 and len(set(_ids)) == 1 and len(_ids) == 8)
# the same across PROCESSES (flock): 4 real subprocesses, same home
_code = ("import sys,os; sys.path.insert(0, %r); import daemon.launch_scanner.close_intents as ci; "
         "print(ci.begin('lanep','MP','pp',[{'kind':'credit','op_id':'op-1','amount':1.0}],{'close_ts':1795593430.2,'realized_usd':1.0},stable_id=True))" % REPO)
_ps = [subprocess.Popen([sys.executable, "-B", "-c", _code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ)) for _ in range(4)]
_outs = [p.communicate() for p in _ps]
_pids = {o[0].strip() for o in _outs}
_np = len([e for e in ci._read(ci.journal_path("lanep"))[0] if e.get("ev") == "intent"])
check("4 concurrent stable begins (separate processes): ONE intent event (%d), one distinct id (%d), all exit 0 (%r)" % (_np, len(_pids), [p.returncode for p in _ps]),
      _np == 1 and len(_pids) == 1 and all(p.returncode == 0 for p in _ps))
# stable_id on an unreadable journal fails closed
jp2 = ci.journal_path("lanet"); open(jp2, "w").write("{not json}\n{also not json}\n")
try:
    ci.begin("lanet", "MS", "ps", OPS, dict(INFO), stable_id=True); _sr = False
except ci.IntentNotDurable:
    _sr = True
check("stable_id on a torn journal fails closed (raises IntentNotDurable)", _sr)

# marker failure semantics: a lost marker is reported (False), nothing terminal is written without its evidence, and the intent stays pending for recovery
mk = ci.begin("lanem", "MM", "pm", OPS, dict(INFO))
_ra = ci._append; ci._append = lambda path, ev: False
try:
    _ma = ci.mark_account_applied("lanem", mk); _ml = ci.mark_lane_applied("lanem", mk)
finally:
    ci._append = _ra
check("marker appends that fail return False (%r, %r) and the intent is still PENDING for recovery (%r)" % (_ma, _ml, mk in [x["intent_id"] for x in ci.pending("lanem")]),
      _ma is False and _ml is False and mk in [x["intent_id"] for x in ci.pending("lanem")])
_okm = ci.mark_account_applied("lanem", mk) and ci.mark_lane_applied("lanem", mk)
check("healthy markers return True and the intent becomes terminal (applied)", _okm is True and ci.intent_status("lanem", mk) == "applied")

# concurrency, DISTINCT operations: 8 threads x 25 begins, each with its OWN op id (and pos) in the same second -> all ids distinct, every intent recorded once
import threading
ids = []; lk = threading.Lock()
def work(tn):
    for k in range(25):
        x = ci.begin("lanecs", "MC", "pc-%d-%d" % (tn, k), [{"kind": "credit", "op_id": "op-%d-%d" % (tn, k), "amount": 1.0}], dict(INFO))
        with lk:
            ids.append(x)
ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
[t_.start() for t_ in ts]; [t_.join() for t_ in ts]
ev = [e for e in ci._read(ci.journal_path("lanecs"))[0] if e.get("ev") == "intent"]
check("8 threads x 25 same-second begins of DISTINCT operations: %d ids, %d distinct, %d intent events, %d distinct in journal" % (len(ids), len(set(ids)), len(ev), len({e["intent_id"] for e in ev})),
      len(ids) == 200 and len(set(ids)) == 200 and len(ev) == 200 and len({e["intent_id"] for e in ev}) == 200)
_sm = []
def work_same():
    for _ in range(25):
        x = ci.begin("lanecss", "MC", "pc", OPS, dict(INFO))
        with lk:
            _sm.append(x)
ts = [threading.Thread(target=work_same) for _ in range(8)]
[t_.start() for t_ in ts]; [t_.join() for t_ in ts]
ev = [e for e in ci._read(ci.journal_path("lanecss"))[0] if e.get("ev") == "intent"]
check("8 threads x 25 begins of the SAME single operation: ONE intent (%d) and ONE id (%d distinct of %d)" % (len(ev), len(set(_sm)), len(_sm)),
      len(ev) == 1 and len(set(_sm)) == 1 and len(_sm) == 200)

# concurrency across PROCESSES: 4 children x 25 begins of DISTINCT operations (child index in the op id)
code = ("import os,sys;sys.path.insert(0,%r);import daemon.launch_scanner.close_intents as ci;n=sys.argv[1];"
        "[ci.begin('laneps','MP','pp-'+n+'-'+str(k),[{'kind':'credit','op_id':'o-'+n+'-'+str(k),'amount':1.0}],{'close_ts':1795593430.5}) for k in range(25)]") % REPO
procs = [subprocess.Popen([sys.executable, "-B", "-c", code, str(n)], env=dict(os.environ)) for n in range(4)]
rcs = [p_.wait() for p_ in procs]
ev = [e for e in ci._read(ci.journal_path("laneps"))[0] if e.get("ev") == "intent"]
check("4 processes x 25 same-second begins of DISTINCT operations: rcs %r, %d intent events, %d distinct ids, journal not torn" % (rcs, len(ev), len({e["intent_id"] for e in ev})),
      rcs == [0, 0, 0, 0] and len(ev) == 100 and len({e["intent_id"] for e in ev}) == 100 and ci._read(ci.journal_path("laneps"))[1] is None)
code2 = ("import os,sys;sys.path.insert(0,%r);import daemon.launch_scanner.close_intents as ci;"
         "[print(ci.begin('lanepss','MP','pp',[{'kind':'credit','op_id':'o','amount':1.0}],{'close_ts':1795593430.5})) for _ in range(25)]") % REPO
procs2 = [subprocess.Popen([sys.executable, "-B", "-c", code2], stdout=subprocess.PIPE, text=True, env=dict(os.environ)) for _ in range(4)]
outs2 = [p_.communicate()[0].split() for p_ in procs2]
ev = [e for e in ci._read(ci.journal_path("lanepss"))[0] if e.get("ev") == "intent"]
_all2 = {x for o in outs2 for x in o}
check("4 processes x 25 begins of the SAME single operation: rcs %r, ONE intent (%d), ONE id (%d), 100 returns (%d)" % ([p_.returncode for p_ in procs2], len(ev), len(_all2), sum(len(o) for o in outs2)),
      all(p_.returncode == 0 for p_ in procs2) and len(ev) == 1 and len(_all2) == 1 and sum(len(o) for o in outs2) == 100)
print("fails: %d" % FAILS)
sys.exit(1 if FAILS else 0)
