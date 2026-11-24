# Exam — Ledger Accounting

- **Total questions:** 20
- **Points:** 20 (1 point per question)
- **PASS BAR: ≥85% — at least 17 of 20 correct**
- **Conditions:** Closed-book. Synthetic data only. All answers are derivable from the toolkit's README / `ledger.py` alone.
- **Types:** Multiple-choice (A–D), true/false-with-justification, short-answer, and compute-it questions.

---

## QUESTIONS

**Q1 (MCQ).** A ledger is constructed at a path where neither the main file nor a backup exists. What happens?
A. `CorruptLedgerError` is raised
B. A fresh, empty ledger is created (and no file exists until save)
C. An empty file is written immediately
D. Construction blocks on the lock

**Q2 (MCQ).** The main file is truncated/corrupt but a good backup exists. What happens on construction?
A. A silent empty ledger
B. `CorruptLedgerError`
C. Fallback to the backup, with `recovered_from_backup = True`
D. The corrupt main file is used anyway

**Q3 (True/False + justification).** True or false: if both the main file and the backup are corrupt, the ledger starts empty so trading can continue. Justify.

**Q4 (MCQ).** Which error does a duplicate `(ticket, kind)` pair raise?
A. `OrderingError`
B. `DuplicateEntryError`
C. `LockHeldError`
D. `ValueError` only

**Q5 (Short answer).** Which (ticket, kind) combinations are *allowed* even though they share part of a key with an existing entry? Give both patterns.

**Q6 (Compute).** A ledger holds `T-001 BUY ts=1000`. State the verdict for each attempted add: (a) `T-001 CLOSE ts=1600`; (b) `T-001 CLOSE ts=999`; (c) `T-001 CLOSE ts=1000` (exact BUY timestamp).

**Q7 (MCQ).** A `CLOSE` is added for a ticket with no matching `BUY`. What happens?
A. It is allowed; ordering only applies within a ticket that has a BUY
B. `OrderingError` is raised
C. A BUY is auto-created
D. The entry is sorted into place

**Q8 (True/False + justification).** True or false: a `BUY` that would follow an existing `CLOSE` for the same ticket is rejected. Justify / name the error.

**Q9 (Short answer).** List the four ledger-specific error classes and the one base class they all subclass.

**Q10 (Compute).** In `save()`, order these steps correctly: (1) `os.replace` the temp file over the main path; (2) write `<path>.tmp`; (3) preserve the previous good main file as `<path>.bak`; (4) fsync the temp file. Also: what temp-file residue remains after a clean save?

**Q11 (MCQ).** Why must the temp file be written in the same directory as the main file?
A. So the file is easier to find
B. So `os.replace` is an atomic rename on the same filesystem
C. To avoid needing permissions
D. So the backup can be in the same folder

**Q12 (True/False + justification).** True or false: `ts = 0` is a valid timestamp, but `ts = -1`, `ts = True`, and `ts = "1000"` are not. Justify the boolean case specifically.

**Q13 (MCQ).** How is the lock file `<path>.lock` created, and why does that mechanism matter?
A. Check-then-create; the check makes it safe
B. With `O_EXCL` exclusive create, so creation itself fails atomically if the lock already exists — no check-then-create race
C. By writing the pid and comparing pids
D. As a directory

**Q14 (Short answer).** A second `locked()` holder is attempted. What is raised, what does its message contain, and what happens to the lock when the first holder exits — including exit by exception?

**Q15 (Compute).** A stale lock file is left on disk after a crash. A new writer calls `locked()`. Does it proceed? What must happen first before any writer can proceed?

**Q16 (MCQ).** What is the normal write path that combines adding, locking, and saving?
A. `add_entry()` alone
B. `save()` alone
C. `add_and_save()` — add + save under the lock
D. `reload()`

**Q17 (Short answer).** `get_entry(ticket, kind)` returns a copy. Why does returning a copy matter for ledger hygiene?

**Q18 (True/False + justification).** True or false: a failed `add_entry` (e.g. a duplicate) still appends the entry, marked as rejected. Justify.

**Q19 (MCQ).** The saved on-disk file has what shape?
A. A bare JSON list of entries
B. A JSON object with `version` / `entries`
C. One JSON object per line
D. A pickled dict

