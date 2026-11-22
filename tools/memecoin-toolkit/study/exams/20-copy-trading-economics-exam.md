# Exam — Copy-Trading Economics

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** The synthetic price path is generated as:
- A. A random walk with no drift, floored at 0
- B. Starting at 1.0, 5-second steps: pump drift +3%/step for the first 25 steps, bleed −1.2%/step to step 200, then −0.4%/step, volatility 3%/step, floored at 0.02
- C. A pump of +3%/step for 200 steps, then flat
- D. A seeded path that differs on every run with the same seed

**Q2.** The lag buckets' extra slippage (and the leader's) are:
- A. Leader 0.005; 15s 0.01, 1m 0.02, 5m 0.05, 15m 0.15
- B. Leader 0.05; 15s 0.10, 1m 0.15, 5m 0.20, 15m 0.25
- C. All buckets pay the leader's 0.005
- D. Leader 0.005; every follower pays 0.15 regardless of lag

**Q3.** `lag_to_steps(300)` (a 5-minute lag) returns:
- A. 5
- B. 30
- C. 60
- D. 300

**Q4.** The exit ladder in `simulate_copy` is:
- A. Sell everything at 2x; otherwise hold to the end
- B. Sell 50% when market price first reaches 2× entry spot; the rest rides a 30% trailing stop from the peak; if 2× never comes, cut everything at a −50% hard stop; anything left exits at the final price
- C. Sell 50% at 1.5x, 50% at 3x, no stops
- D. A 30% trailing stop on the whole position from entry

**Q5.** The headline expectancy over 200 seeded paths is:
- A. Leader +31.2%; copies: 15s +6.2%, 1m −10.3%, 5m −13.4%, 15m −15.7%
- B. Leader +31.2%; copies: 15s −6.2%, 1m −50.3%, 5m −53.4%, 15m −55.7% — negative at every lag, worsening monotonically
- C. Leader −31.2%; all copies worse
- D. Leader +3.12%; copies near zero

**Q6.** `is_copying_viable(seeds, lag_label)` is:
- A. True for the 15s bucket only
- B. True whenever the leader's expectancy is positive
- C. False for every bucket in this lab — True requires that bucket's own expectancy to be positive
- D. True for lags under 1 minute

**Q7.** `simulate_copy` raises `ValueError` for all of these EXCEPT:
- A. A path with fewer than 2 prices
- B. An entry index at the last price
- C. Slippage of exactly 0
- D. Slippage of 1.0

**Q8.** `expectancy(returns)` and `win_rate(returns)` on an empty list:
- A. Return 0.0
- B. Return `None`
- C. Raise `ValueError`
- D. Return the leader's expectancy

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** On any single path, a follower can never beat the leader on that same path.

**Q10.** "The leader wins 62% of the time" is, by itself, evidence that copying the leader is profitable.

**Q11.** Lag alone — with slippage set equal (even zero) — already costs the follower expectancy in this model.

**Q12.** The 15m bucket is roughly three times as bad as the 5m bucket, proportionally to its lag.

### Short answer (Q13–Q17)

**Q13.** The leader's expectancy is +31.2% and the 15-second copy already loses money (−6.2%). Which two distinct costs turn the same signal negative that fast, and which one dominates by the 1-minute bucket?

**Q14.** Ladder triggers are read off the market price relative to entry *spot*, while proceeds are divided by the slippage-inflated effective entry. What would each simpler choice (triggers off effective entry; proceeds off spot) hide?

**Q15.** The 5m and 15m buckets are close together (−53.4% vs −55.7%) compared with the 15s → 1m cliff. What in the exit ladder compresses the tail buckets?

**Q16.** If a copy system could execute at 1-second lag with leader-grade slippage, what does this model predict — and which of its assumptions would you distrust first in the real market?

**Q17.** Why does the live bot require *follower-grade* evidence (not leader-grade evidence) before smart-money agreement could ever add score again?

### Compute it (Q18–Q20) — show your working

**Q18.** From the headline table (leader +31.2%, 1m copy −50.3%), compute the 1-minute lag penalty. Also compute the 15s penalty from the same table.

**Q19.** On a perfectly flat price path (price never doubles and never halves), compute the return for (a) a follower with slippage 0.05 and (b) the leader with slippage 0.005.

**Q20.** Hand-run the ladder: entry spot 1.0, slippage 0, and the path after entry prints 2.0, then 1.4. Compute the trade's return fraction, showing the proceeds of each leg.

---

## ANSWER KEY

**Q1. B** — That seeded path (same seed → same path) is what makes the headline result reproducible and pinnable by tests; fewer than 10 steps or a `None` seed raises `ValueError`.

**Q2. A** — Slippage strictly grows with lag and every follower pays more than the leader's 0.005.

**Q3. C** — At 5s/step: 300 / 5 = 60 steps (likewise 15s → 3, 60s → 12, 900s → 180).

