# Real-data scorecard — live paper bot (LOCAL ONLY, not for the repo)

Generated 2026-10-20 from read-only runs of the real-data adapters in
toolkits 08 / 12 / 13 against the live bot's recorded data. Sources:
`backtest_dataset.jsonl` (paper + judge records), `degen_mirror_ledger.json`,
`ceo_ledger.json`, `steady_ledger.json`, `stacker_ledger.json`,
`degen_outcomes.json`, `killswitch/state.json`. (`veto_log.jsonl` does not
exist at the assigned path or anywhere under `~/.forex-agent/` — see
"could not compute".)

## Conventions

- **Expectancy** = mean(realized multiple) − 1, per $1 staked (statslab
  convention). Paper-dataset rows are journaled close rows (two-tier
  positions journal one row per leg) — counted as recorded.
- **Win** = realized ≥ 1.5x (toolkit 13's fixed definition). Also reported:
  share > 1.0x (toolkit 08's win_rate: breakeven is not a win) and ≥ 1.0x.
- **Rug** = the recorded `rug` flag where the source records one (paper
  dataset). Lane ledgers record no rug flag → proxy: realized ≤ 0.05x
  (near-total loss), labeled "proxy" wherever used.
- **Mirror realized multiple** = sum of per-leg `realized_usd` on the $1
  notional (cross-checks vs 1 + pnl_usd). The 11 `time_stop_24h_unquotable`
  degen_mirror closes record exit price 0.0 with per-leg realized ≈ $0 minus
  fees and pnl_usd ≈ −$1.001; both routes reconstruct to ≈ −0.001x, kept as
  recorded (hence 12 negative multiples overall: 11 mirror + 1 stacker whose
  recorded realized_pnl is −$3.15 on a $3.00 size).

## Headline numbers (450 real closes)

| metric | value |
|---|---|
| total closes analyzed | **450** (383 paper + 41 mirror + 1 CEO + 18 steady + 7 stacker) |
| overall expectancy | **−0.323** per $1 |
| WR (realized ≥ 1.5x) | **9.3%** (42/450; Wilson 95% CI 7.0%–12.4%) |
| WR (realized > 1.0x, statslab) | 18.9% |
| share ≥ 1.0x | 19.6% |
| rug rate | **32.2%** mixed basis — paper recorded-flag rate **34.5%** (132/383) |
| round-trippers (peak ≥2x, closed <1x) | **22**, avg realized 0.191x (peak-known closes only) |
| total P/L at $1/shot | −$145.14 |
| required_n vs 30% null | **not applicable anywhere** — no lane's observed WR exceeds the 30% null, so `statslab.required_n` (which requires p1 > p0) has no edge to power for |

## Per-lane table

| lane | n | expectancy | WR ≥1.5x | >1.0x | ≥1.0x | rug rate | rug basis | round-trippers | peak known |
|---|---|---|---|---|---|---|---|---|---|
| paper (main dataset) | 383 | −0.327 | 10.2% | 18.5% | 19.3% | 34.5% | recorded | 22 | 383 |
| degen_mirror | 37 | −0.440 | 0.0% | 8.1% | 8.1% | 32.4% | proxy | n/a (no peaks) | 0 |
| hl_mirror | 4 | −0.002 | 0.0% | 50.0% | 50.0% | 0.0% | proxy | n/a (no peaks) | 0 |
| ceo | 1 | +0.405 | 0.0% | 100% | 100% | 0.0% | proxy | n/a (no peaks) | 0 |
| steady | 18 | −0.006 | 16.7% | 44.4% | 44.4% | 0.0% | proxy | 0 | 18 |
| stacker | 7 | −0.562 | 0.0% | 0.0% | 0.0% | 14.3% | proxy | 0 | 7 |
| **TOTAL** | **450** | **−0.323** | **9.3%** | **18.9%** | **19.6%** | **32.2%** | mixed | **22** | 408 |

Paper closes by recorded source: (blank) 289, exp −0.300, rug 36.3%;
pumpfun 47, exp −0.619, rug 53.2%; gecko-trending 43, exp −0.199, rug 4.7%;
launch.early_digest 2, exp −0.044; launch.lowcap_runner 2, exp −0.402.

Leakage (peak-known closes, n=408; 42 mirror/CEO closes record no peak and
are excluded, never treated as "never peaked"): 66 closes peaked ≥2x;
22 of them round-tripped to <1x (avg realized 0.191x); 42 closes were wins.

## Funnel autopsy on real journal records (toolkit 12)

Judge records classified: **305,799**

| class | n | share |
|---|---|---|
| never-met-bar | 298,846 | 97.73% |
| safety-unknown-lag | 6,549 | 2.14% |
| caught-or-other | 404 | 0.13% |
| gate-correctly-blocked-rug | 0 | — no evidence recorded (see below) |
| invented-ceiling-block | 0 | — unreachable from recorded data |
| burned-one-shot-convergence | 0 | — unreachable from recorded data |

Paper closes: **383 of 383 unclassifiable** by the funnel classifier — they
record no snapshots (empty on every close) and no gate log. Supplementary
outcome-pattern tallies (NOT funnel classes; precedence round-tripper →
rug-flagged → winner → plain-loss → breakeven-band): round-tripper 22,
rug-flagged 118 (of 132 recorded rugs; 14 rugs were also round-trippers and
count under that pattern first), winner 39, plain-loss 169, breakeven-band 35.

Why three classes are zero: the journal records **no gate-log reasons at
all** (ceiling and convergence classes are structurally unobservable), and
judge records **never** set `outcome.rug` (0 of 305,799) and **never**
record safety `fails` (0 of 305,799) — so the rug-block class has no
supporting evidence in judge records either. Zeros here are absence of
evidence, not evidence of absence. Derivations used: momentum proxied as
buys5m > sells5m (no per-snapshot momentum flag is recorded); safety is
recorded once per record, applied to all its snapshots.

## Killswitch state (context, read from state.json)

Halted: main, degen, challenge, degen_v2 (structural kill), degen_mirror.
Not halted: nadir, spark, steady, hl_mirror, ceo.

## What could NOT be honestly computed

- **Veto statistics** — `veto_log.jsonl` does not exist at
  `/home/hatch/.forex-agent/veto_log.jsonl` or anywhere under
  `~/.forex-agent/` (verified by filesystem search). No veto counts reported.
- **Degen card outcome rates** — `degen_outcomes.json` holds 366 alert
  records with alert-time fields only (alert_price / alert_mcap / score /
  components); it records no peak or final outcome per card, so no card
  win/rug rate can be computed from it. (A separate `degen_peaks.json`
  with per-mint peak multiples exists but was outside the assigned source
  list and was not used.)
- **Round-trippers / leakage for mirror + CEO lanes** — those ledgers
  record no peak multiple (42 closes). Excluded and counted, not zeroed.
- **Per-lane breakdown of the paper dataset beyond `source`** — the dataset
  records no lane field for paper closes (verdict is uniformly "confirm";
  `leg` A/B and `label` exist but are not lanes). Paper is reported as one
  lane plus a per-source split.
- **`entry_features` analysis** — only 6 paper records carry the key and
  all 6 values are null in the dataset as recorded (the CEO ledger's single
  close does carry a real entry_features dict). No feature-level analysis
  is possible yet.
- **required_n** — undefined (not zero) for every group: all observed WRs
  sit below the 30% null the toolkit tests against.

Raw run outputs alongside this file: `toolkit08-real-report.json`,
`toolkit12-real-autopsy.json`, `toolkit13-real-scorecard-table.md`.
