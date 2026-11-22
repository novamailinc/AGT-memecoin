# Exam — Post-Trade Ledger Analytics

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.
- **Fixture referenced below** (the module's 10-close demo fixture, $1 shots): degen — 2.6, 0.4 (peak 2.6), 0.0 (rug), 1.8, 0.7; steady — 1.6, 1.1, 0.9 (peak 2.4), 0.0 (rug), 3.2. Peaks ≥2x occur on closes realising 2.6, 0.4, 1.8, 0.9, 3.2.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** The module's fixed definitions are:
- A. Win = realised ≥ 1.0; peak threshold = 1.5
- B. Win = realised ≥ 1.5 (`WIN_MULT`); peak threshold = 2.0 (`PEAK_MULT`)
- C. Win = realised > 2.0; peak threshold = 2.0
- D. Win is defined per lane

**Q2.** `expectancy(closes)` is:
- A. Win rate × average win
- B. Median realised multiple
- C. Mean realised multiple − 1 (profit per $1 shot)
- D. Total P/L divided by the number of wins

**Q3.** A close is a round-tripper exactly when:
- A. Peak ≥ 2.0 and realised ≤ 1.0
- B. Peak > 2.0 and realised < 1.5
- C. Peak ≥ 2.0 and realised < 1.0 (both boundaries as stated: peak exactly 2.0 counts, realised exactly 1.0 does not)
- D. Peak ≥ 1.5 and realised < 1.0

**Q4.** `capture_rate(closes)` returns `None` when:
- A. There are no wins
- B. No close peaked at ≥ 2.0x — there was nothing to capture
- C. The ledger is non-empty but all closes lost
- D. The round-tripper count is zero

**Q5.** `profit_factor(closes)` returns `inf` when, and `None` when:
- A. `inf` when there are no closes at all; `None` when there are only wins
- B. `inf` when there are gross gains and no losses; `None` when neither gains nor losses exist (flat/empty)
- C. `inf` when gains equal losses; `None` when there are no wins
- D. It never returns either; it always returns a finite float

**Q6.** `verdict_ready(closes, min_closes, min_expectancy=0.0)` is True only when:
- A. The close count is at least `min_closes` OR expectancy is above the bar
- B. The close count is at least `min_closes` AND expectancy is strictly above `min_expectancy` (exactly-at-bar fails)
- C. Win rate exceeds 50%
- D. Expectancy is non-negative, regardless of count

**Q7.** A close record missing any required field (e.g. `peak_mult`) causes:
- A. The row to be silently skipped
- B. The field to be treated as 0
- C. A `KeyError` from the public function that needs it — a corrupt row is loud
- D. The whole ledger to be treated as empty

**Q8.** For win/loss streaks, a breakeven-band close (1.0 ≤ realised < 1.5):
- A. Counts as a win
- B. Counts as a loss
- C. Breaks both the win streak and the loss streak
- D. Is ignored, so streaks continue through it

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** On an empty ledger, `win_rate` returns `None`.

**Q10.** A close peaking at 2.5x and realising exactly 1.0x is a round-tripper.

**Q11.** `pnl_usd(closes, shot_usd=0)` raises `ValueError`.

**Q12.** `lane_ranking` ranks lanes primarily by win rate.

### Short answer (Q13–Q17)

**Q13.** The fixture's two lanes have identical win rates (0.40) but different expectancy (degen +0.10, steady +0.36). How is that possible?

**Q14.** Why is a "win" defined at 1.5x rather than 1.0x, and which function reports the ≥1.0x share instead?

**Q15.** A close peaks at 2.4x and realises 0.9x. Which report counts it, and what does its presence say about entries versus exits?

**Q16.** Why does the module crash on a row missing `peak_mult` instead of skipping the row?

**Q17.** How is `max_drawdown_usd` defined, including where the curve starts?

### Compute it (Q18–Q20) — show your working, using the fixture in the header

**Q18.** Compute the fixture's overall win rate, expectancy, and rug rate.

**Q19.** Compute the fixture's profit factor (show gross gains and gross losses separately).

**Q20.** Compute the fixture's capture rate and the round-trippers' average realised multiple. Also state total P/L if shots were $5 instead of $1.

---

## ANSWER KEY

**Q1. B** — One fixed win definition everywhere (`WIN_MULT = 1.5`, `PEAK_MULT = 2.0`) so lanes are comparable.

**Q2. C** — Expectancy = mean(realised) − 1; e.g. the fixture mean is 1.23, so expectancy is +0.23 per $1 shot.

**Q3. C** — Peak exactly 2.0x with realised 0.999 counts; realised exactly 1.0x or peak 1.999x does not.

**Q4. B** — Nothing peaked means nothing to capture, which is not the same as capturing nothing (that case returns 0.0).

**Q5. B** — Gains with no losses → `inf`; no gains and no losses → `None`; no gains with losses → 0.0. Exactly-1.0 closes feed neither side.

**Q6. B** — The pre-registered gate needs both: enough closes AND expectancy strictly above the bar.

**Q7. C** — Every public function raises `KeyError` on a missing required field — fail-loud, never silently averaged away.

**Q8. C** — Wins need ≥1.5 and losses need <1.0, so the 1.0–1.5 band is neither and breaks both runs.

**Q9. False** — An empty ledger returns 0.0 for rates (win rate, breakeven rate, rug rate); it is medians/means/averages with nothing to average that return `None`.

**Q10. False** — Round-tripping requires realised strictly < 1.0; exactly 1.0x is breakeven, not a round-tripper.

**Q11. True** — `pnl_usd` (and the cumulative curve) reject a non-positive shot size with `ValueError`.

**Q12. False** — Ranking is by expectancy first, then close count, then lane name; win rate is not the sort key.

**Q13.** Win rate counts only how many closes cleared 1.5x; expectancy also weighs *how much* each close won or lost. Steady's wins/losses are sized differently from degen's (steady holds a 3.2x win and shallower ordinary losses), so the same 2-wins-in-5 frequency yields a higher mean realised multiple (1.36 vs 1.10) and therefore higher expectancy.

**Q14.** 1.0x is mere breakeven — getting your stake back is not a win, and after real-world costs it would be a loss; 1.5x is a meaningful profit bar. The ≥1.0x share is reported separately by `breakeven_rate()`.

**Q15.** It is counted in `round_trippers()` and in the leakage report (`leakage_report`: `n_round_tripped`, and its realised multiple feeds `avg_realized_round_trippers`). A position that saw 2.4x and closed a loser was entered fine — the failure is in the exit policy (nothing locked in the peak), not the entry.

**Q16.** Averaging over a silently-dropped or zero-filled row would corrupt every statistic while looking plausible. Raising `KeyError` makes ledger corruption loud at the point of use, so it gets fixed instead of quietly biasing lane verdicts.

**Q17.** The largest peak-to-trough fall of the cumulative P/L curve (running P/L in ledger order), measured starting from the curve's 0 start — so an opening losing streak counts as drawdown from zero. Fixture value: 1.6.

**Q18.** Wins (≥1.5): 2.6, 1.8, 1.6, 3.2 = 4 of 10 → **win rate 0.40**. Sum of realised = 5.5 (degen) + 6.8 (steady) = 12.3; mean 1.23 → **expectancy +0.23**. Rugs: 2 of 10 → **rug rate 0.20**.

**Q19.** Gross gains (realised − 1 over closes > 1.0): 1.6 + 0.8 + 0.6 + 0.1 + 2.2 = **5.3**. Gross losses (1 − realised over closes < 1.0): 0.6 + 1.0 + 0.3 + 0.1 + 1.0 = **3.0**. Profit factor = 5.3 / 3.0 ≈ **1.767**.

**Q20.** Peaked ≥2x: 5 closes; wins among them: 2.6, 1.8, 3.2 = 3 → **capture rate 3/5 = 0.60**. Round-trippers: realised 0.4 and 0.9 → mean = 1.3 / 2 = **0.65x**. P/L scales linearly with shot size: +$2.30 at $1 → **+$11.50 at $5**.

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Naming a function without explaining the mechanism scores 0.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown from the fixture numbers. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, recompute the full fixture scorecard by hand, and retake the full exam (not just the missed questions) no sooner than the next study session.
