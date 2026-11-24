# Deployer Forensics — study notes

## Core ideas (teach-back)

Before asking "is this token good?", ask "who launched it, and what
does their record actually support saying about them?" This toolkit
grades a token **deployer** (creator) from their launch history —
each record a `Launch(deployer, mint, name, timestamp_hours,
funded_by, outcome)` with outcome `rug / runner / flat / unknown` —
into exactly one of four verdicts [S]:

- **UNKNOWN** — no history at all, or a history whose outcomes are
  *all* unknown. The headline principle: **unknown is not clean**.
  Absence of evidence is not evidence of a clean creator.
- **SERIAL_RUGGER** — the conviction bar: **≥ 3 launches AND ≥ 2
  rugs**. Both halves required.
- **WATCH** — worth attention, not conviction: any single rug below
  the serial bar, heavy funding-source reuse, heavy name-pattern
  reuse, or a record too thin or too incomplete to clear.
- **CLEAN** — earned only with real evidence: **≥ 2 launches, every
  outcome known, zero rugs, and no reuse flags**. Anything less
  stays WATCH or UNKNOWN.

Only `SERIAL_RUGGER` triggers the veto: `should_veto()` is true for
that verdict alone, and `blacklist_candidates()` lists exactly
those deployers. WATCH and UNKNOWN *inform without convicting* —
and a non-veto must never be read as "clean" [S].

Around the verdict sits a **fingerprint**: launch count, known
outcomes, rug count, distinct funding sources, funding-reuse ratio,
name-reuse ratio, median hours between launches (cadence), and
distinct name patterns. No single feature is trusted alone; reuse
features can push a zero-rug deployer to WATCH but can never convict
or veto by themselves [S]. Construction is fail-closed: a `Launch`
with an empty deployer or mint, a negative timestamp, or an outcome
outside the documented set raises `ValueError`.

This mirrors the live veto engine's repeat-rug deployer check: a
repeat rugger is vetoed outright (no score overrides it), while one
rug or a recycled pattern is a warning to weigh [M].

## Key derivations & formulas

**Rug rate — over known outcomes only:**

```
known      = launches with outcome ∈ {rug, runner, flat}
rug_rate   = rug_count / known_count
             → None when known_count = 0
```

Returning `None` instead of 0.0 is the point: no caller can read
missing evidence as a 0% rug rate [S]. (Three launches, three
unknown outcomes → rug rate `None`, verdict UNKNOWN.)

**Serial bar.**

```
is_serial_rugger ⇔ launch_count ≥ 3 AND rug_count ≥ 2
```

Two launches, both rugs: **not** serial (WATCH) — the launch-count
half protects against convicting on a sample so thin that two bad
outcomes could be circumstance rather than a pattern [R, from the
rule structure in S].

**Fingerprint ratios** (records sorted by time first, so the
fingerprint is order-insensitive) [S]:

```
funding_reuse = (launches whose funder funded >1 of this creator's
                 launches) / launch_count
name_reuse    = 1 − distinct_name_patterns / launch_count
median_interval = median of consecutive timestamp gaps (None if < 2 launches)
```

**Name normalisation** (`name_pattern`): lowercase, strip
punctuation/non-alphanumerics, strip trailing digits/separators —
so `"MoonCat"`, `"MoonCat 2"` and `"MoonCat 2!!"` collide into one
pattern. An unusable name becomes `"<empty>"`. Note it strips
trailing *digits and spaces* only: `"MoonCat IV"` normalises to
`"mooncat iv"`, a distinct pattern — the fixture's numbers depend
on exactly that [S].

**Verdict procedure**, in order:

```
no records                              → UNKNOWN
serial bar met                          → SERIAL_RUGGER
known_outcomes = 0                      → UNKNOWN
rug_count ≥ 1                           → WATCH
rug_rate ≥ 0.25                         → WATCH
funding_reuse ≥ 0.5 (and ≥ 2 launches)  → WATCH
name_reuse    ≥ 0.5 (and ≥ 2 launches)  → WATCH
launches < 2 OR known < launches        → WATCH
otherwise                               → CLEAN
```

