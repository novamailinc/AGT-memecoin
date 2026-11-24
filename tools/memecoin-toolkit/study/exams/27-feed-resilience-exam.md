# Exam — Feed Resilience

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only — an injected clock and a call-counting fake transport; no real HTTP. All answers are derivable from the toolkit's README / `resilience.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

---

## QUESTIONS

**Q1 (MCQ).** `SyntheticClock(start=1000.0)` — which operations are fail-closed errors?
A. A negative start, and advancing by a negative amount
B. Only advancing by zero
C. Reading `now()` twice
D. None

**Q2 (Compute).** A `FloodGuard` with the default buffer receives `on_rate_limited(retry_after=30)` at t = 1000. Until what time is it parked, what is `remaining()` immediately afterwards, and at what clock time does it resume (parked at the boundary — yes or no)?

**Q3 (MCQ).** While the guard is parked, `try_send(transport, payload)`:
A. Retries once, then queues
B. Returns `None` *without touching the transport*
C. Raises `ValueError`
D. Sends with a warning header

**Q4 (Short answer).** How does the test suite *prove* zero calls happen during cooldown, rather than merely asserting the guard reports itself parked? Name the test double and the counter.

**Q5 (True/False + justification).** True or false: flood-guard state cannot survive a restart, so a daemon restarting mid-ban starts unparked. Justify with the module's API.

**Q6 (Short answer).** Why does the cooldown add a +5s buffer on top of `retry_after`? What clock problem does it protect against?

**Q7 (Short answer).** A `RateBucket(capacity=2, refill_per_s=...)`: after draining both tokens with no time passing, a third `try_acquire()` returns what? Over synthetic time, tokens refill but are capped at what?

**Q8 (Short answer).** `DualKeyBuckets(main, discovery)`: main is fully drained. Can a discovery call still acquire? What live failure does this independence prevent, and what does an unknown key do?

**Q9 (Compute).** `backoff_delays()` with defaults (base 1.0, factor 2.0, max 60.0, attempts 5, seed 7, jitter_frac 0.25) produces the demo list. Reproduce it (to 2 decimal places) and state: same seed → same or different list? Different seed → same or different list (in general)?

**Q10 (True/False + justification).** True or false: jitter in `backoff_delays` is drawn from the system RNG so every run differs. Justify the design, and state why jitter is needed in production at all when every client shares the same base and factor.

**Q11 (MCQ).** Which backoff inputs are fail-closed errors?
A. Non-positive base, factor < 1, `max_s < base_s`, negative attempts, jitter outside [0, 1]
B. Only jitter above 1
C. Only a factor below 1
D. None — they are clamped

**Q12 (Short answer).** `CircuitBreaker(failure_threshold=3, reset_timeout_s=30)`: trace the states — after 3 failures; after the reset timeout elapses with no call; after a success in that state; after a failure in that state. What does `allow()` return in the open state?

**Q13 (True/False + justification).** True or false: a failure while half-open merely counts toward a new threshold of 3. Justify.

**Q14 (MCQ).** `should_request_gzip(estimated_bytes)` is true at and above what threshold? Evaluate it at 49,999, 50,000, and 100,000 bytes.
A. 10,000 — true, true, true
B. 50,000 — false, true, true
C. 100,000 — false, false, true
D. 50,000 — false, false, true

**Q15 (Short answer).** What header does `encoding_headers(expect_large=True)` request, and what is the egress lesson behind it (what happens to large uncompressed responses)?

**Q16 (MCQ).** `decode_body(body, content_encoding)` raises `ValueError` on:
A. A `None` body or an unsupported encoding
B. An `identity` encoding
C. A `gzip` encoding
D. A small body

**Q17 (Compute).** Per the demo, a 10,000-byte repetitive payload gzip-round-trips to how many compressed bytes? State both numbers the demo/helper reports (raw, compressed).

**Q18 (Short answer).** Why does the FloodGuard park *all* mutating calls on a single 429 instead of just retrying the failed call after `retry_after`?

**Q19 (Compute / scenario).** Demo trace: before any 429, one send succeeds (calls=1). A 429 with `retry_after=30` arrives. A send is attempted during cooldown, then the clock advances past the cooldown and another send is attempted. What are the transport call counts at each of the three stages, and what does the during-cooldown send return?

**Q20 (True/False + justification).** True or false: `FloodGuard.restore` accepts a state dict missing `parked_until`, defaulting to unparked. Justify. Also: are a negative `retry_after` or a negative `buffer_s` accepted?

---

## ANSWER KEY

