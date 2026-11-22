# Exam — Signal-Card Design

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only. All answers are derivable from the toolkit's README / `cardcheck.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

---

## QUESTIONS

**Q1 (MCQ).** What does `validate(card)` return for a fully valid card?
A. `True`
B. An empty list `[]`
C. `None`
D. The string `"valid"`

**Q2 (MCQ).** Which valuation values satisfy the mcap/FDV element?
A. Both `mcap_usd` and `fdv_usd` must be present and positive
B. Either `mcap_usd` or `fdv_usd` alone, if a positive number, satisfies it
C. Zero counts, because the field is present
D. The boolean `True` counts as the number 1

**Q3 (True/False + justification).** True or false: a non-pick card (`is_pick=False`) fails validation if it has no `📍 I bought this` button. Justify in one sentence.

**Q4 (MCQ).** A card's `chain` is `"dogechain"` but every other element is present and correct. What problem is reported for the chain?
A. `"unknown chain"`
B. `"invalid CA"`
C. `"missing chain provenance"`
D. No problem — unknown chains are passed through with a warning

**Q5 (Short answer).** Name the two distinct problem strings a CA can produce, and state the condition for each.

**Q6 (Compute).** A bare-CA card contains only a valid CA and nothing else (not a pick). Using the demo/README result, how many problems does `validate` report, and which required element is *not* in that list because it is satisfied?

**Q7 (Compute).** How many elements are in `required_fields()`? List them in order.

**Q8 (MCQ).** Which characters are *excluded* from the base58 alphabet used by `is_valid_ca`?
A. `0`, `O`, `I`, `l`
B. `1`, `a`, `Z`, `9`
C. `+`, `/`, `=`, `-`
D. All uppercase letters

**Q9 (Compute).** State whether `is_valid_ca` accepts each of these lengths for an otherwise pure-base58 string: 31, 32, 44, 45 characters.

**Q10 (True/False + justification).** True or false: `mcap_usd = 0` satisfies the mcap/FDV element because the field is present. Justify.

**Q11 (MCQ).** A Solana card's `exit_hint` is non-empty but reads "Exit on Aerodrome, watch Base gas." What is reported?
A. `"missing per-chain exit hint"`
B. `"exit hint does not match chain"`
C. No problem — any non-empty hint passes
D. `"missing chain provenance"`

**Q12 (Short answer).** Give one chain-specific word that makes an exit hint match each chain: solana, base, ethereum, hyperliquid.

**Q13 (MCQ).** What does `validate(None)` do?
A. Returns `["missing CA"]`
B. Returns an empty list
C. Raises `ValueError`
D. Returns `None`

