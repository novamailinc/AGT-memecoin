# Exam — Sniper-Window Economics

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.
- **Model defaults:** peak 10x, rise 120s, decay half-life 180s, round-trip cost 4%. Net returns apply the cost multiplicatively: net = (exit/entry) × (1 − cost) − 1.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** During the rise, the price multiple at time t is:
- A. `1 + (peak − 1) × t / rise`
- B. `peak ** (t / rise)` — a geometric rise from 1x to the peak
- C. `peak × t / rise`
- D. `peak ** (1 − t / rise)`

**Q2.** `delay_bucket(300)` and `delay_bucket(1)` return, respectively:
- A. `polling` and `fast_polling`
- B. `copy_window_2_10min` and `sniper`
- C. `late` and `sniper`
- D. `copy_window_2_10min` and `fast_polling`

**Q3.** `fraction_of_move_remaining` is measured on what scale, and its value at the peak is:
- A. Linear share of the peak multiple; 0.5 at the peak
- B. Log (exponent) scale; 1.0 at the peak
- C. Log (exponent) scale; 0.0 at and after the peak
- D. Share of clock time elapsed; 0.0 at launch

**Q4.** The best-case net return for entering exactly at the peak (delay = 120s, exit at the peak) is:
- A. 0%
- B. +4%
- C. −4.0% — you pay the full round-trip cost and gain nothing
- D. It raises `ValueError`

**Q5.** The break-even solver's answer at the defaults is approximately:
- A. 2.0s
- B. 60s
- C. 117.9s — and it still assumes you sell the exact peak
- D. 600s

**Q6.** In the seeded expectancy simulation, the rug share of synthetic launches is:
- A. 10%
- B. 35%
- C. 50%
- D. 0% — rugs are excluded

**Q7.** The seeded copy-window (2–10 min) mean expectancy is approximately:
- A. −52.1% — negative overall and at each of 120s / 300s / 600s individually
- B. −5.2%
- C. +11.2%
- D. Slightly positive, but too risky to use

**Q8.** Real snipers fill at, and polling-based detection alone cannot land before:
- A. Snipers 10–15s; polling 60s
- B. Snipers 0.4–2s; polling ≥15–30s
- C. Snipers 30s; polling 2 minutes
- D. Snipers and polling both land at ~5s

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** `net_return_at_peak_pct(180)` returns a negative number, since the trader entered after the peak.

**Q10.** At 60s into the default 120s rise, exactly half of the (log) move remains.

**Q11.** The model is deliberately pessimistic toward late entrants, which is why the copy window looks bad.

**Q12.** Raising round-trip costs pushes the break-even delay later, giving late entrants more room.

### Short answer (Q13–Q17)

**Q13.** The break-even solver says a 117.9s entry can still break even. Why does that not rescue the 2–10 minute window?

**Q14.** Which generous assumption does the expectancy simulation grant early entrants, and why does granting it make the copy window's negative verdict *stronger*, not weaker?

**Q15.** A polling detector improves from 30s to 15s. What does it gain in entry multiple on the default path, and why is it still not competing with a 1s sniper?

**Q16.** Why is the move measured on a log (exponent) scale rather than as a linear share of the peak multiple?

**Q17.** In the demo, entering at 120s, 300s, or 600s and holding 60s all give the same net (−23.8%). Why are the three numbers identical?

### Compute it (Q18–Q20) — show your working, defaults apply

**Q18.** Compute the entry multiple and `fraction_of_move_remaining` at a 30s delay.

**Q19.** Compute the best-case net return at the peak for (a) an instant entry (delay 0) and (b) a 60s delay.

**Q20.** A trader enters at the peak (120s) and holds exactly one half-life (180s). Compute the net return.

---

## ANSWER KEY

**Q1. B** — Geometric rise: `multiple(t) = peak ** (t / rise)`; after the peak, `peak * 0.5 ** ((t − rise) / half_life)`.

