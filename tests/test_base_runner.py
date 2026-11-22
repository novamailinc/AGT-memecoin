#!/usr/bin/env python3
"""Base runner adapter tests (pure functions, synthetic data, no network).

Covers the six stages of scripts/base-runner-feed: discovery parsing,
survival pre-filter (HIGHER bar than Solana), safety-callback wiring,
manipulation filters, positive-evidence gate, continuation check,
score/tier, tap-callback provenance, and the feed kill-switch hook.
"""
SCRIPT = "/home/hatch/workspace/Forex-agent/Forex-agent-main/scripts/base-runner-feed"
_src = open(SCRIPT).read()
_ns = {"__name__": "base_runner_test", "__file__": SCRIPT}
exec(compile(_src.split('if __name__ == "__main__":')[0], "base_runner", "exec"), _ns)

_parse_pools = _ns["_parse_pools"]
passes_prefilter = _ns["passes_prefilter"]
passes_manipulation = _ns["passes_manipulation"]
positive_evidence = _ns["positive_evidence"]
continuation_ok = _ns["continuation_ok"]
score_pool = _ns["score_pool"]
tap_callback = _ns["tap_callback"]
feed_live = _ns["feed_live"]

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


MINT = "0x" + "a" * 40


def txw(buys=0, sells=0, buyers=0, sellers=0):
    return {"buys": buys, "sells": sells, "buyers": buyers, "sellers": sellers}


def pool(**kw):
    p = {"mint": MINT, "name": "RUNR", "age_min": 25.0, "dex": "aerodrome",
         "mcap": 400_000.0, "fdv": 400_000.0, "liq": 50_000.0,
         "price_usd": 0.001,
         "tx": {"m5": txw(40, 8, 30, 5), "m15": txw(120, 30, 60, 20),
                "m30": txw(0, 0, 0, 0), "h1": txw(0, 0, 0, 0)},
         "vol": {"m5": 4000.0, "m15": 9000.0, "m30": 0.0, "h1": 24000.0},
         "chg": {"m5": 8.0, "m15": 20.0, "h1": 60.0}}
    for k, v in kw.items():
        p[k] = v
    return p


# ---- 1. discovery parsing ----
from datetime import datetime, timezone

now = datetime.now(timezone.utc)


def raw_pool(base="base_0x" + "a" * 40, quote="base_0x4200000000000000000000000000000000000006",
             dex="aerodrome", age_min=20):
    ts = (now.timestamp() - age_min * 60)
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return {"attributes": {
        "name": "RUNR / WETH", "pool_created_at": iso,
        "fdv_usd": "400000", "market_cap_usd": "400000",
        "reserve_in_usd": "50000", "base_token_price_usd": "0.001",
        "transactions": {"m5": {"buys": 40, "sells": 8, "buyers": 30, "sellers": 5},
                         "m15": {"buys": 120, "sells": 30, "buyers": 60, "sellers": 20},
                         "h1": {"buys": 300, "sells": 90}},
        "volume_usd": {"m5": "4000", "m15": "9000", "h1": "24000"},
        "price_change_percentage": {"m5": "8", "m15": "20", "h1": "60"}},
        "relationships": {
            "dex": {"data": {"id": dex}},
            "base_token": {"data": {"id": base}},
            "quote_token": {"data": {"id": quote}}}}


got = _parse_pools({"data": [raw_pool()]}, now)
check("parse extracts 0x mint", len(got) == 1 and got[0]["mint"] == MINT)
check("parse carries tx windows", got[0]["tx"]["m15"]["buyers"] == 60)
check("parse skips quote==base", _parse_pools({"data": [raw_pool(base="base_0x" + "b" * 40, quote="base_0x" + "b" * 40)]}, now) == [])
check("parse skips non-0x mint", _parse_pools({"data": [raw_pool(base="base_notanaddress")]}, now) == [])
check("parse skips unknown dex", _parse_pools({"data": [raw_pool(dex="random-dex")]}, now) == [])
check("parse accepts aerodrome-slipstream",
      len(_parse_pools({"data": [raw_pool(dex="aerodrome-slipstream")]}, now)) == 1)
