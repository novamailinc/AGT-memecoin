# Exam — Cross-Chain Costs

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only — every figure is a documented model constant checkable by hand. All answers are derivable from the toolkit's README / `chaincosts.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

**Model constants provided by the toolkit (you must use these, not live fees):** flat per-tx — solana $0.001, base $0.03, ethereum $1.00; hyperliquid 0.035% of notional per tx; worst-case round trip = 4 txs; gate limit = 25.0%.

---

## QUESTIONS

**Q1 (MCQ).** Why is the worst-case round trip modelled as 4 transactions rather than 2?
A. Gas is charged twice
B. 1 entry + up to 3 exit legs, because the exit ladder can fire in pieces
C. 2 entries + 2 exits
D. A safety margin with no structural reason

**Q2 (Compute).** Ethereum at a $1 notional: compute the round-trip cost in dollars and as a percentage, and give the gate verdict.

**Q3 (Compute).** Ethereum at a $25 notional: compute the round-trip cost in dollars and as a percentage, and give the gate verdict.

**Q4 (Compute).** Solana and Base at a $1 notional: compute each round-trip percentage and verdict.

**Q5 (Compute).** Hyperliquid at notionals $1, $1,000, and $1,000,000: what is the round-trip percentage in each case, and can this gate ever block Hyperliquid? Show the $1,000 round-trip dollar cost.

**Q6 (MCQ).** The gate rule is precisely:
A. Blocked iff round-trip cost ≥ 25% of notional
B. Blocked iff round-trip cost *exceeds* 25% of notional; exactly 25% is allowed
C. Blocked iff round-trip cost exceeds 20% of notional
D. Blocked iff round-trip cost exceeds $1

**Q7 (Compute).** Ethereum at a $16 notional: compute the round-trip percentage and the verdict. Why is this row the boundary case?

**Q8 (Compute).** Give `min_viable_notional` for all four chains at the default 25% gate / 4 txs, and show the formula for a flat chain.

**Q9 (True/False + justification).** True or false: 99% of Ethereum's minimum viable notional fails the gate, while 100% of it passes. Justify arithmetically.

**Q10 (MCQ).** `tx_cost_usd("hyperliquid", 100)` equals:
A. $1.00
B. $0.035
C. $3.50
D. $0.0014

**Q11 (Compute).** `tx_cost_usd` on a flat chain: compute one transaction on Base at notionals $1 and $10,000. What does the comparison prove about flat fees?

**Q12 (Short answer).** What structural difference between the flat and percentage fee models explains why Ethereum can be blocked while Hyperliquid never can be (by this gate)?

**Q13 (True/False + justification).** True or false: chain names are case-insensitive, so `"Ethereum"` is accepted. Justify.

**Q14 (MCQ).** `cost_table()` with default notionals (1.0, 5.0, 25.0) and default chains produces how many rows?
A. 3
B. 4
C. 12
D. 25

**Q15 (Compute).** Fill the $5 row of the cost table for all four chains: round-trip dollars, percentage, verdict for each.

**Q16 (Short answer).** `GateResult` also unpacks as a 4-tuple. Name those four unpacked values in order, and state what the `reason` string names in both verdicts.

**Q17 (Compute — what-if, from the README self-exam).** If Ethereum gas doubled in the model to $2.00 per tx: (a) what is the new minimum viable Ethereum notional? (b) Which rows of the $1/$5/$25 Ethereum column change verdict, if any? Show the new percentages at $1, $5, $25.

**Q18 (True/False + justification).** True or false: Solana's minimum viable notional of $0.016 means $0.02 Solana positions are necessarily a good idea. Justify — name what the model does not capture.

**Q19 (MCQ).** Which of these inputs are fail-closed `ValueError`s?
A. Unknown chain, non-positive/non-numeric notional, non-positive or non-integer tx count, bad gate limit
B. Only an unknown chain
C. Only a negative notional
D. None — bad inputs default to Solana

**Q20 (Short answer).** Why does pre-registration of the 25% rule matter — why block *before* entry on a model rather than measuring realised costs afterwards?

---

## ANSWER KEY

