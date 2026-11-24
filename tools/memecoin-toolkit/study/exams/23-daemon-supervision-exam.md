# Exam — Daemon Supervision

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only — this lab never inspects a real process. All answers are derivable from the toolkit's README / `supervisor.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

---

## QUESTIONS

**Q1 (MCQ).** `check_liveness` is given `pidfile_pid = None`. What is the result?
A. `alive=True, reason="ok"`
B. `alive=False, reason="no_pidfile"`
C. `alive=False, reason="no_process"`
D. `ValueError`

**Q2 (MCQ).** A pid exists in the table but its heartbeat age exceeds the maximum. What is the verdict?
A. Alive — the process exists
B. Not alive, reason `"stale_heartbeat"` — a wedged process is not alive
C. Not alive, reason `"no_process"`
D. `ValueError`

**Q3 (Compute).** With `max_heartbeat_age_s = 60`, is a heartbeat age of exactly 60 fresh or stale? What about 60.1? Name the function that encodes this boundary.

**Q4 (Compute).** Using defaults `base_s=1.0, factor=2.0, cap_s=60.0`, write the backoff schedule for attempts 0–6 (`backoff_schedule(7)`).

**Q5 (Compute).** Compute `backoff_delay(attempt=3, base_s=2.0, factor=3.0, cap_s=100.0)`. Show the uncapped value and state whether the cap binds.

**Q6 (True/False + justification).** True or false: `backoff_delay` with `factor = 1.0` produces a flat schedule equal to the base (subject to the cap). Justify from the formula `min(cap, base * factor**attempt)`.

**Q7 (MCQ).** Which inputs to the backoff functions are fail-closed errors?
A. Negative attempt, non-positive base/cap, factor below 1
B. Only a negative attempt
C. Only a cap below the base
D. None — all inputs are clamped

**Q8 (Short answer).** `RestartPolicy` defaults: what are `max_restarts` and `window_s`? What happens when the budget is exhausted, and does a failed `record_restart` consume budget?

**Q9 (MCQ).** Why does the restart policy use a *sliding window* rather than a lifetime counter?
A. So old restarts age out and the budget recovers after a quiet period
B. To make restarts faster
C. To count restarts more accurately
D. Lifetime counters use too much memory

**Q10 (True/False + justification).** True or false: `pid_alive(None, table)` raises `ValueError` because `None` is not a pid. Justify.

**Q11 (Short answer).** `Proc` construction is fail-closed. State the three validation rules for `pid`, `ppid`, and `cmdline`.

**Q12 (Compute — self-match fixture).** Per the demo: for pattern `'smart-watcher'`, the naive kill targets are `[499, 500, 700, 701]` and the safe kill targets are `[700, 701]`. Which pids are the watcher itself and its parent, and why does the parent's command line contain the pattern?

**Q13 (MCQ).** The safe matcher differs from the naive matcher by:
A. Using regex instead of substring matching
B. Excluding the watcher's own pid *and* its parent pid from otherwise identical substring matching
C. Excluding only the watcher's own pid
D. Matching only exact command lines

**Q14 (True/False + justification).** True or false: if naive and safe matchers are run on a table with no self-match, they agree. Justify.

**Q15 (Short answer).** `ensure_single_instance` returns which three statuses, and what match count produces each?

**Q16 (MCQ).** When `ensure_single_instance` finds 2+ matches, what does this lab do?
A. Kills all but the oldest
B. Kills all but the newest
C. Reports `"duplicates"` with the offending records — it never silently resolves by killing extras in this lab
D. Restarts all of them

**Q17 (Compute).** A table contains only the watcher (pid 500) and its parent shell (pid 499), both of whose command lines contain the pattern, and no daemon. What status does `ensure_single_instance` (safe matcher) return, and why?

**Q18 (Short answer).** What operational failure follows if a watchdog restarts a crashing daemon immediately, forever, with no backoff and no budget? Name both missing mechanisms.

**Q19 (MCQ).** Fail-closed inputs to the safe matcher include:
A. An empty pattern and non-positive self/parent pids
B. A pattern with spaces
C. A table with more than 100 rows
D. None