check("parse accepts aerodrome-base",
      len(_parse_pools({"data": [raw_pool(dex="aerodrome-base")]}, now)) == 1)
check("parse accepts aerodrome-slipstream-3",
      len(_parse_pools({"data": [raw_pool(dex="aerodrome-slipstream-3")]}, now)) == 1)


# ---- 2. survival pre-filter (HIGHER bar than Solana) ----
ok, _ = passes_prefilter(pool())
check("good runner passes pre-filter", ok)
check("too young rejected", not passes_prefilter(pool(age_min=5))[0])
check("no max age on trending source: 30d-old surge pool passes age gate",
      passes_prefilter(pool(age_min=43_200))[0])
check("RUNNER-ONLY rule: liq floor is $25k",
      not passes_prefilter(pool(liq=24_999))[0]
      and passes_prefilter(pool(liq=25_001))[0])
check("mcap floor $50k", not passes_prefilter(pool(mcap=40_000))[0])
check("mcap cap $20M (trending runners run bigger)",
      not passes_prefilter(pool(mcap=25_000_000))[0]
      and passes_prefilter(pool(mcap=6_000_000))[0])
check("1-holder mass rejected",
      not passes_prefilter(pool(tx={"m5": txw(40, 8, 30, 5),
                                    "m15": txw(120, 30, 8, 6),
                                    "m30": txw(), "h1": txw()}))[0])
check("low h1 volume rejected", not passes_prefilter(pool(vol={"m5": 4000.0, "m15": 9000.0, "m30": 0.0, "h1": 4000.0}))[0])
check("dumping rejected", not passes_prefilter(pool(chg={"m5": 0.0, "m15": -40.0, "h1": 0.0}))[0])
check("m5 selling rejected",
      not passes_prefilter(pool(tx={"m5": txw(8, 40, 6, 30),
                                    "m15": txw(120, 30, 60, 20),
                                    "m30": txw(), "h1": txw()}))[0])


# ---- 3. manipulation filters ----
ok, _ = passes_manipulation(pool())
check("good runner passes manipulation", ok)
check("wash spam rejected",
      not passes_manipulation(pool(tx={"m5": txw(40, 8, 30, 5),
                                       "m15": txw(300, 30, 20, 20),
                                       "m30": txw(), "h1": txw()}))[0])
check("zero sells = manufactured",
      not passes_manipulation(pool(tx={"m5": txw(40, 0, 30, 0),
                                       "m15": txw(120, 0, 60, 0),
                                       "m30": txw(), "h1": txw()}))[0])
check("distribution rejected",
      not passes_manipulation(pool(tx={"m5": txw(40, 30, 30, 25),
                                       "m15": txw(120, 100, 60, 80),
                                       "m30": txw(), "h1": txw()}))[0])


# ---- 4. positive-evidence gate ----
ev = positive_evidence(pool())
check("runner shows evidence", len(ev) >= 1)
flat = pool(tx={"m5": txw(30, 25, 12, 10), "m15": txw(90, 75, 20, 18),
                "m30": txw(), "h1": txw()},
            vol={"m5": 2000.0, "m15": 9000.0, "m30": 0.0, "h1": 36000.0})
check("flat coin shows no evidence", positive_evidence(flat) == [])


# ---- 5. continuation check ----
ok, _ = continuation_ok(pool())
check("runner continuation ok", ok)
check("m5 buy share <60% rejected",
      not continuation_ok(pool(tx={"m5": txw(30, 30, 20, 20),
                                   "m15": txw(120, 30, 60, 20),
                                   "m30": txw(), "h1": txw()}))[0])
check("volume collapse rejected",
      not continuation_ok(pool(vol={"m5": 4000.0, "m15": 2000.0, "m30": 0.0, "h1": 24000.0}))[0])
check("distribution wick rejected",
      not continuation_ok(pool(chg={"m5": 0.0, "m15": -25.0, "h1": 0.0}))[0])


