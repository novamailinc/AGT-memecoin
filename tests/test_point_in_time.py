#!/usr/bin/env python3
"""Point-in-time integrity tests (TradingAgents #7 stretch, 2026-11-17).

Covers daemon/launch_scanner/point_in_time.py and its wiring into the
two research scripts (scripts/bear-calibrate, scripts/ab-forward-report):

  * assert_training_before_forward: passes on clean windows, fails on
    training end == forward start AND training end > forward start,
    with both timestamps named in human-readable form; empty training
    set (None end) passes.
  * split_training_before_forward: exact training/excluded counts on a
    fixture straddling the window start — boundary close (== start)
    excluded, after-start excluded, undated excluded (fail-closed),
    before-start kept; no forward window recorded -> nothing excluded.
  * training_window: exact (earliest, latest, n); empty -> (None, None, 0).
  * load_forward_start: recorded / missing / malformed state file.
  * ensure_resolved_at: stamps where missing (explicit ts, record's
    own close_ts, record's ts), never overwrites an existing value,
    never mutates the input, never invents a timestamp.
  * verdict_resolved_at: the verdict chain's resolved_at source is the
    event's own ``ts`` (the live chain carries ts on 9,100/9,100 events
    as verified 2026-11-17); missing ts -> None.
  * resolved_at present on generated lessons: every lesson and the
    payload carry resolved_at == newest settled close_ts.
  * bear-calibrate wiring: fixture ledger + ab_forward.json — forward
    closes excluded with exact counts, training window reported,
    main() exits 0 and prints the window/exclusion lines; no forward
    file -> exit 0 with no exclusion.
  * ab-forward-report wiring: counted forward closes asserted inside
    the window; a pre-window or undated close in the counted set fails;
    main() on fixture ledgers exits 0 and prints the point-in-time
    check line.

No network, no live ledgers (all fixtures are in-memory or in temp
dirs). Plain script — run directly, not via pytest.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import lessons as L  # noqa: E402
from daemon.launch_scanner import point_in_time as PIT  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def _raises(fn):
    try:
        fn()
    except PIT.PointInTimeError:
        return True
    except Exception:
        return False
    return False


START = 1794925976.66  # the live forward window start, 2026-11-17 15:32:56 WAT

# ================= assert_training_before_forward =================
check("assert: clean window passes (training end < forward start)",
      PIT.assert_training_before_forward(START - 9527, START) is None)
check("assert: training end == forward start FAILS (boundary is forward)",
      _raises(lambda: PIT.assert_training_before_forward(START, START)))
check("assert: training end > forward start FAILS",
      _raises(lambda: PIT.assert_training_before_forward(START + 874,
                                                        START)))
check("assert: empty training set (None end) passes",
      PIT.assert_training_before_forward(None, START) is None)
try:
    PIT.assert_training_before_forward(START, START)
    _msg = ""
except PIT.PointInTimeError as e:
    _msg = str(e)
check("assert: failure message names both timestamps human-readably",
      "2026-11-17 15:32:56 WAT" in _msg and "epoch 1794925976.66" in _msg
      and "POINT-IN-TIME VIOLATION" in _msg)
check("assert: violation is a ValueError (fail loudly, catchable)",
      issubclass(PIT.PointInTimeError, ValueError))
check("fmt_ts: human-readable WAT + epoch",
      PIT.fmt_ts(START) == "2026-11-17 15:32:56 WAT (epoch 1794925976.66)")

# ================= split_training_before_forward =================
_closes = [
    {"mint": "BEFORE1", "close_ts": 1000.0},
    {"mint": "BEFORE2", "close_ts": 2000.0},
    {"mint": "BOUNDARY", "close_ts": 3000.0},   # == start: excluded
    {"mint": "AFTER", "close_ts": 4000.0},      # > start: excluded
    {"mint": "UNDATED"},                        # no close_ts: excluded
    {"mint": "UNDATED2", "close_ts": None},
]
_train, _excl = PIT.split_training_before_forward(_closes, 3000.0)
check("split: exact training count (2 before-start closes kept)",
      len(_train) == 2
      and [c["mint"] for c in _train] == ["BEFORE1", "BEFORE2"])
check("split: exact excluded count (boundary + after + 2 undated = 4)",
      len(_excl) == 4
      and sorted(c["mint"] for c in _excl)
      == ["AFTER", "BOUNDARY", "UNDATED", "UNDATED2"])
_train2, _excl2 = PIT.split_training_before_forward(_closes, None)
check("split: no forward window recorded -> nothing excluded",
      len(_train2) == 6 and len(_excl2) == 0)
check("training_window: exact (earliest, latest, n)",
      PIT.training_window(_train) == (1000.0, 2000.0, 2))
check("training_window: empty -> (None, None, 0)",
      PIT.training_window([]) == (None, None, 0)
      and PIT.training_window(_excl) == (3000.0, 4000.0, 2))

# ================= load_forward_start =================
_home = tempfile.mkdtemp(prefix="pit-state-")
with open(os.path.join(_home, "ab_forward.json"), "w") as f:
    json.dump({"started_ts": START,
               "started_wat": "2026-11-17 15:32:56 WAT"}, f)
check("load_forward_start: recorded start returned exactly",
      PIT.load_forward_start(_home) == START)
check("load_forward_start: missing file -> None",
      PIT.load_forward_start(tempfile.mkdtemp(prefix="pit-empty-")) is None)
_bad = tempfile.mkdtemp(prefix="pit-bad-")
with open(os.path.join(_bad, "ab_forward.json"), "w") as f:
    f.write("{not json")
check("load_forward_start: malformed file -> None (never raises)",
      PIT.load_forward_start(_bad) is None)

# ================= assert_forward_window =================
_counted = [
    {"mint": "F1", "entry_ts": START},
    {"mint": "F2", "entry_ts": START + 100.0},
]
check("forward-window assert: in-window counted closes pass, count returned",
      PIT.assert_forward_window(_counted, START, "entry_ts") == 2)
check("forward-window assert: pre-window close in counted set FAILS",
      _raises(lambda: PIT.assert_forward_window(
          _counted + [{"mint": "PRE", "entry_ts": START - 1.0}],
          START, "entry_ts")))
check("forward-window assert: undated close in counted set FAILS",
      _raises(lambda: PIT.assert_forward_window(
          _counted + [{"mint": "NODATE"}], START, "entry_ts")))
check("forward-window assert: empty counted set passes",
      PIT.assert_forward_window([], START, "entry_ts") == 0)

# ================= ensure_resolved_at / verdict_resolved_at =================
_rec = {"mint": "M", "close_ts": 5000.0}
_stamped = PIT.ensure_resolved_at(_rec, 6000.0)
check("ensure_resolved_at: stamps explicit ts where missing",
      _stamped["resolved_at"] == 6000.0)
check("ensure_resolved_at: never mutates the input record",
      "resolved_at" not in _rec)
check("ensure_resolved_at: derives from record close_ts when no arg",
      PIT.ensure_resolved_at({"mint": "M", "close_ts": 5000.0})
      ["resolved_at"] == 5000.0)
check("ensure_resolved_at: derives from record ts as fallback",
      PIT.ensure_resolved_at({"kind": "verdict", "ts": 7000.0})
      ["resolved_at"] == 7000.0)
check("ensure_resolved_at: never overwrites an existing resolved_at",
      PIT.ensure_resolved_at({"resolved_at": 1111.0, "close_ts": 5000.0})
      ["resolved_at"] == 1111.0)
check("ensure_resolved_at: no timestamp anywhere -> None, never invented",
      PIT.ensure_resolved_at({"mint": "M"})["resolved_at"] is None)
check("ensure_resolved_at: non-dict input returned unchanged",
      PIT.ensure_resolved_at("nope") == "nope")
check("verdict_resolved_at: event ts IS the resolved_at source",
      PIT.verdict_resolved_at({"kind": "verdict", "lane": "grad",
                               "ts": 1792746647.74}) == 1792746647.74)
check("verdict_resolved_at: checkpoint and close events carry ts too",
      PIT.verdict_resolved_at({"kind": "checkpoint", "ts": 123.0}) == 123.0
      and PIT.verdict_resolved_at({"kind": "close", "ts": 456.0}) == 456.0)
check("verdict_resolved_at: missing ts -> None",
      PIT.verdict_resolved_at({"kind": "verdict"}) is None
      and PIT.verdict_resolved_at("nope") is None)

# ================= resolved_at on generated lessons =================
def _dec(mint, entry_ts, close_ts, pnl):
    return {"kind": "decision", "lane": "grad_select", "mint": mint,
            "symbol": "T", "status": "settled", "entry_ts": entry_ts,
            "features": {"grad_age_s": 12.0,
                         "deployer_launches_24h": 0,
                         "ttg_s": 300.0, "curve_trades": 80,
                         "copycat_others_24h": 0,
                         "entry_price": 0.001, "mcap": 60000.0,
                         "liquidity": 5000.0},
            "lessons_in_context": [], "pnl_usd": pnl,
            "realized_mult": 1.0 + pnl / 25.0,
            "close_reason": "time_stop_24h", "close_ts": close_ts}


_gen_at = 1795000000.0
_payload = L.generate_lessons(
    "grad_select",
    [_dec("A", 1000.0, 2000.0, 10.0),
     _dec("B", 1100.0, 3000.0, -5.0),
     _dec("C", 1200.0, 2500.0, 20.0)],
    generated_at=_gen_at)
check("lessons: payload resolved_at == newest settled close_ts",
      _payload["resolved_at"] == 3000.0)
check("lessons: payload generated_at carried through",
      _payload["generated_at"] == _gen_at)
check("lessons: every generated lesson carries resolved_at",
      len(_payload["lessons"]) > 0
      and all(les.get("resolved_at") is not None
              for les in _payload["lessons"]))
check("lessons: no lesson claims a resolution after the newest close",
      all(les["resolved_at"] <= 3000.0 for les in _payload["lessons"]))
check("lessons: overall lesson resolved_at == newest settled close_ts",
      any(les["dimension"] == "overall"
          and les["resolved_at"] == 3000.0
          for les in _payload["lessons"]))
_empty = L.generate_lessons("grad_select", [])
check("lessons: no settled decisions -> resolved_at None, no lessons",
      _empty["resolved_at"] is None and _empty["lessons"] == [])

# ================= bear-calibrate wiring =================
from importlib.machinery import SourceFileLoader


def _load_script(name, filename):
    spec = importlib.util.spec_from_loader(
        name, SourceFileLoader(name, os.path.join(REPO, "scripts",
                                                  filename)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CAL = _load_script("bear_calibrate", "bear-calibrate")


def _cal_close(mint, pnl, close_ts):
    return {"mint": mint, "symbol": "T", "pnl_usd": pnl,
            "entry_ts": close_ts - 1000.0, "close_ts": close_ts,
            "closed_reason": "test",
            "entry_features": {"select": {
                "deployer_launches_24h": 0, "ticker_others_24h": 0,
                "ttg_s": 35.0, "curve_trades": 80,
                "grad_age_s": 10.0}}}


_cal_home = tempfile.mkdtemp(prefix="pit-cal-")
with open(os.path.join(_cal_home, "grad_select_ledger.json"), "w") as f:
    json.dump({"closes": [
        _cal_close("T1", 10.0, 1000.0),
        _cal_close("T2", -5.0, 2000.0),
        _cal_close("F1", 20.0, 3000.0),   # == forward start: excluded
        _cal_close("F2", -15.0, 4000.0),  # > forward start: excluded
    ]}, f)
with open(os.path.join(_cal_home, "ab_forward.json"), "w") as f:
    json.dump({"started_ts": 3000.0,
               "started_wat": "1970-01-01 00:50:00 WAT"}, f)
_res = CAL.calibrate_lane("grad_select", home=_cal_home)
check("bear-calibrate: forward closes excluded (2 of 4 stored)",
      _res["stored_closes"] == 4 and _res["excluded_forward"] == 2
      and _res["closes"] == 2)
check("bear-calibrate: training window is the pre-window closes only",
      _res["training_window"] == (1000.0, 2000.0, 2))
check("bear-calibrate: overall measured on training closes only",
      _res["overall"] == (2, 1, 5.0))
check("bear-calibrate: forward start recorded on the result",
      _res["forward_start_ts"] == 3000.0)
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _rc = CAL.main(["--home", _cal_home])
_out = _buf.getvalue()
check("bear-calibrate: main exits 0 with a forward window present",
      _rc == 0)
check("bear-calibrate: main prints training window + excluded count",
      "training window: earliest close" in _out
      and "latest close" in _out
      and "Excluded as forward-window closes: 2" in _out
      and "2 excluded as forward-window" in _out)
check("bear-calibrate: main prints the forward window start",
      "Forward window start:" in _out)
_no_fwd_home = tempfile.mkdtemp(prefix="pit-cal-nofwd-")
with open(os.path.join(_no_fwd_home, "grad_select_ledger.json"),
          "w") as f:
    json.dump({"closes": [_cal_close("T1", 10.0, 1000.0)]}, f)
_res2 = CAL.calibrate_lane("grad_select", home=_no_fwd_home)
check("bear-calibrate: no forward file -> nothing excluded",
      _res2["excluded_forward"] == 0 and _res2["closes"] == 1
      and _res2["forward_start_ts"] is None)
_buf2 = io.StringIO()
with contextlib.redirect_stdout(_buf2):
    _rc2 = CAL.main(["--home", _no_fwd_home])
check("bear-calibrate: main exits 0 with no forward file",
      _rc2 == 0 and "no ab_forward.json" in _buf2.getvalue())
check("bear-calibrate: training end at forward start is a violation",
      _raises(lambda: PIT.assert_training_before_forward(
          _res["training_window"][1], 2000.0, label="training set")))

# ================= ab-forward-report wiring =================
RPT = _load_script("ab_forward_report", "ab-forward-report")
_rpt_home = tempfile.mkdtemp(prefix="pit-rpt-")
_rpt_state = os.path.join(_rpt_home, "ab_forward.json")
with open(_rpt_state, "w") as f:
    json.dump({"started_ts": START,
               "started_wat": "2026-11-17 15:32:56 WAT",
               "verdict_closes_required": 100}, f)


def _rpt_close(mint, entry_ts, arm, pnl):
    return {"mint": mint, "symbol": "T", "entry_ts": entry_ts,
            "close_ts": entry_ts + 500.0, "pnl_usd": pnl,
            "entry_features": {"ab_arm": arm}}


_rpt_ledgers = {
    "grad_select": os.path.join(_rpt_home, "grad_select_ledger.json"),
    "grad_raw": os.path.join(_rpt_home, "grad_raw_ledger.json"),
}
with open(_rpt_ledgers["grad_select"], "w") as f:
    json.dump({"closes": [
        _rpt_close("F1", START + 10.0, "A", 5.0),
        _rpt_close("F2", START + 20.0, "A+B", -3.0),
        # Pre-window close: summarize must never count it.
        _rpt_close("PRE", START - 100.0, "A", 99.0),
    ]}, f)
with open(_rpt_ledgers["grad_raw"], "w") as f:
    json.dump({"closes": []}, f)
RPT.STATE_PATH = _rpt_state
RPT.LEDGERS = _rpt_ledgers
_buf3 = io.StringIO()
with contextlib.redirect_stdout(_buf3):
    _rc3 = RPT.main()
_out3 = _buf3.getvalue()
check("ab-forward-report: main exits 0 on fixture ledgers",
      _rc3 == 0)
check("ab-forward-report: pre-window close never counted (total 2)",
      "A 1 closes" in _out3 and "A+B 1 closes" in _out3)
check("ab-forward-report: point-in-time check line printed with OK",
      "point-in-time check: 2 forward closes counted" in _out3
      and ">= window start — OK" in _out3)
check("ab-forward-report: summarize counts only entry_ts >= start",
      RPT.summarize([_rpt_close("F1", START, "A", 1.0),
                     _rpt_close("PRE", START - 1.0, "A", 1.0)],
                    START)["total"] == 1)
# Zero forward closes: still exit 0, check line says so.
with open(_rpt_ledgers["grad_select"], "w") as f:
    json.dump({"closes": [_rpt_close("PRE", START - 100.0, "A", 99.0)]}, f)
_buf4 = io.StringIO()
with contextlib.redirect_stdout(_buf4):
    _rc4 = RPT.main()
check("ab-forward-report: 0 forward closes still exits 0 with check line",
      _rc4 == 0
      and "point-in-time check: 0 forward closes counted" in _buf4.getvalue())

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print("  FAIL " + f)
    sys.exit(1)
print("ALL POINT-IN-TIME TESTS PASSED")
