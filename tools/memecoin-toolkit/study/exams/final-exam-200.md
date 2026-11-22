# Final Exam — Memecoin Toolkit (cross-toolkit)

- **Questions:** 200, numbered Q1–Q200, organised in toolkit order (01–30), each tagged with its toolkit number, e.g. `[07]`.
- **Scoring:** 1 point per question, 200 points total. No partial credit.
- **PASS BAR: ≥85% = ≥170 correct.** Below 170 is a fail, whatever the raw score feels like.
- **Conditions:** Closed-book. Synthetic data only — every figure in this paper comes from the toolkit labs' pinned fixtures, demos, and tests. No network, no live bot, no real tokens.
- **Question types:** multiple-choice (4 options, one correct), true/false-with-justification (the justification is part of the answer), short-answer, and compute-it questions (marked **(Compute)** — show the working; the number alone is not the answer).
- **Answer key and grading rubric** follow the questions. Do not read them before sitting the paper.

---

## Toolkit 01 — bonding-curve-math [01]

*Focus: spot price, buy cost and price impact on virtual reserves; graduation math.*

**Q1. [01] (MCQ)** The default `Curve` is built with virtual token reserves 1.073e9, virtual SOL reserves 30.0, total supply 1e9, fee 100 bps, graduation target 85 SOL. The spot price in SOL per token is:
- A. `total_supply / virtual_token_reserves`
- B. `virtual_sol_reserves / virtual_token_reserves`
- C. `virtual_token_reserves / virtual_sol_reserves`
- D. `graduation_target_sol / total_supply`

**Q2. [01] (Compute)** On the default curve of Q1, compute (a) the spot price in SOL per token and (b) the spot quote in tokens per SOL.

**Q3. [01] (Compute)** On a zero-fee fixture curve with 1,000,000 virtual tokens and 100 virtual SOL, compute the gross SOL cost to buy exactly 1% of a 1,000,000-token supply (i.e. 10,000 tokens), using the constant-product inverse.

**Q4. [01] (True/False + justification)** "At the default 100 bps fee, buying with SOL and immediately selling everything back loses about 1.99%, and at zero fee the same round trip loses exactly 0%." True or false — and what is the loss made of?

**Q5. [01] (MCQ)** On the default curve, 42.5 SOL of real SOL has been collected. `completion_pct` and `is_graduated` are:
- A. 42.5% complete, graduated = True
- B. 50.0% complete, graduated = True
- C. 50.0% complete, graduated = False
- D. 85.0% complete, graduated = False

**Q6. [01] (Short answer)** Why must a true round-trip measurement price the sell leg on the *post-buy* curve state (`curve_after_buy_sol` first) rather than on the original curve?

**Q7. [01] (MCQ)** `sol_to_reach_spot_price(target)` raises `ValueError` when the target is:
- A. Any value at all — the method never succeeds
- B. Positive and strictly above the current spot price
- C. Positive but at or below the current spot price (buys cannot move price down or sideways), or non-positive
- D. More than double the current spot price

## Toolkit 02 — amm-and-lp [02]

*Focus: constant-product quotes, impact vs slippage, min-out guard, LP-lock verdicts, sandwich cost.*

**Q8. [02] (Compute)** Using `get_amount_out(amount_in=1, reserve_in=100, reserve_out=1_000_000, fee_bps=25)`, compute the tokens out. Show the fee-adjusted input in your working.

**Q9. [02] (MCQ)** `verify_lp_lock` with the default threshold returns verdict `"locked"` (passed) iff the secured share (burned + locker-held) is:
- A. > 50%
- B. ≥ 90%
- C. ≥ 75%, provided the deployer holds none
- D. Any share, provided some LP is burned

**Q10. [02] (Compute)** Compute `impermanent_loss_pct(price_ratio=4.0)` using `2·√r/(1+r) − 1`, in percent. What is the value at r = 0.25, and why?

**Q11. [02] (True/False + justification)** "LP sitting in the deployer's wallet counts toward the secured percentage in `verify_lp_lock`, as long as it is reported." True or false — justify from the rule.

**Q12. [02] (MCQ)** The locker balances were never fetched (`None`). The LP verdict is:
- A. `"locked"`, because nothing contradicts it
- B. `"unlocked"`, passed = False
- C. `"unverifiable"`, passed = False
- D. An exception is raised to the caller

**Q13. [02] (Short answer)** For the same trade, price impact and slippage are different numbers. State what each one includes/excludes, and which is always ≥ the other.

**Q14. [02] (MCQ)** In the demo sandwich estimate, the victim's extra loss was 3.86% in the deep (100 SOL) pool. In the 10× thinner pool at the same spot price it was:
- A. Also 3.86% — spot price is what matters
- B. 0.386% — thinner pools are safer
- C. 29.46% — the same attacker trade moves a thin pool far more
- D. 100% — the victim receives nothing

## Toolkit 03 — token-safety-veto [03]

*Focus: component fails vs flags, Token-2022 traps, fail-closed unknown, score-as-floor.*

**Q15. [03] (MCQ)** The score convention in `evaluate` is:
- A. `100 − 10 × len(fails) − 10 × len(flags)`, floored at 0
- B. `100 − 25 × len(fails) − 5 × len(flags)`, floored at 0 — and `passed` is decided by the absence of fails, never by the score
- C. A weighted average of component grades
- D. `100` if passed, `0` if failed — nothing in between

**Q16. [03] (Compute)** A report produces 2 hard fails and 1 flag. Compute its score and state its `passed` value.

**Q17. [03] (MCQ)** A transfer fee of exactly 1000 bps, with everything else clean, gives:
- A. Hard FAIL `transfer_fee_excessive`
- B. PASS with flag `transfer_fee_present`, score 95
- C. UNKNOWN — the boundary is undecidable
- D. PASS with no flag, score 100

**Q18. [03] (True/False + justification)** "A verdict that FAILED can still meet a score floor of 0 via `meets_score_floor`." True or false — justify.

**Q19. [03] (MCQ)** A report was never fetched (extensions and transfer fee missing). The verdict is:
- A. PASS, score 100 — absence of evidence is clean
- B. FAIL with the missing pieces named as ordinary hard fails
- C. UNKNOWN, passed = False, score 0, with the missing pieces named in `fails`
- D. PASS, score 0 — blocked by the floor only

**Q20. [03] (Short answer)** Name the two extensions/findings that are FLAG-only (never hard fails) in this lab, and the score cost of each flag.

**Q21. [03] (MCQ)** Which of the following is a hard FAIL?
- A. `mintCloseAuthority` extension present
- B. `confidentialTransferMint` extension present
- C. Transfer fee of 500 bps
- D. `permanentDelegate` extension present

## Toolkit 04 — holder-concentration [04]

*Focus: user-only top-1/top-10 concentration, strict verdict boundaries, HHI and Nakamoto coefficient.*

**Q22. [04] (Compute)** Top-1 user share is exactly 50.0% and top-10 user share is 60.0%. Using `verdict_for` (FAIL if top1 > 50 or top10 > 70; FLAG if top1 > 25 or top10 > 55; strict comparisons), state the verdict, and the smallest change to top-1 that would flip it to FAIL.

**Q23. [04] (Compute)** Ten user holders each hold exactly 10% of total supply. Compute the Herfindahl index (HHI, 0–10000 scale, sum of squared percentage shares). What is the HHI of a single monopoly holder?

**Q24. [04] (MCQ)** A token's largest account holds 60% of supply and is an off-curve pool/program account. Its contribution to top-1 *user* concentration is:
- A. 60% — size is size
- B. Nothing — program accounts are excluded from the user numerator entirely
- C. 60%, but flagged rather than failed
- D. Counted at half weight

**Q25. [04] (True/False + justification)** "An account missing from the registry is counted as a user wallet, since most accounts are users." True or false — justify, including what it *is* counted as.

**Q26. [04] (MCQ)** Top-1 user share is 9% but top-10 user share is 72%. The verdict is:
- A. PASS — no single whale
- B. FLAG — top-10 above 55
- C. FAIL — top-10 above 70 fails on its own
- D. UNKNOWN — the two metrics disagree

**Q27. [04] (Short answer)** Name two distinct input problems that each produce verdict `"unknown"` with `passed=False` in `analyze`.

**Q28. [04] (Compute)** Aggregated user shares, biggest first, are 30%, 25%, 10%, 5% (rest negligible). Compute the Nakamoto coefficient at the default 50% threshold (smallest number of top holders whose cumulative share is *strictly greater* than 50%). Would a distribution whose top holders cumulate to exactly 50.0% satisfy it?

## Toolkit 05 — bundle-cluster-detection [05]

*Focus: common-funder union-find clusters, share bands, behaviour-based veto.*

**Q29. [05] (MCQ)** A cluster holds 55% of supply and its wallets are provably holding. The verdict is:
- A. PASS — holding is benign
- B. FLAG — elevated share, good behaviour
- C. VETO — at ≥50% share, share alone vetoes regardless of behaviour
- D. UNKNOWN — behaviour and share conflict

**Q30. [05] (MCQ)** A cluster holds 25% of supply and its flow data failed to load (behaviour unknown). The verdict is:
- A. VETO — 25% is over the 20% line
- B. FLAG — in the 20–50% band, missing flow data degrades to FLAG, never VETO
- C. PASS — no evidence of selling
- D. UNKNOWN — no verdict is returned

**Q31. [05] (Compute)** Four sampled wallets have activity; exactly one is distributing (outflows > inflows). Using the rule "distributing iff `distributing_count × 2 ≥ active_count` and `distributing_count > 0`", classify the behaviour, showing the arithmetic. What changes if two of the four are distributing?

**Q32. [05] (True/False + justification)** "Wallets A, B, C with funders F1, {F1, F2}, F2 respectively form two clusters, because A and C share no funder directly." True or false — justify.

**Q33. [05] (MCQ)** `cluster_share` returns `None` (not 0.0) when total supply is missing/non-positive because:
- A. 0.0 would mean "measured zero share" — a pass-shaped answer built on no measurement
- B. An implementation accident
- C. `None` sorts before numbers
- D. The share is genuinely zero when supply is unknown

**Q34. [05] (Short answer)** Why does missing flow data in the 20–50% band produce a FLAG rather than either a VETO or a silent PASS? Answer in terms of what each wrong choice would manufacture or hide.

**Q35. [05] (MCQ)** The severity order used by `worst_verdict` is:
- A. PASS > FLAG > UNKNOWN > VETO
- B. VETO > FLAG > PASS > UNKNOWN
- C. VETO > UNKNOWN > FLAG > PASS, with empty/unrecognised input failing closed to UNKNOWN
- D. UNKNOWN > VETO > FLAG > PASS

## Toolkit 06 — rug-taxonomy [06]

*Focus: the five rug detectors — exact triggers, windows, weights and risk grades.*

**Q36. [06] (MCQ)** `lp_pull` fires when, within a short window (≤3600s, or ≤3 steps if timestamps are absent):
- A. Liquidity falls ≥50% at any speed
- B. Liquidity falls ≥80% from an earlier snapshot *and* price falls ≥50% over the same pair
- C. Price falls ≥80% and liquidity falls ≥50%
- D. Liquidity falls at all after launch

**Q37. [06] (Compute)** A lifecycle triggers exactly `lp_pull` (weight 35) and `cant_sell` (weight 25). Compute its `rug_score` and its `risk_grade` (bands: 0 clean, 1–24 watch, 25–49 elevated, 50–100 severe). What would the raw sum be if all five detectors fired, and what is it capped at?

**Q38. [06] (True/False + justification)** "Supply inflating by exactly +20% between snapshots, with the mint authority active and price then halving, fires `mint_and_dump`." True or false — name the exact comparison that decides it.

**Q39. [06] (MCQ)** A lifecycle has no `sell_sim_ok` field anywhere. `cant_sell` returns:
- A. Triggered — unreported means unsafe
- B. Not triggered — a missing field is unknown, not a failure; only an explicit boolean `False` fires
- C. Triggered, but with low confidence
- D. An error

**Q40. [06] (MCQ)** Holders fall from 500 to 290 over five snapshots, never more than 15% in any single step. `holder_crash`:
- A. Fires — the total drop exceeds 40%
- B. Does not fire — it requires a ≥40% drop between two *consecutive* snapshots; a slow bleed is a different pattern
- C. Fires at reduced weight
- D. Returns unknown

**Q41. [06] (Short answer)** State all three conditions that must hold together for `wash_skew` to fire on a snapshot, with their thresholds.

**Q42. [06] (MCQ)** A `rug_score` of 30 bands as:
- A. `"clean"`
- B. `"watch"`
- C. `"elevated"`
- D. `"severe"`

## Toolkit 07 — exit-ladder-replay [07]

*Focus: no-look-ahead ladder replay, fill conventions, the round-tripper leak.*

**Q43. [07] (Compute)** On the clean-runner path, the user ladder fills 50% @ 2.00x (tp), 25% @ 3.00x (tp), 25% @ 3.50x (trail). Compute the realized multiple, showing the weighted sum.

**Q44. [07] (Compute)** On the round-tripper path, the user ladder fills 50% @ 2.00x (tp), 25% @ 1.82x (trail), 25% @ 0.50x (hard_stop). Compute the realized multiple, and state what hold-to-stop realizes on the same path.

**Q45. [07] (MCQ)** In this lab the user ladder's trailing remainder arms only at:
- A. Entry (1.0x) — it trails from the start
- B. 1.5x
- C. 2.0x — so a coin that never ran is protected by the hard stop alone
- D. 3.0x, after both take-profits

**Q46. [07] (True/False + justification)** "In the replay, a take-profit fills at the price of the bar that crossed it, which is the conservative choice." True or false — justify.

**Q47. [07] (MCQ)** The slow-grinder path's remaining tranches exit at the 1440-minute mark. That exit reason is:
- A. `path_end`
- B. `time_cap` — the 24h cap; anything after the cap is never seen by the replay
- C. `hard_stop`
- D. `trail`

