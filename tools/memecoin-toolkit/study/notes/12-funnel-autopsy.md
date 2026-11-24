# Funnel Autopsy — study notes

## Core ideas (teach-back)

When a runner is missed, the lazy post-mortem is "the funnel failed."
This toolkit forces a sharper question: *which* failure was it — or
was it a failure at all? Given one token's snapshot timeline and its
gate log, `classify(case)` returns exactly one of six verdicts, using
a fixed precedence order, because real cases overlap and the first
matching story must win on purpose, not by accident [S].

Everything starts from one **runner bar**, applied identically
everywhere: in a *single* snapshot, liquidity ≥ **$25,000** AND
market cap ≥ **$50,000** AND momentum positive. Both thresholds are
inclusive (exactly $25,000 / $50,000 passes; one dollar below fails),
and the three conditions must coincide in the same print — a token
that has the liquidity at t=10 and the momentum at t=30 never met
the bar [S].

The six classes, in precedence order:

1. `never-met-bar` — no snapshot ever clears the bar. Not a miss.
2. `gate-correctly-blocked-rug` — the token was a rug (by outcome),
   or safety **failed at a bar-meeting snapshot**. The funnel did
   its job; blaming it here teaches it to admit rugs.
3. `invented-ceiling-block` — a gate-log reason cites a home-grown
   gain ceiling (`ceiling`, `max-gain`, `max_gain`, `max gain`,
   `gain cap`, case-insensitive). A rule we invented killed a real
   runner: a bug, not bad luck.
4. `burned-one-shot-convergence` — a convergence signal was blocked
   early and **never retried strictly afterwards**, while safety was
   known-ok at some point in the timeline. The one shot was the only
   shot, and it was spent before the token ran.
5. `safety-unknown-lag` — the bar was met, but safety was `unknown`
   at *every* bar-meeting snapshot. The data simply never arrived.
6. `caught-or-other` — none of the above: caught, carded late, or a
   story this classifier honestly does not know.

Reporting helpers (`explain`, `first_bar_time`,
`bar_duration_minutes`, `safety_at_first_bar`, `blocking_events`,
`summarise`) only expose the evidence; they never change the
verdict. `summarise` counts over all six classes, zeros included, so
a report shows the full verdict space [S].

This mirrors the live operation's manual autopsy discipline whenever
a missed runner is surfaced: check the bar first, then walk the gate
history — a discipline with real history behind it, including a
runner that died on an invented 300% gain ceiling removed the same
day [M].

## Key derivations & formulas

There is no continuous math here; the "derivation" is the ordered
decision procedure itself. Step by step, `classify` computes:

```
snapshots   = normalised timeline rows (t, liq, mcap, momentum, safety)
bar_meeting = [s for s in snapshots if meets_bar(s)]

meets_bar(s) ⇔ s.liq ≥ 25_000 AND s.mcap ≥ 50_000 AND s.momentum
```

Then the precedence chain, first hit wins:

```
if bar_meeting is empty:                       → never-met-bar
elif outcome is a rug synonym/dict-rug
     OR any bar-meeting snapshot safety==failed: → gate-correctly-blocked-rug
elif any gate-log reason contains a ceiling word: → invented-ceiling-block
elif safety known_ok somewhere in the timeline
     AND a convergence-lane block exists
     AND no retry action at t strictly > first block: → burned-one-shot-convergence
elif every bar-meeting snapshot has safety==unknown: → safety-unknown-lag
else:                                          → caught-or-other
```

Details that decide borderline cases [S]:

- Rug outcomes accept synonyms (`rug`, `rugged`, `flagged_rug`,
  any case) and dict shapes (`{"rug": True}`, `{"status": "rug"}`).
  A *missing* outcome key is not a rug.
- A safety failure at a snapshot **below** the bar does not count
  as a rug block — the rug branch only inspects bar-meeting prints.
- Retry actions are `retry / retried / fired / allowed / carded /
  card`; block actions include `blocked / block / fired_and_blocked
  / convergence_blocked`. Lane and action matching is
  case-insensitive, and a retry in a *different* lane still counts.
- A same-minute retry (t equal to the first block) does **not**
  un-burn the shot; the retry must be strictly later.