**Q1 — B.** 1 entry + up to 3 exit legs; the ladder (e.g. partial exits) can fire in pieces, so the honest worst case is 4 txs (`ROUND_TRIP_TXS = 4`).
**Q2 — $4.00, 400%, BLOCKED.** Working: 4 × $1.00 = $4.00; $4.00 / $1 = 400% > 25%.
**Q3 — $4.00, 16%, ALLOWED.** Working: 4 × $1.00 = $4.00 (flat, notional-independent); $4.00 / $25 = 16% ≤ 25%.
**Q4 — Solana: 0.40%, ALLOWED** (4 × $0.001 = $0.004; $0.004/$1 = 0.40%). **Base: 12.00%, ALLOWED** (4 × $0.03 = $0.12; $0.12/$1 = 12.00%).
**Q5 — 0.14% at every notional; never blocked.** Working: per tx 0.035% × 4 txs = 0.14% of notional, constant because the fee scales with the trade. At $1,000: per-tx $1000 × 0.00035 = $0.35; round trip 4 × $0.35 = **$1.40**, which is still 0.14%.
**Q6 — B.** Blocked iff cost *exceeds* 25% (`GATE_LIMIT_PCT = 25.0`); exactly 25% is allowed.
**Q7 — 25.00%, ALLOWED.** Working: $4.00 / $16 = 25.00% exactly — the boundary; allowed by the strict "exceeds" rule, and $16 is Ethereum's minimum viable notional.
**Q8 —** Formula (flat chains): `txs × fee / limit` = 4 × fee / 0.25. **Solana: $0.016** (4×0.001/0.25; rendered $0.02 in the demo); **Base: $0.48** (4×0.03/0.25); **Ethereum: $16.00** (4×1.00/0.25); **Hyperliquid: $0.0** — a percentage fee scales with the trade, so no floor exists.
**Q9 — True.** At 99% of $16 = $15.84, cost % = $4.00/$15.84 ≈ 25.25% > 25% → blocked. At $16 exactly it is 25.00% → allowed. (Equivalently the test locks: the minimum passes, 99% of it fails.)
**Q10 — B.** $100 × 0.00035 = $0.035 per transaction.
**Q11 — $0.03 at both $1 and $10,000.** A flat per-tx fee is independent of notional — which is exactly why flat-chain cost *percentage* falls as notional grows.
**Q12 —** Flat fees are fixed dollars, so their percentage of a trade blows up as notional shrinks (Ethereum $1 → 400%). A percentage fee is always the same fraction of notional (0.14% round trip) at any size, so it can never cross a percentage gate.
**Q13 — False.** Chain lookup is exact/case-sensitive; case-sensitive lookalikes of a known chain are rejected as unknown chains with `ValueError`.
**Q14 — C.** 3 notionals × 4 chains = 12 rows.
**Q15 — $5 row:** solana $0.0040, 0.08%, ALLOWED; base $0.1200, 2.40%, ALLOWED; ethereum $4.0000, 80.00%, BLOCKED; hyperliquid $0.0070, 0.14%, ALLOWED. (Working: round-trip dollars as in Q2/Q4/Q5; % = cost/$5.)
**Q16 —** `(allowed, cost_usd, cost_pct, reason)` in that order. The `reason` names the chain, the dollar cost, the percentage, and the limit, for both the allowed and blocked verdicts.
**Q17 — (a) $32.00** (4 × $2.00 / 0.25 = $32). **(b) Exactly one row changes:** new round trip = $8.00; at $1 → 800% BLOCKED (was blocked, unchanged); at $5 → 160% BLOCKED (was blocked at 80%, unchanged); at $25 → 32% BLOCKED, whereas the original was ALLOWED at 16% — so **the $25 row flips ALLOWED → BLOCKED**. At $2/tx all three Ethereum rows ($1/$5/$25) are blocked.
**Q18 — False.** The minimum only says the *modelled tx fees* pass the 25% gate. The model does not capture other costs that still eat tiny positions (e.g. slippage / adverse fills and spread — any answer naming a real non-tx cost the model omits, consistent with the README's "what costs does this model not capture", earns the point; the key point is: passing a fee gate ≠ a good trade).
**Q19 — A.** All of those are fail-closed rejections.
**Q20 —** Pre-registering the 25% limit *before* seeing trades makes the block a rule, not a post-hoc rationalisation: the gate decides on the model before any market risk is taken, the math is logged, and the threshold cannot be quietly moved to admit a trade after the fact. Measuring afterwards would let cost-destroyed trades happen first and be explained later.

---

## GRADING RUBRIC

- **MCQ (Q1, Q6, Q10, Q14, Q19):** 1 point for the correct letter only.
- **True/False + justification (Q9, Q13, Q18):** 1 point only if verdict and justification both match the key (Q9 requires the arithmetic).
- **Short-answer (Q12, Q16, Q20):** 1 point if every element in the key is present and correct.
- **Compute-it (Q2, Q3, Q4, Q5, Q7, Q8, Q11, Q15, Q17):** 1 point for the exact numbers and verdicts in the key, with working where shown; a correct verdict with a wrong percentage scores 0. On Q17, the $25 flip must be identified.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `chaincosts.py` if needed), then retake the full exam. Recompute by hand — every figure here is checkable without code.
