#!/usr/bin/env python3
"""Phase 2 tests (pure functions, synthetic data, no network).

Covers scripts/eth-runner-feed (ETH-retuned six-stage funnel) and
scripts/hl-listing-watch (spotMeta diff, listing cards).
"""
ETH = "/home/hatch/workspace/Forex-agent/Forex-agent-main/scripts/eth-runner-feed"
HL = "/home/hatch/workspace/Forex-agent/Forex-agent-main/scripts/hl-listing-watch"


def load(path, modname):
    src = open(path).read()
    ns = {"__name__": modname, "__file__": path}
    exec(compile(src.split('if __name__ == "__main__":')[0], modname, "exec"),
         ns)
    return ns


eth = load(ETH, "eth_runner_test")
hl = load(HL, "hl_watch_test")

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- ETH breakout ----------------
MINT = "0x" + "b" * 40


def txw(buys=0, sells=0, buyers=0, sellers=0):
    return {"buys": buys, "sells": sells, "buyers": buyers, "sellers": sellers}


def pool(**kw):
    # default: 6.0x breakout (h1 60k vs h24-hourly-avg 10k), green h1,
    # deep buyers — should clear every gate and score >= 55.
    p = {"mint": MINT, "name": "ETHBRK", "age_min": 4000.0,
         "dex": "uniswap_v3", "pair": "0x" + "c" * 40,
         "mcap": 5_000_000.0, "fdv": 5_000_000.0, "liq": 800_000.0,
         "price_usd": 0.01,
         "tx": {"m5": txw(50, 20, 35, 12), "m15": txw(150, 60, 55, 30),
                "m30": txw(), "h1": txw()},
         "vol": {"m5": 8000.0, "m15": 18000.0, "m30": 0.0,
                 "h1": 60000.0, "h24": 240_000.0},
         "chg": {"m5": 5.0, "m15": 15.0, "h1": 40.0}}
    for k, v in kw.items():
        p[k] = v
    return p


ok, _ = eth["passes_prefilter"](pool())
check("eth: established runner passes pre-filter", ok)
check("eth: NO age window (breakouts have no age)",
      eth["passes_prefilter"](pool(age_min=5))[0]
      and eth["passes_prefilter"](pool(age_min=100_000))[0])
check("eth: RUNNER-ONLY rule liq floor is $25k",
      not eth["passes_prefilter"](pool(liq=24_999))[0]
      and eth["passes_prefilter"](pool(liq=25_001))[0])
check("eth: mcap zone $500k-$50M (established, not dust)",
      not eth["passes_prefilter"](pool(mcap=499_999))[0]
      and not eth["passes_prefilter"](pool(mcap=50_000_001))[0]
      and eth["passes_prefilter"](pool(mcap=5_000_000))[0])

ok, why = eth["passes_breakout"](pool())
check("eth: 6x breakout passes gate", ok and "6.0x" in why)
check("eth: 2.9x rejected (bar is 3x)",
      not eth["passes_breakout"](pool(
          vol={"m5": 8000.0, "m15": 18000.0, "m30": 0.0,
               "h1": 29000.0, "h24": 240_000.0}))[0])
check("eth: exactly 3.0x passes (inclusive bar)",
      eth["passes_breakout"](pool(
          vol={"m5": 8000.0, "m15": 18000.0, "m30": 0.0,
               "h1": 30000.0, "h24": 240_000.0}))[0])
check("eth: red h1 rejected",
      not eth["passes_breakout"](pool(chg={"m5": 5.0, "m15": 15.0,
                                           "h1": -2.0}))[0])
check("eth: flat h1 rejected",
      not eth["passes_breakout"](pool(chg={"m5": 5.0, "m15": 15.0,
                                           "h1": 0.0}))[0])
check("eth: fewer than 10 m15 buyers rejected",
      not eth["passes_breakout"](pool(
          tx={"m5": txw(50, 20, 35, 12), "m15": txw(40, 20, 9, 6),
              "m30": txw(), "h1": txw()}))[0])
check("eth: h1 vol under $10k rejected",
      not eth["passes_breakout"](pool(
          vol={"m5": 800.0, "m15": 1800.0, "m30": 0.0,
               "h1": 9000.0, "h24": 240_000.0}))[0])
check("eth: no h24 baseline rejected",
      not eth["passes_breakout"](pool(
          vol={"m5": 8000.0, "m15": 18000.0, "m30": 0.0,
               "h1": 60000.0, "h24": 0.0}))[0])
check("eth: breakout ratio judges pool against itself",
      eth["_breakout_ratio"](pool()) == 6.0
      and eth["_breakout_ratio"](pool(
          vol={"m5": 0.0, "m15": 0.0, "m30": 0.0,
               "h1": 0.0, "h24": 0.0})) == 0.0)

ok, _ = eth["passes_manipulation"](pool())
check("eth: good breakout passes manipulation", ok)
check("eth: wash rejected",
      not eth["passes_manipulation"](pool(tx={"m5": txw(50, 20, 35, 12),
                                              "m15": txw(300, 60, 20, 30),
                                              "m30": txw(), "h1": txw()}))[0])
check("eth: zero sells = manufactured",
      not eth["passes_manipulation"](pool(tx={"m5": txw(50, 0, 35, 0),
                                              "m15": txw(150, 0, 55, 0),
                                              "m30": txw(), "h1": txw()}))[0])

score, reasons = eth["score_pool"](pool())
check("eth: strong breakout scores >= 55 (got %.0f)" % score, score >= 55.0)
check("eth: breakout strength leads the reasons",
      any("hourly avg" in r for r in reasons))
