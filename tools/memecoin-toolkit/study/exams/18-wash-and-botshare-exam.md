# Exam — Wash & Bot-Share Detection

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR:** ≥85% — at least **17 of 20** correct
- **Conditions:** Closed-book. Synthetic data only. Answer from the toolkit's README/module alone.

---

## QUESTIONS

### Multiple choice (Q1–Q8) — choose the one correct option

**Q1.** One wallet's round-tripped (wash) volume is:
- A. Its total buy + sell volume
- B. `2 * min(buy_volume, sell_volume)` — the round-tripped part, both legs counted
- C. `min(buy_volume, sell_volume)` — one leg only
- D. `|buy_volume − sell_volume|`

**Q2.** `is_tiny(amount_sol)` is True exactly when:
- A. Amount ≤ 0.10 SOL
- B. Amount < 0.10 SOL (strictly; exactly 0.10 is not tiny)
- C. Amount < 0.01 SOL
- D. Amount < 1.0 SOL

**Q3.** A trade is bot-like when **any** of these holds:
- A. Tiny size (< 0.10 SOL), a high-frequency wallet (≥ 8 trades in the stream), or a crowded slot (≥ 3 trades sharing one slot)
- B. All three hold simultaneously
- C. The trade is a sell from a new wallet
- D. The trade is larger than 1 SOL

**Q4.** A bump trade is:
- A. Any small trade, buy or sell
- B. A small *sell* repeated by one wallet
- C. A small buy (< 0.10 SOL) from a wallet with ≥ 3 such small buys; small sells never count
- D. Any buy in a crowded slot

**Q5.** `toxicity_score()` combines the three shares as:
- A. `wash + bot + bump`, uncapped
- B. `50·wash + 30·bot + 20·bump`, capped at 100, in [0, 100]
- C. The maximum of the three shares × 100
- D. `30·wash + 50·bot + 20·bump`, capped at 100

**Q6.** The flag thresholds in `flags_for()` are:
- A. `WASH_SKEW` ≥ 0.50, `BOT_SWARM` ≥ 0.50, `BUMP_FARM` ≥ 0.40
- B. `WASH_SKEW` ≥ 0.40, `BOT_SWARM` ≥ 0.50, `BUMP_FARM` ≥ 0.50 — a share exactly at the threshold flags
- C. All three at > 0.50, strictly
- D. All three at ≥ 0.25

**Q7.** `assess(trades)` with fewer than the default minimum trades returns:
- A. `"clean"` — thin data cannot be condemned
- B. `"flagged"` — thin data is suspicious
- C. `{"status": "unknown", "flags": [], ...}` with all shares `None` (default `min_trades = 10`)
- D. It raises `ValueError`

**Q8.** `negative_signal_score()` maps bot share to:
- A. A bonus in [0, +100]
- B. A penalty in [−100, 0] — bot share can never score positively
- C. A score in [−50, +50]
- D. The toxicity score

### True / False with justification (Q9–Q12) — state True or False AND justify in one sentence

**Q9.** An `assess` result of `"unknown"` counts as a pass for the token.

**Q10.** The stream functions (e.g. `wash_share`) return 0.0 on an empty stream, for convenience.

**Q11.** Trades with `slot = None` can together form a crowded-slot cluster.

**Q12.** `bot_volume_share` always equals `bot_share` on the same stream.

### Short answer (Q13–Q17)

**Q13.** A wallet only buys and never sells. What does it contribute to wash volume, and why — in terms of the formula?

**Q14.** The high-frequency pattern flags *every* trade from a wallet with ≥ 8 trades, including its large, ordinary-looking ones. What false positive does that create, and why is the pattern still useful?

**Q15.** Why must bot share be a negative-only signal — what would go wrong if a scoring system ever *rewarded* a stream for bot-like activity?

**Q16.** A bump farmer splits dust buys across 20 wallets, 2 buys each. Which detector here goes blind, and what stream feature would you add to catch it?

**Q17.** A stream has 9 trades, all obvious wash round-trips. Why does `assess` still return `"unknown"`, and why is that the safe answer in *both* directions?

### Compute it (Q18–Q20) — show your working

**Q18.** Stream: wallet W1 buys 12 SOL and sells 4 SOL; wallet W2 buys 8 SOL and never sells. Compute total volume, round-trip volume, and `wash_share`.

**Q19.** A stream has wash = 0.40, bot = 0.50, bump = 0.50. Compute `toxicity_score` and list the flags from `flags_for`.

**Q20.** Stream: one whale buy of 10 SOL (not tiny, first trade from its wallet, unique slot) plus nine dust buys of 0.01 SOL each (each from a different wallet, unique slots). Compute `bot_share` (by count), `bot_volume_share`, and `negative_signal_score`.

---

## ANSWER KEY

