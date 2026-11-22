# Exam — Signal Refinery

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only. All answers are derivable from the toolkit's README / `refinery.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

---

## QUESTIONS

**Q1 (MCQ).** The refinery pattern is:
A. Four destinations, one per chain
B. Four chains (Solana, Base, Ethereum, Hyperliquid) feeding *one* destination — a single signal lane — through a validated bus
C. One chain feeding four lanes
D. A bus that accepts any chain added at runtime

**Q2 (Short answer).** Give the full source-code map: each of `df, sw, br, er, hl, ll, hb` to its feed name.

**Q3 (MCQ).** A valid schema-v1 record must contain exactly which required fields?
A. `chain, mint, score`
B. `schema, chain, source, mint, ts, score`
C. `schema, chain, source, mint, ts, score, price`
D. Any six fields

**Q4 (True/False + justification).** True or false: a record with score 0 and a record with score 100 are both in range, but score 100.1 and a boolean score are rejected. Justify the boolean case.

**Q5 (Short answer).** A record arrives: `{"schema": 1, "chain": "dogechain", "source": "zz", "mint": "ab", "ts": -1, "score": 150}` (all fields present). List every rejection reason `validate_record` returns for it, in the demo's sense.

**Q6 (MCQ).** A record with source `br` on chain `solana` (both individually valid) is:
A. Accepted — both values are known
B. Rejected with a source/chain mismatch reason
C. Rewritten to chain `base`
D. Accepted with a warning

**Q7 (Short answer).** Which three source codes have a fixed required chain, and what chain does each require? Which sources have no such constraint named in the module?

**Q8 (Compute).** A `TapOutcome` is closed with `peak_mult = 2.0`; another closed at 1.99; a third is open with `peak_mult = 5.0`. Classify each as win / loss / neither, with the rule.

**Q9 (True/False + justification).** True or false: a closed outcome without a `peak_mult`, and a negative `peak_mult`, are both accepted as losses. Justify what `TapOutcome` actually does.

**Q10 (MCQ).** `TapJournal.win_rate(chain)` for a chain with no closes returns:
A. 0.0
B. 1.0
C. `None` — never a fake 0.0
D. It raises

