#!/usr/bin/env python3
"""GeckoTerminal fallback tape (2026-10-21): when DexScreener is dark,
the tape (runner gates, entry quotes, rt_sync, rescore) must still be
served from GeckoTerminal with the SAME dict shape — and when both are
dark the behavior must be identical to today (skip, naming both).

Plain-script test style like the other suite files (sys.exit at the
end); run directly, not under pytest collection.
"""
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))))

from daemon.launch_scanner import sources as S

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("ok   %s" % name)
    else:
        print("FAIL %s %s" % (name, detail))
        FAILS.append(name)


GT_FIXTURE = {
    "data": [
        {"id": "solana_ABCm1",
         "type": "token",
         "attributes": {
             "address": "ABCm1",
             "name": "Test Coin",
             "symbol": "TST",
             "decimals": 6,
             "price_usd": "0.007474",
             "fdv_usd": "7474000",
             "market_cap_usd": "7474000",
             "total_reserve_in_usd": "320878.5",
             "price_change_percentage": {"m5": "3.2", "h1": "-1.1",
                                         "h24": "45.0"},
         }},
        {"id": "solana_XYZm2",
         "type": "token",
         "attributes": {
             "address": "XYZm2",
             "name": "Partial Coin",
             "symbol": "PRT",
             # no price_change_percentage at all: unknown momentum
             "price_usd": "0.0001",
             "market_cap_usd": "50000",
             "total_reserve_in_usd": "26000",
         }},
    ]
}

DS_TAPE = {"usd_liquidity": 100_000.0, "usd_market_cap": 500_000.0,
           "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40,
           "price_usd": 0.01, "tape_source": "dexscreener"}


def _reset_gt_state():
    S._GT_LAST_CALL = 0.0
    S._GT_DS_FAILS = 0
    S._GT_DS_DARK_UNTIL = 0.0


def _patch_throttle(used=0):
    import time as _t
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gt_throttle.json")
    with open(path, "w") as f:
        json.dump({"win": _t.time(), "used": used}, f)
    return patch.object(S, "_gt_throttle_path", return_value=path), tmp


# --- GT token -> tape shape -------------------------------------------

_reset_gt_state()
st = S._gt_token_to_stats(GT_FIXTURE["data"][0], "solana", "solana")
check("gt: shape keys present",
      all(k in st for k in ("price_usd", "usd_liquidity", "usd_market_cap",
                            "chg_m5", "chg_h1", "buys_m5", "sells_m5",
                            "vol_m5", "vol_h1", "tape_source")))
check("gt: values mapped",
      abs(st["price_usd"] - 0.007474) < 1e-9
      and abs(st["usd_liquidity"] - 320878.5) < 1e-6
      and abs(st["usd_market_cap"] - 7_474_000) < 1e-3
      and st["chg_m5"] == 3.2 and st["chg_h1"] == -1.1
      and st["symbol"] == "TST" and st["tape_source"] == "geckoterminal")
check("gt: unknown txn fields are None, never 0",
      st["buys_m5"] is None and st["sells_m5"] is None
      and st["vol_m5"] is None and st["buys_h1"] is None
      and st["sells_h1"] is None and st["vol_h1"] is None)

st2 = S._gt_token_to_stats(GT_FIXTURE["data"][1], "solana", "solana")
check("gt: missing momentum -> None (unknown, fail-closed upstream)",
      st2["chg_m5"] is None and st2["chg_h1"] is None
      and float(st2.get("chg_m5") or 0) == 0.0)  # gate read: blocked
check("gt: bad token -> None",
      S._gt_token_to_stats({"attributes": {}}, "solana", "solana") is None)
check("gt: non-token dict safe",
      S._gt_token_to_stats("junk", "solana", "solana") is None)

# --- geckoterminal_token_stats: parsing + address matching ------------

_reset_gt_state()
tp, tmp = _patch_throttle()
with tp, patch.object(S, "_gt_get_json", return_value=GT_FIXTURE):
    got = S.geckoterminal_token_stats(["ABCm1", "nope"], chain="solana")
check("gt batch: indexed mint returned, unknown absent",
      "ABCm1" in got and "nope" not in got)
check("gt batch: per-mint key preserved",
      got.get("ABCm1", {}).get("tape_source") == "geckoterminal")

with tp, patch.object(S, "_gt_get_json", return_value={"data": "junk"}):
    check("gt batch: malformed payload -> {}",
          S.geckoterminal_token_stats(["ABCm1"]) == {})
with tp, patch.object(S, "_gt_get_json", return_value=None):
    check("gt batch: fetch failure -> {}",
          S.geckoterminal_token_stats(["ABCm1"]) == {})
check("gt batch: unknown chain -> {}",
      S.geckoterminal_token_stats(["ABCm1"], chain="hyperliquid") == {})