**Q1. B** — Only the matched part of a wallet's flow can have round-tripped; `2 * min(buy, sell)` counts both legs of that part.

**Q2. B** — The tiny boundary is strict: exactly 0.10 SOL is not tiny (and non-positive input raises `ValueError`).

**Q3. A** — Any one pattern suffices (tiny OR high-frequency ≥ 8 OR crowded slot ≥ 3).

**Q4. C** — Bump farming is repeated small *buys* (≥ 3 from one wallet) meant to push a trending list; sells never count.

**Q5. B** — Wash weighs most (50), then bot share (30), then bump (20); the sum is capped at 100.

**Q6. B** — Exactly-at-the-threshold flags; one epsilon below flags nothing. Out-of-range shares raise `ValueError`.

**Q7. C** — Fail-open fingerprint semantics: `None` input or fewer than `min_trades` (default 10) trades is `"unknown"` with `None` shares — no score is reported.

**Q8. B** — `negative_signal_score = −100 × bot_share ∈ [−100, 0]`; it can never go positive by construction.

**Q9. False** — `"unknown"` is neither a pass nor a veto: missing/thin evidence neither clears nor condemns the token.

**Q10. False** — Stream functions raise `ValueError` on `None` or empty input rather than returning a plausible-looking zero.

**Q11. False** — `None` slots never cluster; the crowded-slot pattern only counts trades carrying a real, shared slot.

**Q12. False** — Count share and volume share can diverge sharply (see Q20: 90% by count, <1% by volume); the toxicity score uses the count-based `bot_share`.

**Q13.** Zero. With sell volume 0, `2 * min(buy, 0) = 0`: wash is *round-tripping*, and a one-sided buyer has no matched sell leg to round-trip with. Pure one-sided buying scores a wash share of 0.

**Q14.** A genuinely active legitimate trader (or market maker) crossing 8 trades gets all of their trades — including large, normal ones — marked bot-like, inflating bot share for an honest stream. The pattern is still useful because real bot swarms are high-frequency by necessity, and the signal is used as a fingerprint/penalty to weigh, not as standalone proof.

**Q15.** Bot-like activity is manufactured activity: rewarding it would pay tokens for wash volume, dust swarms, and bump farming — the exact behaviours the detector exists to punish — and would teach launchers that faking demand raises their score. A manipulation indicator can only ever count against a token.

**Q16.** `bump_mask`/`bump_share` goes blind: it requires ≥ 3 small buys *from one wallet*, and 2-per-wallet stays under the bar. A catching feature would aggregate across wallets — e.g. the stream-wide share of tiny buys, or synchronised dust-buy timing/amount similarity across many wallets — rather than per-wallet counts alone.

**Q17.** The fingerprint contract requires at least `min_trades` (default 10) trades before any verdict; 9 trades — however ugly — is below the evidence floor, so all shares are reported as `None` and the status is `"unknown"`. That is safe both ways: the token is not *cleared* on thin data (no free pass), and it is not *vetoed* on thin data either (no conviction without evidence).

**Q18.** Volumes: W1 total 16, W2 total 8 → **total volume 24 SOL**. Round-trip: W1 `2 * min(12, 4) = 8`; W2 `2 * min(8, 0) = 0` → **round-trip volume 8 SOL**. Wash share = 8 / 24 = **1/3 ≈ 0.333**.

**Q19.** Toxicity = 50(0.40) + 30(0.50) + 20(0.50) = 20 + 15 + 10 = **45.0**. Flags: all three shares are exactly at their thresholds, and at-threshold flags → **`[WASH_SKEW, BOT_SWARM, BUMP_FARM]`**.

**Q20.** Bot-like trades: the nine dust buys are tiny → bot-like; the whale buy matches no pattern. Count share = 9 / 10 = **0.90**. Volume share = (9 × 0.01) / (10 + 0.09) = 0.09 / 10.09 ≈ **0.0089 (≈0.89%, under 1%)**. Negative signal score = −100 × 0.90 = **−90.0**. The two shares tell opposite stories — the reason both are reported.

---

## GRADING RUBRIC

- **Multiple choice (Q1–Q8):** 1 point for the correct letter only; no partial credit.
- **True/False with justification (Q9–Q12):** 1 point only if BOTH the verdict and the justification are correct. A correct verdict with a wrong or missing reason scores 0.
- **Short answer (Q13–Q17):** 1 point if the answer states the mechanism named in the key (wording may differ). Answers that describe a detector without explaining the deciding condition score 0.
- **Compute it (Q18–Q20):** 1 point for the correct final value(s) with working shown using the module's formulas. Multi-part questions require every part correct.
- **Pass rule:** ≥85% = **17 or more of 20**. 16 or fewer is a fail.
- **On a fail:** re-study the toolkit note/README, recompute the demo's clean/wash/bot streams by hand, and retake the full exam (not just the missed questions) no sooner than the next study session.