- `bar_duration_minutes` is `max(t) − min(t)` over bar-meeting
  snapshots, and 0.0 for a single print — a one-print runner gave
  the funnel an instant, not a window.
- Fail-closed: an empty case, or one with missing keys, classifies
  as `never-met-bar`.

## Worked example

The six synthetic fixtures classify one per class [S]:

- **DACITY-like → `burned-one-shot-convergence`.** Timeline: bar
  first met at t=5 ($40k liq / $120k mcap), then t=10 and t=40 —
  3 bar-meeting prints spanning **35 minutes**, peaking near $1.6M
  mcap. Gate log: one convergence block at t=1 ("safety unknown at
  convergence"), never retried. Safety is `unknown` at the first bar
  print but `known_ok` by t=10 — that later known-ok is exactly what
  promotes it from safety-lag to burned: the signal could have been
  retried once safety landed, and wasn't.
- **Doomsday-like → `invented-ceiling-block`.** $96k liquidity,
  $1.14M mcap, safety known-ok, blocked only by the reason
  "h24 gain 2540% exceeds 300% ceiling". Every real gate passed;
  only our invented cap fired.
- **dust-that-went-nowhere → `never-met-bar`** (liquidity, mcap and
  momentum never coincide; its t=30 print even has momentum off).
- **fast-runner-slow-safety → `safety-unknown-lag`** (bar met at
  t=3 and t=9, safety `unknown` at both, no convergence story).
- **correctly-blocked-rug → `gate-correctly-blocked-rug`** (safety
  `failed` at bar-meeting prints, outcome `rug`).
- **ordinary-caught-runner → `caught-or-other`**.

`summarise(ALL_CASES)` therefore returns exactly one of each class.

## Trap log

- T1. Blaming the funnel for a correct rug block. If safety failed
  at the bar or the outcome was a rug, the block was the system
  working — "fixing" it admits the next rug.
- T2. Calling dust a miss. If the three bar conditions never
  coincide in one snapshot, there was never a runner to miss, even
  if the token later rugged or spiked on some other axis.
- T3. Splitting the bar across time. Liquidity now plus momentum
  later is not a bar-meeting token; simultaneity is the whole rule.
- T4. Treating a gain ceiling as a safety gate. A ceiling on *how
  much it already rose* is a home-grown rule, and this taxonomy
  ranks it above bad-luck stories precisely so it gets fixed first.
- T5. Assuming a blocked signal retries itself. One-shot lanes burn
  silently: blocked at t=1, token runs for 35 minutes, nobody ever
  looks again.
- T6. Reading `safety-unknown-lag` whenever safety was unknown at
  first bar. If safety later became known-ok and a convergence shot
  was burned, the verdict is the burned class — the precedence
  order exists to force that distinction.
- T7. Letting a safety failure *below* the bar convict the funnel.
  Only failures at bar-meeting snapshots count toward the rug
  class; sub-bar noise is not evidence about the runner decision.

## Source flags

- Bar thresholds ($25k / $50k, inclusive), the six classes, the
  precedence chain, synonym/ceiling-word matching, and the
  strictly-after retry rule are implemented in `autopsy.py` and
  covered by `test_autopsy.py` (44 tests) [S].
- The two headline fixtures are synthetic shapes echoing real miss
  structures, with invented numbers, per `fixtures.py` [S].
- The live-bot autopsy habit and the same-day removal of the real
  300% ceiling are reported in the README [M].
- The reading "precedence = triage priority" (fix rule bugs before
  luck stories) is my synthesis of the ordering rationale [R].

## One-paragraph verdict

This toolkit proves that "we missed it" is not one failure but at
least five different ones with different owners — no miss at all, a
correct rug block, an invented rule, a burned one-shot signal, and
safety data that never arrived — and that a fixed precedence order
can separate them mechanically from a timeline plus a gate log. It
does **not** prove any real token was misclassified (all fixtures are
synthetic), it cannot see causes outside its six classes (they fall
into `caught-or-other` by design), and it judges the funnel against
the $25k/$50k/rising bar only — a token can clear that bar and still
have been a bad trade.