check("gt batch: empty mints -> {}",
      S.geckoterminal_token_stats([]) == {})

# budget spent -> skipped without fetching
tp2, tmp2 = _patch_throttle(used=25)
fetched = []
with tp2, patch.object(S, "_gt_get_json",
                       side_effect=lambda u: fetched.append(u) or GT_FIXTURE):
    check("gt batch: budget spent -> {} and no HTTP",
          S.geckoterminal_token_stats(["ABCm1"]) == {} and not fetched)

# --- tape_batch_stats: DS authoritative, GT fills gaps ---------------

_reset_gt_state()
with patch.object(S, "dexscreener_batch_stats",
                  return_value={"M1": dict(DS_TAPE)}), \
     patch.object(S, "geckoterminal_token_stats",
                  return_value={}) as mgt:
    out = S.tape_batch_stats(["M1", "M2"], chain="solana")
check("tape: DS hit authoritative; GT consulted only for missing mints",
      out["M1"]["tape_source"] == "dexscreener" and "M2" not in out
      and mgt.call_count == 1
      and mgt.call_args[0][0] == ["M2"],
      "mgt calls: %s" % getattr(mgt, "call_args_list", None))

_reset_gt_state()
with patch.object(S, "dexscreener_batch_stats", return_value={}), \
     patch.object(S, "_gt_get_json", return_value=GT_FIXTURE):
    tp3, _ = _patch_throttle()
    with tp3:
        out = S.tape_batch_stats(["ABCm1"], chain="solana")
check("tape: DS dark + GT up -> GT price served, source tagged",
      out.get("ABCm1", {}).get("price_usd") == 0.007474
      and out.get("ABCm1", {}).get("tape_source") == "geckoterminal",
      str(out.get("ABCm1")))

_reset_gt_state()
with patch.object(S, "dexscreener_batch_stats", return_value={}), \
     patch.object(S, "_gt_get_json", return_value=None):
    tp4, _ = _patch_throttle()
    with tp4:
        out = S.tape_batch_stats(["ABCm1"], chain="solana")
check("tape: both dark -> {} (identical to DS-alone behavior)",
      out == {})

# DS dark latch: 3 consecutive empty DS batches -> skip DS
_reset_gt_state()
ds_calls = []
with patch.object(S, "dexscreener_batch_stats",
                  side_effect=lambda m, chain="solana":
                  ds_calls.append(1) or {}), \
     patch.object(S, "_gt_get_json", return_value=None):
    tp5, _ = _patch_throttle()
    with tp5:
        for _ in range(3):
            S.tape_batch_stats(["M1"], chain="solana")
        S.tape_batch_stats(["M1"], chain="solana")
check("tape: dark latch skips DS after 3 empty batches",
      len(ds_calls) == 3, "ds called %d times" % len(ds_calls))

# --- DS dicts carry the source tag -------------------------------------

_reset_gt_state()
pair = {"baseToken": {"symbol": "X", "name": "X", "address": "m1"},
        "dexId": "raydium", "chainId": "solana", "pairAddress": "p1",
        "liquidity": {"usd": 50000}, "marketCap": 200000,
        "priceUsd": "0.02", "txns": {"m5": {"buys": 5, "sells": 2}},
        "volume": {"m5": 100}, "priceChange": {"m5": 1.0}}
ps = S._pair_to_stats(pair)
check("ds: tape_source tagged dexscreener",
      ps.get("tape_source") == "dexscreener")

# --- rt_sync: source lands on tape rows --------------------------------

from daemon.launch_scanner import rt_sync as R
tape = R.Tape()
ok = tape.update("M1", {"price_usd": 0.007474,
                        "usd_liquidity": 320878.5,
                        "usd_market_cap": 7_474_000.0,
                        "tape_source": "geckoterminal"}, now=1000.0)
check("rt: GT row lands on tape with source",
      ok and tape.rows["M1"]["tape_source"] == "geckoterminal"
      and tape.price("M1", 1001.0) == 0.007474)
ok = tape.update("M2", {"price_usd": 0.01, "tape_source": "dexscreener"},
                 now=1000.0)
check("rt: DS row keeps its source",
      ok and tape.rows["M2"]["tape_source"] == "dexscreener")
check("rt: unpriced GT row (price None) never lands",
      not tape.update("M3", {"price_usd": None,
                             "tape_source": "geckoterminal"}, now=1000.0)
      and "M3" not in tape.rows)

# --- gate semantics with GT-shaped tape --------------------------------
# (mirrors the modules' own reads: liq/mcap thresholds + rising check)
from daemon.launch_scanner import ceo as C


def _gate_ok(st):
    liq = float(st.get("usd_liquidity") or 0)
    mcap = float(st.get("usd_market_cap") or 0)
    chg = float(st.get("chg_m5") or 0)
    return liq >= 25_000 and mcap >= 50_000 and chg > 0


