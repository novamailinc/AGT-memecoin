#!/usr/bin/env python3
"""Fee-schedule tests against the LIVE-captured fee accounts (2026-11-23).

Unlike test_pump_fees.py (synthetic encoder fixtures), this suite parses
the real on-chain captures in tests/fixtures/fee_program/ (commit
e9b11e7, bounded PublicNode read 2026-11-23T04:21:32-34Z), verifies the
fee SELECTION logic against the published semantics
(pump-fees::get_fees / pump-fees-math::calculate_fee_tier, mirrored from
the @pump-fun/pump-sdk and @pump-fun/pump-swap-sdk sources), and locks
chain_costs.py's static fallback table to the captured schedule.
"""
import json
import os
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from daemon.launch_scanner import pump_fees as PF  # noqa: E402
from daemon.launch_scanner import pump_fee_schedule as PFS  # noqa: E402
from daemon.launch_scanner import chain_costs as CC  # noqa: E402

FIXTURES = os.path.join(REPO, "tests", "fixtures", "fee_program")
ARTIFACT = os.path.join(REPO, "daemon", "launch_scanner", "data",
                        "pump_fee_schedule_captured.json")

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def refuses(name, fn, *a, **kw):
    try:
        fn(*a, **kw)
        check(name, False)
    except PFS.PumpFeesError:
        check(name, True)
    except Exception:
        check(name + " (PumpFeesError, not %s)"
              % type(sys.exc_info()[1]).__name__, False)


# ---------- real capture loads and cross-validates ----------
bundle = PFS.load_fixture_set(FIXTURES)
fc_pump = bundle["fee_config_pump"]
fc_pamm = bundle["fee_config_pump_amm"]
gc_pamm = bundle["global_config_pump_amm"]
meta = json.load(open(os.path.join(FIXTURES, "capture_meta.json")))

check("capture: pump FeeConfig single SOL tier = 1.25% total",
      len(fc_pump["fee_tiers"]) == 1
      and fc_pump["fee_tiers"][0]["market_cap_lamports_threshold"] == 0
      and PFS.total_fee_bps(fc_pump["fee_tiers"][0]["fees"]) == 125)
check("capture: pump FeeConfig stable tier matches SOL tier",
      len(fc_pump["stable_fee_tiers"]) == 1
      and PFS.total_fee_bps(fc_pump["stable_fee_tiers"][0]["fees"])
      == 125)
check("capture: pump FeeConfig flat == exotic == 1.25%",
      PFS.total_fee_bps(fc_pump["flat_fees"]) == 125
      and PFS.total_fee_bps(fc_pump["exotic_flat_fees"]) == 125)
check("capture: pump FeeConfig fully consumed",
      fc_pump["consumed_bytes"] <= fc_pump["account_bytes"])

check("capture: pump_amm FeeConfig 25 SOL tiers + 25 stable tiers",
      len(fc_pamm["fee_tiers"]) == 25
      and len(fc_pamm["stable_fee_tiers"]) == 25)
check("capture: canonical first tier = 1.25% (2/93/30)",
      PFS.total_fee_bps(fc_pamm["fee_tiers"][0]["fees"]) == 125
      and fc_pamm["fee_tiers"][0]["fees"]["lp_fee_bps"] == 2
      and fc_pamm["fee_tiers"][0]["fees"]["protocol_fee_bps"] == 93
      and fc_pamm["fee_tiers"][0]["fees"]["creator_fee_bps"] == 30)
check("capture: canonical tiers end at 0.30% >= 98240 SOL",
      PFS.total_fee_bps(fc_pamm["fee_tiers"][-1]["fees"]) == 30
      and fc_pamm["fee_tiers"][-1]["market_cap_lamports_threshold"]
      == 98240 * 10**9)
check("capture: stable tiers end at 0.30% >= 20000 quote units",
      PFS.total_fee_bps(fc_pamm["stable_fee_tiers"][-1]["fees"]) == 30
      and fc_pamm["stable_fee_tiers"][-1]
      ["market_cap_lamports_threshold"] == 20000 * 10**9)
check("capture: pump_amm flat = 0.30% (25/5/0), exotic = 0.30%",
      PFS.total_fee_bps(fc_pamm["flat_fees"]) == 30
      and fc_pamm["flat_fees"]["lp_fee_bps"] == 25
      and PFS.total_fee_bps(fc_pamm["exotic_flat_fees"]) == 30)

check("capture: GlobalConfig fallback = 0.30% (20/5/5)",
      gc_pamm["lp_fee_basis_points"] == 20
      and gc_pamm["protocol_fee_basis_points"] == 5
      and gc_pamm["coin_creator_fee_basis_points"] == 5)
