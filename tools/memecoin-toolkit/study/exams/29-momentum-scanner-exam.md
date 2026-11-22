# Exam — Momentum Scanner

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only — pools are hand-built fixtures, safety is an injected predicate. All answers are derivable from the toolkit's README / `momentumscan.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

---

## QUESTIONS

**Q1 (Short answer).** State the pinned thresholds: `MIN_LIQUIDITY`, `MIN_VOLUME_24H`, `MIN_GAIN_24H`, `DUMP_GUARD_H1`, and `COOLDOWN_S` (in hours and seconds).

**Q2 (MCQ).** The funnel's reject reasons are:
A. `gain24_low, liq_low, vol_low, dump_guard, no_momentum, safety, cooldown, bad_pool`
B. `too_new, too_old, rugged`
C. Only `safety` and `cooldown`
D. Numeric codes 1–8

**Q3 (Compute — the Doomsday regression).** The Doomsday fixture: h24 +2540%, liquidity $96k, volume $900k, h1 +19.8%, m5 +5.0%, safety predicate true, fresh cooldown book. Does it qualify? Work it through every funnel bar explicitly. What *would* have blocked it before 2026-10-19?

**Q4 (Compute).** Boundary arithmetic — qualify or reject (name the reason) for each single change to `good_pool()` (liq $120k, vol $500k, h24 +80%, h1 +10%, m5 +3%, safety true): (a) h24 = 20.0; (b) h24 = 19.9; (c) liquidity = $50,000; (d) liquidity = $49,999; (e) volume = $100,000; (f) volume = $99,999.

**Q5 (Compute).** Dump-guard boundaries on `good_pool()`: (a) h1 = −15.0; (b) h1 = −15.01. Give verdict and reason for each. Then: (c) h1 = −20% but m5 = +5% — does the positive m5 save it?

**Q6 (Short answer).** Define `has_positive_momentum` exactly, including: is zero positive? What happens when `change_m5_pct` is `None`?

**Q7 (MCQ).** A pool with h1 = −2% and m5 = +3% (all other bars passed, safety true):
A. Rejects as `no_momentum` — h1 is negative
B. Qualifies — positive momentum can come from m5 alone
C. Rejects as `dump_guard`
D. Rejects as `safety`

**Q8 (True/False + justification).** True or false: a pool with h1 = 0 and m5 = 0 (or absent) and all other bars passed qualifies, because it is not dumping. Justify.

**Q9 (Short answer).** Safety fails closed **three** ways in this lab. List all three and the common reject reason.

**Q10 (MCQ).** A non-`Pool` object passed to `qualify` returns:
A. `(True, None)`
B. `(False, "bad_pool")`
C. A raised `TypeError` with no stats update
D. `(False, "safety")`

**Q11 (Short answer).** `Pool` construction is fail-closed. Which construction inputs raise `ValueError`?

**Q12 (Compute).** Cooldown book: mint X is recorded at t = 0. Is X on cooldown at (a) t = 5h, (b) t = exactly 6h, (c) t = 6h + 1s? Is mint Y (never recorded) on cooldown at t = 5h?

**Q13 (True/False + justification).** True or false: a pool rejected for `safety` during a transient safety-API outage starts its 6h cooldown, so it cannot be re-carded when the API recovers an hour later. Justify the actual rule and the harm the opposite rule would cause.

**Q14 (MCQ).** Check-order priority: a pool with liquidity $10k *and* h24 gain +5% (both failing) is counted as:
A. `gain24_low` — gain is checked first conceptually
B. `liq_low` — liquidity is checked before gain in the funnel order
C. Both reasons are counted
D. `bad_pool`

**Q15 (Compute).** The demo scans 8 pools. Reproduce its stats summary: how many qualified, and the count for each of `gain24_low, liq_low, vol_low, dump_guard, no_momentum, safety, cooldown, bad_pool`, plus `scanned`.

**Q16 (Short answer).** Within a single `scan()`, the same mint appears twice and qualifies the first time. What happens to the second occurrence, and through what mechanism? What does every `qualify` call increment in `stats`, exactly?

**Q17 (True/False + justification).** True or false: an extreme h24 gain of +100,000% is rejected as implausible data. Justify with the regression the tests pin.

**Q18 (Short answer).** Why is an upper ceiling on 24h gain *especially* dangerous in a momentum scanner — what population does a ceiling selectively remove?

**Q19 (MCQ).** `CooldownBook.record(mint, now)` raises `ValueError` on:
A. An empty mint or a negative time
B. A mint recorded twice
C. A time of exactly 0
D. Nothing — it always succeeds

**Q20 (Short answer).** Why does every scan fill a stats dict of reject reasons instead of just returning the qualified pools? What failure mode stays invisible without it — name the historical example.

---

## ANSWER KEY

