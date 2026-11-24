# Exam — Live Risk Controls

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `10-live-risk-controls-meme` README and `risk.py` alone.

---

## QUESTIONS

**Q1 (MCQ).** The three controls in this lab are:
A. A rolling-expectancy kill switch, a pre-registered per-chain cost gate, and a fixed $100 bankroll with $1 shots and no refill
B. A stop-loss, a take-profit, and leverage limits
C. Two-factor auth, cold storage, and an allowlist
D. Rate limits, circuit breakers on the exchange, and insurance

**Q2 (MCQ).** `KillSwitch` defaults are:
A. window=15, min_trades=30
B. window=30, min_trades=15
C. window=100, min_trades=1
D. window=30, min_trades=30

**Q3 (MCQ).** The kill switch halts when:
A. Any single close is below 1.0x
B. At least `min_trades` closes sit in the window AND `mean(realized_mult) − 1 < 0` over that window
C. The loss streak reaches 3
D. Expectancy is exactly 0

**Q4 (Compute).** Fifteen closes all at 0.5x: (a) `expectancy()` after the 15th close; (b) halted after 14 such closes — yes or no; (c) halted after the 15th — yes or no.

**Q5 (Short answer).** Why does the switch refuse to halt on 14 losing closes — what failure mode is the `min_trades` minimum protecting against?

**Q6 (True/False + justification).** "After a halt, one later 50x winner clears the halt automatically." True or false? Justify — and name the ONLY thing that clears a halt.

**Q7 (MCQ).** `rearm(operator, reason)`:
A. Accepts empty strings — any re-arm is fine
B. Raises `ValueError` on an empty operator or reason, and journals every attempt (including refused ones)
C. Clears the recorded closes so the lane starts fresh
D. Is automatic after 24 hours

**Q8 (Short answer).** A re-armed switch halts again after ONE more losing close. What design choice in `rearm` causes that, and why is it the safer choice?

**Q9 (MCQ).** A missing OR corrupt JSON state file on load leaves the switch:
A. Fresh and unhalted
B. `halted=True` with a `load_failed` event journaled (fail-closed)
C. It raises and crashes the program
D. Halted only if corrupt, fresh if missing

**Q10 (Short answer).** Why is a missing state file treated the same as a corrupt one, rather than as a fresh start?

**Q11 (Compute).** Cost gate: worst case = 4 transactions; blocked iff cost > 25% of notional. `CHAIN_COSTS` per tx: solana $0.001, base $0.03, ethereum $1.00; Hyperliquid 0.035% of notional per tx. At $1 notional compute (cost, % of $1, allowed?) for all four chains.

**Q12 (Compute).** `min_notional_to_pass(chain)`: derive and state it for (a) ethereum; (b) base; (c) solana; (d) hyperliquid. Show the inequality you solved for (a).

**Q13 (True/False + justification).** "An Ethereum position at exactly $16.00 notional is blocked by the cost gate." True or false? Justify with the boundary rule. What about $15.99?

**Q14 (MCQ).** `cost_gate` on an unknown chain, or with a non-positive notional:
A. Returns allowed=True (fail-open)
B. Raises `ValueError` instead of passing (fail-closed)
C. Returns (False, 0.0, 0.0)
D. Defaults to solana's costs

**Q15 (MCQ).** Chain-name handling in `per_tx_cost` / `cost_gate`:
A. Case-sensitive, whitespace-sensitive — `" Ethereum "` fails
B. Normalised for case and whitespace
C. Only lowercase names work
D. Any string is accepted as a chain

**Q16 (Compute).** Fresh `Bankroll(start=100.00)`, $1.00 shots: (a) open one position and settle it at 3.0x — cash after settle, and state? (b) From a fresh bankroll, open 10 positions at once — cash and open count? (c) An 11th `open_position()` — what happens and why (name the constant)? (d) All 10 positions then settle at 0.0x — cash returns to what? (Work each from a fresh bankroll where stated.)

**Q17 (MCQ).** `Bankroll` deliberately has:
A. A `top_up` method requiring a reason
B. A `reset` method for the operator
C. NO `top_up` and NO `reset` attribute at all
D. Both, but hidden

**Q18 (True/False + justification).** "Once `state` flips to `bankroll_exhausted`, a later winning `settle` that credits cash flips it back to `active`." True or false? Justify — in what exact sense is the exhaustion permanent if `settle` can still credit cash?

**Q19 (MCQ).** A bankroll that STARTS below one shot (e.g. $0.50):
A. Starts `"active"` and can open fractional positions
B. Is born `"bankroll_exhausted"`
C. Raises at construction and cannot exist
D. Rounds up to $1

**Q20 (MCQ).** `loss_streak()` counts trailing closes below 1.0x. An exactly-breakeven (1.0x) close:
A. Extends the streak
B. Breaks the streak
C. Is rejected with `ValueError`
D. Halts the switch by itself

**Q21 (MCQ).** `would_halt_if(realized_mult)`:
A. Records the close and halts if appropriate
B. Is a non-mutating prediction — it changes no state; an already-halted switch stays halted (returns True)
C. Only works before any close exists
D. Returns the expectancy

