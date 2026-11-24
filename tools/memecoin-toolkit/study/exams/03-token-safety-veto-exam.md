# Exam — Token Safety Veto

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `03-token-safety-veto` README and `veto.py` alone.

---

## QUESTIONS

**Q1 (MCQ).** The standing design rule of this toolkit is:
A. Highest score wins — always trade the best-scoring token
B. The score is a floor, never a ranker — a token clears a minimum bar or it does not pass, and no score outranks a hard FAIL
C. Score ≥ 50 passes regardless of fails
D. Flags and fails are interchangeable

**Q2 (MCQ).** Which of these is a hard FAIL (not a flag)?
A. `mintCloseAuthority`
B. `confidentialTransferMint`
C. Transfer fee of 500 bps
D. `permanentDelegate` extension

**Q3 (MCQ).** Which set contains ONLY flag-only findings?
A. `transferHook`, `nonTransferable`
B. `mintCloseAuthority`, `confidentialTransferMint`, transfer fee with 0 < bps ≤ 1000
C. Mint authority held, freeze authority held
D. Default account state frozen, transfer fee 1001 bps

**Q4 (Compute).** Score convention is `100 − 25 × fails − 5 × flags`, floored at 0. Compute the score for: (a) clean report; (b) 1 flag only; (c) 1 fail only; (d) 2 fails + 1 flag; (e) 7 fails + 2 flags.

**Q5 (Compute).** Transfer-fee boundary: state status/passed/score for a report whose only finding is a transfer fee of (a) 0 bps; (b) 1 bps; (c) exactly 1000 bps; (d) 1001 bps.

**Q6 (True/False + justification).** "A verdict with score 75 and one fail can still pass if `min_score` is set to 0." True or false? Justify via `meets_score_floor`.

**Q7 (MCQ).** `evaluate(None)` returns:
A. PASS, score 100
B. FAIL, score 75
C. Status `"UNKNOWN"`, passed=False, score=0, with the missing pieces named in `fails`
D. It raises `ValueError`

**Q8 (MCQ).** A malformed transfer fee (negative, fractional, a string, or the bool `True`) produces:
A. A hard FAIL `transfer_fee_excessive`
B. Status `"UNKNOWN"` with fail `transfer_fee_invalid`
C. It is treated as 0 bps
D. It raises and crashes `evaluate`

**Q9 (Short answer).** Why must "safety data was never fetched" produce a verdict distinct from both "clean" and "dirty", yet grouped with the failures for pass/fail purposes?

**Q10 (MCQ).** Authority spellings that count as CLEAN (authority gone) include:
A. Only the exact string `"revoked"`
B. `None`, `""`, `"none"`, `"burned"`, `"revoked"` — including case/whitespace variants
C. `"unknown"`
D. Any wallet address

**Q11 (MCQ).** The authority value `"unknown"` (either authority) produces:
A. PASS — unknown means probably fine
B. Fail-closed UNKNOWN with the appropriately named fail
C. A flag only
D. Score 100

**Q12 (True/False + justification).** "The `creator` field can gate a token — a blacklisted creator fails it in this module." True or false? Justify.

**Q13 (Compute).** A report has both Token-2022 trap extensions `transferHook` and `permanentDelegate` and nothing else wrong. State fails list, status, passed, score.

**Q14 (MCQ).** Duplicate extension entries in mixed shapes (string + dict forms of the same extension) are:
A. Counted once per occurrence, stacking fails
B. Counted once — deduplicated (score 75 for the single fail)
C. Ignored entirely
D. Treated as UNKNOWN

**Q15 (Short answer).** Why are extension entries accepted in three shapes (plain name, `"name:value"`, dict) instead of one strict format?

**Q16 (MCQ).** `default_account_state` precedence: when the report's own field and a dict-form extension entry disagree —
A. The extension entry always wins
B. The report's own `default_account_state` takes precedence in both directions
C. The result is UNKNOWN
D. Both are counted as separate fails

**Q17 (Compute).** A token scores 95 (one flag) and another scores 75 (one fail). (a) Which passes? (b) `meets_score_floor` for the 95-token at min_score 95? At 96? (c) `meets_score_floor` for the 75-token at min_score 0?

**Q18 (True/False + justification).** "`evaluate` never raises on missing safety data." True or false? Justify.

**Q19 (MCQ).** `meets_score_floor(verdict, min_score)` raises `ValueError` when `min_score` is:
A. 0
B. 100
C. Not an int in `[0, 100]` (e.g. −1, 101, or a non-int)
D. Equal to the verdict's score