**Q20 (Short answer).** Why does this lab insist on synthetic process tables instead of reading the real process list, even though the real list is available?

---

## ANSWER KEY

**Q1 — B.** `None` pidfile pid is a normal input: `alive=False, reason="no_pidfile"`.
**Q2 — B.** Pid presence alone is not liveness; a stale heartbeat means wedged, judged not alive with reason `"stale_heartbeat"`. (The other reasons are `"no_pidfile"` and `"no_process"`; `"ok"` is the alive case.)
**Q3 — Exactly 60 is fresh; 60.1 is stale.** The boundary is *inclusive*, encoded by `heartbeat_fresh(age, max_age)`.
**Q4 — `[1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]`.** Working: 1·2⁰=1, 2, 4, 8, 16, 32, then 1·2⁶=64 capped at 60.
**Q5 — 54.0.** Uncapped: 2.0 × 3³ = 2 × 27 = 54.0; the 100.0 cap does not bind, so the delay is 54.0.
**Q6 — True.** With factor 1, `base * 1**attempt = base` for every attempt, so every delay is `min(cap, base)` — flat.
**Q7 — A.** Negative attempts, non-positive base/cap, or a factor below 1 all raise `ValueError`.
**Q8 —** Defaults: `max_restarts = 5`, `window_s = 600.0`. When exhausted, `record_restart` raises `RuntimeError` (give up and flag, never a restart storm), `gave_up()` is true — and a failed record does **not** consume budget.
**Q9 — A.** Restarts age out of the sliding window, so the budget recovers; a lifetime counter would permanently condemn a daemon for ancient crashes.
**Q10 — False.** `pid_alive(None, table)` returns `False`, not an error — a missing pidfile pid is a normal input.
**Q11 —** `pid` must be a positive int; `ppid` must be non-negative (negative raises); `cmdline` must be a non-empty string. Violations raise `ValueError`.
**Q12 — Watcher = 500, parent = 499; real daemons = 700, 701.** The parent shell's command line typically contains the pattern too because the restart/kill one-liner itself names the pattern (e.g. the shell command includes the string `smart-watcher`), so substring matching matches the shell running the command.
**Q13 — B.** Identical substring matching, but self pid and parent pid are always excluded. Excluding only self is insufficient (see Q12).
**Q14 — True.** The safe matcher is the naive matcher minus self/parent; with no self-match in the table there is nothing to exclude, so the results agree — and non-matching processes are ignored by both.
**Q15 —** 0 matches → `"start"`; exactly 1 match → `"ok"`; 2 or more → `"duplicates"`.
**Q16 — C.** Duplicates are reported with the offending records; which duplicate should survive is a human decision needing more information.
**Q17 — `"start"`.** The safe matcher excludes both 500 and 499, leaving 0 real matches — no daemon is running, so one should be started.
**Q18 —** A restart storm / crash loop: a permanently crashing daemon is relaunched at full speed forever, burning resources and spamming logs/alerts with no chance to recover. Missing mechanisms: **exponential backoff** and a **restart budget** (that gives up and flags).
**Q19 — A.** Empty patterns and non-positive self/parent pids raise `ValueError`.
**Q20 —** So the dangerous failure modes (especially pattern-based killing, including the self-match trap) can be exercised with zero risk of signalling a real process. The lab models semantics on caller-supplied fake `Proc` tables only; safety of the exercise itself requires that no real process can ever be touched.

---

## GRADING RUBRIC

- **MCQ (Q1, Q2, Q7, Q13, Q16, Q19):** 1 point for the correct letter only.
- **True/False + justification (Q6, Q10, Q14):** 1 point only if verdict and justification both match the key.
- **Short-answer (Q8, Q11, Q15, Q18, Q20):** 1 point if every element in the key is present and correct.
- **Compute-it (Q3, Q4, Q5, Q12, Q17):** 1 point for the exact values/verdicts in the key, with working where asked; a correct final number with wrong working on Q5 scores 0.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `supervisor.py` if needed), then retake the full exam.