**Q48. [07] (Short answer)** A path ends at t=300 with no exit triggered and no cap reached. At what price does the remainder exit, and what is that leg's exit reason?

**Q49. [07] (MCQ)** `replay_batch` over the four demo paths under the user ladder reports:
- A. mean_mult 1.65125, win_rate 0.75, median_time_to_peak_min 55.0
- B. mean_mult 2.10, win_rate 0.50, median_time_to_peak_min 50.0
- C. mean_mult 1.25, win_rate 0.25, median_time_to_peak_min 60.0
- D. mean_mult 1.65125, win_rate 0.25, median_time_to_peak_min 120.0

## Toolkit 08 — statistical-validity-meme [08]

*Focus: expectancy and breakeven, p-hacking, power and required sample size, base rates.*

**Q50. [08] (Compute)** A strategy's average win pays a 3x multiple (win pays W−1 = +2 per $1, a loss pays −1). Compute the breakeven win rate using `breakeven_wr(payoff) = 1 / payoff`, and show the derivation in one line.

**Q51. [08] (Compute)** Compute `expectancy([2.5, 0.0, 1.2, 0.4, 3.0, 0.0, 0.8])` — the mean realized multiple minus 1, per $1 staked. Show the sum and the count.

**Q52. [08] (MCQ)** The pinned number of closes needed to distinguish a 35% win rate from a 30% base rate (one-sided alpha 0.05, power 0.8) is:
- A. 50
- B. 191
- C. 534
- D. 3286

**Q53. [08] (MCQ)** In the p-hacking demo (500 pure-noise strategies, true WR 30%), the crowned best strategy scored in-sample and out-of-sample respectively:
- A. 0.50 in-sample, 0.36 out-of-sample — regressing toward the true 30%
- B. 0.50 in-sample, 0.50 out-of-sample — a real discovery
- C. 0.30 in-sample, 0.50 out-of-sample
- D. 0.36 in-sample, 0.50 out-of-sample

**Q54. [08] (True/False + justification)** "A close at exactly 1.0x counts as a win in `win_rate`." True or false — justify, and name what a 1.0x close *does* do in the summary statistics.

**Q55. [08] (MCQ)** The pinned `BASE_RATES` say the share of wallets profitable over 90 days, and the rate of launches reaching $1M, are:
- A. 25% of wallets; 1 in 100 launches
- B. 6.25% of wallets; 1 in 4200 launches
- C. 6.25% of wallets; 1 in 100 launches
- D. 0.625% of wallets; 1 in 4200 launches

**Q56. [08] (Short answer)** Why does `expectancy` raise `ValueError` on an empty list instead of returning 0.0?

## Toolkit 09 — backtesting-rigor-meme [09]

*Focus: pre-registration freeze, walk-forward embargo, parameter plateaus, deflated expectancy, look-ahead.*

**Q57. [09] (Compute)** An observed expectancy of +0.30 comes from 100 samples after 100 tried variants. Using `deflated_expectancy = observed − √(2·ln(max(n_trials,1))) / √(n_samples)`, compute the deflated expectancy (to 3 decimal places) and state whether the claim survives.

**Q58. [09] (MCQ)** `parameter_plateau` measures the width of the contiguous region around the best parameter keeping at least what share of the best expectancy — and a width of 1 means what?
- A. 50%; width 1 means optimal
- B. 80%; width 1 is a spike, and a spike is fragile
- C. 95%; width 1 means robust
- D. 80%; width 1 means the sweep was too coarse

**Q59. [09] (MCQ)** In `lookahead_experiment` on pure noise (seed 42), the cheating (next-bar) signal and the honest (current-bar) signal earn approximately:
- A. +0.40 and +0.40 — both fine
- B. −0.002 and +0.403 respectively
- C. +0.403 cheating, −0.002 honest — one look-ahead bug fabricates ~+0.40 expectancy from nothing
- D. 0.0 and 0.0 — noise cannot be exploited

**Q60. [09] (True/False + justification)** "After `freeze()`, mutating the dict the caller originally passed to the `PreRegistration` constructor changes the registration's thresholds." True or false — justify.

**Q61. [09] (MCQ)** Walk-forward over 120 bars with 3 folds and embargo 5: the first fold is:
- A. Train 0..34, test 35..59
- B. Train 0..29 (30 bars), test 35..59 (25 bars), with bars 30..34 embargoed in neither set
- C. Train 0..29, test 30..54
- D. Train 0..59, test 65..89

**Q62. [09] (Short answer)** State the formula for `bonferroni_alpha(alpha, n_trials)` and, in one sentence, what failure it prices in.

**Q63. [09] (MCQ)** Calling `certify()` on a `PreRegistration` that was never frozen:
- A. Returns True
- B. Returns False
- C. Raises `RuntimeError` — only a frozen registration can certify
- D. Freezes it automatically, then certifies

## Toolkit 10 — live-risk-controls-meme [10]

*Focus: rolling-expectancy killswitch, the 25% cost gate, the no-refill bankroll.*

**Q64. [10] (Compute)** The cost gate models a worst-case round trip as 4 transactions; Ethereum costs $1.00 per transaction. Compute the round-trip cost in dollars and as a percentage of a $1 notional, and state the gate verdict (blocked iff cost > 25% of notional).

**Q65. [10] (Compute)** Compute `min_notional_to_pass` for Ethereum from the gate rule (smallest notional whose 4-transaction round-trip cost is ≤ 25% of notional). At exactly that notional, is the position allowed or blocked?

**Q66. [10] (MCQ)** The kill switch (window 30, min_trades 15) halts when:
- A. Any single close is below 1.0x
- B. At least 15 closes sit in the window and their mean realized multiple minus 1 is < 0
- C. 15 closes exist in total, ever
- D. The loss streak reaches 5

**Q67. [10] (True/False + justification)** "After the kill switch halts, a later 50x winning close automatically clears the halt." True or false — justify, including the only thing that does clear it.

**Q68. [10] (MCQ)** On load, the kill switch's state file is missing or corrupt. The switch starts:
- A. Unhalted — a fresh start
- B. Halted (`halted=True`), with a `load_failed` event journaled — fail-closed
- C. Unhalted, but with min_trades doubled
- D. It raises and the lane crashes

**Q69. [10] (MCQ)** The `Bankroll` ($100 start, $1 shots) enforces a maximum of how many concurrent open positions — and which two methods deliberately do *not* exist on it?
- A. 5; `pause` and `resume`
- B. 10 (`MAX_OPEN`); `top_up` and `reset`
- C. 100; `borrow` and `leverage`
- D. Unlimited; `top_up` and `reset`

**Q70. [10] (Short answer)** A re-armed kill switch can re-halt on the very next losing close. What design choice in `rearm` causes that, and why is it the safer choice?

## Toolkit 11 — smart-wallet-grading [11]

*Focus: copier lag decay, pack independence, shrinkage grade weights, auto-mute.*

**Q71. [11] (MCQ)** The fixed copier lag gains in `LAG_GAINS` are:
- A. +5%, +10%, +25%
- B. +15%, +50%, +100% — any other lag gain raises `ValueError`
- C. +10%, +50%, +200%
- D. Chosen per wallet from its history

**Q72. [11] (Compute)** Using `grade_weight(n, avg, prior=0.0, m=10)` with `shrunk = (n·avg + m·prior)/(n+m)` and `weight = clamp(1.0 + shrunk, 0.5, 1.5)`: compute the weight for a wallet with n = 2 follows averaging +0.90 realized.

**Q73. [11] (MCQ)** `is_muted(n_follows, avg_realized)` is true iff:
- A. n ≥ 5 and avg ≤ −0.10
- B. n ≥ 10 and avg ≤ −0.20 (exactly 10 at exactly −0.20 mutes; 9 follows never mute)
- C. avg ≤ −0.50 at any n
- D. The wallet's own trades lost money

**Q74. [11] (MCQ)** On the demo path (wallet buys at 1.00; path 1.00 → 1.20 → 1.60 → 2.10 → 1.40 → 0.40 exit), copier realized multiples by lag are:
- A. +15%: 0.333x, +50%: 0.250x, +100%: 0.190x — every copier loses even though the token more than doubled
- B. +15%: 1.20x, +50%: 1.60x, +100%: 2.10x
- C. All 0.40x — everyone exits at the same price
- D. +15%: 0.190x, +50%: 0.250x, +100%: 0.333x

**Q75. [11] (True/False + justification)** "A wallet with no recorded funder is merged into the largest pack, to be safe." True or false — justify from `group_wallets`/`convergence_count` semantics.

**Q76. [11] (Short answer)** The +15% copier on the demo path pays 1.20, not exactly 1.15. Why — what entry-price rule causes it, and who bears the difference?

**Q77. [11] (MCQ)** A wallet averaging +0.90 over 50 follows gets shrunk = (50 × 0.90)/60 = 0.75, so its raw weight would be 1.75. Its actual grade weight is:
- A. 1.75 — grades are uncapped
- B. 1.50 — weights are clamped to [0.5, 1.5]
- C. 1.00 — the prior wins at large n
- D. 0.75

## Toolkit 12 — funnel-autopsy [12]

*Focus: the single runner bar, classifier precedence, self-inflicted vs correct blocks.*

**Q78. [12] (MCQ)** The one runner bar used everywhere in `classify` requires, in the *same* snapshot:
- A. Liquidity ≥ $10,000, mcap ≥ $25,000, any momentum
- B. Liquidity ≥ $25,000, mcap ≥ $50,000, and positive momentum — all three at once
- C. Liquidity ≥ $25,000 at any snapshot and mcap ≥ $50,000 at any (possibly different) snapshot
- D. Positive momentum only — size is checked later

**Q79. [12] (MCQ)** The classifier's precedence order (first match wins) is:
- A. Rug > ceiling > burned > safety-lag > never-met-bar
- B. never-met-bar > gate-correctly-blocked-rug > invented-ceiling-block > burned-one-shot-convergence > safety-unknown-lag > caught-or-other
- C. Ceiling > rug > never-met-bar > burned > safety-lag
- D. Alphabetical by class name

**Q80. [12] (MCQ)** A runner with $96k liquidity / $1.14M mcap / safety ok was blocked only by a gate-log reason citing a 300% gain ceiling. Its class is:
- A. `never-met-bar`
- B. `safety-unknown-lag`
- C. `invented-ceiling-block`
- D. `burned-one-shot-convergence`

**Q81. [12] (True/False + justification)** "A snapshot with liquidity exactly $25,000 and mcap exactly $50,000 (momentum positive) meets the bar, while $24,999 liquidity does not." True or false — justify.

**Q82. [12] (MCQ)** A convergence signal fires, is blocked once at t=1 min, the token then meets the bar for ~35 minutes with safety later known-ok, and the signal is never retried. Its class is:
- A. `safety-unknown-lag`
- B. `burned-one-shot-convergence`
- C. `caught-or-other`
- D. `gate-correctly-blocked-rug`

**Q83. [12] (Short answer)** A safety report *failed* at a snapshot that was below the runner bar, and the token later rugged. Why does that *not* make the case `gate-correctly-blocked-rug`?

**Q84. [12] (Compute)** The DACITY-like fixture's bar-meeting snapshots start at t=5 min and span a 35-minute bar window. At what t does the last bar-meeting snapshot occur, and how many bar-meeting prints does `explain` report for this fixture?

## Toolkit 13 — post-trade-meme [13]

*Focus: the fixed win definition, expectancy vs win rate, peak-vs-realized leakage.*

**Q85. [13] (MCQ)** The fixed definitions used everywhere in this lab are:
- A. Win = realized ≥ 1.0x; round-tripper peak ≥ 1.5x
- B. Win = realized ≥ 1.5x (`WIN_MULT`); peak threshold 2.0x (`PEAK_MULT`); breakeven rate separately reports the share ≥ 1.0x
- C. Win = realized > 2.0x; peak ≥ 3.0x
- D. Each lane defines its own win

**Q86. [13] (Compute)** The 10-close demo fixture has expectancy +0.23 per $1 shot. Compute the total P/L at $1 shots and at $5 shots.

**Q87. [13] (Compute)** A round-tripper is a close with peak ≥ 2.0x and realized < 1.0x (both boundaries exact: peak exactly 2.0 counts, realized exactly 1.0 does not). The fixture has 2 round-trippers averaging 0.65x realized, out of 5 closes that peaked ≥ 2x and 4 closes that were wins. Compute the fixture's `capture_rate` (wins among the ≥2x peaked) from the leakage report's own counts — check it against the pinned value 0.60 by stating which count the rate actually uses.

**Q88. [13] (MCQ)** `capture_rate` on a ledger where *nothing* peaked ≥ 2x returns:
- A. 0.0 — it captured nothing
- B. 1.0 — nothing was missed
- C. `None` — nothing to capture is not the same as capturing nothing
- D. It raises

**Q89. [13] (True/False + justification)** "A ledger row missing `peak_mult` is skipped by the analytics so one corrupt row cannot poison the report." True or false — justify.

**Q90. [13] (MCQ)** The fixture's two lanes have identical win rates (0.40) but expectancy +0.10 (degen) vs +0.36 (steady). That is possible because:
- A. It is not possible — one of the numbers is a bug
- B. Expectancy averages realized multiples, so the *sizes* of wins and losses differ between lanes even at the same win frequency
- C. The steady lane had more closes
- D. Rug rate explains the whole gap (both lanes are 0.20)

**Q91. [13] (Short answer)** `verdict_ready(closes, min_closes, min_expectancy=0.0)` requires two conditions. State both, including the exact comparison on expectancy and what happens at exactly the bar.

## Toolkit 14 — token-lifecycle [14]

*Focus: phase rule order, legal/suspicious transitions, time-in-phase.*

**Q92. [14] (MCQ)** The `Phase` enum contains exactly:
- A. BONDING, GRADUATED, RUNNER, DEAD
- B. BONDING, GRADUATED, RUNNER, FADING, DEAD, UNKNOWN
- C. LAUNCH, PUMP, DUMP, DEAD, UNKNOWN
- D. BONDING, RUNNER, FADING, UNKNOWN