**Q20 (Short answer).** Held mint authority vs held freeze authority: what different attack does each enable? (Answer from the module's semantics: mint = new supply can be created; freeze = existing holders' accounts can be frozen.)

**Q21 (MCQ).** `from_dict({})` (empty dict) yields a report whose evaluation names how many never-fetched pieces?
A. 0 — empty means clean
B. 1
C. All four never-fetched pieces, with the default source
D. It raises

**Q22 (MCQ).** `summarize(verdicts)` returns counts keyed:
A. `{"pass", "fail"}` only
B. `{"total", "passed", "pass", "fail", "unknown"}`
C. A single pass rate
D. A list of scores

---

## ANSWER KEY

- **Q1: B** — Score is a floor, never a ranker; `passed` is decided by absence of fails, never by score.
- **Q2: D** — `permanentDelegate` is a Token-2022 hard fail; the others are flags.
- **Q3: B** — Those three are the complete flag-only set.
- **Q4: (a) 100; (b) 95; (c) 75; (d) 45** (100 − 50 − 5); **(e) 0** (100 − 175 − 10 = −85, floored at 0).
- **Q5: (a) PASS, passed, 100** (no fee, no finding); **(b) PASS, passed, 95** (flag `transfer_fee_present`); **(c) PASS, passed, 95** (1000 bps = 10% is still only a flag — rule is `0 < bps ≤ 1000`); **(d) FAIL, not passed, 75** (fail `transfer_fee_excessive`, rule is `> 1000`).
- **Q6: False** — `meets_score_floor` is True iff the verdict PASSED and score ≥ min_score; a failed/unknown verdict meets no floor, not even 0. The floor can only tighten a PASS, never loosen a FAIL.
- **Q7: C** — Fail-closed UNKNOWN, passed=False, score=0.
- **Q8: B** — Malformed fee → UNKNOWN with `transfer_fee_invalid` (note: `True` is an `int` subclass but is not a fee).
- **Q9:** "Never fetched" is absence of evidence, not evidence of safety or of danger: it needs its own status so callers can distinguish it, but for the pass/fail decision it must block (passed=False) — treating unknown as clean would let unfetched tokens through the single choke point, which is exactly the failure the gate exists to prevent.
- **Q10: B** — All revoked synonyms, case/whitespace-insensitive, count as gone.
- **Q11: B** — `"unknown"` authority fails closed as UNKNOWN with the named fail.
- **Q12: False** — The `creator` field never gates in this module (pinned by tests).
- **Q13:** Fails = `['permanent_delegate', 'transfer_hook']` (order per module), status FAIL, passed=False, **score 50** (100 − 2×25).
- **Q14: B** — Duplicates are deduplicated; one fail → score 75.
- **Q15:** Because upstream sources report extensions in inconsistent shapes; accepting all three (normalised) prevents a dangerous extension from slipping past the gate merely because it arrived in a different encoding. Strictness about format would create false negatives on the safety-critical path.
- **Q16: B** — The report's explicit field wins over the extension-embedded copy in both directions.
- **Q17: (a) Only the 95-token passes.** **(b) True at 95** (meets its exact score), **False at 96**. **(c) False** — a FAIL meets no floor, even 0.
- **Q18: True** — Missing data IS the verdict (UNKNOWN); `evaluate` returns it rather than raising. (It still raises nowhere on missing safety data; by contrast `meets_score_floor`/`from_dict` validate their own arguments.)
- **Q19: C** — `min_score` must be an int in `[0, 100]`.
- **Q20:** Held mint authority lets the holder inflate supply at will (mint-and-dump / dilution). Held freeze authority lets the holder freeze holders' token accounts, trapping them — they can be prevented from selling/transferring even though supply is unchanged.
- **Q21: C** — All four never-fetched pieces are named as fails on the UNKNOWN verdict.
- **Q22: B** — Exactly that dict shape.

---

## GRADING RUBRIC

- **MCQ (Q1–Q3, Q7, Q8, Q10, Q11, Q14, Q16, Q19, Q21, Q22):** 1 point for the correct letter only.
- **True/False + justification (Q6, Q12, Q18):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q9, Q15, Q20):** 1 point if the mechanism in the key is stated.
- **Compute-it (Q4, Q5, Q13, Q17):** 1 point per question; all sub-parts must match the key exactly (scores and statuses are exact integers/strings, no tolerance).
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and `veto.py`, then retake the full exam. The fail-closed UNKNOWN rule and the fee boundary must be exact from memory before this gate's logic is relied on.