**Q1 —** `MIN_LIQUIDITY = 50,000` ($50k); `MIN_VOLUME_24H = 100,000` ($100k); `MIN_GAIN_24H = 20.0` (% , **no maximum**); `DUMP_GUARD_H1 = -15.0` (h1 < −15% rejects); `COOLDOWN_S = 6h = 21,600s`.
**Q2 — A.** Exactly those eight reason strings.
**Q3 — Qualifies: `(True, None)`.** Bar by bar: not on cooldown (fresh book) ✓; liquidity $96k ≥ $50k ✓; volume $900k ≥ $100k ✓; gain +2540% ≥ +20% ✓ — there is **no upper bound**; dump guard: h1 +19.8% is not < −15% ✓; momentum: h1 > 0 ✓; safety true ✓. Before the 2026-10-19 fix, an invented **300% maximum** on 24h gain would have silently blocked it — the regression fixture exists so that ceiling can never creep back unnoticed.
**Q4 —** (a) Qualifies — 20.0 passes (inclusive bar). (b) Rejects `gain24_low` — 19.9 < 20. (c) Qualifies — $50,000 meets the floor. (d) Rejects `liq_low`. (e) Qualifies — $100,000 meets the floor. (f) Rejects `vol_low`.
**Q5 —** (a) h1 = −15.0: **can qualify** — the guard rejects only h1 **<** −15.0; exactly −15.0 is not below it (it must still pass momentum — see key note: with h1 = −15.0, momentum must come from a positive m5; `good_pool` has m5 +3%, so it qualifies). (b) h1 = −15.01: rejects `dump_guard`. (c) **No** — the dump guard fires regardless of m5; a positive m5 does not save an h1 below −15% (test-pinned).
**Q6 —** Positive-momentum *presence*: `h1 > 0`, **or** `m5 > 0` when m5 is present. Zero is **not** positive. When m5 is `None` (absent), the decision falls back to h1 alone.
**Q7 — B.** h1 negative but not below the dump guard, and m5 > 0 supplies positive momentum — momentum via m5 only is test-pinned.
**Q8 — False.** Zero is not positive momentum; with h1 = 0 and no positive m5, the pool rejects as `no_momentum`. "Not dumping" passes only the dump guard, not the momentum requirement.
**Q9 —** Three fail-closed ways, all rejecting with reason `"safety"`: (1) the predicate returns `False`; (2) **no predicate is supplied** (missing predicate); (3) **the predicate raises** an exception. A crashed/absent safety check never lets a pool through.
**Q10 — B.** `(False, "bad_pool")` — rejected and counted, not raised.
**Q11 —** An empty mint, or any non-numeric (or boolean) required field. A pool with missing data never reaches the funnel.
**Q12 —** (a) t = 5h: **on cooldown** (strictly inside 6h). (b) t = exactly 6h: **not on cooldown** — the cooldown expires at exactly 6h (`(now − last) < COOLDOWN_S`). (c) 6h + 1s: not on cooldown. Mint Y: never on cooldown — cooldowns are per-mint.
**Q13 — False.** Only a *qualifying* pool records a cooldown entry — **a rejection never mutes a mint**. The opposite rule would cause this harm: cards suppressed for hours after a transient outage, for a reason that had nothing to do with the token, silently missing runners that qualified as soon as the API recovered.
**Q14 — B.** The funnel order is cooldown → liquidity → volume → gain → dump guard → momentum → safety, and each call increments exactly one outcome; liquidity is checked before gain, so the count is `liq_low`. Order matters because funnel stats are read by first-failing reason.
**Q15 —** Demo stats: `qualified: 2, scanned: 8`, with `gain24_low: 1, liq_low: 1, vol_low: 1, dump_guard: 1, no_momentum: 1, safety: 1, cooldown: 0, bad_pool: 0`. Qualified mints: GoodMint and DoomsdayMint (one pool per reject reason among the other six scanned, in the demo's fixture set).
**Q16 —** The second occurrence rejects as `cooldown`, because `scan` shares **one cooldown book** across the batch and the first qualification recorded the mint — scan-level dedup. Every `qualify` call with a stats dict increments `scanned` plus **exactly one** of `qualified` or the single reject reason.
**Q17 — False.** There is no plausibility cap: an extreme +100,000% gain is test-pinned to qualify (if all other bars pass) — any future "sanity cap" on gain turns this suite red, by design, after the Doomsday ceiling incident.
**Q18 —** A ceiling selectively removes exactly the population a momentum scanner exists to find: the biggest, already-running winners. Ordinary pools are unaffected (they never reach the ceiling), so the harm is invisible in aggregate counts while every exceptional runner is silently discarded.
**Q19 — A.** Empty mint / negative time raise; cooldowns are per-mint and recording is validated.
**Q20 —** Without counted reject reasons, a silent blockage looks identical to "nothing qualified today" — an absence, not a signal. The stats dict makes every non-card an explained event. Historical example: the 300% gain ceiling silently blocked the Doomsday-shaped runner; it was only diagnosable/fixable as a counted funnel reason, and the counters are what keep such a blockage visible thereafter.

---

## GRADING RUBRIC

- **MCQ (Q2, Q7, Q10, Q14, Q19):** 1 point for the correct letter only.
- **True/False + justification (Q8, Q13, Q17):** 1 point only if verdict and justification both match the key.
- **Short-answer (Q1, Q6, Q9, Q11, Q16, Q18, Q20):** 1 point if every element in the key is present and correct; Q1 and Q9 require complete lists.
- **Compute-it (Q3, Q4, Q5, Q12, Q15):** 1 point for every verdict/reason/count in the key; Q3 requires the bar-by-bar working and the 300% ceiling identified, Q5 requires the −15.0 boundary stated correctly.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `momentumscan.py` if needed), then retake the full exam.