**Q93. [14] (MCQ)** A graduated snapshot classifies RUNNER iff (among the stated rules): momentum ≥ 15%, liquidity ≥ $25,000, mcap ≥ $50,000, and holder trend:
- A. ≥ 0%
- B. ≥ −5%
- C. ≥ −15%
- D. Any value — holders are not part of the runner bar

**Q94. [14] (MCQ)** The collapse (DEAD) rule fires when liquidity < $1,000, **or** when both of these hold together:
- A. Momentum ≤ −50% and any holder drop
- B. Momentum ≤ −85% and holder trend ≤ −30%
- C. Momentum ≤ −85% alone — price collapse is enough
- D. Holder trend ≤ −30% alone

**Q95. [14] (True/False + justification)** "A pre-graduation token (curve completion < 100%) printing enormous momentum and liquidity numbers classifies as RUNNER." True or false — justify from the rule order.

**Q96. [14] (MCQ)** The transition `DEAD → RUNNER` is classified as, and `FADING → RUNNER` as:
- A. Both illegal
- B. Both "ok"
- C. DEAD → RUNNER: legal but flagged `"suspicious"` (dormant revival, deserves a second look); FADING → RUNNER: `"ok"` (a normal revival)
- D. DEAD → RUNNER: "ok"; FADING → RUNNER: "suspicious"

**Q97. [14] (Compute)** In the DACITY-shape fixture timeline the phases change at t=0 BONDING, t=38 GRADUATED, t=55 RUNNER, t=80 FADING, t=105 DEAD. Using `time_in_phase` (each point owns the interval to the next), compute the minutes spent in RUNNER and in GRADUATED.

**Q98. [14] (Short answer)** A snapshot is missing one metric. Why does it classify UNKNOWN rather than having the missing value treated as zero or skipped — and which single phase does `is_runner_phase()` return true for?

## Toolkit 15 — feed-data-quality [15]

*Focus: pair selection over pairs[0], staleness, price disagreement, the dust floor.*

**Q99. [15] (MCQ)** In `dust_first_payload()`, `pairs[0]` is a $40 dust pool quoting $9.99 and the real pair is $180,000 quoting $0.50. `select_pair` returns:
- A. `pairs[0]` — API order is authoritative
- B. The $180,000 pair at $0.50 — selection is by eligibility and liquidity, not list position
- C. `None` — the payload is contradictory
- D. The average of the two prices

**Q100. [15] (Compute)** Two eligible pairs quote prices 1.00 and 1.10. Using `price_disagreement_pct = (max − min) / median × 100`, compute the disagreement and state whether it passes the default 15% threshold.

**Q101. [15] (MCQ)** The defaults of `select_pair` are: maximum pair age 900 seconds, maximum price disagreement 15%, and minimum liquidity:
- A. $0 — every pair is eligible
- B. $100 — dust is mostly excluded
- C. $1,000 — pairs under the dust floor are excluded before verification
- D. $25,000 — only runner-grade pairs

**Q102. [15] (True/False + justification)** "A pair with no timestamp at all is treated as fresh, since the API just returned it." True or false — justify.

**Q103. [15] (MCQ)** Quote preference (USDC > SOL > USDT > WETH/ETH > other) applies:
- A. Always — a USDC pair always wins
- B. Only inside a 10% liquidity tie band, so preference never beats a real liquidity gap
- C. Only when volumes are exactly equal
- D. Never — it is vestigial

**Q104. [15] (Short answer)** Why are dust pairs excluded *before* the price-disagreement check rather than merely ranked last?

**Q105. [15] (MCQ)** The same pair address appears twice with different liquidity. `dedupe_pairs` keeps:
- A. The first copy
- B. The average of the two
- C. The highest-liquidity copy, counting the other as dropped
- D. Neither — duplicates invalidate the payload

## Toolkit 16 — sniper-window-economics [16]

*Focus: delay buckets, entry-multiple decay, break-even delay, the structural copy-window verdict.*

**Q106. [16] (Compute)** On the default path (10x peak, 120s geometric rise), compute the entry multiple at a 30s delay (`multiple = peak^(t/rise)`) and the fraction of the (log) move remaining. Repeat the entry multiple for a 60s delay.

**Q107. [16] (Compute)** State the pinned break-even delay at the defaults (10x peak, 120s rise, 4% round-trip cost) — the latest entry whose *best-case* peak exit still covers costs — and the net return of an entry exactly at the peak (120s).

**Q108. [16] (MCQ)** `delay_bucket` names a 1s entrant and a 300s entrant respectively:
- A. `sniper` (≤2s, where real snipers fill at 0.4–2s); `copy_window_2_10min` (120–600s)
- B. `fast_polling`; `late`
- C. `polling`; `copy_window_2_10min`
- D. `sniper`; `late`

**Q109. [16] (MCQ)** The seeded expectancy table (400 launches per delay) gives the 2–10 minute copy window a mean expectancy of:
- A. +52.1% — late is safe
- B. −52.1%, with win rate 0.0% at 180s/300s/600s individually — the window is structurally negative, not merely risky
- C. −4.0% — just the round-trip cost
- D. 0.0% — a fair gamble

**Q110. [16] (True/False + justification)** "`net_return_at_peak_pct` for a delay after the rise ends returns exactly −4% (minus costs)." True or false — justify.

**Q111. [16] (Short answer)** The expectancy simulation grants *early* entrants the peak exit for free. Why does granting that generous assumption make the copy window's negative verdict *stronger*, not weaker?

**Q112. [16] (MCQ)** At the defaults, an instant (0s) entry exiting exactly at the peak nets, after 4% round-trip costs:
- A. +1000%
- B. +900%
- C. +860%
- D. +841.8%

## Toolkit 17 — deployer-forensics [17]

*Focus: serial-rugger bars, rug rate over known outcomes only, reuse fingerprints.*

**Q113. [17] (MCQ)** `SERIAL_RUGGER` requires exactly:
- A. ≥ 2 launches and ≥ 2 rugs
- B. ≥ 3 launches and ≥ 2 rugs
- C. ≥ 3 launches and a rug rate above 50%
- D. Any single rug with a reused funding wallet

**Q114. [17] (Compute)** SerialSam's fixture history has 4 launches and 3 rugs, all outcomes known. Compute his `rug_rate` (rugs over *known* outcomes only). What does `rug_rate` return for a deployer whose outcomes are all unknown, and why not 0.0?

**Q115. [17] (MCQ)** A deployer has exactly 2 launches and both rugged. The verdict is:
- A. `SERIAL_RUGGER` — a 100% rug rate convicts
- B. `WATCH` — the serial bar's launch-count half (≥3) is not met, so thin data cannot convict
- C. `UNKNOWN`
- D. `CLEAN`

**Q116. [17] (True/False + justification)** "A deployer with no launch history is `CLEAN` — there is nothing against them." True or false — justify, and state the actual verdict.

**Q117. [17] (MCQ)** `CLEAN` is earned only with:
- A. ≥ 1 launch, no rugs
- B. ≥ 2 launches, every outcome known, zero rugs, and no funding/name reuse flags
- C. ≥ 5 launches and a rug rate below 10%
- D. No launches in the last 30 days

**Q118. [17] (Short answer)** Funding-source reuse alone can trigger `WATCH` with zero rugs. Why is a recycled funding wallet evidence at all — and why is it not enough, on its own, to veto?

**Q119. [17] (MCQ)** `should_veto(deployer, records)` returns True only for:
- A. `WATCH` and `SERIAL_RUGGER`
- B. `SERIAL_RUGGER` alone — `WATCH` and `UNKNOWN` inform without convicting
- C. Any deployer with rug_rate above 0
- D. `UNKNOWN` — strangers are the risk

## Toolkit 18 — wash-and-botshare [18]

*Focus: round-trip wash share, bot patterns, toxicity weights, fail-open assessment.*

**Q120. [18] (Compute)** A stream's total volume is 24 SOL. One wallet bought 4 SOL and sold 4 SOL; all other wallets only bought. Using `roundtrip_volume = Σ 2·min(buy, sell)` per wallet, compute the stream's `wash_share`. What does a buy-only wallet contribute to round-trip volume?

**Q121. [18] (MCQ)** `is_tiny(amount_sol)` is true iff the amount is:
- A. ≤ 0.10 SOL
- B. Strictly below 0.10 SOL (exactly 0.10 is not tiny)
- C. Below 1.0 SOL
- D. Below the stream's median trade

**Q122. [18] (MCQ)** The flags from `flags_for` fire at exactly these thresholds (a share exactly *at* the threshold flags):
- A. WASH_SKEW ≥ 0.25, BOT_SWARM ≥ 0.25, BUMP_FARM ≥ 0.25
- B. WASH_SKEW ≥ 0.40, BOT_SWARM ≥ 0.50, BUMP_FARM ≥ 0.50
- C. WASH_SKEW > 0.40, BOT_SWARM > 0.50, BUMP_FARM > 0.50 (strictly above only)
- D. Any share above 0

**Q123. [18] (True/False + justification)** "A 9-trade stream of obvious wash round-trips is assessed as flagged." True or false — justify, including the status `assess` actually returns and why it is neither a pass nor a veto.

**Q124. [18] (MCQ)** `toxicity_score` combines the shares as:
- A. `wash + bot + bump`, uncapped
- B. `50·wash + 30·bot + 20·bump`, capped at 100 — so the all-wash demo stream scores exactly 50.0
- C. The maximum of the three shares × 100
- D. `100 − (50·wash + 30·bot + 20·bump)`

**Q125. [18] (Short answer)** One whale trade plus nine dust bot trades: state the bot share by transaction count and (approximately) by volume, and say which of the two the toxicity score uses.

**Q126. [18] (MCQ)** A trade is bot-like when *any* of these holds: tiny size (< 0.10 SOL), its wallet has ≥ 8 trades in the stream, or:
- A. It happens at night
- B. Its slot is shared by ≥ 3 trades (crowded slot; `None` slots never cluster)
- C. Its amount is a round number
- D. The wallet is newly created

## Toolkit 19 — liquidity-velocity [19]

*Focus: inflow/trade rates, velocity bands, sparse-data refusal, prediction vs profit after costs.*

**Q127. [19] (Compute)** The embedded calibration outcome is 55 winners at +30% and 45 losers at −30%. Compute `calibrated_gross_edge()`, then `edge_after_costs()` at the pinned ~4% round-trip cost (`ROUND_TRIP_COST = 0.04`), and state `clears_breakeven()`.

**Q128. [19] (MCQ)** `velocity_band` bands SOL/min as:
- A. slow < 1, steady < 5, fast < 20, igniting ≥ 20
- B. slow < 0.5, steady < 2.0, fast < 5.0, igniting ≥ 5.0 (a rate exactly at a boundary takes the next band up)
- C. slow < 0.5, steady < 2.0, fast < 5.0, igniting > 10.0
- D. A single hot/not-hot cut at 2.0

**Q129. [19] (MCQ)** `graduation_score` refuses to score — returning `None`, and `predict_graduation` returning status `"unknown"` with no call — when the stream has fewer than:
- A. 3 events
- B. 5 events
- C. 10 events (`MIN_TRADES`); exactly 10 is scored
- D. 50 events

**Q130. [19] (True/False + justification)** "The calibration accuracy is 0.97, so following the graduation predictor is profitable." True or false — justify with the lab's pinned edge numbers.

**Q131. [19] (MCQ)** `minutes_to_graduation` returns `None` (not 0) once the target (default 85 SOL) is already collected, because:
- A. A bug in the rate division
- B. "Already there" is a different state from "zero minutes away at the current rate" — callers must not read it as an imminent-graduation signal
- C. The target becomes infinite after graduation
- D. Rates are undefined for graduated tokens

**Q132. [19] (Short answer)** `inflow_rate` divides total inflow by the span from first to last event rather than a fixed window. What stream shape does that choice flatter?

**Q133. [19] (MCQ)** A fast demo launch (20 events) scores 78.0 and predicts graduation; a slow one scores 5.0. The default `predict_graduation` threshold is:
- A. 50.0
- B. 60.0 — graduating iff score ≥ threshold
- C. 78.0
- D. 90.0

## Toolkit 20 — copy-trading-economics [20]

*Focus: lag buckets and slippage, leader-vs-follower expectancy, watchlist-only smart money.*

**Q134. [20] (Compute)** Over 200 seeded paths the leader's expectancy is +31.2% and the 15-second copy's is −6.2%. Compute the 15-second lag penalty (follower expectancy − leader expectancy) in percentage points. Then compute the live-research figure's relationship in one line: the lab README reports delayed cluster copying in the live research averaged roughly −41% — is that a leader number or a follower number?

**Q135. [20] (MCQ)** `LAG_BUCKETS` pairs each lag with its extra slippage; the leader pays 0.005. The buckets are:
- A. 15s: 1%, 1m: 2%, 5m: 5%, 15m: 15% — slippage strictly growing with lag
- B. All buckets 1% — slippage is flat
- C. 15s: 15%, 1m: 5%, 5m: 2%, 15m: 1% — early is expensive
- D. 15s: 0.1%, 1m: 0.2%, 5m: 0.5%, 15m: 1.5%

**Q136. [20] (MCQ)** Across the 200 seeded paths, expectancy by bucket is: leader +31.2%, 15s −6.2%, 1m −50.3%, 5m −53.4%, 15m −55.7%. `is_copying_viable` is therefore:
- A. True for the 15s bucket only
- B. True for leader and 15s
- C. False for every follower bucket — delayed copying is negative at every lag and worsens monotonically
- D. True at 15m — the bleed is over by then

**Q137. [20] (True/False + justification)** "Because the tracked wallets demonstrably win, smart-money agreement adds score to entries in the live bot." True or false — justify with the follower-vs-leader distinction and what re-enabling would require.

