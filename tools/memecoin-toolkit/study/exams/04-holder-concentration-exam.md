# Exam — Holder Concentration

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `04-holder-concentration` README and `holders.py` alone.

---

## QUESTIONS

**Q1 (MCQ).** Concentration in this lab is computed over:
A. All accounts, largest first, raw
B. User wallets only — program/pool/curve accounts are excluded from the numerator
C. Only the single largest account
D. Accounts missing from the registry, assumed to be users

**Q2 (MCQ).** `classify_holder` returns `"user"` iff the registry entry:
A. Exists at all
B. Has `on_curve=True` and is not program-owned
C. Has `on_curve=False`
D. Is a dict of any shape

**Q3 (MCQ).** An account absent from the registry, or with a malformed entry, is classified:
A. `"user"` (benefit of the doubt)
B. `"program"`
C. `"unknown"` — never `"user"`
D. It raises

**Q4 (Compute).** State `verdict_for(top1, top10)` for: (a) (50.0, 60.0); (b) (50.01, 60.0); (c) (10.0, 70.0); (d) (10.0, 70.01); (e) (25.0, 55.0); (f) (25.01, 10.0); (g) (0.0, 0.0); (h) (100.0, 100.0).

**Q5 (True/False + justification).** "Exactly top1 = 50.0% is a FAIL." True or false? Justify with the comparison operators.

**Q6 (MCQ).** A FLAG verdict's `passed` value is:
A. False — only PASS passes
B. True — FLAG counts as passed; only FAIL and unknown block
C. None
D. It depends on top-1

**Q7 (Compute).** Demo Scenario 1: naive top-1 (pool included) is 40.0%, user top-1 is 6.0%, top-10 users 12.0%. (a) Verdict and passed? (b) The pool's contribution to top-1 USER concentration, in percentage points.

**Q8 (MCQ).** Empty holders, non-positive/missing supply, or zero classifiable user wallets all produce:
A. PASS with 0.0%
B. Verdict `"unknown"`, passed=False, percentages `None` (fail-closed)
C. FAIL with 100%
D. A raised exception from `analyze`

**Q9 (Short answer).** Name two distinct input problems that produce verdict `"unknown"` with passed=False.

**Q10 (MCQ).** Amount hardening: a holder row with a negative, NaN, or infinite amount is:
A. Clamped to 0
B. Treated as a malformed row and skipped (never negative concentration)
C. Counted as a program account
D. Raises from `analyze`

**Q11 (Compute).** `herfindahl_index` on the 0–10000 scale: (a) a monopoly user (one user holds all user share considered, 100% share); (b) ten equal holders (10% each); (c) two users with shares 30% and 10% of supply (HHI over user shares as defined: sum of squared percentage shares — compute it).

**Q12 (MCQ).** `effective_holder_count` for two equal holders is:
A. 1.0
B. 2.0, whatever their combined share (shares are normalised among user holders)
C. Their combined share in %
D. `None`

**Q13 (MCQ).** `nakamoto_coefficient` with threshold 50.0%: holders (aggregated) hold 30%, then 20%, then 10% cumulatively from the top. The coefficient is:
A. 2 — cumulative reaches exactly 50.0%
B. 3 — the cumulative share must be STRICTLY greater than the threshold; exactly 50.0% is not a majority
C. 1
D. `None`

**Q14 (True/False + justification).** "If the threshold is never exceeded, `nakamoto_coefficient` returns 0." True or false? Justify.

**Q15 (Short answer).** Top-10 user share is 72% but top-1 is only 9%. What verdict results, and what distribution shape does that suggest?

**Q16 (MCQ).** `aggregate_user_balances` exists because:
A. Percentages need rounding
B. One wallet can appear in several token accounts — balances are summed per address before ranking
C. Program accounts need splitting
D. The registry requires it

**Q17 (Compute).** Supply 10,000. User wallets (after classification): W1 = 600, W2 = 300, W3 = 100, plus 7 users at 20 each; one pool account = 4,000 (program). Compute user top-1 %, top-10 %, and the verdict.

**Q18 (MCQ).** `top_n_user_pct(..., n)` raises `ValueError` unless:
A. n ≤ 10
B. n is a positive int
C. n equals 1 or 10 exactly
D. There are at least n holders

**Q19 (True/False + justification).** "With fewer than ten user wallets, top-10 is undefined and the verdict is unknown." True or false? Justify.

