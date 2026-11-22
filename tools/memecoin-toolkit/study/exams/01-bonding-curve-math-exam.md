# Exam — Bonding Curve Math

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `01-bonding-curve-math` README and `curve.py` alone.

---

## QUESTIONS

**Q1 (MCQ).** What is the spot price in this lab?
A. `virtual_tokens / virtual_sol`
B. `virtual_sol / virtual_tokens` (SOL per token)
C. `total_supply / virtual_sol`
D. `k / virtual_tokens`

**Q2 (MCQ).** The default `Curve` is constructed with which values?
A. 1.073e9 virtual tokens, 30.0 virtual SOL, 1e9 supply, 100 bps fee, 85.0 SOL graduation target
B. 1e9 virtual tokens, 85.0 virtual SOL, 1.073e9 supply, 25 bps fee, 30.0 SOL target
C. 1.073e9 virtual tokens, 85.0 virtual SOL, 1e9 supply, 100 bps fee, 30.0 SOL target
D. 1e9 virtual tokens, 30.0 virtual SOL, 1.073e9 supply, 0 bps fee, 85.0 SOL target

**Q3 (MCQ).** Constructing a `Curve` with `fee_bps = 10000` does what?
A. Is accepted — 100% fee
B. Raises `ValueError` — fee must lie in `[0, 10000)`
C. Is clamped to 9999
D. Raises `TypeError`

**Q4 (True/False + justification).** "The fee is skimmed from the SOL side on both buys and sells, so a same-size buy-then-sell round trip loses only the fee (twice) — there is no hidden spread." True or false? Justify in one sentence.

**Q5 (Compute).** Fixture: zero-fee curve, `virtual_token_reserves = 1,000,000`, `virtual_sol_reserves = 100`, `total_supply = 1,000,000`. (a) Spot price in SOL per token. (b) Gross SOL cost to buy exactly 1% of supply (10,000 tokens).

**Q6 (Compute).** Same zero-fee fixture as Q5. How many tokens does a 10 SOL buy receive?

**Q7 (Compute).** On the default curve (target 85.0 SOL): (a) `completion_pct(42.5)`; (b) `completion_pct(120.0)`; (c) `remaining_sol_to_graduation(42.5)`; (d) `is_graduated(85.0)` — true or false?

**Q8 (MCQ).** `cost_to_buy_tokens` and `tokens_received_for_sol` are related how?
A. They are unrelated approximations
B. `cost_to_buy_tokens` is the exact inverse of `tokens_received_for_sol`
C. One includes the fee, the other does not
D. They differ by exactly the spot price

**Q9 (True/False + justification).** "`curve_after_buy_sol` mutates the original curve in place." True or false? Justify.

**Q10 (Short answer).** Why must a true round-trip sell be priced on the post-buy curve state (`curve_after_buy_sol` first) rather than on the original curve?

**Q11 (MCQ).** At 100 bps fee, `round_trip_loss_pct` for a buy-then-immediate-sell-all is approximately:
A. 0%
B. 1.00%
C. 1.99% (≈ the fee twice)
D. 10.0%

**Q12 (Compute).** Small fixture from the test suite: the SOL needed to double the spot price is 41.421356… SOL. A curve has k = 100,000, virtual SOL = 100, spot = 0.1, fee = 0. Verify the figure: compute gross SOL to reach spot 0.2 using `sqrt(k · target) − virtual_sol`.

**Q13 (MCQ).** `sol_to_reach_spot_price(target)` raises `ValueError` when the target is:
A. Strictly above spot only
B. Positive and above spot
C. At or below the current spot, zero, or `None` — buys cannot move price down or sideways
D. Never; any target works

**Q14 (Short answer).** Why does the per-SOL token rate get worse as buy size grows, even before any fee is applied?

**Q15 (MCQ).** Price impact and slippage in this lab are:
A. Always different, impact larger
B. Numerically identical in both directions — slippage is the same quantity under the trader's name
C. Slippage excludes the fee, impact includes it
D. Both always exactly 0 at zero fee

**Q16 (True/False + justification).** "Buying 100% of supply always raises `ValueError`." True or false? Justify using the default-curve result in the README/tests.

**Q17 (Compute).** Default curve: (a) spot price (SOL per token, to 3 significant figures); (b) tokens per SOL; (c) `market_cap_sol()` (= spot × total supply).

**Q18 (MCQ).** `simulate_buys([])` (empty sequence) does what?
A. Raises `ValueError`
B. Is a no-op returning `(0.0, this curve)`
C. Returns the whole reserve
D. Graduates the curve

**Q19 (Short answer).** `simulate_buys([1, 1])` equals one combined 2-SOL buy. What property of the fee model makes split and combined buys equal here?