**Q138. [20] (MCQ)** Price-path steps are 5 seconds. `lag_to_steps` converts a 1-minute lag to:
- A. 1 step
- B. 5 steps
- C. 12 steps
- D. 60 steps

**Q139. [20] (Short answer)** In `simulate_copy`, ladder triggers are read off the *market* price relative to entry spot, but proceeds are divided by the slippage-inflated effective entry price. What would each simpler choice (triggers on effective price; proceeds on spot) hide?

**Q140. [20] (MCQ)** The exit ladder implemented inside `simulate_copy` is:
- A. Hold to the final price, always
- B. Sell 50% when market price first reaches 2× entry spot; the rest rides a 30% trailing stop from the peak; if 2× never comes, everything is cut at a −50% hard stop; anything left exits at the final price
- C. Sell everything at 2×
- D. Sell 25% at each of 2×, 3×, 4×, 5×

## Toolkit 21 — card-design [21]

*Focus: the nine-element card contract — no bare CAs, ever.*

**Q141. [21] (MCQ)** `required_fields()` names nine always-required card elements. Which of the following is **not** one of them?
- A. Liquidity
- B. A safety status line
- C. A promotional banner naming the caller's channel
- D. A per-chain exit hint

**Q142. [21] (MCQ)** The synthetic CA check `is_valid_ca` accepts a string of:
- A. Any length, hex characters
- B. 32–44 characters, all in the base58 alphabet (which excludes `0`, `O`, `I`, `l`) — 31 and 45 characters fail
- C. Exactly 44 characters
- D. 32–44 characters including `0` and `O`

**Q143. [21] (True/False + justification)** "Every card must carry the 📍 I bought this track button." True or false — justify by card type.

**Q144. [21] (MCQ)** A card whose `chain` is `"dogechain"` (not one of solana/base/ethereum/hyperliquid) fails validation with:
- A. `"unknown chain"` as its own problem string
- B. `"missing chain provenance"` — an unknown chain *is* a provenance failure
- C. No problem — chain is cosmetic
- D. `"invalid CA"`

**Q145. [21] (Short answer)** Why is a pick card missing its 📍 track button a *measurement* failure rather than a UX gap — what downstream decision loses its data?

**Q146. [21] (MCQ)** A bare-CA card (valid CA, nothing else) produces how many problems from `validate`, and does the list include `"missing CA"`?
- A. 1 problem; yes
- B. 8 problems (provenance, mcap/FDV, liquidity, context, safety, copy-CA button, supply, exit hint); no — the CA is present, everything else is missing
- C. 9 problems; yes
- D. 0 problems — a CA is a complete card

## Toolkit 22 — ledger-accounting [22]

*Focus: atomic saves, lock-file discipline, idempotent entries, ordering, torn-write recovery.*

**Q147. [22] (MCQ)** Adding an entry whose `(ticket, kind)` pair already exists raises:
- A. `OrderingError`
- B. `DuplicateEntryError` — and the failed add never appends
- C. `LockHeldError`
- D. Nothing — the entry is appended and deduplicated on save

**Q148. [22] (MCQ)** A `CLOSE` timestamped *before* its matching `BUY` raises `OrderingError`. A `CLOSE` at *exactly* the BUY's timestamp is:
- A. Also rejected — timestamps must be strictly increasing
- B. Allowed — the exact-timestamp boundary passes; a `CLOSE` with no matching `BUY` at all is still rejected
- C. Silently re-sorted into place
- D. Treated as a `BUY`

**Q149. [22] (True/False + justification)** "If both the main ledger file and its backup are corrupt, construction starts from a fresh empty ledger." True or false — justify, including the error raised.

**Q150. [22] (MCQ)** `save()` persists atomically by:
- A. Appending a JSON line to the main file
- B. Writing `<path>.tmp`, fsyncing, preserving the previous good main file as `<path>.bak`, then `os.replace`-ing the temp file over the main path
- C. Writing the main file twice and comparing
- D. Holding the lock file open during the write only

**Q151. [22] (Short answer)** Why must the atomic save's temp file live in the *same directory* as the main file rather than a global temp directory?

**Q152. [22] (MCQ)** The ledger lock is a lock *file* at `<path>.lock` created with `O_EXCL`. A second holder attempting `locked()`:
- A. Waits until the lock frees
- B. Raises `LockHeldError` naming the lock path — the exclusive-create is what closes the check-then-create race
- C. Shares the lock read-only
- D. Deletes the stale lock and proceeds

## Toolkit 23 — daemon-supervision [23]

*Focus: pidfile + heartbeat liveness, restart backoff and budgets, the self-match trap, exactly-one-instance.*

**Q153. [23] (Compute)** Using `backoff_delay(attempt, base_s=1.0, factor=2.0, cap_s=60.0) = min(cap, base × factor^attempt)`, compute the delay for attempt 3 and write out the first seven delays of `backoff_schedule` (attempts 0–6).

**Q154. [23] (MCQ)** A pid exists in the process table but its heartbeat age exceeds the limit. `check_liveness` returns:
- A. alive = True, reason `"ok"` — the process exists
- B. alive = False, reason `"stale_heartbeat"` — a wedged process is not alive
- C. alive = False, reason `"no_process"`
- D. alive = True, reason `"stale_heartbeat"`

**Q155. [23] (MCQ)** In the demo self-match fixture for pattern `smart-watcher`, the naive kill targets are [499, 500, 700, 701] and the safe targets are [700, 701]. Pids 499 and 500 are:
- A. Duplicate daemons
- B. The watcher itself and its parent shell — both excluded by the safe matcher, because the parent shell's command line typically contains the pattern too
- C. Unrelated processes that happen to match
- D. The two real daemons; 700/701 are the watcher and parent

**Q156. [23] (True/False + justification)** "A heartbeat age exactly at the limit is stale." True or false — justify from `heartbeat_fresh`.

**Q157. [23] (MCQ)** `ensure_single_instance` returns `"start"`, `"ok"`, `"duplicates"` for, respectively:
- A. 1, 0, ≥2 matches
- B. 0, 1, ≥2 matches (safe matcher) — duplicates are *reported*, never silently resolved by killing extras in this lab
- C. 0, ≥2, 1 matches
- D. ≥2, 1, 0 matches

**Q158. [23] (Short answer)** Why does `RestartPolicy` (default max 5 restarts per 600s window) use a *sliding window* rather than a lifetime restart counter?

## Toolkit 24 — cross-chain-costs [24]

*Focus: flat vs percentage fee models, the pre-registered 25% gate, minimum viable notionals.*

**Q159. [24] (Compute)** Base costs a flat $0.03 per transaction; a worst-case round trip is 4 transactions; the gate blocks iff round-trip cost > 25% of notional. Compute the round-trip cost and its percentage at a $1 notional, the verdict, and Base's minimum viable notional.

**Q160. [24] (Compute)** Hyperliquid charges 0.035% of notional per transaction. Compute the per-transaction cost, the 4-transaction round-trip cost, and the round-trip percentage at a $1,000 notional, and state the verdict. Why is the percentage the same at $1?

**Q161. [24] (MCQ)** The worst-case round trip is modelled as 4 transactions rather than 2 because:
- A. Every chain charges a double fee
- B. It is 1 entry + up to 3 exit legs — the exit ladder can fire in pieces
- C. Retries are assumed for every transaction
- D. Two transactions are approval transactions

**Q162. [24] (True/False + justification)** "A round-trip cost of exactly 25% of notional is blocked by the gate." True or false — justify with the Ethereum $16 boundary.

**Q163. [24] (MCQ)** Ethereum's gate verdicts at $5 and $25 notionals are:
- A. Allowed at $5 (20%), blocked at $25
- B. Blocked at $5 (cost $4.00 = 80%), allowed at $25 (cost $4.00 = 16%)
- C. Blocked at both — Ethereum never passes
- D. Allowed at both

**Q164. [24] (Short answer)** Ethereum at $1 is blocked at 400% while Hyperliquid can never be blocked by this gate at any size. What structural difference between the two fee models causes that?

## Toolkit 25 — lottery-sizing [25]

*Focus: discrete outcome distributions, full Kelly, expected log growth, seeded ruin.*

**Q165. [25] (Compute)** For the lottery distribution [(0.90, 0.0), (0.10, 15.0)], compute the expected multiple, the expectancy per $1, the probability of loss, and the probability of the ruin (0x) outcome.

**Q166. [25] (Compute)** The pinned full-Kelly fraction for the Q165 distribution is 1/28. Convert it to a percentage (2 decimal places) and to a per-shot stake on a $100 bankroll. The CEO discipline bets a flat $1 on $100 — state that fraction and whether it is above or below Kelly.

**Q167. [25] (MCQ)** For the Q165 distribution, expected log growth per bet at Kelly, at 2×Kelly, and at f = 0.50 is approximately:
- A. +0.007816, +0.002618, −0.4159 — 2×Kelly grows *slower* than Kelly, and far-overbetting turns growth negative while expectancy stays +0.50
- B. +0.007816, +0.015632, +0.50 — growth scales with the fraction
- C. −0.4159, +0.002618, +0.007816
- D. All three are +0.50 — expectancy is growth

**Q168. [25] (True/False + justification)** "`validate_distribution` quietly renormalises probabilities summing to 0.9 up to 1.0." True or false — justify.

**Q169. [25] (MCQ)** `expected_log_growth(f=1.0, outcomes)` where a 0x outcome is possible:
- A. Returns −∞ and lets the caller decide
- B. Returns a large negative number
- C. Raises `ValueError` — the bankroll factor would be ≤ 0, ruin is possible, and the log is undefined there
- D. Returns 0.0

**Q170. [25] (Short answer)** The seeded Monte Carlo (flat $1 shots, 200 shots, seed 42) gives ruin probability 0.00 on a $100 bankroll and 0.48 on a $10 bankroll for the lottery distribution. Why can "the ruin probability of this strategy" never be quoted as a single number?

## Toolkit 26 — time-to-peak [26]

*Focus: two time-to-peak populations that must never be pooled; survival-based time stops.*

**Q171. [26] (Compute)** For the peaked≥2x population, P(peak after 60 min) = 0.420. Compute the fraction of eventual ≥2x winners a 60-minute time stop sacrifices, and the corresponding figure for a 30-minute stop given P(peak after 30 min) = 0.750.

**Q172. [26] (MCQ)** The two seeded populations' pinned stats are:
- A. peaked_2x: median 53.5 min, mean 75.7 min; post_graduation: median 4.7 min, mean 6.0 min — two different clocks, differing by >5×
- B. Both populations: median ~30 min
- C. peaked_2x: median 4.7 min; post_graduation: median 53.5 min
- D. peaked_2x: median = mean = 53.5 min

**Q173. [26] (True/False + justification)** "Pooling peaked_2x and post_graduation samples with `mix_populations` is fine as long as n is large." True or false — justify, including the exception that exists and its explicit cost.

**Q174. [26] (MCQ)** `recommend_time_stop(values, max_miss_rate=0.10)` returns the (1 − miss rate) percentile. For population A (peaked≥2x) that stop is:
- A. 53.5 min (the median)
- B. 75.7 min (the mean)
- C. 155.2 min (the p90)
- D. The maximum observed peak time

**Q175. [26] (MCQ)** Population A's histogram with edges [0, 10, 30, 60, 120, 240, 10000] is [8, 42, 66, 54, 22, 8] over n=200. The count of peaks landing in [30, 60) minutes is:
- A. 42
- B. 66
- C. 54
- D. 120

**Q176. [26] (Short answer)** Population A has median 53.5 min but mean 75.7 min. What does that gap say about the distribution's shape, and which kind of statistic (median/mean vs percentile/survival) should drive a time stop?

## Toolkit 27 — feed-resilience [27]

*Focus: flood-guard cooldowns, dual-key token buckets, circuit breakers, seeded backoff, gzip.*

**Q177. [27] (Compute)** A `FloodGuard` (buffer 5.0s) receives a 429 with `retry_after = 30` at t = 1000.0. Compute `parked_until` and the remaining cooldown immediately after parking. One send succeeded before the 429 (calls = 1): how many transport calls does a `try_send` during cooldown add, and what does it return?

**Q178. [27] (MCQ)** `should_request_gzip` flips to True at an estimated size of:
- A. 1,000 bytes
- B. 10,000 bytes
- C. 50,000 bytes — because the egress proxy drops large uncompressed responses
- D. 1,000,000 bytes

**Q179. [27] (MCQ)** The `CircuitBreaker` (failure_threshold 3, reset_timeout 30s) opens after 3 failures; after the timeout it becomes `half_open`. A failure while half-open:
- A. Is ignored — half-open is a grace state
- B. Re-opens the breaker immediately
- C. Closes the breaker
- D. Resets the failure count to 1

**Q180. [27] (True/False + justification)** "Cooldown state is in-memory only, so a daemon restart mid-ban starts with a fresh, unparked guard." True or false — justify with the guard's persistence API and the failure it prevents.

**Q181. [27] (MCQ)** `DualKeyBuckets` drains its main and discovery buckets independently. `acquire` with an unknown key:
- A. Drains the main bucket
- B. Returns False
- C. Raises `ValueError` — a discovery sweep can never starve live calls, and a mistyped key fails closed
- D. Creates a new bucket

**Q182. [27] (Short answer)** `backoff_delays` uses *seeded* jitter. State (a) why the lab seeds it, and (b) why production retries need jitter at all when every client shares the same base and factor.

## Toolkit 28 — signal-refinery [28]

*Focus: bus record validation, the tap journal, per-chain kill switches with human-only resume.*

**Q183. [28] (Compute)** A Base tap journal has 11 closed taps with exactly 1 win. Compute the win rate (to 3 decimal places), then apply the auto-pause rule (≥10 closed taps **and** win rate strictly < 20%) and state whether Base pauses.

**Q184. [28] (MCQ)** A `TapOutcome` counts as a win iff:
- A. It closed at any profit
- B. It is closed and `peak_mult ≥ 2.0` — exactly 2.0 wins, 1.99 is a loss, and an open outcome is neither win nor loss
- C. It is closed and `peak_mult > 2.0`
- D. Its realized multiple ≥ 1.5