**Q22 (Compute).** `chains_passing(notional)`: state the sorted list at (a) $1; (b) $16. Also: a negative realized multiple passed to `record_close` or `settle` — accepted or rejected, and with what effect on state?

---

## ANSWER KEY

- **Q1: A** — The three controls exactly as specified.
- **Q2: B** — window=30, min_trades=15.
- **Q3: B** — Both conditions: enough closes in the window AND negative rolling expectancy (mean multiple below 1.0). Zero expectancy does NOT halt (the inequality is strict).
- **Q4: (a) −0.50** (mean 0.5 − 1). **(b) No** — only 14 closes < min_trades 15. **(c) Yes** — the 15th close satisfies min_trades with negative expectancy.
- **Q5:** It protects against halting a lane on a tiny, statistically meaningless sample — a short unlucky streak at the start (small-sample noise) permanently killing a possibly-fine lane and, worse, training operators to re-arm reflexively. The minimum forces the halt to rest on at least a pre-registered body of evidence.
- **Q6: False** — A halt is sticky: later winners never clear it. ONLY a human `rearm(operator, reason)` clears it. There is deliberately no auto re-arm method.
- **Q7: B** — Both operator and reason must be non-empty; every attempt is journaled.
- **Q8:** `rearm` PRESERVES the recorded closes (it clears only the halt flag). The window still holds 15 losing closes, so the very next negative close re-evaluates to negative expectancy and re-halts. Safer because re-arming cannot launder the evidence — a human can override the halt, but cannot erase the record that caused it.
- **Q9: B** — Missing and corrupt are both fail-closed halted, with `load_failed` journaled.
- **Q10:** Because "no file" is indistinguishable from "the file was deleted / the disk failed / state was tampered with". Treating it as a fresh start would let any state loss silently resurrect a halted lane — the dangerous direction. Halting costs a human re-arm; a false fresh start can cost the whole bankroll's evidence integrity.
- **Q11:** Solana: cost 4×$0.001 = **$0.004, 0.40%, ALLOWED**. Base: 4×$0.03 = **$0.12, 12.00%, ALLOWED**. Ethereum: 4×$1.00 = **$4.00, 400.00%, BLOCKED**. Hyperliquid: 4×(0.035% × $1) = 4×$0.00035 = **$0.0014, 0.14%, ALLOWED**.
- **Q12:** Solve worst-case cost / notional ≤ 0.25. (a) Ethereum: 4.00/N ≤ 0.25 → N ≥ 4.00/0.25 = **$16.00**. (b) Base: 0.12/0.25 = **$0.48**. (c) Solana: 0.004/0.25 = **$0.016**. (d) Hyperliquid: **0.0** — its cost is a percentage of notional (0.14% at any size), so it passes at every notional and the minimum is 0.
- **Q13: False** — Blocked iff cost is STRICTLY greater than 25% of notional; at $16.00 the cost ($4.00) is EXACTLY 25%, and exactly-25% passes (allowed). At $15.99, $4.00 is > 25% of notional → blocked.
- **Q14: B** — Unknown chain / non-positive notional raises `ValueError` (fail-closed).
- **Q15: B** — Names are normalised for case and whitespace.
- **Q16: (a) Cash $102.00, state `"active"`** (100 − 1 + 3.00). **(b) Cash $90.00, open = 10** (100 − 10×1). **(c) Refused/raises** — `MAX_OPEN = 10` concurrent positions already open. **(d) Cash $90.00** — settling at 0.0x credits $0 per position; the $10 of stakes is gone.
- **Q17: C** — The absence of both attributes is asserted by tests; the account can never be refilled or reset, keeping its ledger untainted evidence.
- **Q18: False** — `state` is permanent once flipped: `settle` can still credit cash for positions that were already open (money in flight is honoured), but the exhausted state never flips back and no new positions can be opened on the strength of it. Permanence is about the STATE and the right to open, not about refusing credits.
- **Q19: B** — Born exhausted (state flips when less than one shot remains, including at birth).
- **Q20: B** — Exactly 1.0x breaks the streak (only closes strictly below 1.0 count).
- **Q21: B** — Pure prediction, no mutation; sticky True once halted.
- **Q22: (a) At $1: `['base', 'hyperliquid', 'solana']`** (ethereum blocked). **(b) At $16: all four** — `['base', 'ethereum', 'hyperliquid', 'solana']` (ethereum exactly at its boundary passes). **Negative multiples are rejected with `ValueError`** in both `record_close` and `settle`, WITHOUT recording the close / closing the position.

---

## GRADING RUBRIC

- **MCQ (Q1–Q3, Q7, Q9, Q14, Q15, Q17, Q19–Q21):** 1 point for the correct letter only.
- **True/False + justification (Q6, Q13, Q18):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q5, Q8, Q10):** 1 point if the key mechanism is stated.
- **Compute-it (Q4, Q11, Q12, Q16, Q22):** 1 point per question; ALL sub-parts numerically correct (money to 2 d.p., percentages to 2 d.p. as in the demo). Working must be shown for Q11 and Q12.
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and `risk.py`, then retake the full exam. The 25% cost gate, the 15-close minimum, and the no-top-up rule must be exact from memory before any live-mode discussion relies on them.
