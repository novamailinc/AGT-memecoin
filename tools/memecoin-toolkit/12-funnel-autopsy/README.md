# Funnel Autopsy (lab)

A from-scratch, pure-stdlib lab for the post-mortem question every missed runner deserves: did the funnel never really have a chance, did it correctly keep out a rug, or did one of our own rules throw away a real runner? Given a token's snapshot timeline and gate log, the classifier returns exactly one of five failure/verdict classes — or an honest "caught-or-other" — using a documented precedence order, because real cases overlap and the first matching story must win on purpose, not by accident. Every case is a synthetic fixture; the two headline shapes merely echo the structure of real misses.

## What it does

- Applies one runner bar everywhere — liquidity ≥ $25,000, market cap ≥ $50,000, and positive momentum in the same snapshot — before calling anything a miss.
- Separates a correct rug block from a genuine miss, so a funnel is never blamed for doing its job.
- Names two self-inflicted wounds distinctly: a home-grown gain ceiling that blocks a qualifying runner, and a one-shot convergence signal that fires, is blocked early, and is never retried while the token runs for tens of minutes.
- Flags the quieter failure where the token clears the bar while safety data simply never arrives in time.

## Files

- `autopsy.py` — constants `LIQ_MIN = 25_000`, `MCAP_MIN = 50_000`, and `CLASSES` (the six return values); `meets_bar(snapshot) -> bool` requires all three bar conditions at once in the same snapshot, with both numeric thresholds inclusive; `classify(case) -> str` takes `{timeline, gate_log, outcome?}` with tuple or dict rows and checks, in order: `never-met-bar`, `gate-correctly-blocked-rug`, `invented-ceiling-block`, `burned-one-shot-convergence`, `safety-unknown-lag`, else `caught-or-other`. Rug outcomes accept synonyms (`rugged`, `flagged_rug`, any case) and dict shapes (`{"rug": True}`, `{"status": "rug"}`); ceiling reasons match `ceiling` / `max-gain` / `max_gain` / `max gain` / `gain cap`, case-insensitively; lane and action matching for the convergence rule is likewise case-insensitive, and a retry only counts strictly after the first block. Fail-closed: an empty or bar-less case (including one with missing keys) is `never-met-bar`, and a rug outcome or a failed safety report at a bar-meeting snapshot outranks every miss story except never meeting the bar — a safety failure at a snapshot below the bar does not count.
- `autopsy.py` — reporting helpers (never change the verdict): `bar_meeting_snapshots(case) -> list`, `first_bar_time(case) -> t | None`, `bar_duration_minutes(case) -> float` (0.0 for a single print or none), `safety_at_first_bar(case) -> str | None`, `blocking_events(case) -> list`, `has_ceiling_block(case) -> bool`, `retry_after_first_block(case) -> bool`, `explain(case) -> dict` (classification plus the evidence counts/times behind it), and `summarise(cases) -> dict` (counts keyed by all six classes, zeros included).
- `fixtures.py` — six synthetic cases: `DACITY_CASE`, `DOOMSDAY_CASE`, `NEVER_MET_CASE`, `SAFETY_UNKNOWN_CASE`, `RUG_CASE`, `CAUGHT_CASE`, plus `ALL_CASES`.
- `demo.py` — classifies all six fixtures and narrates the two headline shapes.
- `realdata.py` — real-data adapter: builds autopsy cases from the live journal's `judge` records (snapshots + record-level safety + outcome; momentum proxied as buys5m > sells5m, gate log empty because none is recorded) and runs the existing classifier over them. Paper closes record no snapshots and no gate log, so they are counted as *unclassifiable from recorded data* — never forced into a class — with a separate outcome-pattern tally. Two classes are structurally unreachable from the journal (no gate-log reasons are recorded) and the adapter says so. Sample fixtures: `sample_judge_records.jsonl`, `sample_paper_closes.jsonl` (real records, fields whitelisted).
- `test_autopsy.py` — the pytest suite below.
- `test_realdata.py` — adapter tests against the sample fixtures.