**Q185. [28] (True/False + justification)** "If Solana's tap win rate fell to 0% over 50 closed taps, `evaluate_auto_pause` would pause it like any other chain." True or false — justify, including what a manual pause attempt returns and records.

**Q186. [28] (MCQ)** Resuming a paused chain with `resume(by="auto")`:
- A. Succeeds if the journal has improved
- B. Is refused and flagged — there is no auto-resume; only `by="human"` resumes
- C. Succeeds after 24 hours
- D. Raises and crashes the switch board

**Q187. [28] (MCQ)** A record with source code `br` (base-runner-feed) on chain `solana` is:
- A. Valid — both the chain and the source are individually known
- B. Rejected as a source/chain mismatch — `br` must be Base, `er` Ethereum, `hl` Hyperliquid
- C. Rewritten to chain `base` automatically
- D. Valid but flagged

**Q188. [28] (Short answer)** `TapJournal.win_rate(chain)` returns `None` when a chain has no closes, never a fake 0.0. Why does that distinction matter to the auto-pause rule?

## Toolkit 29 — momentum-scanner [29]

*Focus: the qualification funnel, counted reject reasons, no upper gain ceiling, per-mint cooldown.*

**Q189. [29] (MCQ)** The qualification thresholds are:
- A. Liquidity ≥ $25k, volume ≥ $50k, 24h gain between +20% and +300%
- B. Liquidity ≥ $50,000, 24h volume ≥ $100,000, 24h gain ≥ +20% with **no maximum**, dump guard h1 < −15%
- C. Liquidity ≥ $50,000, volume ≥ $100,000, gain ≥ +20% capped at +2540%
- D. Liquidity ≥ $10k, volume ≥ $10k, any positive gain

**Q190. [29] (MCQ)** The Doomsday regression pool — 24h gain +2540%, liquidity $96k, high volume, h1 +19.8%, safety ok — under `qualify`:
- A. Rejects with `gain24_low` — the gain is out of band
- B. Qualifies — there is no upper ceiling on 24h gain; the ceiling that once blocked this shape was removed and the fixture pins its absence
- C. Rejects with `dump_guard`
- D. Rejects with `safety`

**Q191. [29] (True/False + justification)** "A pool with h1 change of exactly −15.0% is rejected by the dump guard." True or false — justify, and state the h1 value that does reject.

**Q192. [29] (MCQ)** The injected safety predicate raises an exception mid-scan. The pool:
- A. Qualifies — a crashed check is treated as a pass "just this once"
- B. Rejects with reason `safety` — safety fails closed three ways: missing predicate, False return, or a predicate that raises
- C. Crashes the scan
- D. Rejects with reason `bad_pool`

**Q193. [29] (MCQ)** The per-mint card cooldown is 6 hours. A pool that is *rejected* (e.g. on safety):
- A. Starts its cooldown anyway — it was scanned
- B. Never starts a cooldown — only a qualifying pool records a cooldown entry, so a transient safety outage cannot mute a mint for 6h
- C. Starts a 1-hour cooldown
- D. Is permanently muted

**Q194. [29] (Short answer)** Why does every scan fill a stats dict counting *each reject reason*, instead of just returning the qualified pools — what failure mode stays invisible without it?

## Toolkit 30 — verdict-protocol [30]

*Focus: the verdict precedence matrix, sample bars for measuring/killing/funding, the four live gates.*

**Q195. [30] (MCQ)** The protocol's pinned sample thresholds and kill line are:
- A. Full 200, any-verdict 20, kill 50, deep-negative −0.10
- B. Full 534, any-verdict 20 (`MIN_N_ANY_VERDICT`), kill 50 (`MIN_N_KILL`), deep-negative expectancy ≤ −0.20 per $1
- C. Full 534, any-verdict 50, kill 20, deep-negative −0.20
- D. Full 1000, any-verdict 100, kill 500, deep-negative −0.50

**Q196. [30] (MCQ)** The `halted_394_fixture` — 394 closes, expectancy −0.375 per $1 — decides:
- A. KEEP-MEASURING — under the 534 full sample, nothing can be decided
- B. KILL — expectancy ≤ −0.20 at ≥50 closes; the evidence bar for killing is lower than the bar for funding
- C. KEEP-FIXING
- D. FUND-ELIGIBLE if its gates are green

**Q197. [30] (MCQ)** A lane with 18 closes and positive expectancy decides:
- A. FUND-ELIGIBLE
- B. KEEP-FIXING
- C. KEEP-MEASURING — below 20 closes the verdict is refused; there is no verdict to give, only more measuring
- D. KILL

**Q198. [30] (MCQ)** A lane with 533 closes, all four gates green, and positive expectancy decides:
- A. FUND-ELIGIBLE — 533 is close enough
- B. Not FUND-ELIGIBLE — funding demands ≥534 closes *and* positive expectancy *and* all four gates; at 533 it keeps measuring/fixing with the shortfall named
- C. KILL
- D. FUND-ELIGIBLE at half size

**Q199. [30] (True/False + justification)** "A structurally dead lane with only 10 closes and all gates green must be kept measuring until it reaches 20 closes." True or false — justify from the precedence order.

**Q200. [30] (Short answer)** Name all four live gates a lane needs green for FUND-ELIGIBLE, and state what dropping any single one does — even at 600 closes with +0.20 expectancy.

---

## ANSWER KEY

### Toolkit 01 [01]

- **Q1. B** — Spot price = virtual SOL ÷ virtual tokens; the reciprocal is the tokens-per-SOL quote.

- **Q2. (Compute)** (a) 30 / 1.073e9 ≈ 2.80 × 10⁻⁸ SOL per token (demo prints 0.0000000280). (b) 1.073e9 / 30 ≈ 35,766,667 tokens per SOL.

- **Q3. (Compute)** Cost = reserve_sol × tokens / (reserve_tokens − tokens) = 100 × 10,000 / (1,000,000 − 10,000) = 1,000,000 / 990,000 ≈ **1.0101 SOL**.

- **Q4. True** — The fee is skimmed from the SOL side on both the buy and the sell, so the round trip loses ≈ the fee twice (2 × 1% ≈ 1.99% with impact dust); at zero fee the loss is exactly 0 — there is no hidden spread.

- **Q5. C** — 42.5 / 85 = 50.0% complete; graduation requires collected ≥ target, so graduated = False.

- **Q6.** The buy itself moves the curve (more SOL in, fewer virtual tokens), so selling into the *original* state prices the exit at prices your own trade already moved past — it understates the true round-trip cost. The sell must be priced on the post-buy state the buy actually left behind.

- **Q7. C** — Buys can only push price up; an at-or-below-spot or non-positive target raises `ValueError`.


### Toolkit 02 [02]

- **Q8. (Compute)** Fee-adjusted input = 1 × (1 − 25/10000) = 0.9975. Out = 0.9975 × 1,000,000 / (100 + 0.9975) = 997,500 / 100.9975 ≈ **9,876.5 tokens** (9,876.48).

- **Q9. B** — Locked iff burned + locker-held ≥ the 90% threshold; exactly 90% locks.

- **Q10. (Compute)** 2·√4 / (1+4) − 1 = 4/5 − 1 = **−20%**. At r = 0.25 also −20%: IL is symmetric under r → 1/r (2·√0.25/1.25 = 1/1.25 = 0.8).

- **Q11. False** — Deployer-held LP is *reported but never counted* as secured: a promise not to pull is not a lock, and counting it would let the one actor who can rug certify their own liquidity.

- **Q12. C** — Missing data returns `"unverifiable"`, passed = False — never a guessed "locked", and "unlocked" would also be wrong because nothing was measured.

