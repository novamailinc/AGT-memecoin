# Exam — Liquidity Velocity

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** `inflow_rate(events)` is:
- A. Total inflow ÷ a fixed 5-minute window
- B. Total SOL in ÷ the span in minutes from the first to the last event (input sorted first)
- C. The largest single event ÷ minutes
- D. Total inflow ÷ trade count

**Q2.** The predictor's constants are:
- A. `MIN_TRADES = 5`, graduation target 100 SOL
- B. `MIN_TRADES = 10`, graduation target 85 SOL
- C. `MIN_TRADES = 20`, graduation target 85 SOL
- D. `MIN_TRADES = 10`, graduation target 69 SOL

**Q3.** `velocity_band()` maps rates (SOL/min) as:
- A. slow < 1, steady < 3, fast < 10, igniting ≥ 10
- B. slow < 0.5, steady < 2.0, fast < 5.0, igniting ≥ 5.0 (a rate exactly at a boundary takes the higher band)
- C. slow ≤ 0.5, steady ≤ 2.0, fast ≤ 5.0, igniting > 5.0
- D. Bands are percentiles of the stream itself

**Q4.** `graduation_score()` (0–100) is built from:
- A. Inflow pace only, up to 100 points
- B. Inflow pace up to 60 points across the calibrated bands, plus trade pace up to 40 points
- C. Trade pace up to 60, inflow up to 40
- D. A machine-learned model over the events

**Q5.** `predict_graduation(events)` by default calls graduation when:
- A. Score > 50
- B. Score ≥ threshold, default threshold 60.0
- C. Inflow rate ≥ 5 SOL/min, whatever the score
- D. The score is not `None`

**Q6.** In the calibration set, graduate and non-graduate inflow rates are drawn around, and a 2.0 SOL/min cut classifies them with accuracy:
- A. Graduates ~0.8, non-graduates ~4.0 SOL/min; accuracy 0.50
- B. Graduates ~4.0, non-graduates ~0.8 SOL/min; accuracy 0.97 on the default seed
- C. Graduates ~4.0, non-graduates ~0.8 SOL/min; accuracy 0.60
- D. Both around 2.0 SOL/min; accuracy 0.97

**Q7.** The calibrated cost reckoning is:
- A. Gross edge +0.040, cost 0.03, net +0.010 — clears breakeven
- B. Gross edge +0.030, round-trip cost 0.04, edge after costs −0.010 — does not clear breakeven
- C. Gross edge −0.030, cost 0.04, net −0.070
- D. Gross edge +0.30, cost 0.04, net +0.26

**Q8.** `minutes_to_graduation()` returns `None` when:
- A. The inflow rate is zero
- B. There are fewer than 10 events
- C. The target has already been collected
- D. The stream is unsorted

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** `inflow_rate` works fine on a single-event stream, returning that event's amount.

**Q10.** A predictor whose gross edge exactly equals the round-trip cost clears breakeven.

**Q11.** `predict_graduation` on a 9-event stream returns a scored "will not graduate" call.

**Q12.** `velocity_band(5.0)` returns `"fast"`.

### Short answer (Q13–Q17)

**Q13.** The calibration accuracy is 0.97 and the predictor still loses money. In one sentence each: what does the accuracy measure, and what does it not measure?

**Q14.** A launch has 3 events totalling 90 SOL in 20 seconds — an enormous raw inflow rate. Why does the predictor still refuse to score it?

**Q15.** `inflow_rate` divides by the first-to-last span rather than a fixed window. What stream shape does that choice flatter?

**Q16.** Why does `minutes_to_graduation` return `None` once the target is collected rather than 0?

**Q17.** Raw inflow rate and trades-normalised velocity disagree about which of two launches is "faster". Which measure would you trust for spotting *manufactured* pace, and why?

### Compute it (Q18–Q20) — show your working

**Q18.** A stream has 20 events spanning exactly 10 minutes (first to last) and totalling 50 SOL in. Compute inflow rate and its band, trade rate, mean trade size, progress rate (target 85 SOL), and minutes to graduation.

**Q19.** A scored stream has inflow rate 3.0 SOL/min and trade rate 6 trades/min. Using the score bands (inflow: ≥5 → 60, ≥2 → 45, ≥0.5 → 25, below → 5; trade pace: ≥20 → 40, ≥10 → 30, ≥5 → 18, ≥2 → 8, below → 0), compute `graduation_score` and the default `predict_graduation` call.

**Q20.** Using the calibrated gross edge (+0.030): (a) compute the edge after costs at the default 4% cost and state whether it clears breakeven; (b) compute it at a 2% cost; (c) at a 6% cost, what gross edge would a predictor need to clear breakeven?