**Q1 — A.** A negative start and a negative advance both raise `ValueError` — time never runs backwards.
**Q2 — Parked until 1035.0; `remaining()` = 35s; at exactly t = 1035 it has resumed (not parked).** Working: 1000 + 30 (retry_after) + 5 (buffer) = 1035; the guard resumes at and after the exact boundary (test-pinned).
**Q3 — B.** It returns `None` without touching the transport — zero calls during cooldown.
**Q4 —** Via the call-counting `FakeTransport`: every `send()` increments its `calls` counter (and appends to `sent`). Tests attempt sends while parked and assert the counter does not move — behaviour proven by a counter, not asserted from the guard's own report.
**Q5 — False.** `export_state()` / `FloodGuard.restore(clock, state)` persist and restore the cooldown across a (simulated) restart, still parked. Without this, a restart mid-ban would storm a still-banned API with a fresh, unparked guard.
**Q6 —** The `retry_after` was measured on the *server's* clock / at an earlier moment; clock skew and transit delay mean your clock may reach the nominal expiry before the server considers the ban lifted. The +5s buffer absorbs that race so the first post-ban call does not land early and re-trigger the limit.
**Q7 —** Third acquire returns `False` (bucket empty). Refill accrues over time but is **capped at capacity** (2) — tokens never accumulate beyond capacity.
**Q8 — Yes** — the buckets drain independently; main exhausted leaves discovery untouched. This prevents a research/discovery sweep from starving live trading calls by consuming a shared budget. An **unknown key raises `ValueError`**.
**Q9 — `[0.91, 1.65, 4.30, 6.29, 16.29]`** (demo rounding; full values ≈ [0.9119, 1.6508, 4.3019, 6.2897, 16.2871]). **Same seed → identical list** (seeded jitter is deterministic, and list/single forms agree). **Different seed → a different list in general** (seed sensitivity is test-pinned).
**Q10 — False.** Jitter is *seeded*, not drawn from the system RNG — determinism is what makes the lab's claims testable/reproducible. In production jitter is needed because identical base/factor clients would otherwise retry in lockstep (a thundering herd that re-overloads the endpoint at the same instants); jitter spreads the retries.
**Q11 — A.** All listed inputs raise `ValueError`.
**Q12 —** After 3 failures: **open**, `allow()` = **False**. After the 30s reset timeout elapses: state flips to **half_open** (allow is true only when closed or half-open, so a probe is let through). A success in half-open: **closes** and resets. A failure in half-open: **re-opens immediately**.
**Q13 — False.** A failure while half-open re-opens the breaker *immediately* — it does not start a fresh count to 3. That is the safe choice: the probe just proved the endpoint is still failing.
**Q14 — B.** Threshold is **50,000 bytes**: 49,999 → False; 50,000 → True; 100,000 → True.
**Q15 —** `Accept-Encoding: gzip` (headers: `{'Accept-Encoding': 'gzip', 'Accept': 'application/json'}`). Lesson: the egress proxy **drops large uncompressed responses**, so large payloads must be requested compressed — gzip was the fix, not retries.
**Q16 — A.** `None` bodies and unsupported encodings raise; `None`/`identity`/`gzip` encodings are the handled cases — identity and gzip round-trip correctly.
**Q17 — (10000, 45)** — raw 10,000 bytes compress to **45 bytes** for the demo's repetitive payload.
**Q18 —** Because retrying makes a flood ban worse: a 429 is the service saying the *sender* is flooding, so every mutating call must park until `retry_after` + buffer and queued alerts must wait rather than storm. Retrying just the failed call keeps generating the very traffic the ban is punishing and can extend/escalate it.
**Q19 — Stage counts: 1 → 1 → 2.** Before: calls=1 after one successful send. During cooldown: the send returns **`None`** and calls stays **1** (zero new calls). After cooldown: the send succeeds and calls becomes **2**.
**Q20 — False.** A state dict missing `parked_until` raises `ValueError` (fail-closed restore). No — a negative `retry_after` and a negative `buffer_s` both raise `ValueError` as well.

---

## GRADING RUBRIC

- **MCQ (Q1, Q3, Q11, Q14, Q16):** 1 point for the correct letter only.
- **True/False + justification (Q5, Q10, Q13, Q20):** 1 point only if verdict and justification both match the key.
- **Short-answer (Q4, Q6, Q7, Q8, Q12, Q15, Q18):** 1 point if every element in the key is present and correct.
- **Compute-it (Q2, Q9, Q17, Q19):** 1 point for the exact numbers in the key; Q9 accepts the 2-decimal demo values, Q2 requires the boundary verdict.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `resilience.py` if needed), then retake the full exam.