# gate-passing but soft everywhere: 3.0x, +1% h1, 10 buyers, 50/50
# buys, $600k mcap -> 20+5+8+5+5 = 43 < 55
weak = pool(mcap=600_000.0,
            tx={"m5": txw(30, 22, 18, 14), "m15": txw(60, 60, 10, 8),
                "m30": txw(), "h1": txw()},
            vol={"m5": 3000.0, "m15": 12000.0, "m30": 0.0,
                 "h1": 30000.0, "h24": 240_000.0},
            chg={"m5": 1.0, "m15": 1.0, "h1": 1.0})
check("eth: weak breakout passes gate",
      eth["passes_breakout"](weak)[0])
score_w, _ = eth["score_pool"](weak)
check("eth: weak breakout scores < 55 (got %.0f)" % score_w, score_w < 55.0)

check("eth: tap callback carries chain+short code",
      eth["tap_callback"](MINT) == "track:ethereum:er:" + MINT)
check("eth: tap callback fits Telegram's 64-byte cap",
      len(eth["tap_callback"]("0x" + "b" * 40).encode("utf-8")) <= 64)

eth["_HAS_BUS"] = True
eth["feed_enabled"] = lambda c: False
check("eth: paused feed -> feed_live False", eth["feed_live"]() is False)
asked = []
eth["feed_enabled"] = lambda c: asked.append(c) or True
eth["feed_live"]()
check("eth: kill switch only queries ethereum", asked == ["ethereum"])

from signal_bus import make_signal, validate
sig = make_signal("ethereum", "eth-runner", MINT, symbol="ETHBRK",
                  score=66.0, tier="breakout-pick", age_min=4000,
                  mcap=5_000_000, liquidity=800_000, buys=50, sells=20,
                  evidence={"safety": "ok"})
ok, errs = validate(sig)
check("eth: breakout signal validates on bus", ok and not errs)

# ETH card renders with provenance + ETH exit hint
from card_design import build_card
c = build_card(symbol="ETHBRK", score=66.0, mint=MINT, mcap=5_000_000,
               liquidity=800_000, buyers=55, age_min=4000, buys_5m=50,
               sells_5m=20, safety="ok", is_runner=True,
               chain="ethereum", source="eth-runner")
check("eth: card shows Ethereum provenance",
      "\u26d3 Ethereum \u00b7 eth-runner" in c)
check("eth: card shows ETH exit hint (let the runner breathe)",
      "let the runner portion breathe" in c)
check("eth: card under line cap", len(c.splitlines()) <= 12)

# ---------------- HL listing watch ----------------
check("hl: listing_id is colon-free and alphanumeric",
      ":" not in hl["listing_id"]("PURR/USDC")
      and hl["listing_id"]("PURR/USDC").isalnum())
check("hl: listing_id keeps pair readable",
      hl["listing_id"]("PURR/USDC") == "HLPURRUSDC")

seen = {"0": "PURR/USDC", "5": "XYZ/USDC"}
universe = [{"index": 0, "name": "PURR/USDC", "tokens": [1, 0]},
            {"index": 5, "name": "XYZ/USDC", "tokens": [7, 0]},
            {"index": 9, "name": "NEW/USDC", "tokens": [12, 0]}]
new = hl["detect_new"](universe, seen)
check("hl: diff finds exactly the new listing",
      len(new) == 1 and new[0]["name"] == "NEW/USDC")
check("hl: no false positives on full overlap",
      hl["detect_new"](universe[:2], seen) == [])

listing = {"pair": "NEW/USDC", "spot_index": 9, "token_id": "0xabc",
           "mark_px": 1.25}
cb = hl["tap_callback"](listing)
check("hl: tap callback carries chain+short code",
      cb == "track:hyperliquid:hl:HLNEWUSDC")
parts = cb.split(":")
check("hl: tap callback parses to 4 parts", len(parts) == 4
      and parts[1] == "hyperliquid" and parts[2] == "hl")
check("hl: tap callback fits Telegram's 64-byte cap",
      len(cb.encode("utf-8")) <= 64)

hl["_HAS_BUS"] = True
hl["feed_enabled"] = lambda c: False
check("hl: paused feed -> feed_live False", hl["feed_live"]() is False)
asked = []
hl["feed_enabled"] = lambda c: asked.append(c) or True
hl["feed_live"]()
check("hl: kill switch only queries hyperliquid", asked == ["hyperliquid"])

sig = make_signal("hyperliquid", "hl-listing", hl["listing_id"]("NEW/USDC"),
                  symbol="NEW", score=70.0, tier="listing",
                  evidence={"pair": "NEW/USDC", "spot_index": 9})
ok, errs = validate(sig)
check("hl: listing signal validates on bus", ok and not errs)

text = hl["build_text"](listing)
check("hl: listing card names the pair", "NEW/USDC" in text)
check("hl: listing card carries auction context", "1.25" in text)
check("hl: listing card is honest about safety coverage",
      "GoPlus/Honeypot.is do not cover" in text)
check("hl: listing card shows HL provenance",
      "⛓ Hyperliquid · hl-listing" in text)
check("hl: listing card shows listing-event hint",
      "trade the listing" in text)
check("hl: listing card under line cap", len(text.splitlines()) <= 12)

print()
if fails:
    print("FAILURES: %d" % len(fails))
    raise SystemExit(1)
print("ALL PHASE 2 TESTS PASSED")