- **Q13.** Price impact = % worse than spot from curve movement alone, fee *excluded* (the pool's shape). Slippage = % worse than the spot quote with the fee *included* (what the trader actually pays). Slippage ≥ impact, always.

- **Q14. C** — Same spot price, 10× less depth: the attacker's front-run moves the thin pool far more, costing the victim 29.46% extra vs 3.86%.


### Toolkit 03 [03]

- **Q15. B** — Score = 100 − 25/fail − 5/flag, floored at 0; `passed` is the absence of fails, never the score. The score is a floor, never a ranker.

- **Q16. (Compute)** 100 − 25×2 − 5×1 = 100 − 50 − 5 = **45**; passed = **False** (any hard fail blocks, whatever the score).

- **Q17. B** — 1000 bps (10%) is the flag boundary: PASS, flag `transfer_fee_present`, score 95. 1001 bps is the hard fail.

- **Q18. False** — `meets_score_floor` is True only for a verdict that *passed* and scores ≥ the floor. A FAIL or UNKNOWN meets no floor, not even 0: the floor can only tighten a PASS, never loosen a FAIL.

- **Q19. C** — Unknown fails closed as its own status: UNKNOWN, passed = False, score 0, missing pieces named (`extensions_unknown`, `transfer_fee_unknown`).

- **Q20.** `mintCloseAuthority` and `confidentialTransferMint` — FLAG-only, 5 points each. (A present-but-≤1000 bps transfer fee is the third flag, `transfer_fee_present`.)

- **Q21. D** — `permanentDelegate` is a hard fail (with `transferHook`, `nonTransferable`, frozen-by-default, held mint/freeze authority, fee > 1000 bps). The other three options are flags or clean.


### Toolkit 04 [04]

- **Q22. (Compute)** Verdict = **FLAG**: top1 50.0 is *not* > 50 (strict), so no FAIL; top1 > 25 (and top10 60 > 55) flags. Smallest flip: top1 = 50.01 (+0.01) → > 50 → FAIL.

- **Q23. (Compute)** HHI = 10 × (10²) = **1000**. A monopoly holder: 100² = **10000**.

- **Q24. B** — Program/off-curveaccounts (pools, curves, escrow PDAs) are excluded before the user percentages are computed; counting it would mislabel every liquid token as whale-controlled.

- **Q25. False** — An absent/malformed registry entry classifies as `"unknown"`, never `"user"`: assuming userhood for unexamined accounts would manufacture concentration evidence (or hide it) from no data.

- **Q26. C** — Top-10 > 70 fails on its own; a crowd of mid-size holders summing past 70% is still concentration, just distributed.

- **Q27.** Any two of: empty holder list; total supply missing/zero/negative/non-numeric (incl. NaN/infinite); zero classifiable user wallets (all program/unknown). Each fails closed to `"unknown"`, passed = False.

- **Q28. (Compute)** Cumulative: 30, then 30+25 = 55 > 50 → coefficient = **2**. Exactly 50.0% does *not* satisfy it — the rule is strictly greater; exactly half is not a majority.


### Toolkit 05 [05]

- **Q29. C** — At ≥50% share the verdict is VETO on share alone, for every behaviour including provable holding.

- **Q30. B** — In the 20–50% band a veto needs *distributing* behaviour; missing flow data degrades to FLAG — it can flag but never veto, and never silently pass.

- **Q31. (Compute)** 1 × 2 = 2 < 4 → **holding**. With 2 distributing: 2 × 2 = 4 ≥ 4 and count > 0 → **distributing** (at 25% share that would flip FLAG → VETO).

- **Q32. False** — Union-find is transitive: A–B share F1 and B–C share F2, so all three share one root — **one** cluster. Direct pairwise sharing is not required.

- **Q33. A** — 0.0 would assert a measured zero share (a pass-shaped result); `None` says the share is unmeasurable, and callers must treat UNKNOWN as not-a-pass.

- **Q34.** A VETO on missing data would manufacture a conviction from an evidence gap (any API outage could condemn clean tokens); a PASS would hide a genuinely elevated 20–50% share. FLAG is the honest middle: visible, non-blocking, and revisited when flows arrive.

- **Q35. C** — VETO > UNKNOWN > FLAG > PASS; empty or unrecognised input fails closed to UNKNOWN.


### Toolkit 06 [06]

- **Q36. B** — Both legs are required, over the same snapshot pair, inside the ≤3600s / ≤3-step window; either leg alone does not fire.

- **Q37. (Compute)** Score = 35 + 25 = **60** → grade **"severe"** (50–100). All five raw: 35+30+25+20+15 = 125, **capped at 100**.

- **Q38. False** — The inflation rule is *strictly* > 20%; exactly +20% does not fire (nor does a price drop of exactly 50% — that leg is also strict).

- **Q39. B** — Only an explicit boolean `False` fires `cant_sell`; a missing field is unknown, and `None`, `0`, `"false"`, `1` do not fire either.

- **Q40. B** — `holder_crash` needs ≥40% between two *consecutive* snapshots; a multi-step bleed totalling 40%+ never fires it (a different pattern/watch catches slow bleeds).

- **Q41.** All three together: buy_vol + sell_vol > $10,000; unique_buyers / trades < 0.1; holder count moved < 2% versus the first snapshot.

- **Q42. C** — Bands: 0 clean, 1–24 watch, 25–49 elevated, 50–100 severe. 30 → `"elevated"`.


### Toolkit 07 [07]

- **Q43. (Compute)** 0.50×2.00 + 0.25×3.00 + 0.25×3.50 = 1.000 + 0.750 + 0.875 = **2.625x**.

- **Q44. (Compute)** 0.50×2.00 + 0.25×1.82 + 0.25×0.50 = 1.000 + 0.455 + 0.125 = **1.580x**. Hold-to-stop on the same path realizes **0.50x** — the proven leak: a coin that peaked at 2.6x closing a loser without the ladder.

- **Q45. C** — The trailing tranche arms at 2.0x; below that, only the −50% hard stop protects the position.

- **Q46. False** — TPs fill *at the target price*, not the overshooting bar's price. Filling at the bar price would credit the replay with money a real fill may not have gotten; the target fill is the conservative convention.

- **Q47. B** — 1440 minutes is the 24h time cap; legs close as `time_cap` at that bar's price.

- **Q48.** The remainder exits at the **final bar's price**, with exit reason **`path_end`**.

- **Q49. A** — mean 1.65125 (= (1.58+2.625+0.50+1.90)/4), win rate 0.75, median time-to-peak 55.0 min.


### Toolkit 08 [08]

- **Q50. (Compute)** EV per $1: p·(W−1) − (1−p)·1 = 0 → p = 1/W = 1/3 ≈ **33.3%** breakeven win rate at a 3x payoff.

- **Q51. (Compute)** Sum = 2.5+0+1.2+0.4+3.0+0+0.8 = 7.9 over n = 7 closes; mean multiple 7.9/7 ≈ 1.129; expectancy = **+0.129 per $1**.

- **Q52. C** — 534 closes (alpha 0.05 one-sided, power 0.8). At 100 closes achieved power is only ≈0.297.

- **Q53. A** — Best-of-500 noise "wins" at 0.500 in-sample, then regresses to 0.360 out-of-sample toward the true 0.30; the gap is selection, not skill.

- **Q54. False** — `win_rate` counts multiples *strictly above* 1.0; breakeven is not a win. A 1.0x close does break a loss streak (and counts in the ≥1.0 breakeven share elsewhere).

- **Q55. B** — 6.25% of wallets profitable over 90 days; 1 in 4200 launches reaches $1M (graduation rate itself is pinned as the range 0.63%–1.7%).

- **Q56.** The mean of no outcomes is undefined — returning 0.0 would fabricate neutral evidence ("this lane breaks even") from an empty ledger and let verdicts proceed on nothing. Raising forces the caller to confront the missing sample.


### Toolkit 09 [09]

- **Q57. (Compute)** Penalty = √(2·ln 100) / √100 = √9.2103 / 10 ≈ 3.035 / 10 ≈ 0.303. Deflated = 0.30 − 0.303 ≈ **−0.003** — the claim does **not** survive 100 tried variants (it survives 10 trials at ≈ +0.085).

- **Q58. B** — The plateau is the contiguous region keeping ≥80% of best expectancy; width 1 is a spike, and a spike is fragile — the neighbours' collapse says the setting, not the strategy, did the work.

- **Q59. C** — Cheating +0.403 vs honest −0.002: a single peek at the next bar fabricates ≈ +0.40 expectancy from pure noise.

- **Q60. False** — The registration copies the thresholds dict at construction; mutating the caller's original changes nothing (and post-freeze, every mutation route on the registration itself raises).

- **Q61. B** — Train 0..29, embargo bars 30..34 in neither set, test 35..59; later folds expand the train and keep the 5-bar gap.

- **Q62.** `bonferroni_alpha = alpha / n_trials`. It prices in multiple comparisons: the more variants you test at one alpha, the more false "discoveries" pure chance guarantees, so each test's bar must fall.

- **Q63. C** — Only a frozen registration can certify; unfrozen `certify()` raises `RuntimeError`.


### Toolkit 10 [10]

- **Q64. (Compute)** Round trip = 4 × $1.00 = **$4.00**; as a share of $1 notional = **400%** → **BLOCKED** (400% > 25%).

- **Q65. (Compute)** Need 4 × $1.00 / notional ≤ 0.25 → notional ≥ 4.00 / 0.25 = **$16.00**. At exactly $16 cost is exactly 25% → **allowed** (the gate blocks iff cost *exceeds* 25%).

- **Q66. B** — Halt iff ≥ `min_trades` (15) closes are in the rolling window and mean(multiple) − 1 < 0; 14 losing closes do not halt.

- **Q67. False** — A halt is sticky: later winners never clear it. Only a named human `rearm(operator, reason)` clears it (empty operator/reason raises and is journaled).

- **Q68. B** — Missing/corrupt state loads `halted=True` with a `load_failed` journal entry: a lane that cannot prove its state does not get to trade.

- **Q69. B** — `MAX_OPEN` = 10 concurrent positions; `top_up` and `reset` deliberately do not exist — the $100 record must stay untainted evidence.

- **Q70.** `rearm` *preserves* the closes rather than wiping them, so the (still negative) window re-halts on the next negative close. That is safer because re-arming cannot launder a lane's record into a fresh start — the human decision is visible and the evidence survives it.


### Toolkit 11 [11]

- **Q71. B** — `LAG_GAINS = (0.15, 0.50, 1.00)`; any other lag gain raises `ValueError`. An unreached level is a skip, never a chased entry.

- **Q72. (Compute)** shrunk = (2 × 0.90 + 10 × 0) / (2 + 10) = 1.8 / 12 = 0.15 → weight = 1.0 + 0.15 = **1.15** — small samples are shrunk toward the neutral prior.

- **Q73. B** — Muted iff n ≥ 10 **and** avg ≤ −0.20; both boundaries are inclusive, and 9 follows never mute however bad.

- **Q74. A** — Entries 1.20 / 1.60 / 2.10 all exit at 0.40: 0.333x / 0.250x / 0.190x. Lag decay on a run-then-die path punishes every copier.

- **Q75. False** — A funder-less (or empty-funder) wallet is treated as *its own* funder and stands alone. Unknown funding never merges wallets — fail-closed against manufacturing a pack (or fake convergence).

- **Q76.** The copier's entry is the first path print at or above the target (1.15 target → the 1.20 print), not the target itself. The overshoot is charged to the copier because that print is the price a real late transaction would actually pay.

- **Q77. B** — Weights are clamped to [0.5, 1.5]; the proven wallet's 1.75 raw shrinks to the 1.50 ceiling.


### Toolkit 12 [12]

- **Q78. B** — All three conditions, inclusive thresholds, in *one* snapshot; conditions split across prints never meet the bar (`never-met-bar`).

- **Q79. B** — never-met-bar > rug > ceiling > burned > safety-lag > caught-or-other: the first matching story wins on purpose, so a funnel is never blamed for a correct rug block, and a self-inflicted ceiling outranks bad luck.

- **Q80. C** — `invented-ceiling-block`: the token met every legitimate rule and died on a home-grown 300% gain ceiling.

- **Q81. True** — Both thresholds are inclusive: exactly $25,000 / $50,000 meets the bar; $24,999 fails it by a dollar.

- **Q82. B** — `burned-one-shot-convergence`: a one-shot signal blocked early, bar then met for ~35 minutes, safety later known-ok, and never retried (a retry only counts strictly after the first block, by any lane).

- **Q83.** The rug-block class requires the safety failure (or rug outcome) at a *bar-meeting* snapshot — the gate keeping out a token that actually qualified. A failure recorded while the token was below the bar proves nothing about the funnel's decision on a runner, so it does not count.

- **Q84. (Compute)** Last bar-meeting snapshot at t = 5 + 35 = **40 min**; `explain` reports **3** bar-meeting prints for the DACITY-like fixture (first at t=5, 35-minute window, safety unknown at first bar).


### Toolkit 13 [13]

- **Q85. B** — One fixed win definition everywhere (realized ≥ 1.5x) so lanes compare honestly; `breakeven_rate` separately reports ≥ 1.0x.

- **Q86. (Compute)** Total P/L = 10 closes × $1 × 0.23 = **+$2.30** at $1 shots; × 5 = **+$11.50** at $5 shots.

- **Q87. (Compute)** `capture_rate` uses only the peaked closes: of 5 that peaked ≥ 2x, 2 round-tripped, so 3 were wins → 3 / 5 = **0.60** ✓. (The fixture's 4th win never peaked ≥ 2x, so it is in neither the numerator nor the denominator.)

- **Q88. C** — `None`: with nothing peaked there was nothing to capture; reporting 0.0 would libel a lane for an exit failure it never had the chance to commit.

- **Q89. False** — A record missing any required field raises `KeyError` from every public function. A corrupt row must be loud: silently skipping it would average the ledger into a fiction and hide the corruption.

- **Q90. B** — Win rate counts frequency only; expectancy averages the realized multiples, so differing win/loss *sizes* separate the lanes (here +0.10 vs +0.36 at identical 0.40 WR and identical 0.20 rug rate).

- **Q91.** Both: len(closes) ≥ `min_closes` **and** expectancy *strictly above* `min_expectancy`. Exactly at the expectancy bar fails — a gate that passes on a tie is a gate that passes on noise.


### Toolkit 14 [14]

- **Q92. B** — BONDING, GRADUATED, RUNNER, FADING, DEAD, UNKNOWN — and UNKNOWN (a feed gap) is never a huntable phase.

- **Q93. B** — Holder trend ≥ −5%: holders roughly holding is part of the runner bar; a runner whose holders are fleeing is not running.

- **Q94. B** — Collapse needs *both* momentum ≤ −85% and holder trend ≤ −30% (price + holder flight); price alone is not enough when liquidity survives.

- **Q95. False** — Rule order: completion < 100% returns BONDING before the RUNNER rule is ever reached. Pre-graduation numbers are unproven — the token has not yet survived graduation.

- **Q96. C** — DEAD → RUNNER is legal-but-`"suspicious"` (dormant revivals happen and deserve a second look); FADING → RUNNER is a normal `"ok"` revival. RUNNER never returns to GRADUATED, and no phase returns to BONDING.

- **Q97. (Compute)** RUNNER owns t=55 → 80 = **25 min**; GRADUATED owns t=38 → 55 = **17 min**. (The last point, DEAD at t=105, owns nothing without an end time.)

- **Q98.** Treating a missing metric as zero (or skipping it) would *invent* the very numbers the phase decision rests on — a feed gap would masquerade as a dead (or live) token. UNKNOWN refuses the guess, fails closed, and `is_runner_phase()` is true only for **RUNNER**.


### Toolkit 15 [15]

- **Q99. B** — The selector picks the eligible, fresh, highest-liquidity pair at $0.50; `pairs[0]` is a list position, not evidence.

- **Q100. (Compute)** Median of {1.00, 1.10} = 1.05; disagreement = (1.10 − 1.00) / 1.05 × 100 ≈ **9.52%** → passes the default 15% threshold.

- **Q101. C** — $1,000 dust floor by default; dust pairs are excluded *before* the disagreement check.

- **Q102. False** — Missing time is stale, fail-closed: a timestamp that was never recorded (and no `pairCreatedAt` fallback) cannot prove freshness, and "probably fresh" is how stale prices get traded on.

- **Q103. B** — Quote preference applies only inside a 10% liquidity tie band; unrestricted preference would let a thin USDC dust pair beat the pair that actually carries the market.

- **Q104.** A dust pool's fantasy price ($9.99 against a real $0.50) would poison the disagreement check and force a `None` verdict on a perfectly verifiable token. Excluding sub-floor pairs first keeps the verification about the market that exists.

- **Q105. C** — One record per pair address; the highest-liquidity copy wins and the other is counted as dropped.


### Toolkit 16 [16]

- **Q106. (Compute)** At 30s: 10^(30/120) = 10^0.25 ≈ **1.78x** entry; fraction of move remaining = 1 − 30/120 = **0.75**. At 60s: 10^0.5 ≈ **3.16x** entry.

- **Q107. (Compute)** Pinned break-even delay = **117.9s** (and it still assumes selling the exact peak). An entry exactly at the peak nets **−4.0%** — the full round-trip cost, with no move left.

- **Q108. A** — `sniper` ≤ 2s (real snipers fill at 0.4–2s); `copy_window_2_10min` covers 120–600s; polling detection alone (15–120s bucket) already lands after much of the move.

- **Q109. B** — Copy-window mean expectancy −52.1%, win rate 0.0% at each of 180/300/600s. This is the structural case that killed the live 2–10 min lane: the window cannot pay a late entrant, not merely "hasn't yet".

- **Q110. False** — It raises `ValueError`: after the rise, the peak happened *before* you entered, and the function refuses to sell you a peak you never owned.

- **Q111.** The simulation is deliberately generous to the *late* entrant's counterfactual — early entrants are handed the best possible exit, so by construction nothing in the setup is rigged against the window. If, even with the peak exit granted for free, the 2–10 min entrant still loses ~52%, the loss is in the entry timing itself, not in exit pessimism.

- **Q112. C** — (10 / 1) × (1 − 0.04) − 1 = 9.6 − 1 = **+860%**. (The +841.8% in the demo table is the *1-second* entry; the −4% entries at 120s show the same cost arithmetic with no move left.)


### Toolkit 17 [17]

- **Q113. B** — ≥ 3 launches **and** ≥ 2 rugs; failing either half fails the serial bar.

- **Q114. (Compute)** rug_rate = 3 / 4 = **75%**. For all-unknown outcomes `rug_rate` returns **None** — returning 0.0 would read missing evidence as a spotless record and let a ghost pass as clean.

- **Q115. B** — `WATCH`. The launch-count half protects against convicting on thin data: two launches is a warning to weigh, not a pattern to blacklist.

- **Q116. False** — No history (or all-unknown outcomes) is **UNKNOWN**, never CLEAN — and `should_veto` not firing for a stranger must never be read as "clean" either.

- **Q117. B** — CLEAN is earned: ≥ 2 launches, every outcome known, zero rugs, no reuse flags. A single clean launch, or any unknown outcome, blocks it.

- **Q118.** A recycled funding wallet links a creator's launches to one money source — the same fingerprint as serial re-launchers changing names — so it is real evidence of a pattern. But legitimate creators also reuse their own wallet openly, and reuse alone shows no victim: it warns (WATCH) and lets other evidence convict, instead of vetoing on plumbing.

- **Q119. B** — Only `SERIAL_RUGGER` vetoes. `WATCH` and `UNKNOWN` inform without convicting.


### Toolkit 18 [18]

- **Q120. (Compute)** Round-trip volume = 2 × min(4, 4) = 8 SOL (all other wallets contribute 2 × min(buy, 0) = 0). wash_share = 8 / 24 = **1/3 ≈ 0.333**. A buy-only wallet contributes **0** — only the matched, round-tripped leg counts, on both sides.

- **Q121. B** — Strictly below 0.10 SOL; exactly 0.10 is not tiny.

- **Q122. B** — WASH_SKEW ≥ 0.40, BOT_SWARM ≥ 0.50, BUMP_FARM ≥ 0.50, inclusive at the threshold; shares outside [0, 1] raise `ValueError`.

- **Q123. False** — `assess` returns status `"unknown"` with all shares `None` below `min_trades` (10). Fail-open fingerprint semantics: thin data is never a pass (9 wash trades are not clean) and never a veto (9 trades are not proof either) — no score is reported at all.

- **Q124. B** — Toxicity = 50·wash + 30·bot + 20·bump, capped at 100; the all-wash stream (wash 1.0, others 0) scores exactly 50.0, and the all-bot stream exactly 30.0.

- **Q125.** By count: 9/10 = **90%** bot-like. By volume: the nine dust trades are **< 1%** of volume against the one whale trade. The toxicity score uses the **count** share (`bot_share`) — the two measures can tell opposite stories, which is why both are reported.

- **Q126. B** — Tiny size, a high-frequency wallet (≥ 8 trades), or a crowded slot (≥ 3 trades in one slot; `None` slots never cluster).


### Toolkit 19 [19]

- **Q127. (Compute)** Gross edge = (55 × 0.30 − 45 × 0.30) / 100 = (16.5 − 13.5) / 100 = **+0.030**. After costs: 0.030 − 0.04 = **−0.010**. `clears_breakeven()` = **False** (it requires *strictly* more than the cost).

- **Q128. B** — slow < 0.5, steady < 2.0, fast < 5.0, igniting ≥ 5.0 SOL/min; a rate exactly at a boundary takes the next band up.

- **Q129. C** — `MIN_TRADES` = 10: 9 events → score `None`, prediction `"unknown"` with no call; exactly 10 is scored. No confident score from thin air.

- **Q130. False** — The 0.97 accuracy measures predicting the *event* (graduation), not profiting from it: the calibrated gross edge is +3.0% and the round trip costs ~4%, so following it nets −1.0%. Predicting graduation is not the same as profiting from it.

- **Q131. B** — `None` means "already there" — a terminal state, distinct from a fast token that is 0 minutes away at its current rate; conflating them would let callers treat graduated tokens as imminent graduations.

- **Q132.** Streams whose events cluster into a very short first-to-last span (one burst): dividing by that brief own-span inflates the rate versus any fixed window, and ignores idle time outside the span — burst-shaped launches look faster than sustained ones with the same total inflow.

- **Q133. B** — Default threshold 60.0, graduating iff score ≥ threshold: the 78.0 fast launch calls graduation, the 5.0 slow launch does not.


### Toolkit 20 [20]

- **Q134. (Compute)** Lag penalty = −6.2 − (+31.2) = **−37.4 percentage points** (deepening to −81.5 / −84.6 / −86.9 at 1m / 5m / 15m). The live research's roughly **−41%** is a **follower** number — what delayed copying averaged — not the leader's result; the leader's record and the follower's record are different trades.

- **Q135. A** — 15s (15, 0.01), 1m (60, 0.02), 5m (300, 0.05), 15m (900, 0.15); the leader pays 0.005.

- **Q136. C** — `is_copying_viable` is False for every bucket: negative at every lag, strictly worsening leader → 15s → 1m → 5m → 15m.

- **Q137. False** — Smart money is watchlist-only in the live bot: agreement adds **zero** score in every entry path, because the copied wallets' edge *is* their entry price and a follower necessarily buys after the move and pays extra slippage. Re-enabling requires follower-grade evidence, not leader-grade evidence.

- **Q138. C** — 60s ÷ 5s per step = **12 steps** (15s → 3, 5m → 60, 15m → 180).

- **Q139.** Triggers on the effective (slippage-inflated) price would move the goalposts — the market reaching 2× would no longer be the event being measured, hiding whether the ladder itself fired. Proceeds on spot would hide the slippage drag on *every* exit leg, flattering each follower result by exactly the cost the lab exists to expose.

- **Q140. B** — 50% at the first 2× market print, the rest on a 30% trail from the peak, a −50% hard stop cutting everything if 2× never comes, and any remainder out at the final price.


### Toolkit 21 [21]

- **Q141. C** — The nine are: mcap/FDV, liquidity, context line, safety status line, CA, copy-CA button, total supply line, chain provenance, per-chain exit hint (plus the track button on picks). No promotional element is required — a card that cannot be judged cannot be sent.

- **Q142. B** — 32–44 chars, base58 only (excluding `0`, `O`, `I`, `l`); 31 and 45 chars fail, as does any string containing an excluded character.

- **Q143. False** — Only pick cards (`is_pick=True`) must carry the 📍 track button; a non-pick card needs none.

- **Q144. B** — `"missing chain provenance"`: an unknown chain is a provenance failure (chain must be one of the four *and* carry a non-empty provenance line).

- **Q145.** The 📍 taps are the user's outcome journal: they record per-chain realised results and drive the per-chain feed kill switches / auto-pause decisions. A missing button silently deletes that chain's evidence — the card still reads fine, but the measurement the verdicts depend on never happens.

- **Q146. B** — 8 problems, in stable field order; `"missing CA"` is not among them because the CA is present — everything *around* it is missing, which is exactly the bare-CA failure the contract exists to block.


### Toolkit 22 [22]

- **Q147. B** — `DuplicateEntryError`; the failed add never appends, so a replayed fill can never double-count (same ticket with a different kind, or same kind on a different ticket, is fine).

- **Q148. B** — The exact-timestamp boundary is allowed; strictly-before is `OrderingError`, and a `CLOSE` with no matching `BUY` raises regardless of timestamp.

- **Q149. False** — Both-copies-corrupt (or corrupt main with no backup) raises **`CorruptLedgerError`**. Starting empty would fake a clean slate and destroy the record the verdict gates depend on; a torn main alone recovers from the backup with `recovered_from_backup=True`.

- **Q150. B** — Temp file in place, fsync, previous good main preserved as `.bak`, atomic `os.replace`; no `.tmp` is left behind after a clean save.

- **Q151.** `os.replace` is atomic only within one filesystem: a temp file on another device/mount turns the "atomic rename" into a copy, re-opening exactly the torn-write window (crash mid-copy leaves a half-written main) the design exists to close.

- **Q152. B** — `LockHeldError`, naming the lock path. `O_EXCL` makes creation itself the test: a check-then-create sequence races (two writers both see "no lock", both create, both write).


### Toolkit 23 [23]

- **Q153. (Compute)** Attempt 3: min(60, 1 × 2³) = **8s**. Schedule attempts 0–6: **[1, 2, 4, 8, 16, 32, 60]** (attempt 6 would be 64, capped at 60).

- **Q154. B** — A live pid with a stale heartbeat is judged *not alive*: pid existence catches a dead process, heartbeat freshness catches a wedged one; each signal catches what the other misses.

- **Q155. B** — 499 is the watcher's own pid and 500 its parent shell (whose restart one-liner contains the pattern text). Excluding only self is insufficient — that is the exact self-kill this lab encodes.

- **Q156. False** — `heartbeat_fresh` is inclusive at the boundary: an age exactly at the limit is fresh; only exceeding it is stale.

- **Q157. B** — 0 matches → `"start"`, exactly 1 → `"ok"`, ≥ 2 → `"duplicates"`, reported with the offending records — never silently resolved by killing extras, because which duplicate should survive is a decision, not a default.

- **Q158.** Restarts in a sliding window age out, so the budget recovers: a daemon that crashes twice a month forever is healthy-but-flaky, while a lifetime counter would eventually give up on it permanently — and a crash *loop* still exhausts its window budget quickly and gives up, as it should.


### Toolkit 24 [24]

- **Q159. (Compute)** Round trip = 4 × $0.03 = **$0.12** = **12.00%** of $1 → **ALLOWED** (12% ≤ 25%). Minimum viable notional = 4 × 0.03 / 0.25 = **$0.48**.

- **Q160. (Compute)** Per tx = 1000 × 0.00035 = **$0.35**; round trip = 4 × 0.35 = **$1.40**; percentage = 1.40 / 1000 = **0.14%** → **ALLOWED**. The percentage is identical at $1 ($0.0014 round trip) because a percentage fee scales with the trade — the share of notional is constant at every size, so HL is never blocked by this gate.

- **Q161. B** — 1 entry + up to 3 exit legs (50%@2x, 25%@3x, trailing remainder can each be their own transaction); the gate prices the worst case, not the tidy case.

- **Q162. False** — Blocked iff cost *exceeds* 25%; exactly 25% is allowed: Ethereum at $16 pays $4.00 / $16 = exactly 25% and passes, and one cent below $16 it blocks.

- **Q163. B** — The flat $4.00 round trip is 80% of $5 (BLOCKED) and 16% of $25 (ALLOWED): on a flat-fee chain the verdict is a function of size, which is the whole point of a minimum viable notional.

- **Q164.** Ethereum's fee is a fixed dollar amount per transaction, so its percentage explodes as notional shrinks (400% at $1) and only size can dilute it. Hyperliquid's fee is a fixed *percentage* of notional, so its round-trip share is 0.14% at $0.01 and at $1,000,000 alike — there is no size at which the model alone destroys the trade.


### Toolkit 25 [25]

- **Q165. (Compute)** Expected multiple = 0.90×0 + 0.10×15 = **1.50**; expectancy = 1.50 − 1 = **+0.50** per $1; P(loss) = **0.90** (the 0x outcome is the only sub-stake return); P(ruin outcome) = **0.90**.

- **Q166. (Compute)** 1/28 ≈ **3.57%** of bankroll → 0.0357 × $100 = **$3.57 per shot**. The flat $1 on $100 is a **1%** fraction — deliberately *below* Kelly: less growth bought with far smaller swings and ruin risk.

- **Q167. A** — Kelly ≈ +0.007816, 2×Kelly ≈ +0.002618 (slower, despite identical +0.50 expectancy), f = 0.50 ≈ −0.4159 (negative growth). Overbetting a genuinely good distribution still ruins you.

- **Q168. False** — It raises `ValueError` (tolerance 1e-6): nothing is silently renormalised, because a distribution that does not sum to 1 is a modelling error, and quietly scaling it would price risk on probabilities nobody stated.

- **Q169. C** — At f = 1 with a possible 0x, the bankroll factor 1 − f + f×0 = 0 and the log is undefined — ruin is possible, so the function fails closed instead of returning −∞ for the caller to mishandle. (For a certain win, f = 1 is fine.)

- **Q170.** Ruin is a property of distribution **+ bankroll + bet size + horizon (+ seed/path)**, not of the strategy alone: the identical lottery distribution at identical $1 sizing ruins 0% of seeded paths on $100 over 200 shots and 48% on $10. Quote all the qualifiers or quote nothing.


### Toolkit 26 [26]

- **Q171. (Compute)** A stop at T sacrifices every winner whose peak arrives after T: at 60 min, **42%** of eventual ≥2x winners are cut (only 58% have peaked). At 30 min: P(peak after 30) = 0.750 → **75%** sacrificed. That is why the slow population keeps a generous 24h ceiling: P(peak after 1440) ≈ 0.000, so it costs almost nothing there.

- **Q172. A** — peaked_2x median 53.5 / mean 75.7 min vs post_graduation median 4.7 / mean 6.0 min. Pooling them produces a median that describes neither.

- **Q173. False** — `mix_populations` raises `MixedPopulationError` for different populations unless `allow_mix=True` is passed explicitly. The exception exists, but it is a deliberate, visible override — never a silent pool, however large n is.

- **Q174. C** — The recommender returns the (1 − 0.10) = 90th percentile: **155.2 min** for population A. Not the median (would miss half the peaks), not the max (one outlier would set policy).

- **Q175. B** — Buckets are [0,10), [10,30), [30,60), [60,120), [120,240), [240,∞): the third bucket, [30, 60), holds **66** of 200.

- **Q176.** Mean (75.7) well above median (53.5) means a **right-skewed** distribution: most winners peak reasonably fast, but a long tail of slow peaks drags the mean up. A time stop should therefore be set from the survival/percentile view (`prob_peak_after`, the p90-style recommender) — the share of winners a stop at T actually sacrifices — not from the mean, which prices the tail twice over.


### Toolkit 27 [27]

- **Q177. (Compute)** parked_until = 1000 + 30 + 5 = **1035.0**; remaining immediately after parking = **35s**. A `try_send` while parked adds **0** transport calls (the counter stays at 1 — proven by the call-counting fake, not asserted) and returns **None**; alerts queue instead of storming.

- **Q178. C** — 50,000 bytes: large responses are requested gzip-encoded because the egress proxy drops large uncompressed ones.

- **Q179. B** — A failure while half-open re-opens the breaker immediately; only a success closes it and resets the failure count.

- **Q180. False** — `export_state()` / `FloodGuard.restore(clock, state)` persist `parked_until` across a restart. Without it, a daemon restarting mid-ban would wake unparked, retry into a live flood ban, and make the ban worse — the exact retry-storm the guard exists to prevent.

- **Q181. C** — Unknown keys raise `ValueError`. Independent buckets mean a discovery/research sweep draining its own budget can never starve the main key's live calls.

- **Q182.** (a) Seeded jitter makes the delay list deterministic — the same seed reproduces the same schedule, so tests can pin it. (b) In production, unjittered clients sharing base/factor retry in lockstep after a shared outage (a thundering herd that re-trips the limit); jitter desynchronises them.


### Toolkit 28 [28]

- **Q183. (Compute)** Win rate = 1 / 11 ≈ **0.091 (9.1%)**. Rule: 11 ≥ 10 closed taps **and** 9.1% < 20% → **Base auto-pauses**. (Exactly 20% would *not* pause — the comparison is strict.)

- **Q184. B** — Win = closed with peak ≥ 2.0 (the same bar the exit ladder is built around); exactly 2.0 wins, 1.99 loses, and an open outcome is neither — counting opens as losses would systematically libel young chains.

- **Q185. False** — Solana can never be paused by this machinery: a pause attempt (human or auto) is refused, returns **False**, and is **recorded in `flags`** rather than silently ignored. Solana is the home lane; the switches govern the satellite chains.

- **Q186. B** — There is deliberately no auto-resume: `resume(by="auto")` is refused and flagged; only a human resume re-opens a chain.

- **Q187. B** — Rejected as a source/chain mismatch. Individually-valid fields in an impossible combination are exactly the class of bug (a feed wired to the wrong chain) this check hunts — a mismatch must die at the bus, not on the user's phone.

- **Q188.** `None` says "no evidence yet"; 0.0 would assert a *measured* total failure. The auto-pause rule needs both the tap count and the rate to mean anything — a fake 0.0 on a chain with two closes would misstate its record and poison every downstream consumer that reads the rate without the count.


### Toolkit 29 [29]

- **Q189. B** — $50k liquidity / $100k volume / ≥ +20% 24h gain with no maximum, plus the h1 dump guard (< −15% rejects) and a 6h per-mint cooldown.

- **Q190. B** — Qualifies. The invented 300% ceiling that once silently blocked exactly this shape was removed on 2026-10-19; the Doomsday fixture (+2540%, and an extreme +100,000% case) pins the absence — any future "sanity cap" turns the suite red.

- **Q191. False** — The guard rejects h1 **<** −15% (strict): exactly −15.0 can still qualify (with positive momentum and every other bar met); −15.01 rejects.

- **Q192. B** — Safety fails closed all three ways — missing predicate, False, or raising — each rejects with reason `safety`. A crashed check is an unknown, and unknown never passes.

- **Q193. B** — Only a qualifying pool records a cooldown entry. The opposite rule would let one transient safety-API outage mute every scanned mint for 6 hours.

- **Q194.** Without counted reject reasons, a silent blockage (the invented ceiling blocked real runners for as long as it lived) looks identical to a quiet market: absence of cards, nothing to audit. The stats dict turns every rejection into a counted, named event, so a funnel leak shows up as a spike in one reason instead of as silence.


### Toolkit 30 [30]

- **Q195. B** — REQUIRED_N_FULL = 534, MIN_N_ANY_VERDICT = 20, MIN_N_KILL = 50, DEEP_NEGATIVE_EXPECTANCY = −0.20 per $1.

- **Q196. B** — KILL: expectancy ≤ −0.20 at ≥ 50 closes kills at 394 closes, even though *funding* needs 534. The asymmetry is the point: killing a deep-negative lane stops a known bleed; funding risks capital, so its bar is higher.

- **Q197. C** — KEEP-MEASURING: below 20 closes the protocol refuses a verdict outright — and calendar time never substitutes (52 weeks at 5 closes is still KEEP-MEASURING).

- **Q198. B** — FUND-ELIGIBLE demands all four gates green **and** ≥ 534 closes **and** positive expectancy. At 533 the shortfall is named and the lane keeps measuring/fixing — 533 is not "close enough" for a claim whose whole content is the win-rate separation.

- **Q199. False** — Structural kill is precedence (1) in `decide`: it fires regardless of sample size or gates. "Keep measuring" a proven-dead window buys nothing — the window's economics, not its statistics, are the verdict.

- **Q200.** The four gates: **sustained_positive_expectancy, proven_entry_strategy, top_picks_winning, hardened_execution_safety**. Dropping any single one revokes FUND-ELIGIBLE at any sample size or expectancy — the lane falls to KEEP-FIXING with the red gates listed. A gate waived "just this once" is not a gate.


---

## GRADING RUBRIC

### Scoring

- **1 point per question, 200 points total.** MCQ: the letter alone scores. True/False: the verdict *and* a justification consistent with the key both score — a correct verdict with a wrong reason scores 0. Compute: the final number (within the key's rounding) *and* visible working both score — a correct number with no working, or working that reaches a different number than the one written, scores 0. Short-answer: the key's load-bearing claim must appear; extra correct detail neither adds nor subtracts.
- **PASS BAR: ≥170 / 200 (≥85%).** 170–179 is a pass with named weak spots (below). 160–169 is a near-miss: re-study the weak toolkits only, retake this paper. Below 160 is a fail: re-study in toolkit order and retake in full.
- **No penalty for wrong answers** beyond the lost point — guessing on an MCQ is strictly better than leaving it blank.

### Per-toolkit weak-spot diagnosis

Each question is tagged `[NN]`. Score by tag:

| Tag | Toolkit | Questions | Misses that trigger re-study |
|-----|---------|-----------|------------------------------|
| [01] | bonding-curve-math | Q1–Q7 (7) | ≥3 wrong |
| [02] | amm-and-lp | Q8–Q14 (7) | ≥3 wrong |
| [03] | token-safety-veto | Q15–Q21 (7) | ≥3 wrong |
| [04] | holder-concentration | Q22–Q28 (7) | ≥3 wrong |
| [05] | bundle-cluster-detection | Q29–Q35 (7) | ≥3 wrong |
| [06] | rug-taxonomy | Q36–Q42 (7) | ≥3 wrong |
| [07] | exit-ladder-replay | Q43–Q49 (7) | ≥3 wrong |
| [08] | statistical-validity-meme | Q50–Q56 (7) | ≥3 wrong |
| [09] | backtesting-rigor-meme | Q57–Q63 (7) | ≥3 wrong |
| [10] | live-risk-controls-meme | Q64–Q70 (7) | ≥3 wrong |
| [11] | smart-wallet-grading | Q71–Q77 (7) | ≥3 wrong |
| [12] | funnel-autopsy | Q78–Q84 (7) | ≥3 wrong |
| [13] | post-trade-meme | Q85–Q91 (7) | ≥3 wrong |
| [14] | token-lifecycle | Q92–Q98 (7) | ≥3 wrong |
| [15] | feed-data-quality | Q99–Q105 (7) | ≥3 wrong |
| [16] | sniper-window-economics | Q106–Q112 (7) | ≥3 wrong |
| [17] | deployer-forensics | Q113–Q119 (7) | ≥3 wrong |
| [18] | wash-and-botshare | Q120–Q126 (7) | ≥3 wrong |
| [19] | liquidity-velocity | Q127–Q133 (7) | ≥3 wrong |
| [20] | copy-trading-economics | Q134–Q140 (7) | ≥3 wrong |
| [21] | card-design | Q141–Q146 (6) | ≥3 wrong |
| [22] | ledger-accounting | Q147–Q152 (6) | ≥3 wrong |
| [23] | daemon-supervision | Q153–Q158 (6) | ≥3 wrong |
| [24] | cross-chain-costs | Q159–Q164 (6) | ≥3 wrong |
| [25] | lottery-sizing | Q165–Q170 (6) | ≥3 wrong |
| [26] | time-to-peak | Q171–Q176 (6) | ≥3 wrong |
| [27] | feed-resilience | Q177–Q182 (6) | ≥3 wrong |
| [28] | signal-refinery | Q183–Q188 (6) | ≥3 wrong |
| [29] | momentum-scanner | Q189–Q194 (6) | ≥3 wrong |
| [30] | verdict-protocol | Q195–Q200 (6) | ≥3 wrong |

**Rule:** a cluster of misses inside one `[tag]` (≥3 wrong in that toolkit's block) means the *toolkit*, not the question, was not learned. For each triggered tag: re-read that toolkit's note/README in full, re-run its demo, then retake that toolkit's own per-toolkit exam (where one exists under `study/exams/`) before retaking this paper. A pass (≥170) with one triggered tag is recorded as a **conditional pass** — the condition is clearing that toolkit's re-study.

### Cross-cutting diagnosis

- **Compute cluster:** if ≥10 of the 37 compute questions are wrong while conceptual questions are mostly right, the failure is arithmetic discipline, not concepts — redo the compute questions with working shown, checking each against the toolkit demo output.
- **Boundary cluster:** if the misses concentrate on strict-vs-inclusive boundaries (fee 1000/1001 bps, holder 50.0/50.01, gate exactly-25%, dump guard −15.0/−15.01, auto-pause exactly-20%, verdict 533/534), re-study the boundary tables — in this system, boundaries *are* the policy.
- **Fail-closed cluster:** if the misses concentrate on unknown/missing-data questions (safety UNKNOWN, holder unknown, LP unverifiable, ledger corruption, feed staleness, deployer UNKNOWN), the core posture was missed: in every toolkit here, *unknown is never a pass*. Re-read toolkit 03 first, then re-check the others against it.


---

## APPENDIX — Source map (for the examiner; not part of the paper)

Every figure in this exam is pinned in the named toolkit's README (demo output and/or test-suite description) and module:

| Tag | Lab module | Pinned figures used |
|-----|-----------|---------------------|
| [01] | `curve.py` | 1.073e9 / 30 SOL defaults, 85 SOL target, 100 bps fee, 1.0101 SOL zero-fee 1% cost, ~1.99% round trip |
| [02] | `amm.py` | 9,876.5 quote at 25 bps, ≥90% lock threshold, −20% IL at r=4/0.25, 3.86% vs 29.46% sandwich |
| [03] | `veto.py` | 100 − 25/fail − 5/flag, 1000/1001 bps fee boundary, UNKNOWN at score 0 |
| [04] | `holders.py` | >50/>70 FAIL, >25/>55 FLAG (strict), HHI 1000 for ten equal holders, strict->50 Nakamoto |
| [05] | `bundle.py` | ≥50% veto, 20–50% + distributing veto, `count×2 ≥ active` behaviour rule |
| [06] | `ruglab.py` | 80%/50% LP-pull, >20% mint inflation, weights 35/30/25/20/15, grade bands |
| [07] | `replay.py` | 2.625x clean runner, 1.580x vs 0.500x round-tripper, 1440-min cap, batch mean 1.65125 |
| [08] | `statslab.py` | 534 closes, +0.129 demo expectancy, 0.500→0.360 p-hacking, 6.25% / 1-in-4200 base rates |
| [09] | `rigor.py` | +0.403 vs −0.002 look-ahead, −0.003 deflated at 100 trials, embargo-5 fold layout |
| [10] | `risk.py` | window 30 / min 15, ETH $4.00 = 400% blocked, $16 boundary, MAX_OPEN 10 |
| [11] | `grading.py` | lags +15/+50/+100%, 0.333/0.250/0.190 demo, weight clamp [0.5, 1.5], mute ≥10 & ≤ −0.20 |
| [12] | `autopsy.py` | $25k / $50k bar (inclusive), precedence order, DACITY 3 prints / t=5 / 35 min |
| [13] | `posttrade.py` | WIN 1.5 / PEAK 2.0, fixture exp +0.23, 2 round-trippers @ 0.65, capture 0.60 |
| [14] | `lifecycle.py` | runner bar (≥15%, ≥$25k, ≥$50k, holders ≥ −5%), collapse ≤ −85% & ≤ −30%, DACITY times |
| [15] | `feedquality.py` | $40/$9.99 dust vs $180k/$0.50 real, 900s / 15% / $1,000 defaults, 10% tie band |
| [16] | `sniperwindow.py` | 10^0.25 ≈ 1.78x @30s, break-even 117.9s, copy window −52.1%, buckets ≤2s / 120–600s |
| [17] | `deployer.py` | serial = ≥3 launches & ≥2 rugs, SerialSam 3/4 = 75%, rug_rate None when unknown |
| [18] | `wash.py` | 2·min(buy,sell), tiny < 0.10, flags 0.40/0.50/0.50, toxicity 50/30/20, min_trades 10 |
| [19] | `liqvelocity.py` | bands 0.5/2.0/5.0, MIN_TRADES 10, gross +0.030, after-cost −0.010, accuracy 0.97 |
| [20] | `copyecon.py` | leader +31.2%, lags −6.2/−50.3/−53.4/−55.7%, slippage 1/2/5/15%, live copy ≈ −41% |
| [21] | `cardcheck.py` | nine required elements, base58 32–44 excl. `0 O I l`, 8 problems on a bare CA |
| [22] | `ledger.py` | Duplicate/Ordering/LockHeld/Corrupt errors, .tmp → fsync → .bak → os.replace |
| [23] | `supervisor.py` | backoff [1,2,4,8,16,32,60], naive [499,500,700,701] vs safe [700,701], 5 per 600s |
| [24] | `chaincosts.py` | flat 0.001/0.03/1.00, HL 0.035%, 4 txs, 25% gate, min notionals 0.016/0.48/16.00 |
| [25] | `sizing.py` | lottery E = 1.50 / +0.50, Kelly 1/28 ≈ 3.57%, growth +0.007816 / +0.002618 / −0.4159 |
| [26] | `timetopeak.py` | medians 53.5 vs 4.7 min, P(after 60) = 0.420, p90 stop 155.2, histogram A |
| [27] | `resilience.py` | retry_after + 5s buffer = 35s, gzip at 50,000 bytes, breaker threshold 3 / 30s |
| [28] | `refinery.py` | win = closed & peak ≥ 2.0, auto-pause ≥10 taps & WR < 20%, Solana never pauses |
| [29] | `momentumscan.py` | $50k / $100k / ≥+20% (no cap), dump guard < −15%, 6h cooldown, Doomsday +2540% |
| [30] | `verdict.py` | 534 / 20 / 50, deep-negative −0.20, 394-close −0.375 KILL fixture, four gate names |

Examiner's notes:

- Figures that appear in a toolkit README only as prose claims without a demo, fixture, or
  test pin were not used as compute answers; where a number is used, its working is shown
  in the key so any grader can re-derive it from the lab's own formulas.
- The live-bot context numbers quoted inside questions (e.g. the ≈ −41% delayed-copying
  average in [20], the Doomsday shape in [12]/[29]) are the values the toolkits' own
  READMEs state when explaining what the lab mirrors; they are asked about as
  *reported* values, never as measurements the candidate must reproduce.
- Two populations are kept apart throughout, as the labs insist: peaked≥2x closes vs
  post-graduation peaks in [26], and leader vs follower records in [11]/[20]. Questions
  that mix them do so only as the wrong option.