**Q20 (Compute / scenario).** Following the demo round trip: entries `T-001 BUY 1000`, `T-001 CLOSE 1600`, `T-002 BUY 2000` are saved and reloaded, then one more valid entry is added and saved, then the main file is truncated. On the next load, how many entries are recovered, and from where? (Use the demo's captured numbers.)

---

## ANSWER KEY

**Q1 — B.** Missing main + missing backup = fresh empty ledger; no file exists until `save()`.
**Q2 — C.** Fail-closed recovery falls back to the last good backup (`<path>.bak`) and sets `recovered_from_backup = True`.
**Q3 — False.** Both copies corrupt (or a corrupt main with no backup) raises `CorruptLedgerError` — never a silent empty state, because a silently emptied ledger would fake a clean slate.
**Q4 — B.** `DuplicateEntryError`.
**Q5 —** Same ticket with a *different* kind (e.g. `T-001 BUY` then `T-001 CLOSE`), and same kind with a *different* ticket (e.g. `T-001 BUY` then `T-002 BUY`). Uniqueness is on the pair.
**Q6 —** (a) Allowed — CLOSE after BUY. (b) Rejected — `OrderingError`, CLOSE ts 999 is before BUY ts 1000. (c) Allowed — the exact-timestamp boundary is permitted (only *before* is rejected).
**Q7 — B.** CLOSE-without-BUY raises `OrderingError`. Non-ordered kinds are exempt, BUY/CLOSE are not.
**Q8 — True.** A BUY that would follow an existing CLOSE for that ticket raises `OrderingError`; the history is impossible and is rejected rather than sorted.
**Q9 —** `LockHeldError`, `DuplicateEntryError`, `OrderingError`, `CorruptLedgerError` — all subclass `LedgerError`.
**Q10 — Correct order: (2) write `.tmp` → (4) fsync → (3) preserve previous good main as `.bak` → (1) `os.replace`.** Residue after a clean save: **no `.tmp` file is left behind**.
**Q11 — B.** `os.replace` is only an atomic rename within the same filesystem/directory; a global temp dir could be a different filesystem, breaking atomicity.
**Q12 — True.** Timestamps must be numeric and non-negative; `0` is allowed. Booleans are explicitly rejected even though `True == 1` in Python — a boolean is not a timestamp (same non-numeric/negative cases raise `ValueError`).
**Q13 — B.** `O_EXCL` makes create-or-fail atomic; a check-then-create implementation has a race where two writers both see "no lock" and both proceed.
**Q14 —** `LockHeldError` is raised, with the lock path in its message. The lock is always released on exit from the first holder's block, including on exception.
**Q15 — No, it does not proceed.** A stale lock file blocks (the lab does not distinguish stale from live); the lock file must be removed before a writer can proceed.
**Q16 — C.** `add_and_save()` is add + save under the lock — the normal write path.
**Q17 —** So a caller mutating the returned dict cannot silently mutate the ledger's internal state / recorded history; lookups hand out copies, not references.
**Q18 — False.** A failed add never appends. The in-memory (and therefore saved) state is unchanged by a rejected entry.
**Q19 — B.** A JSON object with a `version`/`entries` shape.
**Q20 — 4 entries, recovered from the backup.** After reload there were 3; one more valid entry was added and saved (4 total, and that save preserved the good state as backup); truncating the main file then triggers backup recovery with `recovered_from_backup = True`, entries = 4 (the demo's captured figure).

---

## GRADING RUBRIC

- **MCQ (Q1, Q2, Q4, Q7, Q11, Q13, Q16, Q19):** 1 point for the correct letter only.
- **True/False + justification (Q3, Q8, Q12, Q18):** 1 point only if the verdict and the justification in the key are both correct.
- **Short-answer (Q5, Q9, Q14, Q17):** 1 point if every element in the key is present and correct.
- **Compute-it (Q6, Q10, Q15, Q20):** 1 point for the exact verdicts/order/count in the key; partial sequences score 0.
- **Pass rule:** Pass = **≥17/20 (≥85%)**. Below 17 is a fail.
- **What a fail means:** Re-study the toolkit README (and `ledger.py` if needed), then retake the full exam. A ledger that fails silently is the failure this toolkit exists to prevent — the exam is graded the same way.