**Q4. B** — Exactly that ladder; triggers are read off the market price relative to entry spot.

**Q5. B** — Delayed copying is negative at *every* lag and worsens monotonically (leader → 15s → 1m → 5m → 15m).

**Q6. C** — Viability is judged per bucket on the follower's own expectancy, which is negative for all four buckets here.

**Q7. C** — Slippage must lie in [0, 1): 0 is legal, 1.0 is not. The other three options (short path, entry at/past the last price, plus non-positive prices) all raise.

**Q8. C** — Both statistics helpers raise `ValueError` on empty/`None` input rather than inventing a 0.

**Q9. False** — Per-path, a follower sometimes beats the leader: e.g. the leader can be cut by the −50% hard stop on an early dip that the later-entering follower never experiences, after which the follower catches the recovery/double the leader missed. (This is why the headline claim is an aggregate test, not a per-path one.)

**Q10. False** — The leader's win rate describes the leader's trade (their entry price, their timing). The follower's trade is a different trade — later entry, extra slippage, the bleed inherited — and its win rate collapses with lag; leader-grade statistics say nothing about follower expectancy.

**Q11. True** — The tests isolate it: with slippage zeroed/equalised, the lagged entry alone still costs the follower, because by entry time the pump is spent and the remaining path is the bleed.

**Q12. False** — −53.4% vs −55.7% is a small gap despite a 3× lag difference; the ladder's downside machinery compresses the tail (see Q15).

**Q13.** (1) **Price movement / staleness:** the follower buys after the pump has already moved — the information is stale and the entry spot is higher, with the bleed ahead. (2) **Extra slippage:** followers pay more than the leader (0.01 at 15s, growing with lag), charged on the effective entry so it drags every exit. By the 1-minute bucket the price-movement cost dominates: the penalty is −81.5 points against a slippage of only 0.02 — the pump (~2 minutes long) is essentially over and the follower is buying the top.

**Q14.** Triggers off the *effective* entry would move the 2x and −50% levels up by the slippage, hiding the fact that the market events themselves (the price doubling/halving versus spot) happen when they happen, regardless of what you paid. Proceeds off *spot* would erase the slippage entirely, reporting returns as if the inflated entry had never been paid. The split keeps both truths visible: the market triggers on spot; your money settles on what you actually paid.

**Q15.** The ladder bounds the downside: a position that never doubles is cut at the −50% hard stop, and anything surviving exits at the final (deeply bled) price, so returns cluster in a narrow band around −50%. Once a bucket is late enough to live entirely in the bleed, extra lag mostly changes *which* bounded exit fires, not how bad it can get — hence −53.4% and −55.7% sit close together.

**Q16.** Prediction: expectancy near the leader's — at ~1s the entry spot is barely above t0 and, with leader-grade slippage, the follower's trade approximates the leader's own, which is +31.2% in this model. First assumption to distrust: the path is exogenous — in the real market the leader's own buying (and a crowd of copiers piling in) *moves* the price, leaders are selected with survivorship bias, and real fills/liquidity at 1s are nothing like a clean seeded path.

**Q17.** Because the leader's record and the follower's record are different trades: the copied wallets' edge *is* their entry price and speed, and delayed cluster copying averaged roughly −41% in the underlying research. Tracked-wallet buys therefore remain display/journal/grading signals (zero score) until evidence measured on *followers'* outcomes — at real lags, with real slippage — shows copying itself pays.

**Q18.** Penalty = follower expectancy − leader expectancy. 1m: −50.3 − 31.2 = **−81.5 percentage points**. 15s: −6.2 − 31.2 = **−37.4 percentage points**. (Both match the demo's penalty table; penalties deepen with lag.)

**Q19.** On a flat path nothing triggers, so the whole position exits at the final price = entry spot: return = 1/(1 + slippage) − 1. (a) Follower: 1/1.05 − 1 ≈ **−0.0476 (−4.76%)** — a flat path loses exactly the slippage. (b) Leader: 1/1.005 − 1 ≈ **−0.00498 (−0.50%)**.

**Q20.** At print 2.0: multiple = 2.0 ≥ 2× → sell 50%: proceeds += 0.5 × (2.0 / 1.0) = **1.0**; remaining 0.5; peak = 2.0. At print 1.4: 1.4 ≤ peak × (1 − 0.30) = 1.4 → trailing stop fires: proceeds += 0.5 × (1.4 / 1.0) = **0.7**. Total proceeds 1.7 on a 1.0 stake → **return +0.70 (+70%)**.

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Answers that say only "copying is bad" without the deciding mechanism score 0.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown using the model's ladder, slippage, and expectancy definitions. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, hand-run the ladder on a pump-then-bleed path, and retake the full exam (not just the missed questions) no sooner than the next study session.