**Q11 (Compute).** A Base journal has 11 closed taps, exactly 1 of them a win. Compute the win rate (to 3 decimal places / as the demo's rounded figure) and state whether `evaluate_auto_pause` pauses Base. Show both auto-pause conditions checked.

**Q12 (Compute).** Three more chains: (a) 9 closed taps, 0 wins; (b) 10 closed taps, 3 wins; (c) 10 closed taps, exactly 2 wins. For each, does auto-pause fire? Name the condition that decides each case.

**Q13 (True/False + justification).** True or false: `evaluate_auto_pause` is idempotent, and running it twice can pause additional chains the second time on the same journal. Justify the idempotency claim precisely (accept the key's wording).

**Q14 (MCQ).** An attempt to pause Solana — automatically or by a human — results in:
A. Solana pauses like any chain
B. Refusal: returns `False`, Solana stays unpaused, and the attempt is recorded in `flags`
C. A raised exception with no record
D. Solana pauses but publishes anyway

**Q15 (Short answer).** How does a paused chain get unpaused? State who can resume, who cannot, and what happens (including flags) on a forbidden resume attempt.

**Q16 (MCQ).** `publish_allowed(rec)` for a valid record on a paused chain returns:
A. `(True, [])`
B. `(False, ["chain_paused"])`
C. `(True, ["chain_paused"])`
D. It raises

**Q17 (True/False + justification).** True or false: if Base is paused, Solana records are also refused by `publish_allowed` until Base resumes. Justify.

**Q18 (Short answer).** The validator rejects an unknown chain instead of passing the record through with a warning. What is the cost asymmetry at the destination that justifies fail-closed here?

**Q19 (MCQ).** Which record defects are fail-closed rejections? (Pick the complete list.)
A. Non-dict record; any missing required field; wrong schema version; unknown chain; unknown source; short/non-string mint; non-positive or boolean `ts`
B. Only an unknown chain
C. Only a missing field
D. Only a wrong schema version

**Q20 (Short answer).** An outcome that is still open counts as neither win nor loss. What verdict distortion would counting open positions as losses introduce, particularly for a young chain?

---

## ANSWER KEY

**Q1 — B.** Four chains, one destination, validated bus, per-chain switches driven by the tap journal.
**Q2 —** `df` = degen-feed; `sw` = smart-watcher; `br` = base-runner-feed; `er` = eth-runner-feed; `hl` = hl-listing-watch; `ll` = locked-liq-scan; `hb` = heartbeat. The map round-trips both ways (`source_name` / `code_for_source`); unknown codes/names raise `ValueError`.
**Q3 — B.** `schema, chain, source, mint, ts, score` (schema version = 1). Missing fields reject as `missing:<field>`.
**Q4 — True.** Score must be in [0, 100] inclusive — 0 and 100 are the test-pinned boundaries. 100.1 is out of range. A boolean is rejected even though `True == 1`: booleans are explicitly excluded from numeric fields (the boolean trap), here and for `ts`.
**Q5 — `['unknown_chain', 'unknown_source', 'bad_mint', 'bad_ts', 'bad_score']`** — exactly the demo's invalid-record list. (Mint "ab" is short/non-qualifying, ts −1 is non-positive, score 150 is outside [0,100]. Schema 1 is correct, so no schema reason, and no mismatch reason because the source is unknown.)
**Q6 — B.** Rejected as a source/chain mismatch — the check hunts wiring bugs where a feed's records are tagged with the wrong chain, which would corrupt per-chain journals/switches.
**Q7 —** `br` → base; `er` → ethereum; `hl` → hyperliquid. The other sources (`df`, `sw`, `ll`, `hb`) have no fixed-chain constraint named in the module.
**Q8 —** Closed at 2.0: **win** (win iff closed and peak_mult ≥ 2.0 — the boundary is inclusive). Closed at 1.99: **loss** (closed and < 2.0). Open at 5.0: **neither** — an open outcome is neither win nor loss regardless of its peak so far.
**Q9 — False.** Both raise `ValueError` at construction (fail-closed): a closed outcome without a `peak_mult`, and a negative multiple, are invalid `TapOutcome`s — as are an unknown chain and an empty mint.
**Q10 — C.** `None` with no closes — never a fake 0.0 that would look like a measured 0% win rate.
**Q11 — Win rate = 1/11 ≈ 0.091** (demo: 0.091). Conditions: closed taps 11 ≥ `AUTO_PAUSE_MIN_TAPS = 10` ✓; win rate 0.091 < `AUTO_PAUSE_MAX_WIN_RATE = 0.20` ✓. **Auto-pause fires** — Base is paused (`['base']` in the demo).
**Q12 — (a) No pause** — 9 taps < 10 minimum (the sample-size condition blocks it despite 0% WR). **(b) No pause** — 3/10 = 30% is not < 20% (the win-rate condition blocks it). **(c) No pause** — 2/10 = exactly 20%, and the rule is *strictly* < 20%; exactly 20% does not pause.
**Q13 — False (as a combined statement).** `evaluate_auto_pause` *is* idempotent: a second evaluation on the same, unchanged journal changes nothing — already-paused chains stay paused and no additional chains are newly paused. A claim that a rerun pauses more chains on the same journal scores 0.
**Q14 — B.** Solana can never be paused; the attempt is refused, returns `False`, and is recorded in `flags` (demo: `SOLANA_PAUSE_REFUSED`). Flagging, rather than silently ignoring, leaves an audit trail of the attempt.
**Q15 —** Only a **human** resume: `resume(chain, by="human")` succeeds. `resume(by="auto")` is **refused** (returns False, chain stays paused) and **flagged**. There is deliberately no auto-resume.
**Q16 — B.** Validation passes, then the paused chain is refused as `(False, ["chain_paused"])`.
**Q17 — False.** Switches are per-chain: Solana publishing is unaffected by other chains' pauses (test-pinned).
**Q18 —** A bus record lands on the one lane the user actually watches and gambles on: letting a malformed/unknown-chain record through pollutes that destination and its per-chain accounting, while rejecting a record costs only that one signal. Dropping must happen at the bus, not on the user's phone — so doubt resolves to rejection.
**Q19 — A.** All listed defects reject with named reasons (`missing:<field>`, `unknown_chain`, `unknown_source`, `bad_mint`, `bad_ts`, `bad_score`, `bad_schema_version`, `record_not_a_dict`, mismatch); nothing is silently coerced.
**Q20 —** It would systematically depress a chain's measured win rate with positions whose outcomes are not yet known — young chains, whose positions are disproportionately still open, would look like losers and could be auto-paused on unfinished evidence. Open = neither keeps the win rate a statement about *closed* outcomes only.

---

## GRADING RUBRIC

- **MCQ (Q1, Q10, Q14, Q19):** 1 point for the correct letter only. (Q3, Q6, Q16 are also MCQ in form — same rule.)
- **True/False + justification (Q4, Q9, Q13, Q17):** 1 point only if verdict and justification both match the key.
- **Short-answer (Q2, Q5, Q7, Q15, Q18, Q20):** 1 point if every element in the key is present and correct; Q2 and Q5 require complete lists.
- **Compute-it (Q8, Q11, Q12):** 1 point for the exact classifications/win rate and the deciding condition in each case.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `refinery.py` if needed), then retake the full exam.
