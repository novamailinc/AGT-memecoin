# Exam — Exit Ladder Replay

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `07-exit-ladder-replay` README and `replay.py` alone. All fixture paths have p0 = 1.0.

---

## QUESTIONS

**Q1 (MCQ).** The user's ladder (`USER_LADDER`) is exactly:
A. 50% @ 1.5x, 25% @ 2x, 25% trailing (20%, arm 1.5x); stop 0.6x; cap 240 min
B. 50% @ 2x, 25% @ 3x, 25% trailing (30%, arm 2.0x); hard stop 0.5x; time cap 1440 min
C. 100% hold; stop 0.5x; cap 1440 min
D. 50% @ 2x, 50% @ 3x; no trailing tranche

**Q2 (MCQ).** `FAST_HARVEST` differs from `USER_LADDER` in that it has:
A. Tranches 50% @ 1.5x / 25% @ 2.0x / 25% trail 20% armed at 1.5x, hard stop 0.6x, time cap 240 min
B. A lower hard stop and a longer cap
C. No take-profits
D. The same tranches but a 0.5x stop

**Q3 (Compute).** Clean runner under `user_ladder` finishes with legs 50% @ 2.00x (tp), 25% @ 3.00x (tp), 25% @ 3.50x (trail). Compute the realized multiple, showing the weighted sum.

**Q4 (Compute).** Round-tripper under `user_ladder`: legs 50% @ 2.00x (tp), 25% @ 1.82x (trail), 25% @ 0.50x (hard_stop). (a) Realized multiple? (b) The same path under `hold_to_stop` realizes what, and in one phrase, what leak does the contrast prove?

**Q5 (MCQ).** The trailing remainder in `USER_LADDER` arms only at 2.0x because:
A. Trailing below 2x is mathematically impossible
B. Otherwise a coin that never ran would "trail" out at e.g. 0.77x on its first dip, and the hard-stop behaviour on a straight rug could never fire cleanly — below the arm, the hard stop alone protects the position
C. The exchange requires it
D. It makes the replay faster

**Q6 (Compute).** Peak reaches 5.0x (p0 = 1.0). The `USER_LADDER` trailing tranche (30%) exits at what price/multiple when price falls to the trail price? Show the formula.

**Q7 (True/False + justification).** "A dip to the would-be trail price while the peak is only 1.99x trails out the remainder." True or false? Justify. What changes at exactly 2.0x?

**Q8 (MCQ).** Fill-price conventions: a take-profit tranche fills at:
A. The overshooting bar's actual price
B. Its target price (target × p0) — the conservative choice
C. The running peak
D. The bar after the trigger

**Q9 (MCQ).** A bar gaps straight from 1.9x to 0.4x on `USER_LADDER`. Which fills fire?
A. Both TPs at their targets, then the trail
B. The hard stop fires at the stop price (0.5 × p0) and exits ALL remaining tranches at once; no TP fires (price never reached 2x on or before this bar in a fillable way above the stop — the stop is evaluated on this bar)
C. Nothing — gaps are ignored
D. The position exits at 0.4x, the bar's price

