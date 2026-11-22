# Token Lifecycle — study notes

## Core ideas (teach-back)

A memecoin is not one thing over its life; it passes through phases,
and each phase deserves a different action. This toolkit names six:
`BONDING → GRADUATED → RUNNER → FADING → DEAD`, plus `UNKNOWN` —
and classifies one snapshot into exactly one phase using six
metrics: curve completion %, age, liquidity, market cap, momentum
%, and holder trend % [S].

The phases, in plain language:

- **BONDING** — still on the bonding curve (completion < 100%).
  Unproven by construction, whatever its other numbers say.
- **GRADUATED** — off the curve, alive, but not yet running and not
  yet fading. Undecided.
- **RUNNER** — graduated *and* actually running: momentum ≥ **15%**,
  liquidity ≥ **$25,000**, mcap ≥ **$50,000**, holder trend ≥
  **−5%** (holders holding). This is the **only** huntable phase:
  `is_runner_phase()` is true only for `RUNNER` [S].
- **FADING** — graduated but going the wrong way: momentum ≤
  **−10%**, *or* holder trend ≤ **−15%**, *or* liquidity <
  **$10,000** (any one suffices).
- **DEAD** — collapsed: liquidity < **$1,000**, or the joint
  collapse of momentum ≤ **−85%** *and* holder trend ≤ **−30%**.
- **UNKNOWN** — any required metric missing or invalid. A feed gap,
  not a phase, and never an opportunity.

Rules are applied **in a fixed order** (validation → collapse →
bonding → runner → fading → graduated), and the order does real
work: death beats bonding (a pre-graduation collapse is DEAD, not
BONDING), and bonding beats runner-grade numbers (a token still on
the curve is BONDING even with enormous momentum and liquidity).

Timelines add a second layer: a **legal-transition table**. No phase
returns to BONDING; RUNNER never returns to GRADUATED; BONDING may
die without graduating; `FADING → RUNNER` is a normal revival
("ok"); `DEAD → RUNNER` is legal but flagged **"suspicious"** —
dormant revivals happen and deserve a second look. UNKNOWN may
appear before/after anything, because it is a feed state, not a
token state [S]. Helpers measure time-in-phase, phase counts, and
first-seen times over a timeline.

## Key derivations & formulas

Classification is an ordered predicate chain, not a score. Step by
step for one snapshot [S]:

```
1. snapshot_issues ≠ ∅  → UNKNOWN
   (any of the 6 fields None / NaN / non-numeric, curve outside
    [0,100], or negative age / liquidity / mcap)
2. liquidity < 1_000
   OR (momentum ≤ −85 AND holder_trend ≤ −30)        → DEAD
3. curve_completion < 100                            → BONDING
4. momentum ≥ 15 AND liquidity ≥ 25_000
   AND mcap ≥ 50_000 AND holder_trend ≥ −5           → RUNNER
5. momentum ≤ −10 OR holder_trend ≤ −15
   OR liquidity < 10_000                             → FADING
6. otherwise                                         → GRADUATED
```

Note the asymmetry, which is deliberate: RUNNER needs **all four**
conditions (AND); FADING needs only **one** of three (OR); the
price-and-holders collapse needs **both** legs (AND) — price alone
crashing is not death while holders stay, and holders leaving alone
is not death while price holds — but the liquidity leg of DEAD
(< $1,000) stands alone, because with no liquidity there is no
market left to argue about [R, from the rule structure in S].

Transition checking:

```
transition_flag(before, after):
    "suspicious" if (before, after) == (DEAD, RUNNER)
    "ok"         if after ∈ LEGAL_TRANSITIONS[before]
    "illegal"    otherwise
```

Non-`Phase` inputs raise `ValueError`; `validate_timeline_phases`
returns every non-"ok" step as `(index, before, after, flag)`.

