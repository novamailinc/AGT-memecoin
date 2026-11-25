#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). NEVER RUN without main's explicit clearance for this exact blob. Spawns CHILD processes through the same contained helper chain as
tests/real_lane_entry_recovery_harness-SOURCE-ONLY.py (temp home, real grad.py + paper_account + demo fill, network blocked, shared $10 DEMO account only, no executor).
PURPOSE: an evidence-CONTRACT test against the REAL paper account, not a hand-built mock. A real entry is opened through consider_candidate (flag on); then the candidate's own
account_evidence() is read and classify() is fed the REAL evidence plus a record built from the real ledger op. It asserts the real schema facts the classifier relies on and the explicit
bindings (record -> ledger op -> WAL debit; lane; mint; pos_id; stake; qty), and what a partial and a full real sale do to the evidence. It never edits the account or the WAL by hand.
Cases (one specific expected value each):
  C1 evidence ok, op debit {lane, mint, pos_id, stake, qty}; exactly ONE WAL event for the op with the same op_id/lane/mint/pos_id/stake/qty; ONE open position keyed by MINT in lane grad;
     the open position carries NO mint field (the schema fact that broke the first candidate); open_n 1.
  C2 classify(real evidence, empty lane) == B; with the real lane position == A.
  C3 negative controls on the SAME real evidence: record lane differs -> E; qty differs -> E; stake differs -> E; mint differs -> E; unknown op id -> C.
  C4 a REAL partial sale (account credit of half the units with the real pos_id): open qty now differs -> classify E (documented conservative freeze, never auto-reduced or adopted).
  C5 a REAL full sale of the rest: position no longer open, settle events present with lane/mint/pos_id/op_id and a final credit: D-settled ONLY with a lane closes record, E without it.
  C6 durable refusal (demo mode): a REAL oversized debit is refused: ledger op debit_refused + exactly one WAL debit_refused event -> classify D-refused.
  C7 every settle event the real WAL produced carries lane grad, the entry mint, the real pos_id and a distinct op_id (the fields _settle_bad binds on).
