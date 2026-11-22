# Post-Trade Ledger Analytics (lab)

A from-scratch, pure-stdlib analytics module for a $1-shot memecoin paper ledger: it reads synthetic close records and produces win rates, expectancy, rug rates, per-lane scorecards, and — the number that matters most — a leakage report showing how many positions peaked above 2x and still closed as losers. One fixed win definition (realized >= 1.5x) is used everywhere and documented, so lanes can be compared honestly.

## What it does

- Computes ledger-wide win rate (realized >= 1.5x), breakeven rate (realized >= 1.0), expectancy per $1 shot, and rug rate from close records of the form `{lane, mint, realized_mult, peak_mult, exit_reason, was_rug}`.
- Finds round-trippers — closes that peaked at >= 2.0x but realized < 1.0x — and quantifies the leakage: how many 2x peaks existed, how many closes were wins, how many round-tripped, and what the round-trippers actually realized on average.
- Breaks every statistic down per lane and renders a plain-text scorecard table with a TOTAL row and a leakage line.

## Files

- `posttrade.py` — `win_rate(closes) -> float` (share realized >= 1.5), `breakeven_rate(closes) -> float` (share >= 1.0), `expectancy(closes) -> float` (mean realized − 1), `round_trippers(closes) -> {"count": int, "mean_realized": float | None}` (peak >= 2.0 and realized < 1.0, both boundaries exact), `leakage_report(closes) -> {"n_peaked_2x", "n_closed_win", "n_round_tripped", "avg_realized_round_trippers"}`, `rug_rate(closes) -> float`, `per_lane_scorecard(closes) -> {lane: {"n", "wr", "breakeven_rate", "expectancy", "rug_rate"}}`, `render_scorecard(closes) -> str`, and `demo_fixture()` returning 10 synthetic closes across 2 lanes.
- `posttrade.py` — extended analytics: `pnl_usd(closes, shot_usd=1.0) -> float` (total dollars; non-positive shot raises `ValueError`), `median_realized(closes) -> float | None`, `best_close` / `worst_close` (the record itself, first on ties, `None` when empty), `capture_rate(closes) -> float | None` (wins among the >= 2x peaked; `None` when nothing peaked — nothing to capture is not the same as capturing nothing), `profit_factor(closes) -> float | None` (gross gains / gross losses; `inf` with gains and no losses, `None` when neither side exists), `cumulative_pnl_curve(closes, shot_usd=1.0) -> list` (running P/L in ledger order), `max_drawdown_usd(closes, shot_usd=1.0) -> float` (peak-to-trough of that curve, measured from the 0 start), `longest_win_streak` / `longest_loss_streak` (the 1.0–1.5 breakeven band breaks both), `exit_reason_breakdown(closes) -> {reason: {"n", "wr", "expectancy"}}` (keys sorted), `filter_by_lane(closes, lane) -> list`, `lane_ranking(closes) -> [(lane, row)]` (expectancy, then n, then name), and `verdict_ready(closes, min_closes, min_expectancy=0.0) -> bool` (the pre-registered gate: enough closes AND expectancy strictly above the bar). Fail-closed/empty behaviour: an empty ledger never crashes — rates return 0.0, medians/means/averages that have nothing to average are `None`, the scorecard and breakdown are `{}`, streaks are 0, drawdown is 0.0, and the rendered table says "no closes recorded"; a record missing any required field raises `KeyError` from every public function so a corrupt row is loud, not silently averaged away. Constants `WIN_MULT = 1.5`, `PEAK_MULT = 2.0`.
- `demo.py` — renders the scorecard for the fixture and prints the leakage summary. Runs clean, exit 0.
- `realdata.py` — real-data loader: reads the live paper bot's closes (dataset paper records + mirror/CEO/steady/stacker ledgers, read-only, fields whitelisted — entry snapshots dropped because the real ones carry wallet-name fragments) into this toolkit's canonical close shape and produces the full real scorecard via `posttrade` itself: per-lane n / WR / expectancy / rug rate (recorded flag where present, else the documented ≤0.05x proxy), round-tripper leakage over peak-known closes only, with peak-unknown closes (mirror/CEO record no peak) counted explicitly. `sample_ledgers.json` is a small real-shape fixture the adapter tests run against.
- `test_posttrade.py` — pytest suite (see below).
- `test_realdata.py` — adapter tests against the sample fixture, hand-computed.

