# MANIFEST — memecoin-toolkit

Regenerated from the coordinator's own full verification run on 2026-10-20
(Africa/Lagos) after the "nitro" expansion and the same-day gap-closing
(real-data adapters, exam sitting, self-audit hook): `bash
run_all_tests.sh` with `~/workspace/Forex-agent/.venv/bin/python`, every
demo executed, LOC counted from the tree. All code original, stdlib-only,
no network. Fixtures are synthetic except the three small anonymized
real-data samples in toolkits 08/12/13 (field-whitelisted; no wallet,
grade, or registry data anywhere in this repo).

**Aggregate: 30 toolkits · 1,303 passed, 0 failed · 30/30 demos exit 0 ·
19,303 total Python LOC (modules + demos + tests) · study layer: 30
notes (5,285 lines) + 32 exam files (5,534 lines), incl. the
200-question final exam and its graded result record**

| Toolkit | Modules | .py LOC | Tests | Result |
|---|---|---|---|---|
| 01-bonding-curve-math | `curve.py`, `demo.py`, `test_curve.py` | 719 | 43 | ✅ 43 passed |
| 02-amm-and-lp | `amm.py`, `demo.py`, `test_amm.py` | 732 | 45 | ✅ 45 passed |
| 03-token-safety-veto | `veto.py`, `demo.py`, `test_veto.py` | 584 | 41 | ✅ 41 passed |
| 04-holder-concentration | `holders.py`, `demo.py`, `test_holders.py` | 808 | 45 | ✅ 45 passed |
| 05-bundle-cluster-detection | `bundle.py`, `demo.py`, `test_bundle.py` | 724 | 43 | ✅ 43 passed |
| 06-rug-taxonomy | `ruglab.py`, `demo.py`, `test_ruglab.py` | 702 | 44 | ✅ 44 passed |
| 07-exit-ladder-replay | `replay.py`, `demo.py`, `test_replay.py` | 852 | 44 | ✅ 44 passed |
| 08-statistical-validity-meme | `statslab.py`, `realdata.py`, `demo.py`, `test_statslab.py`, `test_realdata.py` | 1127 | 49 | ✅ 49 passed |
| 09-backtesting-rigor-meme | `rigor.py`, `demo.py`, `test_rigor.py` | 868 | 40 | ✅ 40 passed |
| 10-live-risk-controls-meme | `risk.py`, `demo.py`, `test_risk.py` | 779 | 45 | ✅ 45 passed |
| 11-smart-wallet-grading | `grading.py`, `demo.py`, `test_grading.py` | 736 | 45 | ✅ 45 passed |
| 12-funnel-autopsy | `autopsy.py`, `fixtures.py`, `realdata.py`, `demo.py`, `test_autopsy.py`, `test_realdata.py` | 1047 | 49 | ✅ 49 passed |
| 13-post-trade-meme | `posttrade.py`, `realdata.py`, `demo.py`, `test_posttrade.py`, `test_realdata.py` | 1028 | 47 | ✅ 47 passed |
| 14-token-lifecycle | `lifecycle.py`, `demo.py`, `test_lifecycle.py` | 621 | 56 | ✅ 56 passed |
| 15-feed-data-quality | `feedquality.py`, `demo.py`, `test_feedquality.py` | 607 | 49 | ✅ 49 passed |
| 16-sniper-window-economics | `sniperwindow.py`, `demo.py`, `test_sniperwindow.py` | 521 | 54 | ✅ 54 passed |
| 17-deployer-forensics | `deployer.py`, `demo.py`, `test_deployer.py` | 542 | 46 | ✅ 46 passed |
| 18-wash-and-botshare | `wash.py`, `demo.py`, `test_wash.py` | 541 | 39 | ✅ 39 passed |
| 19-liquidity-velocity | `liqvelocity.py`, `demo.py`, `test_liqvelocity.py` | 514 | 39 | ✅ 39 passed |
| 20-copy-trading-economics | `copyecon.py`, `demo.py`, `test_copyecon.py` | 459 | 38 | ✅ 38 passed |
| 21-card-design | `cardcheck.py`, `demo.py`, `test_cardcheck.py` | 464 | 38 | ✅ 38 passed |
| 22-ledger-accounting | `ledger.py`, `demo.py`, `test_ledger.py` | 703 | 45 | ✅ 45 passed |
| 23-daemon-supervision | `supervisor.py`, `demo.py`, `test_supervisor.py` | 558 | 38 | ✅ 38 passed |
| 24-cross-chain-costs | `chaincosts.py`, `demo.py`, `test_chaincosts.py` | 417 | 38 | ✅ 38 passed |
| 25-lottery-sizing | `sizing.py`, `demo.py`, `test_sizing.py` | 570 | 45 | ✅ 45 passed |
| 26-time-to-peak | `timetopeak.py`, `demo.py`, `test_timetopeak.py` | 399 | 41 | ✅ 41 passed |
| 27-feed-resilience | `resilience.py`, `demo.py`, `test_resilience.py` | 509 | 43 | ✅ 43 passed |
| 28-signal-refinery | `refinery.py`, `demo.py`, `test_refinery.py` | 421 | 38 | ✅ 38 passed |
| 29-momentum-scanner | `momentumscan.py`, `demo.py`, `test_momentumscan.py` | 350 | 38 | ✅ 38 passed |
| 30-verdict-protocol | `verdict.py`, `demo.py`, `test_verdict.py` | 401 | 38 | ✅ 38 passed |

Every toolkit is at or above the 35-test floor (minimum: 38). Suites grew
from the pre-expansion counts (6–25 for toolkits 01–13) by adding module
capability and pinning new boundaries — not by padding.

Study layer (not covered by `run_all_tests.sh`):
- `study/notes/` — 30 notes, one per toolkit, 5,285 lines total.
- `study/exams/` — 30 per-toolkit exams (20–22 questions each) +
  `final-exam-200.md` (200 questions, all 30 toolkits, 1,544 lines) +
  `final-exam-200-result.md` (the graded 2026-10-20 sitting record);
  5,534 lines total. Every exam states its ≥85% pass bar and carries a
  separated answer key + grading rubric.
- `study/real-data-summary.md` — aggregate-only results of the real-data
  adapters (toolkits 08/12/13 over the live bot's real records; full
  outputs are kept outside this repo).

Gap-closing verification (2026-10-20):
- Final exam sat closed-book from a questions-only extract and graded
  by a separate agent against the key: **199/200 (99.5%), PASS** —
  record in `study/exams/final-exam-200-result.md`.
- The bot's `scripts/bot-self-audit` runs this full suite at most once
  per 24h (stamp `~/.forex-agent/toolkit_last_check.json`) as a
  report-only check; verified live (first run executed, second run
  skipped as not due).

Root files: `README.md` (library overview + 30-toolkit map), `MANIFEST.md`
(this file), `run_all_tests.sh` (per-toolkit runner with summary; exit 1
if any suite fails or collects no tests).

Constraint audit (2026-10-20, post-expansion): import scan across all
Python files — only stdlib (`math`, `random`, `statistics`, `hashlib`,
`gzip`, `json`, `os`, `re`, `copy`, `enum`, `dataclasses`, `collections`,
`contextlib`, `tempfile`, `pathlib`, `types`, `typing`) + `pytest` in
tests + local module imports. No `urllib`/`socket`/`requests`/`http`
anywhere. (`gzip` appears only in toolkit 27, compressing synthetic
payloads locally to prove the encoding lesson — no network.)