Time accounting: each timeline point owns the interval until the
next point; the **last point owns nothing** unless `end_minutes` is
supplied (and it must not precede the last point). Non-monotonic
timestamps raise `ValueError`. This convention prevents an open
ended final observation from silently accruing infinite duration
[S].

## Worked example

The four synthetic fixture timelines, verbatim from the demo [S]:

- **Fast rug** — BONDING at t+0 and t+2, DEAD at t+5 and t+9
  (liquidity $400 < $1,000, momentum −92%, holders −60%). Time:
  BONDING 5m, DEAD 4m. It never graduated; death-before-graduation
  is a normal legal path.
- **DACITY-shape** — BONDING (0–38m) → GRADUATED at t+38 (curve
  100%, momentum only +8%, so not a runner) → RUNNER at t+55
  ($80k liq, $300k mcap, +80% momentum) → FADING at t+80
  (momentum −30%) → DEAD at t+105 (liquidity $500). Time: BONDING
  38m, GRADUATED 17m, RUNNER 25m, FADING 25m — the whole arc in
  under 2 hours.
- **Slow steady runner** — BONDING 30m, GRADUATED 30m, then RUNNER
  from t+60 through t+360: **300 minutes** of runner phase with
  holders intact.
- **Zombie** — GRADUATED 120m, then FADING for **2,760 minutes**
  (t+120 to t+2880) without ever dying: liquidity decays
  $12k → $4.5k, staying above the $1,000 dead floor. "Not dead yet"
  is a phase, not a buy signal.

Fail-closed demo: a snapshot with only curve and age set classifies
UNKNOWN; the full snapshot `(100, 30, 60000, 200000, 40, 12)`
classifies RUNNER. `DEAD → RUNNER` flags "suspicious";
`RUNNER → BONDING` flags "illegal" [S].

## Trap log

- T1. Treating a missing metric as zero or skipping it. Zero
  liquidity would shout DEAD; a skipped momentum check could invent
  a RUNNER. Missing means UNKNOWN — no verdict at all.
- T2. Hunting BONDING tokens with runner-grade prints. The ordering
  (bonding before runner) exists because curve tokens are unproven
  no matter how hot the snapshot looks.
- T3. Calling a token dead on price alone. The collapse rule needs
  momentum ≤ −85% **and** holders ≤ −30% together (or the liquidity
  floor); a flash crash with holders holding is not death.
- T4. Treating DEAD → RUNNER as either impossible or normal. It is
  legal-but-suspicious: revivals are real, and so are traps dressed
  as revivals.
- T5. Letting the last timeline point own open-ended time. Without
  an explicit end, durations inflate by however long you forgot
  about the token.
- T6. Reading the zombie as "due a bounce". Two days of FADING is
  evidence of leaving, not of value; the phase system gives it no
  path back except through an actual RUNNER print.
- T7. Acting on UNKNOWN because "the other numbers looked fine".
  UNKNOWN is never huntable — `is_runner_phase` is true for RUNNER
  only.

## Source flags

- All thresholds ($25k / $50k / +15% / −5% runner bar; −10% / −15% /
  $10k fading; $1k and −85%/−30% death), the rule order, the
  transition table, and the fixture timelines/timings are from
  `lifecycle.py`, exercised by `test_lifecycle.py` (56 tests) [S].
- The AND/OR asymmetry reading is derived from the rule structure
  [R]; the thresholds themselves are source constants [S].
- The mapping to the live launch-scanner's phase reasoning and its
  scepticism toward dormant revivals is per the README [M].

## One-paragraph verdict

This toolkit proves that a six-metric snapshot can be sorted into a
defensible lifecycle phase with fail-closed handling of missing
data, that rule *order* (death before bonding, bonding before
runner) carries real meaning, and that phase histories can be
audited for illegal or suspicious jumps. It does **not** prove the
thresholds are the right ones for live trading — $25k/$50k/+15% is
the runner bar as documented, not a validated optimum — and it
classifies snapshots, not intentions: a token can print RUNNER
honestly and still rug on the next print.