## Worked example

The mixed synthetic history — four deployers, four verdicts [S]:

- **SerialSam → SERIAL_RUGGER.** 4 launches (MoonCat, MoonCat 2,
  MoonCat 3!, MoonCat IV), 3 rugs, rug rate **75%**, one funding
  source, funding reuse **100%**, name reuse **50%** (patterns:
  `mooncat` ×3 and `mooncat iv` ×1 → 1 − 2/4), median interval
  **6.0h** (gaps 5, 6, 9). On the blacklist; `should_veto` true.
- **HonestAda → CLEAN.** 3 launches, all known, 0 rugs, rug rate
  **0%**, 3 distinct funders, funding reuse 0%, name reuse 0%,
  median interval **130.0h** (gaps 100, 160).
- **OneRugRita → WATCH.** 2 launches, 1 rug, rug rate **50%**, one
  funder (reuse 100%), name reuse 50% (LuckyFrog / LuckyFrog 2
  collide), median interval 30.0h. One rug + total reuse = watched,
  not vetoed.
- **GhostGary → UNKNOWN.** 1 launch, outcome `unknown`, rug rate
  **unknown** (None) — and the demo makes the point explicitly:
  vetoing a total stranger returns False, *but their verdict is
  UNKNOWN, not CLEAN* [S].

## Trap log

- T1. Reading "no blacklist hit" as "clean creator". A stranger is
  UNKNOWN; the veto list names only the convicted. Silence is not
  clearance.
- T2. Computing rug rate over *all* launches. Unknown outcomes in
  the denominator dilute real rugs and fabricate reassurance; the
  denominator is known outcomes only, and the all-unknown case is
  `None`, never 0%.
- T3. Convicting on two rugs in two launches. That is WATCH. The
  ≥3-launch half of the serial bar exists so a pattern, not a pair
  of bad days, earns a veto.
- T4. Calling one clean launch CLEAN. One known non-rug launch is
  too thin and lands on WATCH (launches < 2); CLEAN needs ≥ 2
  launches with *every* outcome known.
- T5. Ignoring reuse because "no rugs yet". A creator recycling one
  funding wallet and one name pattern across launches is showing
  you a production line; reuse ≥ 0.5 earns WATCH with zero rugs.
- T6. Over-trusting name matching. Normalisation catches
  "MoonCat 2!!" but not "MoonCat IV" (letters survive), and a
  legitimate creator running an honest series could trip the same
  reuse flag — which is why reuse warns and never vetoes.
- T7. Treating cadence as character. A 6h median interval fits the
  serial fixture and a 130h interval the clean one, but cadence is
  a fingerprint feature, not a verdict input — fast is not guilty.

## Source flags

- Verdict definitions, the serial bar (≥3 / ≥2), CLEAN's evidence
  requirements, reuse thresholds (0.5 / 0.5, rug-rate flag 0.25),
  the `None` rug-rate rule, name normalisation, and all fixture
  numbers are from `deployer.py`, covered by `test_deployer.py`
  (46 tests) [S].
- The "pattern vs circumstance" rationale for the launch-count half
  of the bar is my reading of the rule's structure [R].
- The live veto-engine parallel (repeat-rug veto, no score
  override) is per the README [M].

## One-paragraph verdict

This toolkit proves that deployer history can be graded fail-closed
with a small, auditable rule set: unknown never promotes to clean,
conviction requires both repetition (≥3 launches) and harm (≥2
rugs), cleanliness must be *earned* with complete known history,
and soft signals (funding/name reuse, cadence) warn without
convicting. It does **not** prove any real deployer is a rugger —
all histories are synthetic — it cannot see a creator's *first*
rug before it happens (a stranger stays merely UNKNOWN), and a
determined serial rugger who rotates funders, names, and wallets
defeats exactly the fingerprints this lab measures.
