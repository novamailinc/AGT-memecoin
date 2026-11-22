# Real-data summary — adapters in toolkits 08 / 12 / 13

Aggregate results from running the three toolkits' new `realdata.py`
adapters over the live paper bot's recorded data (read-only, 2026-10-20).
Aggregates only: no per-trade rows, no wallet data of any kind. Full
outputs are kept locally, outside this repo.

## Conventions

- Expectancy = mean(realized multiple) − 1 per $1 staked.
- Win = realized ≥ 1.5x; also reported: share > 1.0x and ≥ 1.0x.
- Rug = the recorded rug flag where a source records one (paper
  dataset); in lane ledgers, which record no flag, rug is the
  documented proxy realized ≤ 0.05x (labeled "proxy" per lane below).
- Round-tripper = peaked ≥ 2.0x but closed < 1.0x, over closes whose
  peak was recorded.

## Scorecard — 450 real closes

| lane | n | expectancy | WR ≥1.5x | ≥1.0x | rug rate | basis | round-trippers |
|---|---|---|---|---|---|---|---|
| paper | 383 | −0.327 | 10.2% | 19.3% | 34.5% | recorded | 22 |
| degen_mirror | 37 | −0.440 | 0.0% | 8.1% | 32.4% | proxy | n/a — no peaks recorded |
| hl_mirror | 4 | −0.002 | 0.0% | 50.0% | 0.0% | proxy | n/a — no peaks recorded |
| ceo | 1 | +0.405 | 0.0% | 100% | 0.0% | proxy | n/a — no peaks recorded |
| steady | 18 | −0.006 | 16.7% | 44.4% | 0.0% | proxy | 0 |
| stacker | 7 | −0.562 | 0.0% | 0.0% | 14.3% | proxy | 0 |
| **total** | **450** | **−0.323** | **9.3%** | **19.6%** | **32.2%** | mixed | **22** |

Overall WR > 1.0x (breakeven excluded): 18.9%. Wilson 95% CI for the
≥1.5x win rate: 7.0%–12.4%. Leakage over the 408 peak-known closes:
66 peaked ≥2x, 22 round-tripped (avg realized 0.191x), 42 closed as wins.

**required_n:** no lane's observed win rate exceeds the 30% null, so at
the toolkit's convention (`statslab.required_n`, p1 must exceed p0)
the required sample size is undefined for every group — there is no
observed edge to power for. That is a result, not a gap in the math.

## Funnel autopsy — 305,799 real judge records

| class | n | share |
|---|---|---|
| never-met-bar | 298,846 | 97.73% |
| safety-unknown-lag | 6,549 | 2.14% |
| caught-or-other | 404 | 0.13% |
| gate-correctly-blocked-rug | 0 | no rug evidence recorded in judge data |
| invented-ceiling-block | 0 | unreachable — no gate-log reasons recorded |
| burned-one-shot-convergence | 0 | unreachable — no gate-log reasons recorded |

All 383 paper closes are **unclassifiable** by the funnel classifier:
they record no snapshots and no gate log. Their outcome patterns
(separate tally, not funnel classes): 22 round-trippers, 118
rug-flagged (of 132 recorded rugs; 14 count as round-trippers first),
39 winners, 169 plain losses, 35 in the breakeven band. Momentum in
judge cases is proxied as buys5m > sells5m (no per-snapshot momentum
flag is recorded); safety is recorded once per record.

## Honest gaps

- No veto log exists in the recorded data — no veto statistics.
- The tracked-card outcomes file records alert-time fields only, so
  no card outcome rate is computable from it.
- Mirror/CEO ledgers record no peak multiple: their leakage is
  unknown (42 closes), not zero.
- The paper dataset records no lane field; per-lane detail beyond the
  ledgers above is a per-source split only.
- `entry_features` is null on every dataset record that carries the
  key — no feature-level analysis is possible yet.
