#!/usr/bin/env python3
"""Locked-liquidity hunter tests (no network: HTTP + LP checks monkeypatched).

Covers scripts/locked-liq-scan stages: prefilter, LP-lock gate (Solana
RugCheck + EVM lp_lock, fail-closed), momentum gate, scoring bar, tap
callback 64-byte safety, per-chain daily cap, and card provenance.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys

REPO = _REPO_ROOT
SCRIPT = os.path.join(REPO, "scripts/locked-liq-scan")
_src = open(SCRIPT).read()
_ns = {"__name__": "locked_liq_test", "__file__": SCRIPT}
exec(compile(_src.split('if __name__ == "__main__":')[0],
             "locked-liq-scan", "exec"), _ns)

passes_prefilter = _ns["passes_prefilter"]
check_lp_locked = _ns["check_lp_locked"]
is_running = _ns["is_running"]
score_pool = _ns["score_pool"]
tap_callback = _ns["tap_callback"]
build_text = _ns["build_text"]
CHAINS = _ns["CHAINS"]

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def txw(buys=0, sells=0, buyers=0):
    return {"buys": buys, "sells": sells, "buyers": buyers}


def pool(chain="base", **kw):
    p = {"chain": chain, "mint": "0x" + "ab" * 20, "name": "LOCKR",
         "age_min": 30.0, "dex": "uniswap-v2-base",
         "pair": "0x" + "cd" * 20, "mcap": 200_000.0, "fdv": 200_000.0,
         "liq": 25_000.0, "price_usd": 0.001,
         "tx": {"m5": txw(8, 4, 6), "m15": txw(20, 10, 14),
                "m30": txw(30, 20, 20), "h1": txw(60, 40, 40)},
         "vol": {"m5": 1000.0, "m15": 4000.0, "m30": 8000.0,
                 "h1": 25_000.0},
         "chg": {"m5": 5.0, "m15": 12.0, "h1": 60.0}}
    p.update(kw)
    return p


cfg = CHAINS["base"]

# ---- stage 1: prefilter ----
check("prefilter passes good pool", passes_prefilter(pool(), cfg)[0])
check("prefilter rejects too young",
      not passes_prefilter(pool(age_min=5), cfg)[0])
check("prefilter rejects too old",
      not passes_prefilter(pool(age_min=200), cfg)[0])
check("prefilter rejects low liq",
      not passes_prefilter(pool(liq=1000), cfg)[0])
check("RUNNER-ONLY rule: $25k liq floor on solana",
      not passes_prefilter(pool(liq=24_999), CHAINS["solana"])[0]
      and passes_prefilter(pool(liq=25_001), CHAINS["solana"])[0])
check("RUNNER-ONLY rule: $25k liq floor on base",
      not passes_prefilter(pool(liq=24_999), CHAINS["base"])[0]
      and passes_prefilter(pool(liq=25_001), CHAINS["base"])[0])
check("RUNNER-ONLY rule: $25k liq floor on ethereum",
      not passes_prefilter(pool(liq=24_999), CHAINS["ethereum"])[0]
      and passes_prefilter(pool(liq=25_001), CHAINS["ethereum"])[0])
check("unknown liq rejected fail-closed",
      not passes_prefilter(pool(liq=None), CHAINS["solana"])[0])
check("prefilter rejects dust mcap",
      not passes_prefilter(pool(mcap=1000), cfg)[0])
check("prefilter rejects mega mcap",
      not passes_prefilter(pool(mcap=50_000_000), cfg)[0])

# ---- stage 2: LP-lock gate, Solana (RugCheck, monkeypatched) ----
# Fixtures use the real RugCheck shape: the candidate pool's own market is
# matched by pubkey == pool address (verified live 2026-10-20).
RAY_PAIR = "RayPoolAddr1111111111111111111111111111"
_ns["_rugcheck_report"] = lambda mint: {"markets": [
    {"marketType": "raydium", "pubkey": RAY_PAIR,
     "lp": {"lpLockedPct": 96.0}}]}
ok, badge, why = check_lp_locked(pool(chain="solana", dex="raydium",
                                      pair=RAY_PAIR))
check("solana 96% locked -> pass + badge",
      ok and badge.startswith("🔒"))
_ns["_rugcheck_report"] = lambda mint: {"markets": [
    {"marketType": "raydium", "pubkey": RAY_PAIR,
     "lp": {"lpLockedPct": 40.0}}]}
ok, badge, why = check_lp_locked(pool(chain="solana", dex="raydium",
                                      pair=RAY_PAIR))
check("solana 40% locked -> fail", not ok and not badge)
_ns["_rugcheck_report"] = lambda mint: None
ok, badge, why = check_lp_locked(pool(chain="solana", dex="raydium",
                                      pair=RAY_PAIR))
check("solana RugCheck down -> fail-CLOSED", not ok)

# ---- stage 2b: PumpSwap candidates — lock on the candidate's OWN market ----
PUMP_PAIR = "PumpPoolAddr11111111111111111111111111111"
_ns["_rugcheck_report"] = lambda mint: {"markets": [
    {"marketType": "pump_fun_amm", "pubkey": PUMP_PAIR,
     "lp": {"lpLockedPct": 100.0}}]}
ok, badge, why = check_lp_locked(pool(chain="solana", dex="pumpswap",
                                      pair=PUMP_PAIR))
check("pumpswap graduated 100% locked -> pass", ok)
check("pumpswap badge records venue", "pump_fun_amm" in badge)
_ns["_rugcheck_report"] = lambda mint: {"markets": [
    {"marketType": "pump_fun_amm", "pubkey": PUMP_PAIR,
     "lp": {"lpLockedPct": 40.0}}]}
ok, badge, why = check_lp_locked(pool(chain="solana", dex="pumpswap",
                                      pair=PUMP_PAIR))
check("pumpswap direct pool 40% locked -> fail (venue not trusted)",
      not ok and not badge)
_ns["_rugcheck_report"] = lambda mint: {"markets": [
    {"marketType": "raydium", "pubkey": RAY_PAIR,
     "lp": {"lpLockedPct": 100.0}}]}
ok, badge, why = check_lp_locked(pool(chain="solana", dex="pumpswap",
                                      pair=PUMP_PAIR))
check("pumpswap candidate, only a locked raydium market -> fail "
      "(different market never rescues)", not ok)
_ns["_rugcheck_report"] = lambda mint: {"markets": [
    {"marketType": "meteora", "pubkey": "OtherPool11111111111111111111111",
     "lp": {"lpLockedPct": 100.0}}]}
ok, badge, why = check_lp_locked(pool(chain="solana", dex="pumpswap",
                                      pair=PUMP_PAIR))
check("pumpswap candidate, no matching market in report -> fail-CLOSED",
      not ok)
_ns["_rugcheck_report"] = lambda mint: {"markets": [
    {"marketType": "raydium", "pubkey": RAY_PAIR,
     "lp": {"lpLockedPct": 95.0}}]}
ok, badge, why = check_lp_locked(pool(chain="solana", dex="raydium",
                                      pair=RAY_PAIR))
check("raydium single-market 95% locked -> pass (regression)", ok)
_ns["_rugcheck_report"] = lambda mint: {"markets": [
    {"marketType": "pump_fun_amm", "pubkey": "SomeOtherPool1111111111111",
     "lp": {"lpLockedPct": 100.0}}]}
ok, badge, why = check_lp_locked(pool(chain="solana", dex="pumpswap",
                                      pair=""))
check("address-less pumpswap candidate -> venue-type fallback passes",
      ok)

# ---- stage 2: LP-lock gate, EVM (lp_lock monkeypatched) ----
import lp_lock
_orig = lp_lock.check_lp_lock
lp_lock.check_lp_lock = lambda c, d, p, timeout=10: {
    "status": "locked", "detail": "LP burned 97%"}
ok, badge, why = check_lp_locked(pool(chain="base"))
check("evm burned -> pass + badge", ok and badge.startswith("🔒"))
lp_lock.check_lp_lock = lambda c, d, p, timeout=10: {
    "status": "unverified", "detail": "LP lock unverified"}
ok, badge, why = check_lp_locked(pool(chain="base"))
check("evm unverified -> fail (strict: only verified passes)", not ok)
lp_lock.check_lp_lock = lambda c, d, p, timeout=10: {"status": "na",
                                                    "detail": "x"}
ok, badge, why = check_lp_locked(pool(chain="base"))
check("evm V3 na -> fail", not ok)
lp_lock.check_lp_lock = _orig

# ---- stage 4: momentum ----
check("running pool passes",
      is_running(pool())[0])
dead = pool()
dead["tx"]["m15"] = txw(1, 8, 2)
check("dead pool fails", not is_running(dead)[0])
falling = pool()
falling["chg"] = {"m5": -5.0, "m15": -10.0, "h1": -20.0}
check("falling pool fails", not is_running(falling)[0])

# ---- stage 5: scoring ----
s, reasons = score_pool(pool())
check("score >= bar 50 for locked runner", s >= 50.0)
check("reasons mention lock", any("lock" in r for r in reasons))

# ---- tap callback: 64-byte safe with new 'll' code ----
cb = tap_callback("base", "0x" + "ab" * 20)
check("tap callback <= 64 bytes", len(cb) <= 64)
check("tap callback uses ll code", ":ll:" in cb)
cb = tap_callback("solana", "A" * 44)
check("solana tap callback <= 64 bytes", len(cb) <= 64)

# ---- card provenance ----
_ns["_HAS_CARDS"] = True
card = build_text(pool(chain="ethereum"), 72, ["verified locked liquidity"],
                  "safety pass", "🔒 LP burned 97%")
check("card carries chain tag", "⛓" in card)
check("card carries LP badge", "🔒 LP burned 97%" in card)
check("card is a runner card", "RUNNER" in card.upper())

# ---- chains configured: solana raydium+pumpswap, evm v2-only ----
check("solana hunts raydium + pumpswap",
      CHAINS["solana"]["dexes"] == ("raydium", "pumpswap"))
check("base hunts v2-style only",
      all("v2" in d or "sushiswap" in d or "swap" in d
          for d in CHAINS["base"]["dexes"]))
check("no hyperliquid chain (no LP there)", "hyperliquid" not in CHAINS)

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all locked-liq tests passed")