check("capture: GlobalConfig creator fee configurable, max 300 bps",
      gc_pamm["creator_fee_configurable"] is True
      and gc_pamm["max_configurable_creator_fee_bps"] == 300)
check("capture: consumed bytes == account bytes (no trailing junk)",
      gc_pamm["consumed_bytes"] == gc_pamm["account_bytes"])
check("capture: owners and timestamps validated",
      bundle["meta"]["fee_config_pump"]["owner"] == PFS.PUMP_FEES_PROGRAM
      and bundle["meta"]["global_config_pump_amm"]["owner"]
      == PFS.PUMP_AMM_PROGRAM
      and bundle["meta"]["fee_config_pump"]["captured_utc"]
      == "2026-11-23T04:21:32Z")


# ---------- fixture tamper refusals ----------
def tampered_meta(mutate):
    import copy
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp()
    try:
        for f in os.listdir(FIXTURES):
            src = os.path.join(FIXTURES, f)
            with open(src, "rb") as fh:
                data = fh.read()
            with open(os.path.join(tmp, f), "wb") as fh:
                fh.write(data)
        m = json.load(open(os.path.join(tmp, "capture_meta.json")))
        rewrote = mutate(m, tmp)
        if not rewrote:
            json.dump(m, open(os.path.join(tmp, "capture_meta.json"),
                              "w"))
        PFS.load_fixture_set(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


refuses("refuse: tampered data_len",
        lambda: tampered_meta(
            lambda m, d: m["accounts"]["FC_PUMP"].update(data_len=1)))
refuses("refuse: tampered owner",
        lambda: tampered_meta(
            lambda m, d: m["accounts"]["GC_PAMM"].update(owner="x")))
refuses("refuse: tampered timestamp",
        lambda: tampered_meta(
            lambda m, d: m["accounts"]["FC_PAMM"].update(
                captured_utc="not-a-time")))
refuses("refuse: missing fixture file",
        lambda: tampered_meta(
            lambda m, d: os.remove(os.path.join(
                d, m["accounts"]["FC_PUMP"]["file"]))))
def _break_meta_json(m, d):
    with open(os.path.join(d, "capture_meta.json"), "w") as fh:
        fh.write("{")
    return True


refuses("refuse: malformed metadata json",
        lambda: tampered_meta(_break_meta_json))
refuses("refuse: parse on truncated bytes",
        PF.parse_fee_config,
        open(os.path.join(FIXTURES, "fc_pump.bin"), "rb").read()[:100])


# ---------- tier selection (SDK mirror) ----------
tiers = fc_pamm["fee_tiers"]
check("tier: below first threshold -> first tier",
      PFS.calculate_fee_tier(
          [{"market_cap_lamports_threshold": 5,
            "fees": {"lp_fee_bps": 1, "protocol_fee_bps": 2,
                     "creator_fee_bps": 3}}], 4)
      == {"lp_fee_bps": 1, "protocol_fee_bps": 2, "creator_fee_bps": 3})
check("tier: mcap 0 -> 125 bps",
      PFS.total_fee_bps(PFS.calculate_fee_tier(tiers, 0)) == 125)
check("tier: exact 420 SOL boundary -> 120 bps",
      PFS.total_fee_bps(PFS.calculate_fee_tier(tiers, 420 * 10**9))
      == 120)
check("tier: one lamport below 420 -> 125 bps",
      PFS.total_fee_bps(PFS.calculate_fee_tier(tiers, 420 * 10**9 - 1))
      == 125)
check("tier: 58940 SOL -> 50 bps (captured value, not doc 52.5)",
      PFS.total_fee_bps(PFS.calculate_fee_tier(tiers, 58940 * 10**9))
      == 50)
check("tier: far above top -> 30 bps",
      PFS.total_fee_bps(PFS.calculate_fee_tier(tiers, 10**18)) == 30)
check("tier: unknown mcap -> highest-fee tier (conservative)",
      PFS.total_fee_bps(PFS.calculate_fee_tier(tiers, None)) == 125)
check("tier: unknown mcap picks max even if list were unsorted",
      PFS.total_fee_bps(PFS.calculate_fee_tier(
          list(reversed(tiers[:3])), None)) == 125)
refuses("tier: empty tier list refuses",
        PFS.calculate_fee_tier, [], 0)
refuses("tier: negative mcap refuses",
        PFS.calculate_fee_tier, tiers, -1)


# ---------- quote category ----------
check("quote: None/empty -> sol",
      PFS.quote_category(None) == "sol" and PFS.quote_category("") == "sol")
check("quote: native mint + default pubkey -> sol",
      PFS.quote_category("So11111111111111111111111111111111111111112")
      == "sol"
      and PFS.quote_category("11111111111111111111111111111111") == "sol")
check("quote: USDC -> stable",
      PFS.quote_category("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
      == "stable")
check("quote: random mint -> exotic",
      PFS.quote_category("4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6")
      == "exotic")
refuses("quote: non-string refuses", PFS.quote_category, 123)


# ---------- fee selection semantics ----------
check("fees: non-canonical pool -> flat 0.30% regardless of mcap",
      PFS.total_fee_bps(PFS.fee_config_fees(
          fc_pamm, quote_category_="sol",
          market_cap_lamports=10**18, is_pump_pool=False)) == 30)
check("fees: stable quote uses stable tiers",
      PFS.total_fee_bps(PFS.fee_config_fees(
          fc_pamm, quote_category_="stable",
          market_cap_lamports=300 * 10**9)) == 115)
check("fees: stable tiers fall back to SOL tiers when empty",
      PFS.total_fee_bps(PFS.fee_config_fees(
          {"flat_fees": fc_pamm["flat_fees"],
           "fee_tiers": tiers, "stable_fee_tiers": [],
           "exotic_flat_fees": fc_pamm["exotic_flat_fees"]},
          quote_category_="stable", market_cap_lamports=420 * 10**9))
      == 120)
check("fees: exotic quote uses exotic flat",
      PFS.total_fee_bps(PFS.fee_config_fees(
          fc_pamm, quote_category_="exotic",
          market_cap_lamports=10**18)) == 30)
check("fees: zero exotic flat falls back to flat_fees",
      PFS.total_fee_bps(PFS.fee_config_fees(
          {"flat_fees": {"lp_fee_bps": 1, "protocol_fee_bps": 1,
                         "creator_fee_bps": 1},
           "fee_tiers": tiers, "stable_fee_tiers": [],
           "exotic_flat_fees": {"lp_fee_bps": 0, "protocol_fee_bps": 0,
                                "creator_fee_bps": 0}},
          quote_category_="exotic")) == 3)
refuses("fees: missing FeeConfig refuses",
        PFS.fee_config_fees, None, quote_category_="sol")

check("curve: single tier -> 1.25% at any mcap",
      PFS.total_fee_bps(PFS.bonding_curve_fees(
          fc_pump, market_cap_lamports=10**18)) == 125)
check("curve: no FeeConfig uses Global fallback",
      PFS.total_fee_bps(PFS.bonding_curve_fees(
          None, global_fee_bps=100, global_creator_bps=50)) == 150)
refuses("curve: no FeeConfig and no Global refuses",
        PFS.bonding_curve_fees, None)
check("curve: creator override only when configurable",
      PFS.bonding_curve_fees(fc_pump, creator_fee_configurable=True,
                             creator_fee_bps=70)["creator_fee_bps"] == 70
      and PFS.bonding_curve_fees(
          fc_pump, creator_fee_configurable=False,
          creator_fee_bps=70)["creator_fee_bps"] == 30)
check("curve: unset creator zeroes creator fee except new curve",
      PFS.bonding_curve_fees(fc_pump, creator_unset=True)
      ["creator_fee_bps"] == 0
      and PFS.bonding_curve_fees(fc_pump, creator_unset=True,
                                 is_new_curve=True)
      ["creator_fee_bps"] == 30)

check("amm: no FeeConfig uses GlobalConfig fallback (0.30%)",
      PFS.total_fee_bps(PFS.pumpswap_fees(
          None, gc_pamm, is_pump_pool=True)) == 30)
refuses("amm: no FeeConfig and no GlobalConfig refuses",
        PFS.pumpswap_fees, None, None, is_pump_pool=True)
check("amm: creator override via GlobalConfig configurability",
      PFS.pumpswap_fees(fc_pamm, gc_pamm, is_pump_pool=True,
                        creator_fee_bps=250)["creator_fee_bps"] == 250)
check("amm: default-pubkey coin creator zeroes creator fee",
      PFS.pumpswap_fees(fc_pamm, gc_pamm, is_pump_pool=True,
                        coin_creator_unset=True)
      ["creator_fee_bps"] == 0)


# ---------- market-cap helpers ----------
check("mcap: curve formula floor division",
      PFS.market_cap_lamports_curve(85 * 10**9, 10**15,
                                    1073 * 10**12)
      == (85 * 10**9 * 10**15) // (1073 * 10**12))
refuses("mcap: zero virtual token reserves refuses",
        PFS.market_cap_lamports_curve, 1, 1, 0)
check("mcap: pool uses effective quote reserve (vault + virtual)",
      PFS.market_cap_lamports_pool(quote_reserve=100, base_mint_supply=50,
                                   base_reserve=25,
                                   virtual_quote_reserves=-10)
      == ((100 - 10) * 50) // 25)
check("mcap: mayhem pool uses the fixed supply",
      PFS.market_cap_lamports_pool(100, 50, 25, is_mayhem_mode=True,
                                   mayhem_supply=10**15)
      == (100 * 10**15) // 25)
refuses("mcap: zero base reserve refuses",
        PFS.market_cap_lamports_pool, 1, 1, 0)
refuses("mcap: mayhem without supply refuses",
        PFS.market_cap_lamports_pool, 100, 50, 25, is_mayhem_mode=True)


# ---------- artifact + chain_costs lockstep ----------
sched = PFS.load_schedule_file(ARTIFACT)
rebuilt = PFS.build_schedule(PFS.load_fixture_set(FIXTURES))
check("artifact: committed file equals rebuild from fixtures",
      json.dumps(sched, sort_keys=True)
      == json.dumps(rebuilt, sort_keys=True))
check("artifact: provenance carries capture timestamps and owners",
      sched["source"]["accounts"]["fee_config_pump"]["captured_utc"]
      == "2026-11-23T04:21:32Z"
      and sched["source"]["accounts"]["global_config_pump_amm"]
      ["captured_utc"] == "2026-11-23T04:21:34Z")

# static fallback table == captured SOL tier table (exact)
expected = []
prev = 0
for t in fc_pamm["fee_tiers"]:
    th = t["market_cap_lamports_threshold"]
    assert th == prev or th > prev
    prev = th
totals = [PFS.total_fee_pct(t["fees"]) for t in fc_pamm["fee_tiers"]]
bounds = [t["market_cap_lamports_threshold"] // 10**9
          for t in fc_pamm["fee_tiers"][1:]] + [float("inf")]
static = list(CC.PUMP_CANONICAL_TIERS_SOL)
check("chain_costs: static fallback table == captured SOL tiers",
      len(static) == len(totals)
      and all(abs(p - tp) < 1e-9
              for (b, p), tp in zip(static, totals))
      and [b for (b, _) in static] == bounds)

check("chain_costs: unknown mcap -> first tier 1.25%",
      CC.pump_canonical_fee_pct() == 1.25
      and CC.venue_fee_pct("solana", "pump") == 1.25)
check("chain_costs: tier boundaries via mcap_sol",
      CC.pump_canonical_fee_pct(mcap_sol=419.999999) == 1.25
      and CC.pump_canonical_fee_pct(mcap_sol=420) == 1.20
      and CC.pump_canonical_fee_pct(mcap_sol=98240) == 0.30)
check("chain_costs: lamports win over sol when both given",
      CC.pump_canonical_fee_pct(mcap_sol=420,
                                mcap_lamports=0) == 1.25)
check("chain_costs: venue kinds",
      CC.venue_fee_pct("solana", "pumpfun_curve") == 1.25
      and CC.venue_fee_pct("solana", "pumpswap") == 1.25
      and CC.venue_fee_pct("solana", "pumpswap_flat") == 0.30
      and CC.venue_fee_pct("solana", "raydium") == 0.0
      and CC.venue_fee_pct("solana") == 0.0
      and CC.venue_fee_pct("ethereum", "pump") == 0.0)
check("chain_costs: stable quote tiers via quote_mint",
      CC.pump_canonical_fee_pct(
          quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
          mcap_lamports=300 * 10**9) == 1.15)
check("chain_costs: bonding curve flat at any mcap",
      CC.pump_bonding_curve_fee_pct(mcap_lamports=10**18) == 1.25)
check("chain_costs: round-trip math unchanged for $1 pump trade",
      abs(CC.round_trip_cost_usd("solana", 1.0, venue="pump")
          - (4 * 0.001 + 2 * 0.0125)) < 1e-12)
check("chain_costs: never raises on garbage",
      CC.venue_fee_pct(None, "pump", mcap_sol="junk") is not None
      and CC.pump_canonical_fee_pct(mcap_sol=-5)
      == CC.PUMP_FEE_PCT_CANONICAL_LOWEST_TIER
      and CC.cost_gate("solana", 1.0, venue="pump")[0] is False)

print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL PUMP FEE-SCHEDULE TESTS PASSED")