**Q10 (Short answer).** Why is filling a take-profit at its target (not the bar's price) the conservative choice for a replay?

**Q11 (MCQ).** No-look-ahead means:
A. The engine may use the bar after the current one for stops
B. A fill only ever uses bars up to and including the current bar; bars after the final exit are never seen, and truncating the path at the exit bar reproduces the full result
C. Paths are sorted by peak before replay
D. The peak used for trailing is the path's eventual peak

**Q12 (Compute).** Batch over all 4 demo paths under `user_ladder` realizes: round-tripper 1.58x, clean runner 2.625x, straight rug 0.5x, slow grinder 1.9x. Compute (a) `mean_mult`; (b) `win_rate` (share of paths with realized > 1.0); (c) `median_time_to_peak_min` given the four times-to-peak are 10, 50, 60, 1200 minutes.

**Q13 (True/False + justification).** "The slow grinder's post-cap crash (after t = 1440) can reduce its `user_ladder` result." True or false? Justify.

**Q14 (MCQ).** A path ends at t = 300 with no exit triggered and no cap reached (`USER_LADDER` cap 1440). Remaining tranches exit:
A. At the hard-stop price, reason `hard_stop`
B. At that final bar's actual price, reason `path_end`
C. At spot 1.0x, reason `time_cap`
D. They never exit; the result is invalid

**Q15 (MCQ).** `replay` raises `ValueError` for all of these EXCEPT:
A. Empty path
B. Path not starting at t = 0
C. Non-increasing timestamps
D. A path whose entry price is 2.0 instead of 1.0 (entry-price scaling is legitimate — results are scale-invariant)

**Q16 (Compute).** Fast-harvest on the round-tripper: legs 50% @ 1.50x, 25% @ 2.00x, 25% @ 2.08x (trail). Compute the realized multiple.

**Q17 (Short answer).** On the straight rug under `user_ladder`, why are there THREE legs all at 0.50x with reason `hard_stop`, while `hold_to_stop` shows ONE leg at 0.50x?

**Q18 (MCQ).** `validate_ladder` rejects a custom ladder when:
A. Tranche fractions sum to 0.8 (or anything ≠ 1.0 within 1e-6), a TP target ≤ 1.0, a trail % outside (0, 1), a hard stop outside (0, 1), or a non-positive time cap — any one suffices
B. Only when fractions sum to more than 1.0
C. Never — custom ladders are trusted
D. The ladder has more than 3 tranches

**Q19 (True/False + justification).** "`is_round_tripper(result)` is true for the demo batch's round-tripper under `user_ladder` (batch reports `round_trippers: 0`)." True or false? Justify from the batch output / definition implied by it.

**Q20 (MCQ).** `max_drawdown(path)` is measured from:
A. The path's eventual (future) peak
B. The RUNNING peak — the highest price seen up to each bar, never a future one
C. The entry price only
D. The final price

**Q21 (Compute).** `time_above_multiple` uses the discrete-bar interval convention. A path has bars at t = 0 (1.0x), 10 (2.5x), 20 (2.2x), 30 (1.0x). Using the convention that each bar's price holds until the next bar, how many minutes are spent at/above 2.0x?

**Q22 (MCQ).** `replay_batch([], ladder)` returns:
A. It raises
B. Zeros — `mean_mult` 0, `win_rate` 0, `round_trippers` 0, `median_time_to_peak_min` 0 — no crash
C. The demo batch stats
D. `None`

---

## ANSWER KEY

- **Q1: B** — Exact `USER_LADDER` constants in `replay.py`.
- **Q2: A** — Exact `FAST_HARVEST` constants (earlier/smaller harvest, tighter 0.6 stop, 240-min cap).
- **Q3: 2.625x.** Working: 0.50×2.00 + 0.25×3.00 + 0.25×3.50 = 1.000 + 0.750 + 0.875 = 2.625.
- **Q4: (a) 1.58x** (0.50×2.00 + 0.25×1.82 + 0.25×0.50 = 1.000 + 0.455 + 0.125 = 1.580). **(b) 0.5x** under hold-to-stop; the leak: a coin that doubles still closes a loser if nothing is harvested on the way up — the ladder turns the identical path into a winner.
- **Q5: B** — The arm exists so pre-run dips are governed by the hard stop alone.
- **Q6: 3.50 (× p0).** Working: trail price = peak × (1 − trail_pct) = 5.0 × (1 − 0.30) = 5.0 × 0.70 = 3.50.
- **Q7: False** — Below the 2.0x arm the trailing tranche is unarmed, so the dip does not trail out (pinned by tests three ways). At exactly 2.0x the tranche arms, and a fall to peak × (1 − 0.30) fills at that trail price.
- **Q8: B** — TPs fill at target, trail at trail price, stops at stop price, cap/path-end at the bar's price.
- **Q9: B** — The hard stop is evaluated on the bar and exits everything at the stop price 0.5×p0; fills never use the gapped-through bar price for the stop, and no TP target above the bar was reached on an earlier bar.
- **Q10:** Crediting the overshoot would flatter the policy with fills a real thin book rarely gives at size; target-price fills understate rather than overstate the ladder's value, so a positive replay result is trustworthy.
- **Q11: B** — Strict time order, current bar only.
- **Q12: (a) 1.65125** ((1.58 + 2.625 + 0.5 + 1.9)/4 = 6.605/4). **(b) 0.75** (3 of 4 paths realized > 1.0; only the rug lost). **(c) 55.0 min** (sorted 10, 50, 60, 1200 → median = (50+60)/2 = 55).
- **Q13: False** — The position is closed at the 1440-minute cap at that bar's price (slow grinder legs exit `time_cap` at 1.80x); bars after the cap/exit are never seen.
- **Q14: B** — Path-end exits fill at the final bar's price with reason `path_end`.
- **Q15: D** — Entry-price scaling is explicitly supported (invariance tested; stop price scales with p0). All the others raise.
- **Q16: 1.77x.** Working: 0.50×1.50 + 0.25×2.00 + 0.25×2.08 = 0.750 + 0.500 + 0.520 = 1.770.
- **Q17:** The hard stop exits ALL remaining tranches at once, but `USER_LADDER` records them as one leg per tranche (three tranches → three 0.50x hard-stop legs summing to 100%); `HOLD_TO_STOP` has a single 100% tranche → a single leg.
- **Q18: A** — All listed malformations raise before a bar is replayed.
- **Q19: False** — The batch over the 4 demo paths reports `round_trippers: 0` for `user_ladder`: a round-tripper result is one that peaked ≥2x yet realized < 1.0x (still closed a loser); under the ladder the round-tripper path realized 1.58x, so it no longer classifies as a round-tripper. (Under hold-to-stop at 0.5x it would.)
- **Q20: B** — Running peak only; using the future peak would be look-ahead.
- **Q21: 20 minutes.** Bars at t=10 (2.5x) and t=20 (2.2x) are ≥2.0; each holds for a 10-minute interval (10→20, 20→30); the t=0 and t=30 bars are below. 10 + 10 = 20.
- **Q22: B** — Empty batch returns zeros, no crash.

---

## GRADING RUBRIC

- **MCQ (Q1, Q2, Q5, Q8, Q9, Q11, Q14, Q15, Q18, Q20, Q22):** 1 point for the correct letter only.
- **True/False + justification (Q7, Q13, Q19):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q10, Q17):** 1 point if the key mechanism is stated.
- **Compute-it (Q3, Q4, Q6, Q12, Q16, Q21):** 1 point per question; ALL sub-parts numerically exact (multiples to 3 d.p.). Working must be shown for Q3, Q4, Q12.
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and the ladder constants in `replay.py`, then retake the full exam.
