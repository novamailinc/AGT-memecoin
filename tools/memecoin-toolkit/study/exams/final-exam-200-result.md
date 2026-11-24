# Final Exam — Memecoin Toolkit — Official Sitting Record

- **Exam:** Final Exam — Memecoin Toolkit (cross-toolkit), 200 questions (`final-exam-200.md`)
- **Date of sitting record:** 2026-10-20
- **Final score:** **199 / 200**
- **Percentage:** **99.5%**
- **Pass bar:** ≥85% (≥170/200)
- **Result:** **PASS**

## Per-toolkit subscores

| Tag | Toolkit | Questions | Score | % |
|-----|---------|-----------|-------|---|
| [01] | bonding-curve-math | Q1–Q7 (7) | 7/7 | 100% |
| [02] | amm-and-lp | Q8–Q14 (7) | 7/7 | 100% |
| [03] | token-safety-veto | Q15–Q21 (7) | 7/7 | 100% |
| [04] | holder-concentration | Q22–Q28 (7) | 7/7 | 100% |
| [05] | bundle-cluster-detection | Q29–Q35 (7) | 7/7 | 100% |
| [06] | rug-taxonomy | Q36–Q42 (7) | 7/7 | 100% |
| [07] | exit-ladder-replay | Q43–Q49 (7) | 7/7 | 100% |
| [08] | statistical-validity-meme | Q50–Q56 (7) | 7/7 | 100% |
| [09] | backtesting-rigor-meme | Q57–Q63 (7) | 7/7 | 100% |
| [10] | live-risk-controls-meme | Q64–Q70 (7) | 7/7 | 100% |
| [11] | smart-wallet-grading | Q71–Q77 (7) | 7/7 | 100% |
| [12] | funnel-autopsy | Q78–Q84 (7) | 7/7 | 100% |
| [13] | post-trade-meme | Q85–Q91 (7) | 7/7 | 100% |
| [14] | token-lifecycle | Q92–Q98 (7) | 7/7 | 100% |
| [15] | feed-data-quality | Q99–Q105 (7) | 7/7 | 100% |
| [16] | sniper-window-economics | Q106–Q112 (7) | 7/7 | 100% |
| [17] | deployer-forensics | Q113–Q119 (7) | 7/7 | 100% |
| [18] | wash-and-botshare | Q120–Q126 (7) | 7/7 | 100% |
| [19] | liquidity-velocity | Q127–Q133 (7) | 7/7 | 100% |
| [20] | copy-trading-economics | Q134–Q140 (7) | 7/7 | 100% |
| [21] | card-design | Q141–Q146 (6) | 6/6 | 100% |
| [22] | ledger-accounting | Q147–Q152 (6) | 5/6 | 83.3% |
| [23] | daemon-supervision | Q153–Q158 (6) | 6/6 | 100% |
| [24] | cross-chain-costs | Q159–Q164 (6) | 6/6 | 100% |
| [25] | lottery-sizing | Q165–Q170 (6) | 6/6 | 100% |
| [26] | time-to-peak | Q171–Q176 (6) | 6/6 | 100% |
| [27] | feed-resilience | Q177–Q182 (6) | 6/6 | 100% |
| [28] | signal-refinery | Q183–Q188 (6) | 6/6 | 100% |
| [29] | momentum-scanner | Q189–Q194 (6) | 6/6 | 100% |
| [30] | verdict-protocol | Q195–Q200 (6) | 6/6 | 100% |
| | **Total** | **200** | **199/200** | **99.5%** |

Flag: toolkit [22] ledger-accounting at 5/6 (83.3%) is the only toolkit below 85% individually. Under the exam's own rubric, re-study is triggered only by ≥3 wrong in a toolkit block, so no toolkit triggers re-study and the pass carries no condition.

## Missed questions (complete list — 1 question)

| Q | Toolkit | Candidate's answer (brief) | Correct answer (from the key) |
|---|---------|----------------------------|-------------------------------|
| Q149 | [22] ledger-accounting | False (correct verdict) — construction does not start fresh and raises a corrupt-ledger error, named by the candidate as `LedgerCorruptError`. The rest of the justification (starting empty would erase the record / fake a clean slate; failing loud preserves it) matches the key. | **False** — Both-copies-corrupt (or corrupt main with no backup) raises **`CorruptLedgerError`**. Starting empty would fake a clean slate and destroy the record the verdict gates depend on; a torn main alone recovers from the backup with `recovered_from_backup=True`. |

Scoring note on Q149: the question explicitly requires the justification to include the error raised, so the error name is part of the scored answer. The candidate's identifier is transposed and does not exist in the toolkit (`CorruptLedgerError` is the class defined in `22-ledger-accounting/ledger.py`). Under the rubric's True/False rule (verdict **and** a justification consistent with the key), the question scores 0, with no partial credit.

All other 199 questions were correct: every MCQ letter matches the key, every True/False verdict matches with a justification consistent with the key, every compute question shows working reaching the key's value within its rounding, and every short answer contains the key's load-bearing claim. Three borderline wording-equivalence calls (Q22, Q60, Q191) were credited after checking the toolkit modules/study material, as documented in the full grading sheet at `exam-sitting/grading.md`. No question was found where the key is provably, mechanically wrong, and no score was adjusted on key-error grounds.

## Method

The candidate was a separate agent answering closed-book from a mechanically-produced questions-only extract (key stripped by line cut before the sitting; candidate barred from study/exams/ and git history and confirmed it never opened them), the answers were derived from study/notes, toolkit READMEs, modules, demos and tests, 0 answers were marked as guesses by the candidate, and grading was done by a separate independent agent against the key under the exam's rubric with no partial credit beyond what the rubric allows.