# ---- 6. score / tier ----
score, reasons = score_pool(pool(), ["volume accelerating"])
check("strong runner scores >= 60", score >= 60.0)
weak = pool(mcap=60_000.0,
            tx={"m5": txw(25, 12, 18, 10), "m15": txw(80, 40, 30, 22),
                "m30": txw(), "h1": txw()},
            vol={"m5": 3000.0, "m15": 9000.0, "m30": 0.0, "h1": 36000.0})
score_w, _ = score_pool(weak, ["buy share rising"])
check("weak runner scores < 60 (higher bar than Solana 45)",
      score_w < 60.0)


# ---- 7. tap callback provenance (Phase 0 wiring) ----
# Short source code: the full "track:base:base-runner:<mint>" overflows
# Telegram's 64-byte callback_data cap (BUTTON_DATA_INVALID — the card
# never delivers). The parser resolves "br" back to "base-runner".
check("tap callback carries chain+short code",
      tap_callback(MINT) == "track:base:br:" + MINT)
parts = tap_callback(MINT).split(":")
check("tap callback parses to 4 parts", len(parts) == 4
      and parts[1] == "base" and parts[2] == "br")
check("tap callback fits Telegram's 64-byte cap",
      len(tap_callback("0x" + "a" * 40).encode("utf-8")) <= 64)


# ---- 8. kill-switch hook ----
_ns["_HAS_BUS"] = True
_ns["feed_enabled"] = lambda c: False
check("paused feed -> feed_live False", feed_live() is False)
_ns["feed_enabled"] = lambda c: True
check("enabled feed -> feed_live True", feed_live() is True)
# Solana is never gated: feed_live only ever asks about "base"
asked = []
_ns["feed_enabled"] = lambda c: asked.append(c) or True
feed_live()
check("kill switch only queries base", asked == ["base"])


# ---- 9. signal bus schema (base signals validate) ----
from signal_bus import make_signal, validate
sig = make_signal("base", "base-runner", MINT, symbol="RUNR", score=72.0,
                  tier="runner-pick", age_min=25, mcap=400_000, liquidity=50_000,
                  buys=40, sells=8, evidence={"safety": "ok"})
ok, errs = validate(sig)
check("base runner signal validates on bus", ok and not errs)

# ---- 10. trending source: merge + dedupe by pool address (no network) ----
def canned_pool(addr, suffix, dex="uniswap-v3-base", age_min=60):
    rp = raw_pool(base="base_0x" + suffix * 40, dex=dex, age_min=age_min)
    rp["attributes"]["address"] = addr
    rp["id"] = "base_" + addr
    return rp


_calls = []


def fake_http(url, timeout=12):
    _calls.append(url)
    if "trending_pools" in url:
        return {"data": [canned_pool("0x" + "1" * 40, "c"),
                         canned_pool("0x" + "2" * 40, "d")]}
    if "aerodrome-slipstream" in url:
        # 0x11..11 repeats the trending pool -> must dedupe; 0x33..33 new
        return {"data": [canned_pool("0x" + "1" * 40, "c",
                                     dex="aerodrome-slipstream"),
                         canned_pool("0x" + "3" * 40, "e",
                                     dex="aerodrome-slipstream")]}
    return None


_ns["_http"] = fake_http
merged = _ns["get_pools"]((1,))
addrs = [p["pair"] for p in merged]
check("merges trending + aerodrome pools", len(merged) == 3)
check("dedupes by pool address", len(set(addrs)) == 3 == len(addrs))
check("polls both endpoints",
      any("trending_pools" in u for u in _calls)
      and any("aerodrome-slipstream" in u for u in _calls))
check("empty address falls back to pool id (no '' collapse)",
      all(p["pair"] for p in merged))


# ---- 11. score FDV zone widened for trending runners ----
big = pool(mcap=8_000_000.0)
score_b, reasons_b = score_pool(big, ["volume accelerating"])
check("large runner (8M mcap) gets partial zone credit",
      any("large runner" in r for r in reasons_b))
check("strong large runner still clears the 60 bar", score_b >= 60.0)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    raise SystemExit(1)
print("ALL BASE RUNNER TESTS PASSED")
