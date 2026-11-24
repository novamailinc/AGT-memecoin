# Verdict Protocol — study notes

## Core ideas (teach-back)

Every trading lane eventually faces one meeting: fund it, keep fixing it, keep measuring it, or kill it.
This lab makes that meeting a procedure instead of an argument. Given a lane's record and four gates,
`decide(lane, gates)` returns one of four verdicts — `FUND-ELIGIBLE`, `KEEP-FIXING`, `KEEP-MEASURING`,
or `KILL` — always with reasons attached.

The protocol's spine is a refusal:

> Below a minimum sample there is no verdict to give, > only more measuring.

A win rate is a claim about a probability, and a claim needs enough observations to mean anything. The
toolkit quantifies "enough" for the funding claim at the centre of the live-mode question: telling a
**35%** win rate from a **30%** base rate, at one-sided alpha 0.05 with 80% power, takes **534 closes**.
That number, `REQUIRED_N_FULL`, is computed, not asserted — `required_n()` derives it from the normal
approximation (see below).

Three sample levels structure everything:

- `MIN_N_ANY_VERDICT = 20` — below 20 closes, no verdict at all: `KEEP-MEASURING`.
- `MIN_N_KILL = 50` — a deep-negative kill needs at least 50 closes.
- `REQUIRED_N_FULL = 534` — funding needs the full sample.

And one threshold for how bad is bad: `DEEP_NEGATIVE_EXPECTANCY = -0.20` per $1. Expectancy at or below
−0.20, on enough closes, is a kill.

The funding claim also needs the **four live gates**, all of them, named in `GATE_NAMES`:

1. `sustained_positive_expectancy`
2. `proven_entry_strategy`
3. `top_picks_winning`
4. `hardened_execution_safety`

Dropping any single gate revokes `FUND-ELIGIBLE`, even at 600 closes with +0.20 expectancy. The gates
are a conjunction, not a score.

Two asymmetries are deliberate and worth teaching:

- **Killing is cheaper than funding.** A structural kill or a deep-negative kill fires on far less evidence than the 534 closes funding demands. That is the right direction of asymmetry: the cost of funding a bad lane is money; the cost of killing a good one is opportunity — and lanes can be re-measured, while lost money cannot.
- **Calendar time is not sample.** `weeks_of_data` is recorded and reported in reasons, but it never substitutes for closes. A lane with 52 weeks and 5 closes is still `KEEP-MEASURING`, because a win-rate claim is made of *trades*, not of Tuesdays.