**Q14 (Compute).** An empty dict card `{}` is validated. How many problems are reported? (Hint: count the demo's bare-CA list and add the CA problem the bare-CA card did not have.)

**Q15 (Short answer).** Why is a pick card missing its 📍 track button a *measurement* failure, not merely a UX gap? Name the downstream decision that loses its data.

**Q16 (MCQ).** How does the validator detect the copy-CA button?
A. A field named `copy_ca` is `True`
B. A button containing the marker `📋 Copy CA`
C. The mint appears twice on the card
D. `buttons` has length ≥ 2

**Q17 (True/False + justification).** True or false: on a dict card, a `ca` key is accepted in place of `mint`. Justify from the module's documented behaviour.

**Q18 (Short answer).** The CA check is purely syntactic. Name two things it cannot tell you about a mint, and name the other card element that is supposed to carry safety information instead.

**Q19 (MCQ).** What does `summarize(card)` return?
A. A rendered card string
B. A dict with keys `valid`, `problems`, `chain`, `is_pick`
C. Only the list of problems
D. The nine required field names

**Q20 (Compute / short answer).** A card is otherwise valid, is a pick, and is missing exactly three things: its safety line, its copy-CA button, and its supply line. The track button is present. How many problems are reported, and what are they exactly?

---

## ANSWER KEY

**Q1 — B.** `validate` returns a list of problems; an empty list means valid.
**Q2 — B.** Either one alone satisfies it; zero/negative and booleans do not count as positive numbers.
**Q3 — False.** The track button is required only when `is_pick=True`; non-pick cards need no track button.
**Q4 — C.** An unknown chain is a provenance failure and reports `"missing chain provenance"` — there is no separate unknown-chain string.
**Q5 —** `"missing CA"` when the mint/`ca` is absent or blank; `"invalid CA"` when present but failing the synthetic check (not a 32–44-char base58 string).
**Q6 — 8 problems.** They are: missing chain provenance, missing mcap/FDV, missing liquidity, missing context line, missing safety status line, missing copy-CA button, missing total supply line, missing per-chain exit hint. The satisfied / not-listed element is the CA itself (present and valid).
**Q7 — 9.** In order: mcap/FDV, liquidity, context line, safety status line, CA, copy-CA button, total supply line, chain provenance, per-chain exit hint.
**Q8 — A.** Base58 excludes `0`, `O`, `I`, and `l` (to avoid visual ambiguity).
**Q9 —** 31: reject; 32: accept; 44: accept; 45: reject. The valid length range is 32–44 inclusive.
**Q10 — False.** Amounts must be *positive*; zero (and negative, and boolean) amounts are rejected as missing.
**Q11 — B.** Aerodrome/Base is another chain's venue language, so a present-but-wrong-chain hint reports `"exit hint does not match chain"` — distinct from a missing hint.
**Q12 —** Any one per chain from the module: solana — Raydium / SOL / Solana / PumpSwap / pump.fun; base — Aerodrome / Base / Coinbase; ethereum — gas / Uniswap / ETH / Ethereum; hyperliquid — mark price / HL / Hyperliquid / perp.
**Q13 — C.** `validate(None)` raises `ValueError` (fail-closed).
**Q14 — 9 problems.** The bare-CA card had 8; an empty card adds `"missing CA"` (its CA is absent rather than valid), for 9 total — every always-required element.
**Q15 —** The 📍 tap journals the user's per-chain outcomes, and that journal drives the per-chain feed kill switches / auto-pause decisions. Without the button there is no tap data, so the per-chain verdict loses its evidence — lost measurement, not cosmetics.
**Q16 — B.** Marker detection: a button containing `📋 Copy CA` (a single string is treated as one button).
**Q17 — True.** Dict cards may supply the CA under a `ca` key instead of `mint`; `validate` reads both.
**Q18 —** Any two of: whether the mint is safe / not a honeypot, whether it can be sold, mint/freeze authority status, tax, real-world identity or legitimacy of the token. The **safety status line** is the element meant to carry safety information.
**Q19 — B.** `summarize` returns `{"valid", "problems", "chain", "is_pick"}` for logging.
**Q20 — 3 problems, exactly:** `"missing safety status line"`, `"missing copy-CA button"`, `"missing total supply line"`. (One problem per broken element; the present track button means no pick problem is added.)

---

## GRADING RUBRIC

- **MCQ (Q1, Q2, Q4, Q8, Q11, Q13, Q16, Q19):** 1 point for the correct letter only; no partial credit.
- **True/False + justification (Q3, Q10, Q17):** 1 point only if both the verdict *and* a correct justification are given; a correct verdict with a wrong/missing reason scores 0.
- **Short-answer (Q5, Q12, Q15, Q18):** 1 point if all elements named in the key are present and correct; 0 if a required element is missing or a wrong element is substituted.
- **Compute-it (Q6, Q7, Q9, Q14, Q20):** 1 point for the exact number *and* the exact list/boundary stated in the key; a correct number with a wrong list scores 0.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `cardcheck.py` if needed), then retake the full exam. Do not proceed on a partial pass — the card contract is fail-closed, and so is this exam.
