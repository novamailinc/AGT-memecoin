# Token Lifecycle (lab)

A from-scratch, pure-stdlib lab for the phases a memecoin moves through: BONDING → GRADUATED → RUNNER → FADING → DEAD, plus UNKNOWN. Everything here is synthetic — no network, no live bot code, no real token data — so every classification can be checked by hand against the documented thresholds and every failure mode can be exercised safely.

## What it does

Module `lifecycle.py` — an enum `Phase` (`BONDING`, `GRADUATED`, `RUNNER`, `FADING`, `DEAD`, `UNKNOWN`), a frozen dataclass `Snapshot(curve_completion_pct, age_minutes, liquidity_usd, mcap_usd, momentum_pct, holder_trend_pct)` (any field may be `None`), and a frozen `TimelinePoint(timestamp_minutes, snapshot)`:

- `classify(snapshot) -> Phase` — one snapshot, one phase, rules applied in order: missing/invalid metric → `UNKNOWN`; collapse (liquidity < $1,000, or momentum ≤ −85% with holder trend ≤ −30%) → `DEAD`; curve completion < 100% → `BONDING`; graduated with momentum ≥ 15%, liquidity ≥ $25,000, mcap ≥ $50,000 and holder trend ≥ −5% → `RUNNER`; graduated but momentum ≤ −10%, holder trend ≤ −15%, or liquidity < $10,000 → `FADING`; otherwise → `GRADUATED`. Fail-closed: `None`, NaN, negative liquidity/mcap/age, or curve completion outside [0, 100] all yield `UNKNOWN`, never a guessed phase — and `UNKNOWN` is never a huntable runner (`is_runner_phase()` is true only for `RUNNER`).
- `classify_with_reasons(snapshot) -> Classification` — the phase plus the tuple of reasons (for `UNKNOWN`, the exact list of missing/invalid metrics).
- `snapshot_issues(snapshot) -> tuple[str, ...]` — the validation problems with a snapshot; empty means usable.
- `is_legal_transition(before, after) -> bool` / `transition_flag(before, after) -> str` — the legal-transition table (`LEGAL_TRANSITIONS`): no phase returns to `BONDING`, `RUNNER` never returns to `GRADUATED`, `BONDING` may die without graduating, `FADING → RUNNER` is a normal revival ("ok"), and `DEAD → RUNNER` is legal but flagged `"suspicious"` (dormant revivals happen, and deserve a second look). Raises `ValueError` on non-`Phase` inputs.
- `validate_timeline_phases(phases) -> list` — every non-"ok" transition in a phase sequence, as `(index, before, after, flag)`.
- `classify_timeline(points) -> list[Phase]` — phases for a whole timeline. Raises `ValueError` on non-monotonic timestamps.
- `time_in_phase(points, end_minutes=None) -> dict[Phase, float]` — minutes per phase; each point owns the interval to the next, and the last point owns nothing unless `end_minutes` is given. Raises `ValueError` if `end_minutes` precedes the last point.
- `phase_counts(points) -> dict[Phase, int]` / `first_phase_at(points, phase) -> float | None` — observation counts, and when a phase was first seen.
- Synthetic fixtures: `fast_rug_timeline()` (birth → dead in minutes, never graduates), `dac_timeline()` (bonding → graduated → runner → dead in under 2 hours), `slow_steady_runner_timeline()` (graduates, then runs for hours), `zombie_timeline()` (graduates, never runs, fades forever without dying).

`demo.py` classifies all four fixture timelines, prints time-in-phase for each, and shows the suspicious-revival flag and the fail-closed `UNKNOWN` path.

## Test suite

56 tests, all passing (`pytest -q`: "56 passed"). Covered: every phase from single snapshots; the runner bar piece by piece (liquidity floor and its exact boundary, mcap floor, momentum floor and its exact boundary, collapsing holders); dead beating bonding on a pre-graduation collapse; collapse requiring both price and holder flight; bonding beating runner-grade numbers pre-graduation; fail-closed `UNKNOWN` for each of the six metrics missing individually (parametrised), an empty snapshot, a `None` snapshot, NaN, negative liquidity, negative age, and curve completion over 100%; reasons explaining an `UNKNOWN` verdict; legal, illegal, self, suspicious (`DEAD → RUNNER`), and normal-revival (`FADING → RUNNER`) transitions; non-`Phase` inputs raising; timeline validation reporting only the bad steps; all four fixtures' exact phase sequences; time-in-phase arithmetic with and without an end time; non-monotonic timelines rejected; phase counts; first-phase lookup and its `None` case; and `TimelinePoint` validation.

## Captured output

Verbatim output of `python demo.py`:

```
=== Token lifecycle lab (synthetic timelines) ===

-- Fast rug: birth to dead in minutes --
t+     0 min  BONDING
t+     2 min  BONDING
t+     5 min  DEAD
t+     9 min  DEAD
Time in phase: BONDING 5m, DEAD 4m
Flagged transitions: none

-- DACITY-shape: bonding -> graduated -> runner -> dead in <2h --
t+     0 min  BONDING
t+    20 min  BONDING
t+    38 min  GRADUATED
t+    55 min  RUNNER
t+    80 min  FADING
t+   105 min  DEAD
Time in phase: BONDING 38m, GRADUATED 17m, RUNNER 25m, FADING 25m
Flagged transitions: none

-- Slow steady runner --
t+     0 min  BONDING
t+    30 min  GRADUATED
t+    60 min  RUNNER
t+   180 min  RUNNER
t+   360 min  RUNNER
Time in phase: BONDING 30m, GRADUATED 30m, RUNNER 300m
Flagged transitions: none

-- Zombie: fading forever --
t+     0 min  GRADUATED
t+   120 min  FADING
t+   480 min  FADING
t+  1440 min  FADING
t+  2880 min  FADING
Time in phase: GRADUATED 120m, FADING 2760m
Flagged transitions: none

-- Revival --
DEAD -> RUNNER flag: suspicious (legal, but a dormant revival deserves a second look)
RUNNER -> BONDING flag: illegal

-- Fail-closed --
Snapshot with missing metrics classifies as: UNKNOWN
Full snapshot classifies as: RUNNER
```

## What this mirrors in the live bot

This mirrors the live paper bot's launch-scanner phase reasoning: the part that distinguishes a token still on its bonding curve from one that has graduated, and a graduated token that is actually running — real liquidity, real market cap, rising price, holders holding — from one that is merely alive or already fading. It is also why the hunting lanes only act on the RUNNER phase: BONDING is unproven, GRADUATED is undecided, FADING is leaving, DEAD is gone, and UNKNOWN — a feed gap, not a phase — must never be treated as an opportunity. The suspicious DEAD → RUNNER revival flag mirrors the same scepticism the live system applies to dormant tokens that suddenly print again. It is a teaching/checking model of that reasoning only; it shares no code with the live scanner.

## Self-exam

1. Why must a snapshot with a missing metric classify as UNKNOWN rather than having the missing value treated as zero or skipped?
2. A pre-graduation token prints enormous momentum and liquidity numbers. Why is it still BONDING, and why does that ordering matter for a hunting lane?
3. What two conditions together count as a collapse (DEAD) even when liquidity is still high, and why is price alone not enough?
4. Why is DEAD → RUNNER kept legal-but-suspicious instead of being made simply illegal or simply normal?
5. In `time_in_phase`, why does the last point own no time unless an end time is supplied, and what would go wrong if it owned time indefinitely?
6. A token sits in FADING for two days without dying (the zombie fixture). Why is "not dead yet" not a buy signal?