## Test suite

49 tests, all passing (44 in `test_autopsy.py`, 5 in `test_realdata.py`). Coverage: the bar constants and `meets_bar` needing all three conditions, the thresholds inclusive at exactly $25,000 / $50,000 and failing one dollar below, the three conditions needing to coincide in one snapshot (split across prints is never-met-bar), the `CLASSES` tuple containing exactly the six verdicts and every fixture classifying into it, one correct classification per class (DACITY-like → `burned-one-shot-convergence`, Doomsday-like → `invented-ceiling-block`, plus never-met-bar, safety-unknown-lag, rug blocked by failed safety, rug blocked by outcome alone, and caught-or-other), rug-outcome synonyms and dict-shaped outcomes, a safety failure below the bar not counting as a rug block, a missing outcome key not counting as a rug, all four ceiling-reason spellings plus case-insensitive ceiling matching, an ordinary block reason not counting as a ceiling, three precedence cases (never-met-bar beats a rug outcome, a rug beats a ceiling reason, a ceiling beats a burned convergence), the burned rule requiring safety known-ok somewhere (otherwise it is safety-unknown-lag), a same-minute retry not un-burning the shot, a retry by a different lane counting, a second block still counting as burned, case-insensitive convergence lane/action matching, safety-unknown judged only at bar-meeting snapshots, a retried convergence correctly not counting as burned, dict-shaped rows accepted, an empty case and a key-less case failing closed to `never-met-bar`, and the reporting helpers — `explain` evidence for the DACITY-like (3 bar prints, first at t=5, 35-minute window, safety unknown at first bar) and Doomsday-like cases, `first_bar_time`/`bar_duration_minutes` edges, `blocking_events`/`has_ceiling_block`/`retry_after_first_block`, and `summarise` counting one of each class over the six fixtures and all-zeros on empty input.

## Captured output

Verbatim output of `python demo.py` (exit 0):

```
=== Funnel autopsy demo (synthetic cases, paper only) ===
bar: liq >= $25,000 and mcap >= $50,000 and momentum positive
precedence: never-met-bar > rug > ceiling > burned > safety-lag

DACITY-like                outcome=runner  -> burned-one-shot-convergence
Doomsday-like              outcome=runner  -> invented-ceiling-block
dust-that-went-nowhere     outcome=flat    -> never-met-bar
fast-runner-slow-safety    outcome=runner  -> safety-unknown-lag
correctly-blocked-rug      outcome=rug     -> gate-correctly-blocked-rug
ordinary-caught-runner     outcome=runner  -> caught-or-other

Reading the two headline shapes:
- DACITY-like: convergence blocked once at t=1min, bar met for
  ~35 minutes afterwards, safety later known-ok, never retried.
- Doomsday-like: $96k liq / $1.14M mcap / safety ok, blocked only
  by a gate-log reason citing a 300% gain ceiling.

Demo complete.
```

## What this mirrors in the live bot

The live operation runs exactly this autopsy by hand whenever the user surfaces a missed runner: check whether the token ever met the runner bar (real liquidity, real market cap, actually rising), then walk the gate history for what actually stopped it. That discipline has real history behind it — a runner once died on an invented 300% gain ceiling that was removed the same day, which is why `invented-ceiling-block` exists as its own class and outranks bad-luck stories, and one-shot signals that never retry are a known way to burn a genuine runner. This lab encodes that checklist as a classifier over invented cases only.

## Self-exam

1. A token rugs and also never cleared the bar. Which class wins, and why is that ordering the honest one?
2. What two facts, together, turn an early convergence block into `burned-one-shot-convergence` instead of `safety-unknown-lag`?
3. Why does `invented-ceiling-block` rank above `burned-one-shot-convergence` when a case matches both?
4. The DACITY-like timeline has safety `unknown` at its first bar-meeting snapshot — why is it still not classified as `safety-unknown-lag`?
5. What would have to change in the Doomsday-like gate log for the verdict to become `caught-or-other`?