Every verdict renders with reasons — `render()` prints `"<lane>: <DECISION>"` followed by one ` 
- <reason>` line each. Even the refusals explain themselves: a reasoned `KEEP-MEASURING` ("18 closes < 20
minimum — verdict refused") tells the next decision-maker exactly what is missing and how far away it
is.

Inputs are fail-closed: `LaneRecord` validates every field (empty name, negative, non-int or boolean
closes, non-numeric expectancy, win rate outside `[0, 1]`, negative weeks, non-bool structural flag all
raise `ValueError`), and `decide` raises on wrong types.

## Key derivations & formulas

### The required sample size

`required_n(p1=0.35, p0=0.30, alpha=0.05, power=0.8)` uses the one-sided normal approximation:

```text
required_n = ceil( ( z_a * sqrt(p0(1−p0)) + z_b * sqrt(p1(1−p1)) )^2
                   / (p1 − p0)^2 )

where z_a = z(1 − alpha) ≈ 1.645,  z_b = z(power) ≈ 0.842
```

Step by step with the defaults:

```text
sqrt(p0(1−p0)) = sqrt(0.30*0.70) = sqrt(0.21)   ≈ 0.4583
sqrt(p1(1−p1)) = sqrt(0.35*0.65) = sqrt(0.2275) ≈ 0.4770
numerator      = (1.645*0.4583 + 0.842*0.4770)^2
               ≈ (0.7538 + 0.4014)^2 ≈ 1.1552^2 ≈ 1.3345
denominator    = (0.35 − 0.30)^2 = 0.0025
required_n     = ceil(1.3345 / 0.0025) = ceil(533.79) → 534
```

The z-values come from a stdlib inverse-normal approximation (Acklam's) inside the source. Two
properties matter more than the digits:

- The denominator is the *squared gap*. Halve the gap you are trying to detect and the required sample roughly quadruples — small edges are expensive to prove, arithmetically, not bureaucratically.
- The function fails closed: any probability outside `(0, 1)`, or `p1 <= p0`, raises `ValueError`.

`sample_sufficient(closes, level)` exposes the three levels — `"full"` (534), `"any"` (20), `"kill"`
(50) — and raises on unknown levels or negative closes.

### The decision matrix, in precedence order

```text
decide(lane, gates):
  1. lane.structural_dead
       -> KILL                                    (any sample, any gates)
  2. lane.closes < 20
       -> KEEP-MEASURING                          (verdict refused)
  3. lane.expectancy <= −0.20 AND lane.closes >= 50
       -> KILL
  4. lane.expectancy < 0 AND lane.closes < 534:
       closes < 50  -> KEEP-MEASURING
       closes >= 50 -> KEEP-FIXING                (negative, not deep)
  5. gates.all_green():
       closes >= 534 AND expectancy > 0 -> FUND-ELIGIBLE
       closes < 534                     -> KEEP-MEASURING
       otherwise                        -> KEEP-FIXING
  6. otherwise
       -> KEEP-FIXING, red gates listed by name
```

Precedence is the protocol. Structural death outranks the sample rules because a *proven-dead window* is
not a statistical claim that more data could overturn — measuring it further buys nothing. The
deep-negative kill sits *below* the 20-close refusal, so a lane at −0.50 with only 30 closes is
measured, not killed: it fails step 3's 50-close floor, and step 4 routes it to `KEEP-MEASURING`. The
kill floor exists to prevent executing a lane on what could still be a bad-luck streak; the cost of
waiting is bounded because deep-negative lanes rarely need the wait.

Note an edge the order creates: negative expectancy at ≥534 closes skips step 4 (its condition requires
closes < 534) and lands in step 5 or 6 — both of which return `KEEP-FIXING` for non-positive expectancy
unless step 3 already killed it. Either way, negative expectancy never funds — a test pins that at 1000
closes.

## Worked example

From `demo.py`. First the anchor number: `required_n()` → **534** closes to separate 35% from 30%.

Then the four fixtures, rendered verbatim in shape:

**`degen_v2` → KILL** (structural precedence). 120 closes, expectancy −0.100, win rate 0.28,
`structural_dead=True`, all gates red. Verdict: KILL — "structural kill: the lane's trading window is
proven dead". The middling statistics are irrelevant; structure outranks them.

**`steady_new` → KEEP-MEASURING** (the refusal). 18 closes, expectancy +0.05, win rate 0.39, one gate
green. Verdict: KEEP-MEASURING — "sample too small: 18 closes < 20 minimum — verdict refused", plus the
reminder that a full verdict needs 534.

**`main_lane` → KILL** (deep negative). 394 closes, expectancy **−0.375** per $1, win rate 0.22. 394 <
534, so it could never have funded — but −0.375 ≤ −0.20 at 394 ≥ 50 closes satisfies step 3: KILL —
"continued measuring spends attention, not information".

**`proven_lane` → FUND-ELIGIBLE** (the full conjunction). 600 closes, expectancy +0.18, win rate 0.41,
10 weeks, all four gates green. 600 ≥ 534 and expectancy > 0: FUND-ELIGIBLE, with all three facts stated
in the reasons.

**`promising` → KEEP-FIXING** (one red gate). 300 closes, expectancy +0.08, win rate 0.36, gates green
except `top_picks_winning`. Verdict: KEEP-FIXING — "red gates: top_picks_winning; expectancy positive
(+0.080) — fix the red gates, do not fund yet". Positive and promising is a fixing verdict, not a
funding one.

Boundary fixtures from the tests complete the picture: 19 closes measures, 20 earns a verdict;
deep-negative at exactly 50 closes kills; 533 closes with green gates measures, 534 funds.

## Trap log

- T1. **Verdicts on tiny samples.** An 18-close lane at 39% win rate *looks* like the best lane in the room. It is 18 coin flips. The protocol's first statistical act is refusing to rank it.
- T2. **"Keep measuring" a structurally dead lane.** If the window itself is proven dead — not unlucky, *dead* — more closes measure a corpse. Structural kills outrank sample rules precisely to stop sunk-cost measurement.
- T3. **Killing on a bad streak.** Deep-negative expectancy at 30 closes *feels* conclusive. The 50-close kill floor exists because at 30 closes it might still be variance; the protocol pays a bounded waiting cost to avoid executing lanes on noise.
- T4. **Waiving one gate "just this once".** Each gate guards a different failure — expectancy, entry proof, pick quality, execution safety. A conjunction waived once is a checklist abandoned; the test that drops each gate individually and revokes funding is the protocol defending itself.
- T5. **Substituting calendar time for closes.** "It's been running for a year" is not evidence about a win rate. Fifty-two quiet weeks contain fewer observations than one busy week; the sample is trades.
- T6. **Treating the 534 cliff as sacred.** 533 measuring and 534 funding is a convention, not a law of nature — the power calculation is continuous underneath. The defence of a sharp line is auditability: everyone can check which side a lane is on. Know that is the trade being made.
- T7. **Symmetric evidence bars.** Demanding the same proof to kill as to fund sounds fair and is backwards: the two errors cost different things. The protocol prices them differently on purpose.
- T8. **Bare verdicts.** A one-word KILL or a bare "insufficient data" cannot be audited, appealed, or learned from. Reasons are part of the verdict; a verdict that cannot show its working is an opinion with a stamp.

## Source flags

- All constants (534 / 20 / 50 / −0.20), gate names, the precedence order, and fixture values are from `verdict.py`. [S]
- The step-by-step sample-size arithmetic above reproduces the source's formula and lands on its pinned output of exactly 534; the toolkit's 38 tests also pin it inside 500–700 and growing as the WR gap shrinks, per the README. [S]/[R]
- The mirroring claims — the Oct 22 lane verdict day, the Dec 20 CEO fund/fix/kill verdict, and the four gates being the live-mode gates verbatim — are the README's live-bot account. [M]
- One soft spot, reported not fixed: the module comment for `MIN_N_ANY_VERDICT` says "no exceptions except structural kill needs review flag", but no review flag exists anywhere in the code — the structural kill simply returns KILL with reasons. The comment overpromises a mechanism the implementation does not have; behaviour is consistent, the comment is not. [S]
- The asymmetry defence (money vs opportunity) is reasoned here from the matrix's structure and the README's self-exam. [R]

## One-paragraph verdict

This toolkit proves a verdict procedure can be made explicit, ordered, and auditable: a computed 534-close bar for the funding claim (35% vs 30% win rate at alpha 0.05, power 0.8), a 20-close floor under any verdict, a 50-close floor under a deep-negative (−0.20) kill, structural death outranking all statistics, all four gates required jointly, and reasons rendered with every outcome — exercised at its exact boundaries by fixtures shaped like real lanes (a dead window killed, an 18-close lane refused, a 394-close lane at −0.375 killed, a 600-close all-green lane funded). It does not prove the live lanes' ledgers contain the numbers fed to it, that the four gates are the *right* gates, or that funding a FUND-ELIGIBLE lane would make money; it proves the decision, given the record, is the one the registered rules dictate — no more, and no quieter.