**Q20 (MCQ).** A non-boolean `on_curve` value, a non-dict registry entry, or a `None` registry makes the affected accounts:
A. `"user"`
B. `"program"`
C. `"unknown"`
D. Skipped silently with no counting

**Q21 (Short answer).** Why is the largest on-chain account usually NOT a whale, and what property identifies it in the registry?

**Q22 (MCQ).** In the demo's majority-whale scenario, user top-1 = 52.0%, top-10 = 57.0%. Verdict and passed?
A. FLAG, passed=True
B. FAIL, passed=False (top1 > 50 alone suffices)
C. PASS, passed=True
D. unknown, passed=False

---

## ANSWER KEY

- **Q1: B** — Only user wallets count; pools/curves/programs are excluded.
- **Q2: B** — `on_curve=True` AND not program-owned.
- **Q3: C** — Absent/malformed → `"unknown"`, never assumed a user.
- **Q4:** Rule: FAIL if top1 > 50 or top10 > 70; FLAG if top1 > 25 or top10 > 55; else PASS (all strict). (a) **FLAG** (50.0 not > 50, but 60 > 55 — also 50 > 25); (b) **FAIL** (50.01 > 50); (c) **FLAG** (70.0 not > 70, but > 55); (d) **FAIL** (70.01 > 70); (e) **PASS** (25.0 not > 25, 55.0 not > 55); (f) **FLAG** (25.01 > 25); (g) **PASS**; (h) **FAIL**.
- **Q5: False** — Comparisons are strict (`>`), so exactly 50.0 is FLAG (it exceeds the 25 FLAG line); +0.01 makes it FAIL.
- **Q6: B** — FLAG counts as passed.
- **Q7: (a) PASS, passed=True** (6.0/12.0 breach no line). **(b) 0 points** — a program/pool account contributes nothing to user concentration, however large it is.
- **Q8: B** — Fail-closed unknown.
- **Q9:** Any two of: empty holder list; missing/zero/negative/non-numeric (or NaN/infinite) total supply; holders present but zero classifiable user wallets (e.g. all program/unknown). Accept any two distinct shapes from the README.
- **Q10: B** — Malformed rows are skipped and counted separately; concentration is never negative.
- **Q11: (a) 10000** (100²). **(b) 1000** (10 × 10²). **(c) 1000** (30² + 10² = 900 + 100).
- **Q12: B** — Inverse Simpson over shares normalised among user holders; two equal holders → 2.0.
- **Q13: B** — 30 + 20 = exactly 50.0 does not exceed; adding the third (60 cumulative) first exceeds → 3.
- **Q14: False** — It returns `None` when the threshold is never exceeded.
- **Q15: FAIL** (top10 72 > 70, via the top-10 FAIL line alone). Shape: no single whale, but a small coordinated-looking group — the top ten collectively control the token; concentration risk without a single majority holder.
- **Q16: B** — Duplicate rows per address are summed before any top-n/HHI/Nakamoto computation.
- **Q17:** Top-1 = 600/10,000 = **6.0%**; top-10 = (600+300+100+7×20)/10,000 = 1,140/10,000 = **11.4%**; **verdict PASS** (pool's 4,000 excluded).
- **Q18: B** — n must be a positive int; n larger than the holder count is fine.
- **Q19: False** — Top-10 simply sums however many users exist (with a single user, top1 == top10); it is only unknown when there are NO user wallets / unusable supply.
- **Q20: C** — All such shapes classify as `"unknown"` (with a `None`/empty registry, everything is unknown).
- **Q21:** The largest account is typically a liquidity pool or bonding curve — a program-derived address that sits OFF the ed25519 curve (and/or is program-owned). It holds tokens mechanically for trading, not as an owner's position, so counting it labels nearly every liquid token whale-controlled.
- **Q22: B** — Top1 > 50 triggers FAIL on its own.

---

## GRADING RUBRIC

- **MCQ (Q1–Q3, Q6, Q8, Q10, Q12–Q14→see below, Q16, Q18, Q20, Q22):** 1 point for the correct letter only. (Q14 is True/False format despite placement — grade by the T/F rule.)
- **True/False + justification (Q5, Q14, Q19):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q9, Q15, Q21):** 1 point if the key mechanism is stated (Q9 needs two distinct shapes).
- **Compute-it (Q4, Q7, Q11, Q17):** 1 point per question; ALL sub-parts must be correct — the boundary strictness in Q4 is the point of the question, no tolerance.
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and `holders.py` (especially `verdict_for` strictness and the classification rules), then retake the full exam.