## Test suite

47 tests, all passing (40 in `test_posttrade.py`, 7 in `test_realdata.py`). Coverage: hand-computed overall statistics on the 10-close fixture (win rate 0.40, breakeven rate 0.50, expectancy +0.23, rug rate 0.20, P/L +$2.30 at $1 shots and +$11.50 at $5), fixture shape and the `WIN_MULT`/`PEAK_MULT` constants, round-tripper count and mean (2 closes, 0.65x average) plus the full leakage report (5 peaked >= 2x, 4 wins) and the nothing-peaked leakage shape, exact boundary behaviour (win at exactly 1.5x, breakeven at exactly 1.0x, round-tripper at peak exactly 2.0x with realized 0.999 but not at realized exactly 1.0x or peak 1.999x), capture rate 0.60 on the fixture, `None` when nothing peaked and 0.0 when a peaked close merely failed to win, profit factor 5.3/3.0 by hand plus its `inf` (no losses), `None` (flat/empty) and 0.0 (no gains) edges, the cumulative P/L curve against a hand running total and its shot-size scaling/validation, max drawdown 1.6 on the fixture, 1.0 for an opening losing streak measured from zero, and 0.0 for winners-only/empty, win/loss streaks on the fixture (1 and 2), the breakeven band breaking both streaks, a full-length loss streak on an all-loss ledger, median realized for even/odd/single/pair ledgers, best/worst close with first-on-ties, per-lane scorecards computed by hand for both lanes (degen expectancy +0.10, steady +0.36), a rug rate concentrated entirely in one lane, a zero-sum ledger at exactly 0 expectancy, exit-reason breakdown by hand (trail n=3, expectancy +1.233; rug reasons at −1.0), lane filtering preserving order and agreeing with the scorecard row, lane ranking by expectancy, a single-close ledger consistent across every function, the empty ledger returning zeros/`None`/`{}` without crashing, the rendered table containing both lane names, the TOTAL row carrying the fixture numbers, alphabetical lane order, and "avg realized n/a" when there are no round-trippers, the verdict gate requiring both the close count and strictly-above-bar expectancy (exactly-at-bar fails), and each of the six required fields missing raising `KeyError` from the function that needs it.

## Captured output

```
Post-Trade Analytics — $1 shots, win = realized >= 1.5x

10 synthetic closes
post-trade scorecard (win = realized >= 1.5x, $1 per shot)
lane             n     wr  be>=1x     exp    rug
------------------------------------------------
degen            5   0.40    0.40  +0.100   0.20
steady           5   0.40    0.60  +0.360   0.20
TOTAL           10   0.40    0.50  +0.230   0.20
leakage: peaked>=2x 5, round-tripped 2 (avg realized 0.650x)

round-trippers: 2 closes peaked >=2x but closed <1x, avg realized 0.650x
leakage report: {'n_peaked_2x': 5, 'n_closed_win': 4, 'n_round_tripped': 2, 'avg_realized_round_trippers': 0.65}
overall: win rate 0.40, expectancy +0.230 per $1, rug rate 0.20
empty ledger renders as: 'post-trade scorecard: no closes recorded'
```

## What this mirrors in the live bot

The live bot keeps exactly these ledgers: every paper lane records closes with realized and peak multiples, exit reasons, and rug flags, and lane verdicts are made from them — expectancy, win rate, and rug rate per lane, judged at pre-registered close counts. The leakage report mirrors the operation's central exit question: the ledger has repeatedly shown positions that peaked above 2x closing as losers, which is an exit-policy problem, not an entry problem, and this module is the smallest honest tool that separates the two. The $1-per-shot framing matches the mirror ledger's fixed sizing.

## Self-exam

1. Why is a "win" defined at 1.5x rather than 1.0x, and which function reports the 1.0x share instead?
2. A close peaks at 2.4x and realizes 0.9x. Which report counts it, and what does its presence say about entries versus exits?
3. The fixture's two lanes have identical win rates but different expectancy (+0.10 vs +0.36). How is that possible?
4. Why does an empty ledger return `None` for the round-tripper mean but 0.0 for the win rate?
5. A ledger row is missing `peak_mult`. What happens, and why is crashing preferable to skipping the row?