**Q2. B** — Buckets: sniper ≤ 2s, fast_polling < 15s, polling 15–<120s, copy_window_2_10min 120–600s, late > 600s.

**Q3. C** — It is the share of the exponent still ahead: 1.0 at launch, 0.0 at/after the peak.

**Q4. C** — Entry multiple = exit multiple = 10x, so gross is 0 and the multiplicative 4% cost leaves exactly −4.0%.

**Q5. C** — ≈117.9s at the defaults — inside the rise, before the copy window even opens, and conditional on selling the exact peak.

**Q6. B** — 35% of the seeded launches rug (exit near zero) regardless of timing.

**Q7. A** — ≈−52.1% mean across the window; negative at every tested delay inside it, with 0.0% win rate in the demo table.

**Q8. B** — Snipers fill in 0.4–2s (the `sniper` bucket); polling detection alone is ≥15–30s before execution is even added.

**Q9. False** — It raises `ValueError`: it refuses to sell you a peak that happened before you entered, rather than returning a misleading number.

**Q10. True** — Fraction remaining = 1 − 60/120 = 0.5 on the exponent scale; the price there is √10 ≈ 3.16x, i.e. half the exponent is gone.

**Q11. False** — The model is deliberately *generous* to the late entrant (notably granting peak exits where used); the negative verdict survives generosity, which is the point.

**Q12. False** — Higher costs move the break-even delay *earlier* (the entry multiple that still covers costs is lower, so it is reached sooner); the copy window, already deep in decay, barely notices.

**Q13.** 117.9s is the latest entry whose *best case* — selling the exact peak, which nobody times — merely covers costs. The copy window starts at 120s, already past break-even even under that fantasy, and a realistic entrant holds through the decay instead of exiting at the peak. A break-even bound built on a perfect exit cannot rescue a window defined by imperfect, late exits.

**Q14.** Early entrants are granted the peak exit for free. If, even with the exit every trader wishes they had, the late window is structurally negative while early buckets are positive, the verdict cannot be argued away as pessimism about exits — the entry timing itself is what fails.

**Q15.** Entry multiple falls from 10^(30/120) ≈ 1.78x to 10^(15/120) ≈ 1.33x — a real gain. But a 1s sniper pays ≈1.02x and keeps >98% of the move; the 15s poller still pays a third more than launch price with only 87.5% of the move left, and polling has a hard floor (~15–30s detection) that no tuning removes. It is competing in a different bucket, not with snipers.

**Q16.** Multiples compound multiplicatively, so equal time slices of a geometric rise multiply price by equal factors. On the exponent scale, half the rise time gone means the entry is already at √peak — a linear share of the peak multiple would hide how much of the *gain* (which lives in the exponent) is already spent.

**Q17.** All three entries are post-peak, and over any fixed 60s hold inside the pure decay regime the exit/entry ratio is the same: 0.5 ** (60/180) ≈ 0.794, regardless of where in the decay you start. Net = 0.794 × 0.96 − 1 ≈ −23.8% in all three cases.

**Q18.** Entry multiple = 10 ** (30/120) = 10^0.25 ≈ **1.78x**. Fraction remaining = 1 − 30/120 = **0.75** (75% of the log move still ahead).

**Q19.** Net = (exit/entry) × 0.96 − 1, in %. (a) Instant: entry 1x, exit 10x → 10 × 0.96 − 1 = **+860%**. (b) 60s: entry = 10^0.5 ≈ 3.162x → (10 / 3.162) × 0.96 − 1 = 3.162 × 0.96 − 1 ≈ **+203.6%** (matches the demo ladder).

**Q20.** After one half-life the price halves: exit multiple = 5x against a 10x entry. Net = (5/10) × 0.96 − 1 = 0.48 − 1 = **−52%** (exactly −52% after costs, as the tests pin).

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Answers that say only "the window is bad" without the mechanism score 0.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown using the geometric path and the multiplicative cost. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, recompute the full demo delay ladder by hand, and retake the full exam (not just the missed questions) no sooner than the next study session.