---

## ANSWER KEY

**Q1. B** — Rate = total inflow over the stream's own first-to-last span in minutes; the rate functions raise on `None`/empty/single-event/zero-duration streams.

**Q2. B** — `MIN_TRADES = 10`, `GRADUATION_TARGET_SOL = 85.0`.

**Q3. B** — Bands at 0.5 / 2.0 / 5.0 SOL/min, with a rate exactly at a boundary taking the next band up (0.5 → steady, 2.0 → fast, 5.0 → igniting). Negative/`None` rates raise `ValueError`.

**Q4. B** — 60 points of inflow pace (bands: ≥5 → 60, ≥2 → 45, ≥0.5 → 25, else 5) plus up to 40 of trade pace, capped at 100.

**Q5. B** — Graduating iff score ≥ threshold; the default threshold is 60.0 and a threshold outside [0, 100] raises `ValueError`.

**Q6. B** — Velocity genuinely separates the classes (graduates ~4.0 vs non-graduates ~0.8 SOL/min) and the 2.0 cut scores 0.97 on the default seed.

**Q7. B** — From the embedded outcome (55 winners at +30%, 45 losers at −30%): gross = 0.55(0.30) + 0.45(−0.30) = +0.030; minus the 0.04 round trip = **−0.010**, so `clears_breakeven()` is False.

**Q8. C** — Once collected ≥ target there is no "minutes remaining" to estimate; the function returns `None` rather than a number.

**Q9. False** — A single event has zero duration, and the rate functions raise `ValueError` rather than dividing by nothing (likewise for empty/`None` streams).

**Q10. False** — `clears_breakeven` requires the after-cost edge to be *strictly* greater than zero; exactly at cost does not clear.

**Q11. False** — Below `MIN_TRADES` (10) the prediction is `{"status": "unknown", "score": None, "will_graduate": None}` — it makes no call at all.

**Q12. False** — 5.0 is the igniting boundary and boundaries take the higher band: `velocity_band(5.0) = "igniting"`.

**Q13.** Accuracy measures how well the rate cut predicts the *event* — whether the token graduates. It does not measure what a follower *earns* after entry: graduated tokens still mostly bleed afterward, and once the ~4% round trip is charged the +3% gross edge is underwater. Predicting an event is not the same as profiting from it.

**Q14.** The score is fail-closed on sparse data: fewer than `MIN_TRADES` (10) events returns `None`/"unknown" regardless of the rate. Three events cannot establish a pace — one wallet could be the whole "rate" — so the predictor refuses a confident score from thin air.

**Q15.** A front-loaded burst that then dies: because the span ends at the *last* event, any silence after the burst costs nothing in the rate — a launch that fires all its inflow in a short cluster and stops still shows the burst's rate, with the dead tail invisible.

**Q16.** "Already graduated" is a different state from "0 minutes remaining at the current rate": 0 would claim the (meaningless) estimate that the target is being reached right now, and callers could average or rank it as the fastest possible prediction. `None` keeps a completed event out of the estimate population entirely.

**Q17.** Trades-normalised velocity (inflow per minute per 100 trades) — equivalently, checking mean trade size alongside the raw rate. Manufactured pace is typically many tiny trades: raw SOL/min can look respectable while the per-trade size is dust. Normalising by trade count exposes pace that is trade-count-deep but size-shallow.

**Q18.** Inflow rate = 50 / 10 = **5.0 SOL/min → band "igniting"** (≥ 5.0). Trade rate = 20 / 10 = **2.0 trades/min**. Mean trade size = 50 / 20 = **2.5 SOL**. Progress rate = 5.0 / 85 × 100 ≈ **5.88 pts/min**. Minutes to graduation = (85 − 50) / 5.0 = **7.0 minutes**.

**Q19.** Inflow 3.0 ≥ 2.0 → 45 points. Trade pace 6 ≥ 5 → 18 points. Score = 45 + 18 = **63.0**. 63 ≥ default threshold 60 → **`will_graduate = True`** (status "scored").

**Q20.** (a) 0.030 − 0.040 = **−0.010 → does not clear breakeven** (False). (b) 0.030 − 0.020 = **+0.010 → clears** (strictly > 0). (c) Breakeven gross edge = the cost itself, **0.06** — and the predictor would need a gross edge *strictly above* 0.06 to clear.

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Answers that say only "costs eat the edge" without separating event-prediction from profit score 0 on Q13.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown using the module's formulas and bands. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, recompute the demo's fast/slow launches and the calibration reckoning by hand, and retake the full exam (not just the missed questions) no sooner than the next study session.
