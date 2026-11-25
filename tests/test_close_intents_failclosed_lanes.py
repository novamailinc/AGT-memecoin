#!/usr/bin/env python3
"""Fail-closed close-intent durability for the ceo, watcher, momentum and mirror lanes (2026-11-25). ENGINEERING TEST of the real lane functions with
the real close_intents journal in a temp home; the shared account is replaced only by a SPY object that records credit calls (the point is that NO
account call happens), and the journal write failure is an INJECTED, named fault at close_intents._append.

Contract under test (per lane):
  1. journal not writable  -> _close_leg/_close_remaining return 0.0 BEFORE anything is sold or booked: leg not done, pos["realized_usd"]
     unchanged, no _pa_settle call, pending_exit why close_intent_not_durable (visible, retried next poll).
  2. storage back          -> the same call proceeds (the gate passes and the leg is executed through the lane's own path is NOT exercised here:
     only the gate/begin contract is; full lane exits are covered by their own suites).
  3. the race (probe ok, real intent write fails AFTER the sale fill): the exit is rolled back in memory, no account credit, nothing persisted,
     pending_exit close_intent_not_durable; the retry re-reads the same journaled fill and settles once.
Run: python3 -B tests/test_close_intents_failclosed_lanes.py
"""
import importlib, os, sys, tempfile, time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import tests._guard_fixture as _gfb; _gfb.block_network()
os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="ci-failclosed-")
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
import daemon.launch_scanner.close_intents as CI

FAILS = 0
def check(name, cond):
    global FAILS
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS += 1

_real_append = CI._append
def _no_write(path, event):
    return False   # INJECTED journal write failure (named fault), every event kind
def _no_intent_only(path, event):
    return False if event.get("ev") == "intent" else _real_append(path, event)

class _SpyAccount:
    def __init__(self):
        self.credits = []; self.writedowns = []
    def credit(self, *a, **k):
        self.credits.append(a); return (True, "")
    def write_down(self, *a, **k):
        self.writedowns.append(a); return (True, "")
    def op_outcome(self, *a, **k): return None
    def pos_ever_existed(self, *a, **k): return True

def mkpos(mint, lane=None):
    p = mkpos0(mint)
    if lane:
        p["lane"] = lane
    return p

def mkpos0(mint):
    return {"mint": mint, "chain": "solana", "pa_pos_id": "lane/%s#1" % mint, "tokens": 300.0, "realized_usd": 0.0,
            "execution": {"source": "jupiter_quote_sim"}, "entry_snapshot": {},
            "legs": {k: {"done": False, "frac": 1.0 / 3.0} for k in ("a", "b", "c")}}

