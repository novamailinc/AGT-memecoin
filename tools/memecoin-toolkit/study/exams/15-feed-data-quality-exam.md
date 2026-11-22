# Exam — Feed Data Quality

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** `select_pair()` uses these defaults:
- A. Max age 60s, max disagreement 5%, min liquidity $10,000
- B. Max age 900s, max disagreement 15%, min liquidity $1,000
- C. Max age 900s, max disagreement 50%, min liquidity $100
- D. No defaults — all three must be supplied

**Q2.** In the dust-first fixture (`pairs[0]` = $40 liquidity quoting $9.99; a $180,000 pair quoting $0.50 second; a $60,000 pair quoting $0.51 third), the selector picks:
- A. `pairs[0]`, the $9.99 dust pair
- B. The $180,000 pair quoting $0.50
- C. The $60,000 USDC pair, because USDC is preferred
- D. `None`, because the prices disagree

**Q3.** `parse_pair()` on a malformed entry (e.g. non-positive price, missing blocks, unparseable numbers):
- A. Raises `ValueError`
- B. Returns a record with price 0
- C. Returns `None` — the entry is skipped and counted, never trusted and never raised on
- D. Returns the raw dict unchanged

**Q4.** `parse_payload()` given a non-list payload:
- A. Returns `([], 0)`
- B. Raises `ValueError` — that is a caller bug, not data
- C. Treats the payload as one pair
- D. Returns `None`

**Q5.** When the same pair address appears twice with different liquidity, `dedupe_pairs()` keeps:
- A. The first copy
- B. The average of the copies
- C. The highest-liquidity copy, and reports the drop count
- D. Both — duplicates are harmless

**Q6.** The quote-preference order (most to least preferred) is:
- A. SOL > USDC > ETH > USDT > anything else
- B. USDC > SOL > USDT > WETH/ETH > anything else
- C. ETH > WETH > USDC > SOL > anything else
- D. Alphabetical by symbol

**Q7.** `price_disagreement_pct()` is defined as:
- A. (max − min) / min price, in %
- B. (max − min) / mean price, in %
- C. (max − min) / median price, in %
- D. The standard deviation of prices, in %

**Q8.** In `eligible_pairs()`, pairs under the dust floor are:
- A. Ranked last but still included in the disagreement check
- B. Excluded *before* the price-disagreement verification, so a dust pool's fantasy price cannot poison the check
- C. Included only if their price agrees with the liquid pairs
- D. Used as the fallback selection

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** A pair with no timestamp at all should be treated as probably fresh.

**Q10.** A pair timestamped in the future is stale.

**Q11.** Quote preference can promote a USDC pair with one-tenth the liquidity of a SOL pair to first place.

**Q12.** `price_disagreement_pct([])` returns 0.0.

### Short answer (Q13–Q17)

**Q13.** Why is `pairs[0]` an unsafe default even when the API usually lists a sensible pair first?

**Q14.** Two liquid pairs disagree on price by 57%. Why does the selector return nothing instead of trusting the more liquid one?

**Q15.** The messy fixture contains: a good pair ($120k), an exact duplicate of it, a richer duplicate of the same address ($130k), two malformed entries, another mint's pair, and one stale pair. State the selection and the malformed / duplicate / stale counts the demo reports.

**Q16.** If a raw pair carries no explicit data timestamp, where does its timestamp come from, and what happens if that is also absent?

**Q17.** How does `eligible_pairs()` treat pairs belonging to a different mint, and what input of the caller's raises `ValueError`?

### Compute it (Q18–Q20) — show your working

**Q18.** Two eligible pairs quote $0.50 and $0.90. Compute `price_disagreement_pct` and state what `select_pair` returns at the default 15% threshold.

**Q19.** In the dust-first fixture, the $40 pair is excluded as dust, leaving quotes $0.50 and $0.51. Compute the disagreement and state the consequence for selection.

**Q20.** With `now_ms = 1,900,000` and the default 900s max age, decide `is_stale` for pairs timestamped at: (a) 1,000,000 ms; (b) 999,000 ms; (c) 1,950,000 ms.

---

## ANSWER KEY

**Q1. B** — Defaults: `max_age_seconds=900`, `max_disagreement_pct=15`, `min_liquidity_usd=1000`.

**Q2. B** — The $40 pool is below the dust floor and excluded; the $180k pair is the real market and wins on liquidity (quote preference never beats that gap).

**Q3. C** — Malformed entries parse to `None`, are skipped, and are counted in the stats.

**Q4. B** — A non-list payload is a programming error by the caller, so it raises rather than failing closed as data.

**Q5. C** — One record per pair address; the richest copy wins and the number dropped is returned.

**Q6. B** — `USDC > SOL > USDT > WETH/ETH > anything else`, via `quote_rank()` (case-insensitive).

**Q7. C** — (max − min) / median × 100; 0.0 for a single pair.

**Q8. B** — Dust exclusion happens before verification by design; ranking dust last would still let its price poison the disagreement check.

**Q9. False** — Missing time is stale: a pair with no timestamp is treated as stale (fail-closed), never assumed fresh.

**Q10. True** — A future timestamp gives a negative age, which `is_stale` treats as stale — a clock/feed error is not freshness.

**Q11. False** — Preference applies only inside a 10% liquidity tie band; it never beats a real liquidity gap such as 10x.

**Q12. False** — An empty list raises `ValueError`; 0.0 is the value for a *single* pair.

**Q13.** Payload order is the API's listing order, not a quality ranking: the first entry can be a dust pool nobody trades, quoting a fantasy price ($9.99 on $40 liquidity in the fixture). Trusting position instead of liquidity, freshness, and cross-pair agreement prices the token from a market that does not exist.

**Q14.** When two liquid pairs tell different price stories, the price is unverifiable — at least one feed is wrong and there is no honest way to know which. Picking the more liquid side would be a coin flip dressed as data; fail-closed `None` ("not priced") is the only defensible answer.

**Q15.** Selected: the GoodPair at **$130,000** liquidity (richest duplicate copy). Counts: **malformed skipped 2, duplicates dropped 2, stale rejected 1**. The other mint's pair is ignored (not counted in those three), and the stale $400k pair never wins despite being richest overall.

**Q16.** The timestamp falls back to `pairCreatedAt`. If neither exists, the pair has no timestamp — and missing time is stale, so it is rejected as stale rather than assumed fresh.

**Q17.** Wrong-mint pairs are simply ignored (excluded from candidates and from verification). An empty mint string from the caller raises `ValueError`.

**Q18.** Median of {0.50, 0.90} = 0.70. Disagreement = (0.90 − 0.50) / 0.70 × 100 ≈ **57.1%**. 57.1% > 15% → the price is unverifiable → **`select_pair` returns `None`**.

**Q19.** Median of {0.50, 0.51} = 0.505. Disagreement = (0.51 − 0.50) / 0.505 × 100 ≈ **2.0%**. 2.0% ≤ 15% → the pairs verify each other, and selection proceeds to the most liquid pair (the $180,000 pair at $0.50).

**Q20.** Age = (now − ts)/1000. (a) (1,900,000 − 1,000,000)/1000 = **900s exactly → NOT stale** (stale requires age > 900). (b) 901s → **stale**. (c) Timestamp is 50s in the future → negative age → **stale**.

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism/counts named in the key (wording may differ). Vague answers ("it's unreliable") without the mechanism score 0.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown using the module's formulas. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, re-derive the three fixture outcomes by hand, and retake the full exam (not just the missed questions) no sooner than the next study session.
