#!/usr/bin/env python3
"""Ticker-saturation tests (no network: _fetch_page is monkeypatched).

Covers saturation.py's display rule (thresholds + ordinals), the
watermark delta counter (cold start, date bucketing, boundary
dedupe, incremental refresh), fail-soft behavior, and the
card_design saturation line.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

import saturation as S
from card_design import build_card

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---- display rule (pure) ----
check("sat line: count 0 -> ''", S.format_line(0, "PEPE") == "")
check("sat line: count 1 -> ''", S.format_line(1, "PEPE") == "")
check("sat line: count 2 plain",
      S.format_line(2, "PEPE") == "🎭 2 coins named $PEPE today")
check("sat line: count 4 plain",
      S.format_line(4, "pepe") == "🎭 4 coins named $PEPE today")
check("sat line: count 5 farmed",
      S.format_line(5, "PEPE")
      == "🎭 5th $PEPE today — narrative getting farmed")
for n, want in ((11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"),
                (22, "22nd"), (23, "23rd"), (101, "101st"),
                (111, "111th")):
    check("sat line: ordinal %d" % n, S._ordinal(n) == want)
check("sat line: $-prefixed symbol normalized",
      S.format_line(3, "$pepe") == "🎭 3 coins named $PEPE today")
check("sat line: blank symbol -> ''", S.format_line(9, "  ") == "")

# ---- card integration ----
card = build_card(symbol="PEPE", score=70, mint="MINTX",
                  saturation=S.format_line(7, "PEPE"))
check("card: saturation line present exactly once",
      card.count("🎭 7th $PEPE today") == 1)
card = build_card(symbol="PEPE", score=70, mint="MINTX")
check("card: no saturation param -> no line", "🎭" not in card)

# ---- counter with a scripted firehose ----
_tmp = tempfile.mkdtemp(prefix="sat-test-")
_orig_fetch = S._fetch_page
_orig_path = S.STATE_PATH
_orig_interval = S.REFRESH_MIN_INTERVAL_S
_orig_recorder = S.RECORDER_DIR
_empty_rec = tempfile.mkdtemp(prefix="sat-test-norec-")
S.RECORDER_DIR = _empty_rec  # Phase 3: no local census either
S.STATE_PATH = os.path.join(_tmp, "ticker_saturation.json")

NOW = int(time.time() * 1000)


def _coin(sym, ts, mint):
    return {"symbol": sym, "created_timestamp": ts, "mint": mint}


# Phase 1 (cold start): page 1 full (100), page 2 short (5).
page1 = []
for i in range(100):
    page1.append(_coin("F%03d" % i, NOW - 1000 - i * 1000,
                       "FILLMINT%03d" % i))
page1[10] = _coin("pepe", NOW - 11000, "PEPE1")
page1[20] = _coin("$PEPE", NOW - 21000, "PEPE2")
page1[30] = _coin("Pepe", NOW - 31000, "PEPE3")
page1[40] = _coin("DOGE", NOW - 41000, "DOGE1")
page1[0] = _coin("SOLX", NOW - 1000, "WMINT")  # newest = watermark coin
page2 = [_coin("F100", NOW - 101000, "FILLMINT100"),
         _coin("F101", NOW - 102000, "FILLMINT101"),
         _coin("PEPE", NOW - 86400000 - 5000, "PEPEOLD"),  # yesterday
         _coin("F102", NOW - 103000, "FILLMINT102"),
         _coin("F103", NOW - 104000, "FILLMINT103")]
_script = {0: page1, 100: page2}
S._fetch_page = lambda offset, limit: _script.get(offset, [])
S._cache.clear()
S._last_fail = 0.0

check("sat counter: cold start counts today's PEPE variants",
      S.today_count("PEPE") == 3)
check("sat counter: DOGE counted once", S.today_count("doge") == 1)
check("sat counter: line for 3 is the plain form",
      S.saturation_line("PEPE") == "🎭 3 coins named $PEPE today")
check("sat counter: line for 1 is ''", S.saturation_line("DOGE") == "")
_state = S._load_state()
check("sat counter: yesterday's PEPE kept as a timestamp, "
      "not in today's count",
      (NOW - 86400000 - 5000) in (_state["launches"].get("PEPE") or [])
      and S.today_count("PEPE") == 3)
check("sat counter: watermark at newest coin",
      _state["wm_ts"] == NOW - 1000 and _state["wm_mints"] == ["WMINT"])

# Phase 2 (delta): two fresh PEPE + the watermark coin replayed.
page3 = [_coin("PEPE", NOW + 121000, "PEPE4"),
         _coin("PEPE", NOW + 120000, "PEPE5"),
         _coin("SOLX", NOW - 1000, "WMINT")]  # boundary replay: dedupe
_script = {0: page3}
S.REFRESH_MIN_INTERVAL_S = 0
S._cache.clear()
check("sat counter: delta adds exactly the 2 new PEPE",
      S.today_count("PEPE") == 5)
check("sat counter: boundary coin not double-counted",
      S.today_count("SOLX") == 1)
check("sat counter: 5 flips the line to the farmed form",
      S.saturation_line("PEPE")
      == "🎭 5th $PEPE today — narrative getting farmed")

# Phase 3 (fail-soft): API dark -> stale counts still served, no raise.
S._fetch_page = lambda offset, limit: None
S._cache.clear()
check("sat counter: fetch failure serves stale count",
      S.today_count("PEPE") == 5)

def _boom(offset, limit):
    raise RuntimeError("boom")
S._fetch_page = _boom
S._cache.clear()
S._last_fail = 0.0
check("sat counter: fetch explosion never raises",
      S.today_count("PEPE") == 5 and S.saturation_line("ZZZ") == "")

# Cold + dark: no state at all and the API is down -> 0 / "".
os.remove(S.STATE_PATH)
S._fetch_page = lambda offset, limit: None
S._cache.clear()
S._last_fail = 0.0
check("sat counter: cold and dark -> 0", S.today_count("PEPE") == 0)
check("sat counter: cold and dark -> ''",
      S.saturation_line("PEPE") == "")

# Phase 4 (recorder fallback): API dark, but the local P4 recorder
# birth log has the census — the tally counts from it instead of
# freezing. Fixture recorder dir with today's births.
_rec_dir = tempfile.mkdtemp(prefix="sat-test-rec-")
_today = time.strftime("%Y%m%d", time.gmtime(time.time()))
def _birth_line(sym, ago_s, mint):
    # Compact separators, exactly like the real P4 recorder writes.
    return json.dumps({"ts": time.time() - ago_s, "kind": "birth",
                       "mint": mint, "symbol": sym,
                       "created_ts": (NOW / 1000.0) - ago_s,
                       "source": "pumpportal-new"},
                      separators=(",", ":")) + "\n"
with open(os.path.join(_rec_dir, "events-%s.jsonl" % _today), "w") as f:
    f.write(_birth_line("PEPE", 100, "RPEPE1"))
    f.write(_birth_line("pepe", 200, "RPEPE2"))
    f.write(_birth_line("$PEPE", 300, "RPEPE3"))
    f.write(_birth_line("DOGE", 400, "RDOGE1"))
    f.write(_birth_line("SOLX", 50, "RWMINT"))  # newest = watermark coin
S.RECORDER_DIR = _rec_dir
S._fetch_page = lambda offset, limit: None
S._cache.clear()
S._last_fail = 0.0
S.REFRESH_MIN_INTERVAL_S = 0
check("sat counter: recorder fallback counts today's PEPE variants",
      S.today_count("PEPE") == 3)
check("sat counter: recorder fallback DOGE counted once",
      S.today_count("doge") == 1)
_state = S._load_state()
check("sat counter: recorder fallback watermark at newest birth",
      abs(_state["wm_ts"] - (NOW - 50000)) <= 2
      and _state["wm_mints"] == ["RWMINT"])
check("sat counter: recorder fallback state is fresh (not stale)",
      time.time() - _state["updated"] < 60)

# Phase 4b (recorder delta): two fresh PEPE + watermark replay.
with open(os.path.join(_rec_dir, "events-%s.jsonl" % _today), "a") as f:
    f.write(_birth_line("PEPE", -120, "RPEPE4"))
    f.write(_birth_line("PEPE", -119, "RPEPE5"))
    f.write(_birth_line("SOLX", 50, "RWMINT"))  # boundary replay
S._cache.clear()
S._last_fail = 0.0
check("sat counter: recorder delta adds exactly the 2 new PEPE",
      S.today_count("PEPE") == 5)
check("sat counter: recorder boundary coin not double-counted",
      S.today_count("SOLX") == 1)

# Phase 4c (recorder no-op): no new births — the pre-filter skips
# every line, but the scan still succeeded, so the tally is marked
# fresh and the counts stand.
_before = S._load_state()
S._cache.clear()
S._last_fail = 0.0
check("sat counter: recorder no-op keeps the count",
      S.today_count("PEPE") == 5)
_after = S._load_state()
check("sat counter: recorder no-op still marks the tally fresh",
      _after["updated"] >= _before["updated"]
      and _after["wm_ts"] == _before["wm_ts"])

# Phase 5 (rolling 24h window + 48h prune — 2026-11-08 window
# bug): stale mates must not count, the 24h boundary is exclusive,
# and a legacy days-format state migrates with a cold history.
_now_s = time.time()
_now_ms = int(_now_s * 1000)
_st = S._empty_state()
_st["launches"] = {
    "STALE": sorted([_now_ms - 25 * 3600 * 1000,
                     _now_ms - 30 * 3600 * 1000,
                     _now_ms - 47 * 3600 * 1000,
                     _now_ms - 3600 * 1000]),
    "OLD": [_now_ms - 49 * 3600 * 1000],
    "EDGE": sorted([_now_ms - 86400 * 1000,
                    _now_ms - 86400 * 1000 + 1000]),
    "AT24H": [_now_ms - 86400 * 1000],
    "IN1S": [_now_ms - 86400 * 1000 + 1000],
}
check("sat rolling: mates 25-47h old do not count in 24h window",
      S.rolling_count(_st, "STALE", 86400, _now_s) == 1)
check("sat rolling: mate at exactly 24h is OUTSIDE the window",
      S.rolling_count(_st, "AT24H", 86400, _now_s) == 0
      and S.rolling_count(_st, "EDGE", 86400, _now_s) == 1)
check("sat rolling: mate 1s inside 24h counts",
      S.rolling_count(_st, "IN1S", 86400, _now_s) == 1)
check("sat rolling: unknown ticker -> 0",
      S.rolling_count(_st, "NOPE", 86400, _now_s) == 0)
S._prune_launches(_st, _now_ms)
check("sat prune: 49h-old ticker dropped, 47h-old kept",
      "OLD" not in _st["launches"]
      and _st["launches"].get("STALE") == sorted(
          [_now_ms - 25 * 3600 * 1000, _now_ms - 30 * 3600 * 1000,
           _now_ms - 47 * 3600 * 1000, _now_ms - 3600 * 1000]))
check("sat prune: boundary ticker kept (24h mates inside 48h)",
      _st["launches"].get("EDGE") == sorted(
          [_now_ms - 86400 * 1000, _now_ms - 86400 * 1000 + 1000]))
# today_count on explicit day-boundary timestamps (no reliance on
# the wall-clock hour): 2 launches since UTC midnight, 1 just
# before it.
_day_start_ms = int(_now_s // 86400) * 86400000
_st2 = S._empty_state()
_st2["launches"] = {"TODAY": sorted([_day_start_ms + 1000,
                                     _now_ms - 1000]),
                    "YDAY": [_day_start_ms - 1000]}
_st2["updated"] = time.time()
S._save_state(_st2)
S._cache.clear()
S._last_fail = 0.0
check("sat today_count: only launches since UTC midnight count",
      S.today_count("TODAY") == 2 and S.today_count("YDAY") == 0)
# Legacy migration: a days-format state file loads with the
# watermark preserved and an empty timestamp history.
_leg = os.path.join(_tmp, "legacy.json")
with open(_leg, "w") as f:
    json.dump({"wm_ts": 1234567890000, "wm_mints": ["M1"],
               "days": {"2026-11-07": {"PEPE": 9}}, "gap_jumps": 2,
               "updated": _now_s}, f)
S.STATE_PATH = _leg
_mig = S._load_state()
check("sat migrate: legacy days state keeps watermark, cold launches",
      _mig["wm_ts"] == 1234567890000 and _mig["launches"] == {}
      and _mig["gap_jumps"] == 2 and "days" not in _mig)
S.STATE_PATH = os.path.join(_tmp, "ticker_saturation.json")

S._fetch_page = _orig_fetch
S.STATE_PATH = _orig_path
S.REFRESH_MIN_INTERVAL_S = _orig_interval
S.RECORDER_DIR = _orig_recorder

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL SATURATION TESTS PASSED")