**Q20 (MCQ).** Sell proceeds are bounded above by:
A. The total supply
B. The virtual SOL reserve × (1 − fee)
C. The graduation target
D. There is no bound

**Q21 (True/False + justification).** "A huge buy can drain the virtual token reserve to exactly zero if enough SOL is supplied." True or false? Justify.

**Q22 (Short answer).** The two effects bundled into the single number a trader calls "slippage" against the spot quote are what, in this model?

---

## ANSWER KEY

- **Q1: B** — Spot is defined as `virtual_sol / virtual_tokens`, SOL per token.
- **Q2: A** — Those are the dataclass defaults stated in the README.
- **Q3: B** — Construction is fail-closed: `fee_bps` must be in `[0, 10000)`.
- **Q4: True** — Fee is taken from the SOL side on entry and exit, so the round trip loses ≈ fee × 2 and nothing else.
- **Q5: (a) 0.0001 SOL/token** (100 / 1,000,000). **(b) 1.010101… SOL.** Working: new_tokens = 990,000; new_sol = k/new_tokens = 100,000,000/990,000 = 101.0101…; net = 101.0101 − 100 = 1.010101 SOL (zero fee, so gross = net).
- **Q6: 90,909.09 tokens.** Working: new_sol = 110; new_tokens = 100,000,000/110 = 909,090.91; received = 1,000,000 − 909,090.91 = 90,909.09.
- **Q7: (a) 50.0%; (b) 100.0%** (clamped to [0, 100]); **(c) 42.5 SOL** (85.0 − 42.5); **(d) True** — graduated iff collected ≥ target, so exactly 85.0 counts.
- **Q8: B** — The README defines cost as the exact inverse of tokens-received.
- **Q9: False** — `Curve` is a frozen dataclass; `curve_after_*` returns a new `Curve` and the original never moves.
- **Q10:** The buy itself moves the reserves (SOL in, tokens out), so the sell executes against different — worse-for-the-seller — reserves; pricing it on the pre-buy state would understate the true cost.
- **Q11: C** — ≈1.99%, the 1% fee paid on both legs; exactly 0 at zero fee.
- **Q12: 41.421356 SOL.** Working: sqrt(100,000 × 0.2) = sqrt(20,000) = 141.421356; minus virtual_sol 100 = 41.421356; zero fee so gross = net. ✓
- **Q13: C** — Target must be positive and strictly above spot.
- **Q14:** Constant-product impact: each SOL added raises the marginal price because k must be preserved — later tokens in the same buy are bought at higher prices than earlier ones, fee or no fee.
- **Q15: B** — Tests pin slippage ≡ price impact numerically in both directions in this lab.
- **Q16: False** — On the default curve buying 100% of supply succeeds (≈415.11 SOL) because virtual reserves (1.073e9) exceed supply (1e9), leaving headroom; it raises only when the buy would drain the reserve (e.g. supply == reserves fixture).
- **Q17: (a) ≈2.80e-8 SOL/token** (30 / 1.073e9 = 2.796e-8); **(b) 35,766,667 tokens/SOL** (1.073e9 / 30); **(c) ≈27.96 SOL** (2.796e-8 × 1e9).
- **Q18: B** — Empty sequence is explicitly a no-op; a non-positive leg or `None` raises.
- **Q19:** The fee is linear (a fixed proportion of each leg's SOL, with no fixed/per-trade component), so splitting does not change total fee or the final constant-product state.
- **Q20: B** — You cannot extract more SOL than is in the virtual reserve, and the fee is skimmed from what leaves.
- **Q21: False** — The constant-product curve is asymptotic: a 1B-SOL buy still leaves ~32.5 tokens; reserves approach but never reach zero.
- **Q22:** Fee + price impact (curve movement). At dust size the impact ≈ the fee alone; both are inside the spot-vs-fill gap.

---

## GRADING RUBRIC

- **MCQ (Q1–Q3, Q8, Q11, Q13, Q15, Q18, Q20):** 1 point for the correct letter only; no partial credit.
- **True/False + justification (Q4, Q9, Q16, Q21):** 1 point only if both the verdict AND the justification are correct; correct verdict with wrong/missing justification = 0.
- **Short answer (Q10, Q14, Q19, Q22):** 1 point if the answer states the mechanism named in the key (not merely a restatement of the question).
- **Compute-it (Q5, Q6, Q7, Q12, Q17):** 1 point per question; all sub-parts must be numerically correct (small rounding tolerance, e.g. ±0.01 or 3 s.f. where stated). Working shown with a single arithmetic slip that is then corrected still earns the point; a correct number with the wrong method does not.
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and the missed sections of `curve.py`, then retake the full exam. A fail means the thresholds/formulas are not yet reliable from memory — do not proceed to rely on this toolkit's numbers in live reasoning until a retake passes.
