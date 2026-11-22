#!/usr/bin/env python3
"""HL spot breakout watch tests (pure functions, synthetic data, no network).

Covers the 2026-10-18 breakout addition to scripts/hl-listing-watch:
spotMetaAndAssetCtxs parsing, breakout detection (volume + 24h move +
24h age + 24h card cooldown), tap callbacks (<=64 bytes, "hb" code),
breakout card text, and the shared 2/day listing+breakout budget.
"""
import os
import tempfile

HL = "/home/hatch/workspace/Forex-agent/Forex-agent-main/scripts/hl-listing-watch"


def load(path, modname):
    src = open(path).read()
    ns = {"__name__": modname, "__file__": path}
    exec(compile(src.split('if __name__ == "__main__":')[0], modname, "exec"),
         ns)
    return ns


hl = load(HL, "hl_breakout_test")

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- parse_spot_ctxs ----------------
def ctx_resp():
    return [
        {"universe": [
            {"index": 0, "name": "PURR/USDC"},
            {"index": 1, "name": "HYPE/USDC"},
            {"index": 2, "name": "BROKE/USDC"},
        ]},
        [
            {"coin": "PURR", "markPx": "1.25", "prevDayPx": "1.00",
             "dayNtlVlm": "75000.0"},
            {"coin": "HYPE", "markPx": "43.0", "prevDayPx": "42.0",
             "dayNtlVlm": "5000000.0"},
            {"coin": "BROKE", "markPx": "0.5", "prevDayPx": "1.0",
             "dayNtlVlm": "10.0"},
        ],
    ]


parsed = hl["parse_spot_ctxs"](ctx_resp())
check("parse: 3 entries", len(parsed) == 3)
check("parse: PURR fields", parsed[0]["pair"] == "PURR/USDC"
      and parsed[0]["day_vol"] == 75000.0
      and parsed[0]["mark_px"] == 1.25
      and parsed[0]["prev_px"] == 1.00)
check("parse: garbage -> {}", hl["parse_spot_ctxs"](None) == {}
      and hl["parse_spot_ctxs"]({"x": 1}) == {}
      and hl["parse_spot_ctxs"]([]) == {})
check("parse: bad row skipped",
      hl["parse_spot_ctxs"]([{"universe": [{"index": "x", "name": "?"}]},
                             [{"markPx": "bad"}]]) == {})


def ctx_resp_at():
    # non-canonical "@N" names resolve via the token table
    return [
        {"universe": [
            {"index": 109, "name": "@109", "tokens": [60, 0]},
        ],
         "tokens": [
            {"index": 0, "name": "USDC"},
            {"index": 60, "name": "HYPE"},
        ]},
        [
            {"coin": "@109", "markPx": "43.0", "prevDayPx": "34.0",
             "dayNtlVlm": "34300000.0"},
        ],
    ]


parsed_at = hl["parse_spot_ctxs"](ctx_resp_at())
check("parse: @N name resolved", parsed_at[109]["pair"] == "HYPE/USDC")
check("parse: @N keeps volume", parsed_at[109]["day_vol"] == 34300000.0)


# ---------------- detect_breakouts ----------------
NOW = 1792400000.0
DAY = 86400


def info(vol=75000.0, mark=1.25, prev=1.00):
    return {"pair": "PURR/USDC", "day_vol": vol, "mark_px": mark,
            "prev_px": prev}


def bstate(first_days_ago=2, card_days_ago=None):
    st = {"first_seen": NOW - first_days_ago * DAY}
    if card_days_ago is not None:
        st["last_card"] = NOW - card_days_ago * DAY
    return {"0": st}


ctxs = {0: info()}
check("detect: real breakout fires",
      hl["detect_breakouts"](ctxs, bstate(), NOW) == [(0, info())])
check("detect: low volume skipped",
      hl["detect_breakouts"]({0: info(vol=49999.0)}, bstate(), NOW) == [])
check("detect: small move skipped",
      hl["detect_breakouts"]({0: info(mark=1.24)}, bstate(), NOW) == [])
check("detect: exactly 1.25 move fires",
      len(hl["detect_breakouts"]({0: info(mark=1.25, prev=1.0)},
                                 bstate(), NOW)) == 1)
check("detect: day-one pair skipped",
      hl["detect_breakouts"](ctxs, bstate(first_days_ago=0.5), NOW) == [])
check("detect: carded <24h ago skipped",
      hl["detect_breakouts"](ctxs, bstate(card_days_ago=0.5), NOW) == [])
check("detect: carded >24h ago fires again",
      len(hl["detect_breakouts"](ctxs, bstate(card_days_ago=2), NOW)) == 1)
check("detect: no history skipped",
      hl["detect_breakouts"](ctxs, {}, NOW) == [])
check("detect: zero prev price skipped",
      hl["detect_breakouts"]({0: info(prev=0)}, bstate(), NOW) == [])
check("detect: garbage never raises",
      hl["detect_breakouts"](None, None, NOW) == []
      and hl["detect_breakouts"]({0: {"pair": "?"}}, {}, NOW) == [])


# ---------------- tap callbacks ----------------
MINT = "HLPURRUSDC"
cb = hl["breakout_tap_callback"]("PURR/USDC")
check("breakout callback <=64 bytes", len(cb.encode()) <= 64)
check("breakout callback uses hb code", ":hb:" in cb)
check("breakout callback carries mint id", cb.endswith(MINT))

# signal_bus short code (single source of truth)
import sys as _sys
_sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/"
                     "daemon/launch_scanner")
from signal_bus import track_callback, TRACK_SOURCE_CODES
check("signal_bus has hb code", TRACK_SOURCE_CODES.get("hl-breakout") == "hb")
cb2 = track_callback("hyperliquid", "hl-breakout", MINT)
check("track_callback hb <=64 bytes", len(cb2.encode()) <= 64)
check("track_callback hb format",
      cb2 == "track:hyperliquid:hb:%s" % MINT)


# ---------------- card text ----------------
txt = hl["build_breakout_text"](0, info())
check("breakout card mentions pair", "PURR/USDC" in txt)
check("breakout card mentions move", "25%" in txt or "+25" in txt)
check("breakout card mentions volume", "75" in txt)
check("breakout card has safety line", "exchange-settled" in txt)


# ---------------- shared daily budget ----------------
tmp = tempfile.mkdtemp()
hl["DAILY_FILE"] = os.path.join(tmp, "hl_listing_daily.json")
left, daily = hl["daily_budget_left"]()
check("budget starts at 2", left == 2)
hl["bump_daily"](daily)  # a listing card spends from the same budget
left2, daily2 = hl["daily_budget_left"]()
check("budget shared: listing spend counts", left2 == 1)
hl["bump_daily"](daily2)  # a breakout card spends too
left3, _ = hl["daily_budget_left"]()
check("budget shared: breakout spend counts", left3 == 0)


# ---------------- listing_id hygiene ----------------
check("listing_id alphanumeric",
      hl["listing_id"]("PURR/USDC") == "HLPURRUSDC")
check("listing_id no colon", ":" not in hl["listing_id"]("A/B:C"))


print()
if fails:
    print("FAILURES:", fails)
    raise SystemExit(1)
print("all hl-breakout tests passed")