gt_runner = dict(S._gt_token_to_stats(GT_FIXTURE["data"][0],
                                      "solana", "solana"))
check("gate: GT runner-grade tape passes (liq/mcap/rising)",
      _gate_ok(gt_runner), str({k: gt_runner.get(k) for k in
                                ("usd_liquidity", "usd_market_cap",
                                 "chg_m5", "tape_source")}))
gt_flat = dict(S._gt_token_to_stats(GT_FIXTURE["data"][1],
                                    "solana", "solana"))
check("gate: GT tape with unknown momentum stays fail-closed",
      not _gate_ok(gt_flat), str(gt_flat.get("chg_m5")))
gt_thin = dict(gt_runner)
gt_thin["usd_liquidity"] = 1000.0
check("gate: GT thin-liq tape blocked",
      not _gate_ok(gt_thin))

# --- ceo/mirror gate + quote with DS down, GT up -----------------------

with patch.object(C, "_sources") as ms:
    ms.tape_batch_stats.return_value = {"GTM": dict(gt_runner)}
    st = C._fresh_tape("solana", "GTM")
check("ceo _fresh_tape: GT-served tape returned with source",
      st is not None and st.get("tape_source") == "geckoterminal"
      and C._LAST_TAPE_SOURCE == "geckoterminal")

from daemon.launch_scanner import mirror as M
with patch.object(M, "_sources") as ms:
    ms.tape_batch_stats.return_value = {"GTM": dict(gt_runner)}
    st = M._fresh_tape("solana", "GTM")
check("mirror _fresh_tape: GT-served tape returned with source",
      st is not None and st.get("tape_source") == "geckoterminal"
      and M._LAST_TAPE_SOURCE == "geckoterminal")

# Quote via the tape fallback at GT price, adverse fill intact.
with patch.object(C, "_http_json", return_value=None), \
     patch.object(C, "_fresh_tape", return_value=dict(gt_runner)), \
     patch.object(C, "_fills") as mf:
    mf.honest_entry.side_effect = lambda px, n, liq=None: px * 0.99
    got = C._quote_detail("solana", "GTM")
check("ceo quote: GT tape price served when DS pairs dark",
      abs(got[0] - 0.007474) < 1e-9 and "geckoterminal" in got[1],
      str(got))
with patch.object(M, "_http_json", return_value=None), \
     patch.object(M, "_fresh_tape", return_value=dict(gt_runner)):
    got = M._quote_detail("solana", "GTM")
check("mirror quote: GT tape price served when DS pairs dark",
      abs(got[0] - 0.007474) < 1e-9 and "geckoterminal" in got[1],
      str(got))

# Both dark -> skip, reason names what was tried.
with patch.object(C, "_http_json", return_value=None), \
     patch.object(C, "_fresh_tape", return_value=None), \
     patch.object(C, "time") as _t:
    _t.sleep.return_value = None
    got = C._quote_detail("solana", "GTM")
check("ceo quote: both dark -> 0.0 with a trail naming both attempts",
      got[0] == 0.0 and "pairs fetch" in got[1]
      and "tape fallback" in got[1], str(got))

# Gate confirmed-x2 carries the source in its log line.
with patch.object(C, "_fresh_tape", return_value=dict(gt_runner)), \
     patch.object(C, "RUNNER_CONFIRM_GAP_S", 0.0):
    ok, why = C._runner_gate("solana", "GTM")
check("ceo gate: confirmed via GT tape",
      ok and why == "", str(why))
with patch.object(M, "_fresh_tape", return_value=dict(gt_runner)), \
     patch.object(M, "RUNNER_CONFIRM_GAP_S", 0.0):
    ok, why = M._runner_gate("solana", "GTM")
check("mirror gate: confirmed via GT tape",
      ok and why == "", str(why))
with patch.object(C, "_fresh_tape",
                   side_effect=[dict(gt_runner), dict(gt_flat)]), \
     patch.object(C, "RUNNER_CONFIRM_GAP_S", 0.0):
    C._LAST_TAPE_SOURCE = "geckoterminal"  # what _fresh_tape journals
    ok, why = C._runner_gate("solana", "GTM")
check("ceo gate: GT tape degrades on 2nd read -> unconfirmed, source",
      not ok and "unconfirmed" in why and "geckoterminal" in why,
      str(why))
with patch.object(C, "_fresh_tape", return_value=dict(gt_flat)), \
     patch.object(C, "RUNNER_CONFIRM_GAP_S", 0.0):
    ok, why = C._runner_gate("solana", "GTM")
check("ceo gate: GT flat tape on 1st read -> plain runner-gate skip",
      not ok and "unconfirmed" not in why and "not rising" in why,
      str(why))

print()
if FAILS:
    print("%d FAILURES: %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("ALL TAPE-FALLBACK TESTS PASSED")
sys.exit(0)