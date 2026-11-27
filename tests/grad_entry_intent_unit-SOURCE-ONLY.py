#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). Needs main's explicit clearance for this exact blob before any run. Stdlib only, no project import except daemon/launch_scanner/grad_entry_intent.py loaded by path,
no network, no child processes, temp dirs only. Unit evidence for the entry-intent record + classifier + recover_pass of the candidate (grad_entry_intent.py). NOT a crash test, NOT grad integration
(integration/kill tests are a separate source). Every case asserts ONE specific class/outcome; unknown or mismatched evidence must FREEZE (record kept), never drop or adopt."""
import copy, importlib.util, json, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
_s = importlib.util.spec_from_file_location("gei", os.path.join(REPO, "daemon", "launch_scanner", "grad_entry_intent.py"))
M = importlib.util.module_from_spec(_s); _s.loader.exec_module(M)
FAILS = []
def check(n, ok):
    print(("PASS " if ok else "FAIL ") + n)
    if not ok: FAILS.append(n)
MINT = "GradMint1pump"; OP = "grad:entry:GradMint1pump:1:1"; PID = "grad/GradMint1pump#1"
def mkpos():
    return {"mint": MINT, "tokens": 998.5, "entry_price": 1e-05, "entry_ts": 100.0, "legs": {"a": {}, "c": {"peak": 1e-05}}, "entry_features": {"x": 1},
            "execution": {"entry_op": OP, "source": "t"}}
def mkfill():
    return {"qty_tokens": 998.5, "price_usd": 1e-05, "fee_usd": 0.01, "execution_source": "t"}
def mkrec(**over):
    r = M.build_record("grad", MINT, OP, 1.0, 998.5, mkpos(), mkfill(), {"mint": MINT, "entry_day": "2026-11-24"})
    if over:
        body = {k: v for k, v in r.items() if k != "checksum"}; body.update(over); body["checksum"] = M._sha(body); return body
    return r
WDEB = {"event": "debit", "op_id": OP, "lane": "grad", "mint": MINT, "pos_id": PID, "stake": 1.0, "qty": 998.5}
def ev(**kw):
    e = {"ok": True, "op": {"event": "debit", "lane": "grad", "mint": MINT, "pos_id": PID, "stake": 1.0, "qty": 998.5}, "wal": [WDEB], "open_pos": {"qty": 998.5, "pos_id": PID}, "open_key": MINT, "open_lane": "grad", "open_n": 1, "settle": []}
    e.update(kw); return e
def lane(pos=None, closes=None, dead=None):
    return {"positions": ({MINT: pos} if pos else {}), "closes": closes or [], "dead_mints": dead or {}}
def cls(rec, l, e): return M.classify(rec, l, e)[0]

check("validate: good record is valid", M.validate(mkrec(), "grad") is None)
bad = mkrec(); bad["stake"] = 2.0
check("validate: altered field without checksum update is refused", "checksum" in (M.validate(bad, "grad") or ""))
check("validate: lane mismatch refused", "lane" in (M.validate(mkrec(), "other") or ""))
p = mkpos(); p["tokens"] = 1.0
check("validate: tokens != fill.qty_tokens refused (checksum-valid drift)", "tokens" in (M.validate(mkrec(pos=p), "grad") or ""))
p = mkpos(); p["entry_price"] = 2e-05
check("validate: entry_price != fill.price_usd refused", "entry_price" in (M.validate(mkrec(pos=p), "grad") or ""))
p = mkpos(); del p["legs"]
check("validate: missing required pos key refused", "missing key" in (M.validate(mkrec(pos=p), "grad") or ""))
p = mkpos(); p["execution"]["entry_op"] = "other"
check("validate: pos execution.entry_op != op_id refused", "entry_op" in (M.validate(mkrec(pos=p), "grad") or ""))
check("validate: missing cand.entry_day refused (attribution unknown)", "entry_day" in (M.validate(mkrec(cand={"mint": MINT}), "grad") or ""))
check("validate: fill sha mismatch refused", "sha256" in (M.validate(mkrec(fill_sha256="0" * 64), "grad") or ""))
p = mkpos(); p["pa_pos_id"] = "x"
check("validate: pos already carrying pa_pos_id refused", "pa_pos_id" in (M.validate(mkrec(pos=p), "grad") or ""))
try:
    M.build_record("grad", MINT, OP, 1.0, 998.5, dict(mkpos(), bad=float("nan")), mkfill(), {}); nan_ok = False
except Exception: nan_ok = True
check("build_record: non-finite value refused (nothing stringified silently)", nan_ok)

R = mkrec()
check("classify B: debit durable, open, all match, lane empty -> B with the account pos_id", M.classify(R, lane(), ev()) == ("B", "account debit durable, position open, all fields match, lane has no record: adopt", PID))
check("classify A: lane already holds same pa_pos_id/tokens -> A", cls(R, lane(dict(mkpos(), pa_pos_id=PID)), ev()) == "A")
check("classify E: lane holds a different position for the mint", cls(R, lane(dict(mkpos(), pa_pos_id="other")), ev()) == "E")
check("classify C: no ledger op, no WAL, lane empty -> C", cls(R, lane(), ev(op=None, wal=[], open_pos=None)) == "C")
check("classify E: no ledger op but WAL has events", cls(R, lane(), ev(op=None, open_pos=None)) == "E")
check("classify E: no account event but lane holds a position", cls(R, lane(mkpos()), ev(op=None, wal=[], open_pos=None)) == "E")
check("classify E: account evidence unavailable/degraded", cls(R, lane(), {"ok": False, "why": "x"}) == "E")
check("classify D-refused: ledger refusal confirmed by WAL, no debit", cls(R, lane(), ev(op={"event": "debit_refused"}, wal=[{"event": "debit_refused"}], open_pos=None)) == "D-refused")
check("classify E: ledger refusal NOT confirmed by WAL", cls(R, lane(), ev(op={"event": "debit_refused"}, wal=[], open_pos=None)) == "E")
check("classify E: ledger debit but WAL has no debit", cls(R, lane(), ev(wal=[])) == "E")
check("classify E: account stake differs from record", cls(R, lane(), ev(op={"event": "debit", "pos_id": PID, "stake": 2.0, "qty": 998.5})) == "E")
check("classify B with the REAL account schema (open position keyed by mint, NO mint field)", cls(R, lane(), ev()) == "B")
check("classify E: open position key is another mint (schema-faithful, no mint field)", cls(R, lane(), ev(open_key="OtherMint")) == "E")
check("classify E: ledger op mint differs", cls(R, lane(), ev(op={"event": "debit", "lane": "grad", "mint": "OtherMint", "pos_id": PID, "stake": 1.0, "qty": 998.5})) == "E")
check("classify E: ledger op carries no mint (unproven)", cls(R, lane(), ev(op={"event": "debit", "pos_id": PID, "stake": 1.0, "qty": 998.5})) == "E")
check("classify B/E: instance-style position with its own mint field is compared too", cls(R, lane(), ev(open_pos={"mint": MINT, "instance": True, "qty": 998.5, "pos_id": PID})) == "B" and cls(R, lane(), ev(open_pos={"mint": "OtherMint", "instance": True, "qty": 998.5, "pos_id": PID})) == "E")
def w(**kw): d = dict(WDEB); d.update(kw); return d
check("bind E: ledger op lane differs from the record lane", cls(R, lane(), ev(op={"event": "debit", "lane": "other", "mint": MINT, "pos_id": PID, "stake": 1.0, "qty": 998.5})) == "E")
check("bind E: ledger op has no lane", cls(R, lane(), ev(op={"event": "debit", "mint": MINT, "pos_id": PID, "stake": 1.0, "qty": 998.5})) == "E")
check("bind E: ledger op is an instance lot", cls(R, lane(), ev(op={"event": "debit", "lane": "grad", "mint": MINT, "pos_id": PID, "stake": 1.0, "qty": 998.5, "instance": True})) == "E")
for k, v in (("op_id", "grad:entry:Other:1:1"), ("lane", "other"), ("mint", "OtherMint"), ("pos_id", "grad/GradMint1pump#2"), ("stake", 2.0), ("qty", 5.0)):
    check("bind E: WAL debit %s differs from ledger/record" % k, cls(R, lane(), ev(wal=[w(**{k: v})])) == "E")
check("bind E: WAL debit without a stake", cls(R, lane(), ev(wal=[{kk: vv for kk, vv in WDEB.items() if kk != "stake"}])) == "E")
check("bind E: WAL debit is an instance lot", cls(R, lane(), ev(wal=[w(instance=True)])) == "E")
check("bind E: duplicate identical WAL debit events refused", cls(R, lane(), ev(wal=[w(), w()])) == "E")
check("bind E: conflicting WAL events for the same op (debit + refusal)", cls(R, lane(), ev(wal=[w(), {"event": "debit_refused", "op_id": OP}])) == "E")
check("bind E: duplicate WAL refusal events refused", cls(R, lane(), ev(op={"event": "debit_refused"}, wal=[{"event": "debit_refused", "op_id": OP}] * 2, open_pos=None)) == "E")
check("bind E: account holds TWO open positions for the pos_id", cls(R, lane(), ev(open_n=2)) == "E")
check("bind E: open position is in another lane", cls(R, lane(), ev(open_lane="other")) == "E")
check("bind E: open_n/open_lane absent (older evidence shape) refused", cls(R, lane(), {k: v for k, v in ev().items() if k not in ("open_n", "open_lane")}) == "E")
check("bind E: record lane differs from ledger lane (record side)", cls(dict(R, lane="other"), lane(), ev()) == "E")
check("bind: schema-faithful happy path still B after all bindings", cls(R, lane(), ev()) == "B")
check("classify E: account open qty differs", cls(R, lane(), ev(open_pos={"mint": MINT, "qty": 5.0, "pos_id": PID})) == "E")
check("classify E: account holds it but the lane has a closes record for it", cls(R, lane(closes=[{"mint": MINT, "pa_pos_id": PID}]), ev()) == "E")
check("classify E: account holds it but mint is in dead_mints", cls(R, lane(dead={MINT: 1}), ev()) == "E")
SET = {"event": "credit", "op_id": "s1", "lane": "grad", "mint": MINT, "pos_id": PID, "qty": None, "final": True}
settled = dict(open_pos=None, settle=[SET])
check("bind E: D-settled still needs the same bindings (WAL stake differs)", cls(R, lane(closes=[{"mint": MINT, "pa_pos_id": PID}]), ev(**dict(settled, wal=[w(stake=2.0)]))) == "E")
CL = lane(closes=[{"mint": MINT, "pa_pos_id": PID}])
def sd(sets): return cls(R, CL, ev(open_pos=None, settle=sets))
check("settle D-settled: schema-faithful terminal credit (qty None, final) -> D-settled", sd([SET]) == "D-settled")
check("settle D-settled: partial credit then terminal credit, qty sum == record qty", sd([dict(SET, op_id="s0", qty=400.0, final=False), dict(SET, qty=598.5)]) == "D-settled")
check("settle D-settled: write_down terminal event", sd([{"event": "write_down", "op_id": "w1", "lane": "grad", "mint": MINT, "pos_id": PID, "qty": 998.5}]) == "D-settled")
check("settle E: lane differs", sd([dict(SET, lane="other")]) == "E")
check("settle E: lane missing", sd([{k: v for k, v in SET.items() if k != "lane"}]) == "E")
check("settle E: mint differs", sd([dict(SET, mint="OtherMint")]) == "E")
check("settle E: pos_id differs", sd([dict(SET, pos_id="grad/GradMint1pump#9")]) == "E")
check("settle E: instance lot", sd([dict(SET, instance=True)]) == "E")
check("settle E: duplicated op_id", sd([SET, dict(SET)]) == "E")
check("settle E: missing op_id", sd([{k: v for k, v in SET.items() if k != "op_id"}]) == "E")
check("settle E: only a non-final partial credit (not terminal)", sd([dict(SET, qty=400.0, final=False)]) == "E")
check("settle E: settled qty exceeds the record qty", sd([dict(SET, op_id="s0", qty=999.0, final=False), dict(SET, qty=10.0)]) == "E")
check("settle E: non-positive qty", sd([dict(SET, qty=-1.0)]) == "E")
check("settle E: non-settle event in the settle list", sd([{"event": "debit", "op_id": "x", "lane": "grad", "mint": MINT, "pos_id": PID}, SET]) == "E")
check("duplicate positions across lane dictionaries freeze (open_n 2 with the second in another lane)", cls(R, lane(), ev(open_n=2, open_lane="grad")) == "E")
check("classify D-settled: not open + WAL settle + lane closes record (pa_pos_id+mint)", cls(R, lane(closes=[{"mint": MINT, "pa_pos_id": PID}]), ev(**settled)) == "D-settled")
check("classify E: settled in account but NO lane closes record (unreconciled)", cls(R, lane(), ev(**settled)) == "E")
check("classify E: closed in lane but NO WAL settle event", cls(R, lane(closes=[{"mint": MINT, "pa_pos_id": PID}]), ev(open_pos=None)) == "E")
check("classify E: closes record with a different pa_pos_id is not proof", cls(R, lane(closes=[{"mint": MINT, "pa_pos_id": "z"}]), ev(**settled)) == "E")

def home():
    d = tempfile.mkdtemp(prefix="gei_"); return os.path.join(d, "grad.json")
L = home()
p1 = M.write_record(L, R)
check("write_record: file exists mode 0600, dir mode 0700", os.path.exists(p1) and oct(os.stat(p1).st_mode & 0o777) == "0o600" and oct(os.stat(os.path.dirname(p1)).st_mode & 0o777) == "0o700")
recs, unr = M.scan(L)
check("scan: one valid record, directory readable", len(recs) == 1 and recs[0][1]["op_id"] == OP and unr is False)
open(p1 + ".tmp", "w").write("{")
recs, unr = M.scan(L)
check("scan: stale tmp removed under the (caller-held) lane lock, no age heuristic", not os.path.exists(p1 + ".tmp") and len(recs) == 1)
M.remove_record(p1); M.remove_record(p1)
check("remove_record: idempotent (second call is a no-op)", not os.path.exists(p1) and M.scan(L) == ([], False))
check("scan: missing directory with readable parent == no records", M.scan(home()) == ([], False))
L2 = home(); M.write_record(L2, R); open(os.path.join(M.records_dir(L2), "junk.txt"), "w").write("x")
recs, _ = M.scan(L2)
check("scan: unrecognized file is reported (blocks), not ignored", any(r[1] is None and "unrecognized" in r[2] for r in recs))
L3 = home(); M.write_record(L3, R)
f = os.path.join(M.records_dir(L3), M._fname(OP)); open(f, "w").write("{not json")
recs, _ = M.scan(L3)
check("scan: unparseable record reported as unreadable (blocks)", len(recs) == 1 and recs[0][1] is None and "unreadable" in recs[0][2])
_real_open = os.open
def _failopen(path, flags, *a, **k):
    if os.path.isdir(path): raise OSError("simulated dir fsync open failure")
    return _real_open(path, flags, *a, **k)
M.os.open = _failopen
try:
    M.write_record(home(), R); req = False
except Exception: req = True
finally: M.os.open = _real_open
check("write_record: directory fsync failure RAISES (entry refused before any debit)", req)

class Cb:
    def __init__(s, e, l=None): s.e = e; s.l = l if l is not None else lane(); s.adopted = []; s.dropped = []
    def read_lane(s): return copy.deepcopy(s.l)
    def adopt(s, rec, pid): s.adopted.append((rec["op_id"], pid)); s.l = lane(dict(rec["pos"], pa_pos_id=pid))
    def evidence(s, op): return s.e
    def drop(s, op, mint): s.dropped.append((op, mint))
def run(Lp, cb): return M.recover_pass(Lp, "grad", cb.read_lane, cb.adopt, cb.evidence, cb.drop)

La = home(); M.write_record(La, R); cb = Cb(ev())
why, acts = run(La, cb)
check("recover_pass B: adopts exactly once with the account pos_id, record removed, intent dropped, nothing blocks", why is None and cb.adopted == [(OP, PID)] and M.scan(La) == ([], False) and cb.dropped == [(OP, MINT)])
why2, acts2 = run(La, cb)
check("recover_pass: second pass is a no-op (idempotent, no second adoption)", why2 is None and acts2 == [] and len(cb.adopted) == 1)
Lc = home(); M.write_record(Lc, R); cb = Cb(ev(op=None, wal=[], open_pos=None)); why, acts = run(Lc, cb)
check("recover_pass C: record dropped, no adoption", why is None and cb.adopted == [] and M.scan(Lc) == ([], False))
Le = home(); M.write_record(Le, R); cb = Cb(ev(wal=[])); why, acts = run(Le, cb)
check("recover_pass E: blocks entries naming op and mint, record KEPT, no adoption", why is not None and OP in why and MINT in why and cb.adopted == [] and len(M.scan(Le)[0]) == 1)
Lu = home(); M.write_record(Lu, R); cb = Cb({"ok": False, "why": "account unavailable"}); why, acts = run(Lu, cb)
check("recover_pass: unavailable account evidence freezes (record kept)", why is not None and len(M.scan(Lu)[0]) == 1 and cb.adopted == [])
Lx = home(); M.write_record(Lx, R); cb = Cb(ev()); cb.adopt = lambda rec, pid: (_ for _ in ()).throw(RuntimeError("boom")); why, acts = run(Lx, cb)
check("recover_pass: adopt failure blocks entries and KEEPS the record", why is not None and "adoption failed" in why and len(M.scan(Lx)[0]) == 1)
Lb = home()
for i in range(M.MAX_PER_PASS + 2):
    r = mkrec(op_id="grad:entry:%s:%d:1" % (MINT, i), pos=dict(mkpos(), execution={"entry_op": "grad:entry:%s:%d:1" % (MINT, i)})); M.write_record(Lb, r)
cb = Cb({"ok": False, "why": "x"}); why, acts = run(Lb, cb)
check("recover_pass: backlog beyond the per-pass bound keeps entries blocked, records kept", why is not None and len(M.scan(Lb)[0]) == M.MAX_PER_PASS + 2)
Ld = home(); os.makedirs(M.records_dir(Ld))
_real_ld = os.listdir
def _deny(path=".", *a, **k):
    if str(path) == M.records_dir(Ld): raise PermissionError("simulated unreadable entry-intent directory")
    return _real_ld(path, *a, **k)
M.os.listdir = _deny
try:
    why, acts = run(Ld, Cb(ev()))
finally: M.os.listdir = _real_ld
check("recover_pass: unreadable directory blocks entries (state unknown)", why is not None and "unreadable" in why)
cb = Cb(ev()); cb.read_lane = lambda: (_ for _ in ()).throw(RuntimeError("lane read boom")); Lr = home(); M.write_record(Lr, R); why, acts = run(Lr, cb)
check("recover_pass: an internal failure is a BLOCK reason, never an exception to the caller", why is not None)
Lnone = home(); why, acts = run(Lnone, Cb(ev()))
check("recover_pass: no records and no directory: nothing blocks, no I/O creates the directory", why is None and not os.path.exists(M.records_dir(Lnone)))
check("flag: default OFF", os.environ.pop(M.FLAG, None) is None and M.enabled() is False)
os.environ[M.FLAG] = "1"; on = M.enabled(); os.environ.pop(M.FLAG, None)
check("flag: only the exact value '1' enables", on is True)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