for modname in ("ceo", "watcher_lane", "momentum_lane", "mirror"):
    try:
        M = importlib.import_module("daemon.launch_scanner." + modname)
    except Exception as e:
        check("%s imports (%r)" % (modname, e), False); continue
    spy = _SpyAccount(); settle_calls = []
    pos = mkpos("FC" + modname[:3])
    if modname == "mirror":
        pos["lane"] = "mirror"
    lane = pos.get("lane") or getattr(M, "LANE", modname)
    with patch.object(M, "_paper_account", lambda: spy), patch.object(M, "_pa_settle", lambda *a, **k: settle_calls.append(a)), \
         patch.object(M, "_pa_write_down", lambda *a, **k: settle_calls.append(a)), patch.object(CI, "_append", _no_write):
        r1 = M._close_leg(pos, "a", 0.001, "hard_stop")
        r2 = M._close_remaining(pos, 0.001, "hard_stop")
    pe = pos.get("pending_exit") or {}
    check("%s: journal not writable -> _close_leg and _close_remaining return 0.0, nothing booked (leg a done=%r, realized %r), no settle/write-down call (%d), pending_exit why %r"
          % (modname, pos["legs"]["a"]["done"], pos["realized_usd"], len(settle_calls), pe.get("why")),
          r1 == 0.0 and r2 == 0.0 and not any(v["done"] for v in pos["legs"].values()) and pos["realized_usd"] == 0.0
          and not settle_calls and pe.get("why") == "close_intent_not_durable")
    check("%s: no account credit or write-down reached the account spy (%d/%d)" % (modname, len(spy.credits), len(spy.writedowns)), not spy.credits and not spy.writedowns)
    # the gate itself passes again once storage returns, and leaves no intent behind
    with patch.object(M, "_paper_account", lambda: spy):
        _g = M._intent_gate(mkpos("FCok" + modname[:3], "mirror" if modname == "mirror" else None), "a")
    check("%s: storage back -> the gate passes and the journal holds no intent (probe only)" % modname,
          _g is True and not [e for e in CI._read(CI.journal_path(lane))[0] if e.get("ev") == "intent"])
    # THE RACE (probe ok, the real intent write fails AFTER the sale fill): the exit is rolled back in memory, NO account op, nothing persisted;
    # the retry re-reads the SAME journaled fill (deterministic exit op id, filler returns the existing fill) and only the intent write is retried.
    # The sale itself is a CANNED fill object returned by a fake filler (engineering fixture: the point is the journal/rollback/retry contract, not execution).
    class _FakeFiller:
        def __init__(self):
            self.fills = {}; self.calls = []
        def sell(self, op_id, mint, qty, cap, *a, **k):
            self.calls.append(op_id)
            if op_id not in self.fills:
                self.fills[op_id] = {"status": "filled", "price_usd": 0.001, "gross_usd": 0.1, "fee_usd": 0.001, "execution_source": "canned_fixture",
                                     "signature": "fixture-sig", "provenance": {"fixture": True}}
                if k.get("meta"):
                    self.fills[op_id]["intent"] = dict(k["meta"])
            return self.fills[op_id]
        def get_fill(self, op_id):
            return self.fills.get(op_id)
        def fills_by_prefix(self, prefix, suffix=""):
            return {k: dict(v) for k, v in self.fills.items() if k.startswith(prefix) and k.endswith(suffix)}
    ff = _FakeFiller(); spy2 = _SpyAccount(); spy2.max_price_impact_pct = lambda: 50.0
    pos2 = mkpos("FR" + modname[:3], "mirror" if modname == "mirror" else None)
    if modname == "mirror":
        pos2["lane"] = "mirror"
    _alias_a = pos2["legs"]["a"]; _alias_legs = pos2["legs"]
    with patch.object(M, "_paper_account", lambda: spy2), patch.object(M, "_demo_filler", lambda: ff), patch.object(M, "_exit_fee_venue", lambda p: None), \
         patch.object(CI, "_append", _no_intent_only):
        r_fail = M._close_leg(pos2, "a", 0.001, "leg_a_2x")
    pe2 = pos2.get("pending_exit") or {}
    check("%s rollback keeps the ORIGINAL leg dict objects (aliases held by the poll loop stay valid and show not-done)" % modname,
          pos2["legs"]["a"] is _alias_a and _alias_a["done"] is False and pos2["legs"] is _alias_legs)
    check("%s race: exit rolled back in memory (leg a done=%r, realized %r), NO account credit (%d), pending_exit %r, sale called %d time(s)"
          % (modname, pos2["legs"]["a"]["done"], pos2["realized_usd"], len(spy2.credits), pe2.get("why"), len(ff.calls)),
          r_fail == 0.0 and pos2["legs"]["a"]["done"] is False and pos2["realized_usd"] == 0.0 and not spy2.credits
          and pe2.get("why") == "close_intent_not_durable" and len(ff.calls) == 1)
    check("%s race: nothing durable was written for the failed attempt (no intent event for this mint)" % modname,
          not [e for e in CI._read(CI.journal_path(lane))[0] if e.get("mint") == pos2["mint"]])
    with patch.object(M, "_paper_account", lambda: spy2), patch.object(M, "_demo_filler", lambda: ff), patch.object(M, "_exit_fee_venue", lambda p: None):
        r_ok = M._close_leg(pos2, "a", 0.001, "leg_a_2x")
    # marker events (account_applied, lane_applied, applied, frozen) carry intent_id but NO mint field, so select by the intent id's mint segment too
    ev2 = [e for e in CI._read(CI.journal_path(lane))[0] if e.get("mint") == pos2["mint"] or (":%s:" % pos2["mint"]) in str(e.get("intent_id"))]
    check("%s retry after storage returns: SAME fill re-read (sale op ids %r, one distinct), leg done, ONE credit with the fill's gross (%r), intent journaled then account_applied (%r)"
          % (modname, len(ff.calls), [c[3] if len(c) > 3 else None for c in spy2.credits], [e.get("ev") for e in ev2]),
          r_ok > 0 and pos2["legs"]["a"]["done"] is True and len(set(ff.calls)) == 1 and len(ff.calls) == 2 and len(spy2.credits) == 1
          and abs(float(spy2.credits[0][2]) - 0.1) < 1e-12 and [e.get("ev") for e in ev2][:2] == ["intent", "account_applied"])
    # JOURNAL-DERIVED RESUME. Only a sale that really landed (a journaled fill) is ever resumed; an unsold gate refusal never forces a sale.
    lm = "mirror" if modname == "mirror" else None
    ff5 = _FakeFiller(); spy5 = _SpyAccount(); spy5.max_price_impact_pct = lambda: 50.0
    pos5 = mkpos("FS" + modname[:3], lm); pos5["pending_exit"] = {"leg": "a", "why": "earlier_reason", "custom_field": 42}; pos5["peak_price"] = 7.0
    with patch.object(M, "_paper_account", lambda: spy5), patch.object(M, "_demo_filler", lambda: ff5), patch.object(M, "_exit_fee_venue", lambda p: None), \
         patch.object(CI, "_append", _no_intent_only):
        M._close_leg(pos5, "a", 0.001, "leg_a_2x")
    pe5 = pos5.get("pending_exit") or {}
    _fl5 = ff5.fills.get(M._exit_op(pos5, "a")) or {}
    check("%s hold keeps pre-existing pending fields (prior %r, peak_price %r), counts tries (%r) and the sale fill carries the exit reason in its own record (%r)"
          % (modname, (pe5.get("prior") or {}).get("custom_field"), pos5.get("peak_price"), pe5.get("tries"), (_fl5.get("intent") or {}).get("reason")),
          (pe5.get("prior") or {}).get("custom_field") == 42 and pos5.get("peak_price") == 7.0 and pe5.get("tries") == 1
          and (_fl5.get("intent") or {}).get("reason") == "leg_a_2x" and "resume" not in pe5)
    import json as _json
    pos5r = _json.loads(_json.dumps(pos5))   # persisted and reloaded: a restart
    with patch.object(M, "_paper_account", lambda: spy5), patch.object(M, "_demo_filler", lambda: ff5), patch.object(M, "_exit_fee_venue", lambda p: None):
        _rr = M._resume_pending_exit(pos5r)   # NO price trigger is evaluated anywhere in this call
    check("%s restart + price reversal: resume returns %r, leg a done with the journaled reason (%r), ONE credit (%d), SAME sale op id (%r), pending cleared (%r)"
          % (modname, _rr, pos5r["legs"]["a"].get("exit_reason"), len(spy5.credits), sorted(set(ff5.calls)), pos5r.get("pending_exit")),
          _rr == "resumed" and pos5r["legs"]["a"]["done"] is True and pos5r["legs"]["a"].get("exit_reason") == "leg_a_2x" and len(spy5.credits) == 1
          and len(set(ff5.calls)) == 1 and "pending_exit" not in pos5r)
    check("%s resume with nothing pending/journaled is a no-op (None)" % modname, M._resume_pending_exit(pos5r) is None)
    # CRASH BEFORE the hold was persisted: the position on disk shows leg a open with NO pending_exit at all, the sale fill is in the journal
    ff6 = _FakeFiller(); spy6 = _SpyAccount(); spy6.max_price_impact_pct = lambda: 50.0
    pos6 = mkpos("FG" + modname[:3], lm); _op6 = M._exit_op(pos6, "a")
    ff6.fills[_op6] = {"status": "filled", "price_usd": 0.001, "gross_usd": 0.1, "fee_usd": 0.001, "execution_source": "canned_fixture",
                       "signature": "fixture-sig", "provenance": {"fixture": True}, "intent": {"reason": "leg_a_2x", "pkind": None, "key": "a"}}
    with patch.object(M, "_paper_account", lambda: spy6), patch.object(M, "_demo_filler", lambda: ff6), patch.object(M, "_exit_fee_venue", lambda p: None):
        _r6 = M._resume_pending_exit(pos6)
    check("%s crash before the hold persisted (ghost fill, no pending_exit): poll-start reconciliation settles it once (%r, credits %d, leg done %r)"
          % (modname, _r6, len(spy6.credits), pos6["legs"]["a"]["done"]), _r6 == "resumed" and len(spy6.credits) == 1 and pos6["legs"]["a"]["done"] is True)
    # UNSOLD gate refusal must NOT force a stale sale after the price recovers
    ff7 = _FakeFiller(); spy7 = _SpyAccount(); spy7.max_price_impact_pct = lambda: 50.0
    pos7 = mkpos("FU" + modname[:3], lm)
    with patch.object(M, "_paper_account", lambda: spy7), patch.object(M, "_demo_filler", lambda: ff7), patch.object(M, "_exit_fee_venue", lambda p: None), \
         patch.object(CI, "_append", _no_write):
        M._close_leg(pos7, "a", 0.001, "hard_stop_-50pct"); M._close_leg(pos7, "a", 0.001, "hard_stop_-50pct"); M._close_leg(pos7, "a", 0.001, "hard_stop_-50pct")
    pe7 = pos7.get("pending_exit") or {}
    with patch.object(M, "_paper_account", lambda: spy7), patch.object(M, "_demo_filler", lambda: ff7), patch.object(M, "_exit_fee_venue", lambda p: None):
        _r7 = M._resume_pending_exit(pos7)   # storage is back and the price has recovered: nothing must sell
    check("%s gate refusal (x3, nothing sold): sale calls %d, resume %r (no sale forced), hold tries %r, operator_attention %r, risk_exit_blocked %r, leg still open %r"
          % (modname, len(ff7.calls), _r7, pe7.get("tries"), pe7.get("operator_attention"), pe7.get("risk_exit_blocked"), not pos7["legs"]["a"]["done"]),
          not ff7.calls and _r7 is None and pe7.get("tries") == 3 and pe7.get("operator_attention") is True and pe7.get("risk_exit_blocked") is True
          and pos7["legs"]["a"]["done"] is False and not spy7.credits)
    # an INFLIGHT sale claim is never re-executed by resume: held, visible, counted
    ff8 = _FakeFiller(); spy8 = _SpyAccount(); spy8.max_price_impact_pct = lambda: 50.0
    pos8 = mkpos("FI" + modname[:3], lm); ff8.fills[M._exit_op(pos8, "a")] = {"status": "inflight"}
    with patch.object(M, "_paper_account", lambda: spy8), patch.object(M, "_demo_filler", lambda: ff8):
        _r8 = M._resume_pending_exit(pos8); _r8b = M._resume_pending_exit(pos8)
    check("%s inflight claim: resume holds (%r, %r), no sale call (%d), why %r, tries %r" % (modname, _r8, _r8b, len(ff8.calls), (pos8.get("pending_exit") or {}).get("why"),
          (pos8.get("pending_exit") or {}).get("tries")),
          _r8 == "held" and _r8b == "held" and not ff8.calls and pos8["pending_exit"]["why"] == "unresolved_inflight" and pos8["pending_exit"]["tries"] == 2)
    # POSITION GENERATIONS: a journaled exit fill of an OLDER position (same lane and mint, other pa_pos_id) is never adopted, and the new generation sells fresh
    ffg = _FakeFiller(); spyg = _SpyAccount(); spyg.max_price_impact_pct = lambda: 50.0
    posg_old = mkpos("FN" + modname[:3], lm); posg_old["pa_pos_id"] = "lane/FN#1"
    posg = mkpos("FN" + modname[:3], lm); posg["pa_pos_id"] = "lane/FN#2"
    ffg.fills[M._exit_op(posg_old, "a")] = {"status": "filled", "price_usd": 9.0, "gross_usd": 900.0, "fee_usd": 1.0, "execution_source": "canned_fixture",
                                            "signature": "old-gen", "provenance": {}, "intent": {"reason": "old_reason"}}
    with patch.object(M, "_paper_account", lambda: spyg), patch.object(M, "_demo_filler", lambda: ffg), patch.object(M, "_exit_fee_venue", lambda p: None):
        _rg = M._resume_pending_exit(posg)
        _ng = M._close_leg(posg, "a", 0.001, "leg_a_2x")
    check("%s generations: the older position's fill is not adopted by resume (%r), the new generation sells under ITS OWN op id (%r != %r) and credits its own fill (gross %r)"
          % (modname, _rg, M._exit_op(posg, "a"), M._exit_op(posg_old, "a"), spyg.credits[0][2] if spyg.credits else None),
          _rg is None and M._exit_op(posg, "a") != M._exit_op(posg_old, "a") and len(spyg.credits) == 1 and abs(float(spyg.credits[0][2]) - 0.1) < 1e-12 and _ng > 0)
    # COMBINED REMAINDER goes through the sale too (mirror previously modelled it): one fill under the remainder key, one credit with the fill's gross
    ffr = _FakeFiller(); spyr = _SpyAccount(); spyr.max_price_impact_pct = lambda: 50.0
    posr = mkpos("FM" + modname[:3], lm)
    with patch.object(M, "_paper_account", lambda: spyr), patch.object(M, "_demo_filler", lambda: ffr), patch.object(M, "_exit_fee_venue", lambda p: None):
        _nr = M._close_remaining(posr, 0.0005, "hard_stop_-50pct")
    check("%s combined remainder is SOLD through the filler (calls %r), all legs done %r, ONE credit of the fill gross (%r), exit_execution recorded %r"
          % (modname, ffr.calls, all(v["done"] for v in posr["legs"].values()), [c[2] for c in spyr.credits], posr["legs"]["a"].get("exit_execution", {}).get("source")),
          ffr.calls == [M._exit_op(posr, "remaining:abc")] and all(v["done"] for v in posr["legs"].values()) and len(spyr.credits) == 1
          and abs(float(spyr.credits[0][2]) - 0.1) < 1e-12 and posr["legs"]["a"].get("exit_execution", {}).get("source") == "canned_fixture" and _nr > 0)
    # an UNREADABLE fill journal fails closed: held, no sale
    class _BadJournal(_FakeFiller):
        def get_fill(self, op_id): raise OSError("INJECTED: fill journal unreadable")
        def fills_by_prefix(self, prefix, suffix=""): raise OSError("INJECTED: fill journal unreadable")
    ffu = _BadJournal(); posu = mkpos("FJ" + modname[:3], lm)
    with patch.object(M, "_paper_account", lambda: spyr), patch.object(M, "_demo_filler", lambda: ffu):
        _ru = M._resume_pending_exit(posu)
    check("%s unreadable fill journal: resume holds (%r, why %r), no sale (%d)" % (modname, _ru, (posu.get("pending_exit") or {}).get("why"), len(ffu.calls)),
          _ru == "held" and posu["pending_exit"]["why"] == "unresolved_fill_journal" and not ffu.calls)

    # ---- v11/v12: prefix enumeration, overlap, drift, unknown, legacy quarantine, hold history, trail classification
    def _filled(reason="leg_a_2x"):
        return {"status": "filled", "price_usd": 0.001, "gross_usd": 0.1, "fee_usd": 0.001, "execution_source": "canned_fixture", "signature": "s",
                "provenance": {}, "intent": {"reason": reason}}
    def _resume_with(pos, fills):
        ffx = _FakeFiller(); ffx.fills.update(fills); spx = _SpyAccount(); spx.max_price_impact_pct = lambda: 50.0
        with patch.object(M, "_paper_account", lambda: spx), patch.object(M, "_demo_filler", lambda: ffx), patch.object(M, "_exit_fee_venue", lambda p: None):
            return M._resume_pending_exit(pos), ffx, spx
    # OVERLAP: a filled leg-a record AND a filled remaining record, both unbooked: never first-match, held
    po = mkpos("FO" + modname[:3], lm)
    _r, _f, _s = _resume_with(po, {M._exit_op(po, "a"): _filled(), M._exit_op(po, "remaining:abc"): _filled("hard_stop_-50pct")})
    check("%s overlap (two unbooked filled records): held %r why %r, nothing credited (%d), no sale (%d)" % (modname, _r, (po.get("pending_exit") or {}).get("why"), len(_s.credits), len(_f.calls)),
          _r == "held" and po["pending_exit"]["why"] == "unresolved_exit_overlap" and not _s.credits and not _f.calls)
    # KEY DRIFT: a filled remainder for legs ab while only c ... legs now differ (a already done): held, not adopted
    pd = mkpos("FD" + modname[:3], lm); pd["legs"]["a"]["done"] = True
    _r, _f, _s = _resume_with(pd, {M._exit_op(pd, "remaining:abc"): _filled("hard_stop_-50pct")})
    check("%s key drift (filled remaining:abc but leg a is already booked): held %r why %r, no credit" % (modname, _r, (pd.get("pending_exit") or {}).get("why")),
          _r == "held" and pd["pending_exit"]["why"] == "unresolved_key_drift" and not _s.credits)
    # UNKNOWN record: held, never sold
    pu = mkpos("FK" + modname[:3], lm)
    _r, _f, _s = _resume_with(pu, {M._exit_op(pu, "a"): {"status": "unknown"}})
    check("%s unknown record: held %r why %r, no sale" % (modname, _r, (pu.get("pending_exit") or {}).get("why")),
          _r == "held" and pu["pending_exit"]["why"] == "unresolved_unknown" and not _f.calls)
    # SENSITIVITY control: exactly one unbooked filled record is settled once
    p1 = mkpos("F1" + modname[:3], lm)
    _r, _f, _s = _resume_with(p1, {M._exit_op(p1, "a"): _filled()})
    check("%s control: a single unbooked filled record settles (%r, credits %d)" % (modname, _r, len(_s.credits)), _r == "resumed" and len(_s.credits) == 1)
    # LEGACY (suffix-less) exit record: quarantined, never adopted, never read as absence
    pl = mkpos("FL" + modname[:3], lm); pl["entry_ts"] = 1000.0
    _leg_id = M._exit_op(pl, "a")[:-len(":%s" % pl["pa_pos_id"])]
    _r, _f, _s = _resume_with(pl, {_leg_id: _filled()})
    check("%s legacy suffix-less filled record with no ts: held %r why %r, not adopted (credits %d), no sale (%d)" % (modname, _r, (pl.get("pending_exit") or {}).get("why"), len(_s.credits), len(_f.calls)),
          _r == "held" and pl["pending_exit"]["why"] == "unresolved_legacy_exit_key" and not _s.credits and not _f.calls)
    pl2 = mkpos("FM2" + modname[:3], lm); pl2["entry_ts"] = 1000.0
    _old = _filled(); _old["ts"] = 10.0
    _r, _f, _s = _resume_with(pl2, {M._exit_op(pl2, "a")[:-len(":%s" % pl2["pa_pos_id"])]: _old})
    check("%s control: a legacy record provably OLDER than the entry is ignored (%r)" % (modname, _r), _r is None)
    # HOLD HISTORY reaches the close record with qualification_excluded
    ph = mkpos("FH" + modname[:3], lm)
    ph["symbol"] = "T"; ph["source"] = "t"; ph["entry_ts"] = 1.0; ph["entry_price"] = 0.001; ph["entry_features"] = {}
    _f0 = _FakeFiller(); _s0 = _SpyAccount(); _s0.max_price_impact_pct = lambda: 50.0
    with patch.object(M, "_paper_account", lambda: _s0), patch.object(M, "_demo_filler", lambda: _f0), patch.object(M, "_exit_fee_venue", lambda p: None), \
         patch.object(CI, "_append", _no_write):
        M._close_leg(ph, "a", 0.001, "hard_stop_-50pct")
    check("%s hold history recorded (%r)" % (modname, [h.get("why") for h in ph.get("exit_hold_history", [])]), ph.get("exit_hold_history") and ph["exit_hold_history"][-1]["qualification_excluded"] is True)
    for _k in ph["legs"]: ph["legs"][_k]["done"] = True; ph["legs"][_k]["exit_price"] = 0.001; ph["legs"][_k]["exit_ts"] = 2.0
    dh = {"positions": {ph["mint"]: ph}, "closes": [], "bankroll": 10.0, "state": "running"}
    with patch.object(M, "_write", lambda dd: True), patch.object(M, "_recover_close_intents", lambda: None):
        try:
            rech = M._finalize(dh, ph["mint"], ph, "ladder_complete")
        except Exception as _e:
            rech = {"EXC": repr(_e)}
    check("%s close record carries exit_hold_history and qualification_excluded (%r)" % (modname, (rech or {}).get("qualification_excluded") if isinstance(rech, dict) else rech),
          isinstance(rech, dict) and rech.get("qualification_excluded") is True and rech.get("exit_hold_history"))
    # TRAIL classification: a held trail exit is a RISK exit
    pt = mkpos("FT" + modname[:3], lm)
    with patch.object(M, "_paper_account", lambda: _s0), patch.object(M, "_demo_filler", lambda: _f0), patch.object(CI, "_append", _no_write):
        M._close_leg(pt, "a", 0.001, "trail_stop_-30pct")
    check("%s held trail exit is risk_exit_blocked (%r)" % (modname, (pt.get("pending_exit") or {}).get("risk_exit_blocked")), (pt.get("pending_exit") or {}).get("risk_exit_blocked") is True)
    # an exception that is NOT IntentNotDurable still propagates (the rollback is narrow, never a blanket catch)
    pos3 = mkpos("FX" + modname[:3], "mirror" if modname == "mirror" else None)
    if modname == "mirror":
        pos3["lane"] = "mirror"
    class _Boom(Exception):
        pass
    def _boom_filler():
        raise _Boom("x")
    with patch.object(M, "_paper_account", lambda: spy2), patch.object(M, "_demo_filler", _boom_filler), patch.object(M, "_exit_fee_venue", lambda p: None):
        try:
            M._close_leg(pos3, "a", 0.001, "leg_a_2x"); _prop = False
        except _Boom:
            _prop = True
    check("%s: an unrelated exception still propagates out of the guarded close (rollback is narrow)" % modname, _prop)

    # _finalize must NEVER book a close for a position with a leg still pending (the 0.0 return of a held exit cannot drop the position)
    pos4 = mkpos("FF" + modname[:3], "mirror" if modname == "mirror" else None)
    pos4["symbol"] = "T"; pos4["source"] = "t"; pos4["entry_ts"] = 1.0; pos4["entry_price"] = 0.001; pos4["entry_features"] = {}
    d4 = {"positions": {pos4["mint"]: pos4}, "closes": [], "bankroll": 10.0, "state": "running"}
    wr = []
    with patch.object(M, "_write", lambda dd: wr.append(1)), patch.object(M, "_recover_close_intents", lambda: None):
        rec4 = M._finalize(d4, pos4["mint"], pos4, "ladder_complete")
    check("%s: _finalize with legs not done books nothing (rec %r, still open %r, closes %d, bankroll %r, pending_exit %r)"
          % (modname, rec4, pos4["mint"] in d4["positions"], len(d4["closes"]), d4["bankroll"], (pos4.get("pending_exit") or {}).get("why")),
          rec4 is None and pos4["mint"] in d4["positions"] and not d4["closes"] and d4["bankroll"] == 10.0 and (pos4.get("pending_exit") or {}).get("why"))
    cl4 = []
    M._append_close(cl4, rec4); M._append_close(cl4, {"x": 1})
    check("%s: _append_close skips None and keeps a real record" % modname, cl4 == [{"x": 1}])

print("fails: %d" % FAILS)
sys.exit(1 if FAILS else 0)
