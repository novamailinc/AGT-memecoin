# Rug Taxonomy — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

"Rug" is a slang word for several mechanically different ways a
token dies at its holders' expense. This lab in `ruglab.py` names
five of them and implements each as a small detector over one
shared input: a lifecycle — a list of snapshot dicts carrying
timestamp, price, liquidity, holders, supply, mint authority,
sell-simulation result, volumes, unique buyers, and trades [S].

The five patterns, in plain words [S]:

1. LP pull — the liquidity vanishes and the price collapses
   with it, in the same short window. Liquidity down ≥80% and
   price down ≥50% within ≤3600 seconds (or ≤3 steps when
   timestamps are absent).
2. Mint-and-dump — new supply is printed (>20% growth while the
   mint authority is active) and the price then falls >50%.
   Inflation first, collapse after; order matters.
3. Can't-sell — a sell simulation explicitly fails. You could
   buy; you were never going to be able to sell.
4. Wash skew — big volume that is not real demand: volume above
   $10,000 but fewer than 0.1 unique buyers per trade, while the
   holder count barely moves (<2% vs the first snapshot). The
   same few wallets trading with themselves.
5. Holder crash — holders flee in one step: ≥40% drop between
   two *consecutive* snapshots.

Two design ideas hold the lab together. First, conjunctions:
most detectors need two or three conditions at once (an 80%
liquidity drop with price intact does not fire LP pull — that
could be a migration; the price collapse is what makes it a
pull). Loose single-signal rules would cry wolf constantly [S].

Second, missing data never fires. A snapshot lacking the field
a rule needs is skipped; NaN/infinite fields are treated
exactly like missing ones. Can't-sell is the sharpest case: it
fires only on `sell_sim_ok is False` — a real boolean False.
None, 0, `"false"`, and 1 do not fire. Absence of a sell-sim
result is unknown, not failure [S].

Each detector returns `(triggered, evidence)` — the numbers
that fired, not just a boolean — and all five always run:
`run_detectors` on an empty lifecycle returns five non-firing
results rather than raising [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

All thresholds are named constants at the top of `ruglab.py`
[S]: LP_DROP_PCT 80.0, LP_PRICE_DROP_PCT 50.0, LP_WINDOW_SEC
3600, LP_WINDOW_STEPS 3, MINT_INFLATION_PCT 20.0,
MINT_PRICE_DROP_PCT 50.0, HOLDER_CRASH_PCT 40.0,
WASH_VOLUME_THRESHOLD 10,000, WASH_UNIQUE_RATIO 0.1,
WASH_HOLDER_CHANGE_PCT 2.0.

Per-detector firing rules — note strict vs inclusive, which is
where the tests live [S]:

    lp_pull:        liq_drop ≥ 80% AND price_drop ≥ 50%
                    (inclusive both), same snapshot pair,
                    within 3600s or 3 steps
    mint_and_dump:  supply growth STRICTLY > 20% with mint
                    authority active on either snapshot of the
                    pair, AND price fall STRICTLY > 50% at/after
                    the inflation snapshot vs before it
    cant_sell:      any snapshot with sell_sim_ok is False
    wash_skew:      buy_vol + sell_vol > $10,000 (strict)
                    AND unique_buyers / trades < 0.1 (strict)
                    AND |holder change vs first| < 2% (strict)
    holder_crash:   consecutive drop ≥ 40% (inclusive),
                    drop % = 100 × (h_prev − h_next) / h_prev

The asymmetry is deliberate: exactly +20% inflation does not
fire mint-and-dump, exactly −50% price after inflation does not
fire it either, but exactly −80%/−50% *does* fire LP pull [S].

Combined score — the detectors stay independent and binary;
this layer only weighs co-occurrence [S]:

    DETECTOR_WEIGHTS = {lp_pull: 35, mint_and_dump: 30,
                        cant_sell: 25, holder_crash: 20,
                        wash_skew: 15}
    rug_score = min(100, Σ weights of triggered detectors)
    all five raw = 125 → capped at 100; clean = exactly 0

Risk grade bands [S]:

    0 → "clean"   1–24 → "watch"
    25–49 → "elevated"   50–100 → "severe"
    (non-numeric or out-of-range scores raise ValueError)

`assess` bundles `triggered`, `score`, `grade`, and
`detectors_fired` into one summary, consistent by construction
because all four derive from the same detector pass [S].

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Verbatim from `demo.py` [S]:

Scenario 1 — healthy lifecycle: nothing fires. Each detector
reports its non-firing reason ("no >=80% liquidity drop with
>=50% price collapse in window", etc.). Score 0, grade clean.

Scenario 2 — liquidity pulled, price collapses:

- `[X] lp_pull: liquidity -92.0% ($100,000 -> $8,000) and
  price -80.0% within short window (snapshots 0->2)`
- The other four stay silent — the fixture is a pure LP pull,
  and the taxonomy says so instead of shouting "rug" five ways.

Boundary fixtures from the tests [S]: a 79% liquidity drop does
not fire; an 80%+ drop at 3601s does not fire but at exactly
3600s does; +20.1% inflation fires where +20.0% does not; wash
volume exactly $10,000 does not fire ($10,001 can), unique
ratio exactly 0.1 does not (0.099 can), holder change exactly
2.0% does not (1.8% can); holders 1000 → 600 fires holder
crash, 1000 → 601 does not. A crafted lifecycle fires all five
at once: raw score 125, capped 100, grade "severe" [S].

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. One-condition alarms. Liquidity down 85% with price down
  only 20% is not an LP pull here — the conjunction is the
  detector; dropping it manufactures false rugs.
- T2. Ignoring order in mint-and-dump. A price drop *before*
  the inflation does not count; the dump must follow the print.
- T3. Treating a missing sell-sim as a failed sell-sim. Only a
  real boolean False fires can't-sell; truthy/falsy lookalikes
  (0, "false") do not.
- T4. Calling a slow bleed a crash. Holders falling 40%+ over
  many small steps never fires holder_crash — consecutive-step
  is the definition; slow bleeds need a different detector.
- T5. Reading volume as demand. $50k of volume from a handful
  of wallets with flat holders is wash, not interest — check
  unique buyers per trade and holder movement together.
- T6. Assuming all thresholds compare the same way. LP pull and
  holder crash are inclusive (≥); mint inflation, mint price
  drop, and all three wash conditions are strict (>/<).
- T7. Summing scores past 100. Five fired detectors weigh 125
  raw; the cap at 100 keeps the grade bands meaningful.
- T8. Letting NaN sneak through comparisons. NaN fields are
  normalised to missing before any detector sees them.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- All five rules, constants, strict/inclusive comparisons,
  weights (35/30/25/20/15), cap at 100, grade bands: [S] —
  pinned by the 44-test suite including every boundary named.
- Demo scenarios (healthy = silent; −92%/−80% LP pull): [S] —
  verbatim captured output.
- Live bot spreading the same patterns across layers (sell-sim
  hard blocks, wash into the veto engine, collapses into
  post-entry monitoring): [M] — per README; lab isolates the
  pattern definitions only.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves five rug patterns can be specified tightly
enough to test at their exact edges: windows, conjunctions, and
strictness choices are explicit constants, evidence strings
show the firing numbers, and missing data provably never fires
— across 44 tests including an all-five fixture [S]. It does
not prove real rugs will present this cleanly: live lifecycles
are noisier, snapshots sparser, honest migrations can resemble
pulls, and the weighted score is a triage convenience layered
on binary detectors — the taxonomy classifies patterns in data
you give it; it cannot certify a token's future [R].