CLAIM LIMITS: one machine, real helper chain but fixture stubs (quotes, marks, safety); in-process evidence only (not a kill test); the credits use the fixture price path and may be refused by
account rules, in which case the case FAILS and is reported as it is; no secrets read; flag-off behavior is not covered here.
"""
import importlib.util, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
def _load(n, f):
    sp = importlib.util.spec_from_file_location(n, os.path.join(HERE, f)); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
E = _load("eibase", "real_lane_entry_recovery_harness-SOURCE-ONLY.py")
B = E.B; check = B.check; FAILS = B.FAILS
ACC = E.PRE + r'''
EIM = GR._ei()
r = GR.consider_candidate(mkcand(1), time.time())
out = {"name": name, "opened": bool(r[0]), "why": str(r[1])[:160]}
ents, st, _ = PA._journal_read()
deb = [e for e in ents if e.get("event") == "debit"]
out["n_debit_wal"] = len(deb)
if deb:
    opid = deb[0]["op_id"]; mint = deb[0]["mint"]; pid = deb[0]["pos_id"]
    ev = EIM.account_evidence(PA, opid, 2.0); out["ev"] = {k: ev.get(k) for k in ("ok", "why", "op", "wal", "open_pos", "open_key", "open_lane", "open_n", "settle")}
    qty = float(ev["op"]["qty"]); stake = float(ev["op"]["stake"])
    rec = {"lane": GR.LANE, "mint": mint, "op_id": opid, "stake": stake, "qty": qty}
    emp = {"positions": {}, "closes": [], "dead_mints": {}}
    cl = lambda rc, ld, e: EIM.classify(rc, ld, e)
    out["c2"] = {"B": cl(rec, emp, ev)[0], "A": cl(rec, {"positions": {mint: {"pa_pos_id": pid, "tokens": qty}}, "closes": [], "dead_mints": {}}, ev)[0]}
    out["c3"] = {"lane": cl(dict(rec, lane="other"), emp, ev)[0], "qty": cl(dict(rec, qty=qty * 2), emp, ev)[0], "stake": cl(dict(rec, stake=stake * 2), emp, ev)[0], "mint": cl(dict(rec, mint="OtherMint"), emp, ev)[0],
                 "unknown_op": cl(dict(rec, op_id="grad:entry:none:0:0"), emp, EIM.account_evidence(PA, "grad:entry:none:0:0", 2.0))[0]}
    ok1, why1 = PA.credit(GR.LANE, mint, 0.4, "contract:partial", pos_id=pid, qty=qty / 2.0)
    ev2 = EIM.account_evidence(PA, opid, 2.0)
    out["c4"] = {"credit_ok": bool(ok1), "why": str(why1)[:120], "open_qty": (ev2.get("open_pos") or {}).get("qty"), "cls": cl(rec, emp, ev2)[0], "reason": cl(rec, emp, ev2)[1][:160]}
    ok2, why2 = PA.credit(GR.LANE, mint, 0.4, "contract:rest", pos_id=pid)
    ev3 = EIM.account_evidence(PA, opid, 2.0)
    out["c5"] = {"credit_ok": bool(ok2), "why": str(why2)[:120], "open_pos": ev3.get("open_pos"), "n_settle": len(ev3.get("settle") or []),
                 "with_closes": cl(rec, {"positions": {}, "closes": [{"mint": mint, "pa_pos_id": pid}], "dead_mints": {}}, ev3), "without_closes": cl(rec, emp, ev3)[0],
                 "settle": [{k: e.get(k) for k in ("event", "op_id", "lane", "mint", "pos_id", "qty", "final")} for e in (ev3.get("settle") or [])]}
    out["c5"]["with_closes"] = [out["c5"]["with_closes"][0], out["c5"]["with_closes"][1][:160]]
rop = "grad:entry:RefusedMint:1:1"
okr, whyr, pidr = PA.try_debit(GR.LANE, "RefusedMint", 1000.0, 1.0, rop, durable_refusal=True)
evr = EIM.account_evidence(PA, rop, 2.0)
out["c6"] = {"debit_ok": bool(okr), "ev_op": (evr.get("op") or {}).get("event"), "n_wal": len(evr.get("wal") or []),
             "cls": EIM.classify({"lane": GR.LANE, "mint": "RefusedMint", "op_id": rop, "stake": 1000.0, "qty": 1.0}, {"positions": {}, "closes": [], "dead_mints": {}}, evr)[0]}
print(json.dumps(out, default=str))
'''
def contract():
    h = B.newhome(); p, r = E.run(h, ACC, "acc", "1")
    check("C0 child ran and opened ONE real entry (opened %s, WAL debits %s, rc %s)" % ((r or {}).get("opened"), (r or {}).get("n_debit_wal"), p.returncode), p.returncode == 0 and bool(r) and r.get("opened") is True and r.get("n_debit_wal") == 1)
    if not r or "ev" not in r: return
    ev = r["ev"]; op = ev.get("op") or {}; w = ev.get("wal") or []; pos = ev.get("open_pos") or {}
    check("C1 evidence ok, ledger op debit with lane/mint/pos_id/stake/qty: %s" % {k: op.get(k) for k in ("event", "lane", "mint", "pos_id", "stake", "qty")},
          ev.get("ok") is True and op.get("event") == "debit" and op.get("lane") == "grad" and bool(op.get("mint")) and bool(op.get("pos_id")) and op.get("stake") == 1.0 and (op.get("qty") or 0) > 0)
    check("C1 exactly ONE WAL event for the op, identical op_id/lane/mint/pos_id/stake/qty to the ledger op (%s)" % [(x.get("event"), x.get("op_id"), x.get("lane"), x.get("mint"), x.get("pos_id"), x.get("stake"), x.get("qty")) for x in w],
          len(w) == 1 and w[0].get("event") == "debit" and all(w[0].get(k) == op.get(k) for k in ("lane", "mint", "pos_id", "stake", "qty")) and bool(w[0].get("op_id")))
    check("C1 ONE open position keyed by MINT in lane grad, pos_id matches, NO mint field, qty == op qty (open_n %s, lane %s, key %s, fields %s)" % (ev.get("open_n"), ev.get("open_lane"), ev.get("open_key"), sorted(pos)),
          ev.get("open_n") == 1 and ev.get("open_lane") == "grad" and ev.get("open_key") == op.get("mint") and pos.get("pos_id") == op.get("pos_id") and "mint" not in pos and pos.get("qty") == op.get("qty"))
    check("C2 classify on REAL evidence: empty lane -> %s (expect B), real lane position -> %s (expect A)" % (r["c2"]["B"], r["c2"]["A"]), r["c2"] == {"B": "B", "A": "A"})
    check("C3 negative controls on the same real evidence all E, unknown op C: %s" % r["c3"], r["c3"] == {"lane": "E", "qty": "E", "stake": "E", "mint": "E", "unknown_op": "C"})
    c4 = r.get("c4") or {}
    check("C4 real partial sale accepted (%s %s), open qty now %s, classify %s (expect E: documented conservative freeze); reason: %s" % (c4.get("credit_ok"), c4.get("why"), c4.get("open_qty"), c4.get("cls"), c4.get("reason")),
          c4.get("credit_ok") is True and c4.get("cls") == "E" and (c4.get("open_qty") or 0) < (op.get("qty") or 0))
    c5 = r.get("c5") or {}
    wc = c5.get("with_closes") or ["?", "?"]
    check("C5 real full sale accepted (%s %s), position no longer open (%s), settle events %s: D-settled only with closes (%s, %s), E without (%s)" % (c5.get("credit_ok"), c5.get("why"), c5.get("open_pos"), c5.get("n_settle"), wc[0], wc[1], c5.get("without_closes")),
          c5.get("credit_ok") is True and c5.get("open_pos") is None and (c5.get("n_settle") or 0) >= 2 and wc[0] == "D-settled" and c5.get("without_closes") == "E")
    st = c5.get("settle") or []
    check("C7 every real settle WAL event has lane grad, the entry mint, the real pos_id, a distinct op_id and the last credit is final (%s)" % st,
          len(st) >= 2 and all(e.get("lane") == "grad" and e.get("mint") == op.get("mint") and e.get("pos_id") == op.get("pos_id") and e.get("op_id") for e in st)
          and len({e.get("op_id") for e in st}) == len(st) and any(e.get("final") is True for e in st))
    c6 = r.get("c6") or {}
    check("C6 real durable refusal: debit refused (ok %s), ledger op %s, WAL events %s, classify %s (expect D-refused)" % (c6.get("debit_ok"), c6.get("ev_op"), c6.get("n_wal"), c6.get("cls")),
          c6.get("debit_ok") is False and c6.get("ev_op") == "debit_refused" and c6.get("n_wal") == 1 and c6.get("cls") == "D-refused")
def main():
    try: B.scen("CONTRACT", contract)
    finally: B.reap_all()
    alive = [p.pid for p in B.KIDS if p.poll() is None]
    check("all children reaped (still alive: %s)" % alive, not alive)
    print("elapsed %.1fs of 90s budget; FAILS: %d" % (90.0 - B.left(), len(FAILS))); return 1 if FAILS else 0
if __name__ == "__main__":
    sys.exit(main())
