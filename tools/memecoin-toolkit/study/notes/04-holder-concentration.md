# Holder Concentration — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

Ask "who holds this token?" naively and you get a scary answer
for almost every healthy token: the biggest account is usually
the liquidity pool or the bonding curve itself, holding 40% or
more of supply. That account is not a person about to dump on
you — it is a program. This lab in `holders.py` computes
concentration the honest way: over user wallets only [S].

Classification is the whole game. Each account is looked up in
an injected registry: an account is a `"user"` only if its entry
says `on_curve=True` (a normal ed25519 wallet) and it is not
program-owned. Off-curve or program-owned accounts classify as
`"program"` and leave the numerator entirely. Accounts missing
from the registry, or with malformed entries (non-boolean
`on_curve`, non-dict entry), classify as `"unknown"` — never
assumed to be users. Excluding an unknown from the numerator is
the fail-closed direction: you cannot manufacture a whale out of
an account you could not classify, and you cannot hide one
either without the counts showing it [S].

Over user wallets only, two numbers are computed: top-1 (the
largest single user's share of total supply) and top-10 (the ten
largest users summed). One wallet can appear in several token
accounts, so balances are aggregated per address before ranking
— otherwise a whale split across accounts looks like many small
holders [S].

Verdicts use strict comparisons, which makes the boundaries
exact and testable [S]:

- FAIL if top1 > 50% or top10 > 70%.
- FLAG if top1 > 25% or top10 > 55%.
- Otherwise PASS.
- Exactly 50.0 / 70.0 is FLAG; exactly 25.0 / 55.0 is PASS.

FLAG still counts as passed — it is a warning band, not a block.
And unmeasurable input — empty holders, non-positive or missing
supply, zero classifiable user wallets — returns verdict
`"unknown"` with passed=False, percentages None. No data is a
block, never a quiet PASS [S].

Beyond top-1/top-10 the lab generalises: top-N for any n, a
Herfindahl index over user shares, an effective holder count,
and a Nakamoto coefficient — four lenses on the same distribution,
each fail-closed to None on unusable input [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

User percentages [S]:

    top1 %  = 100 × (largest aggregated user balance) / supply
    top10 % = 100 × (sum of 10 largest user balances) / supply
    top_n % = same, generalised — n must be a positive int

Verdict function (all comparisons strict) [S]:

    if top1 > 50 or top10 > 70:  FAIL
    elif top1 > 25 or top10 > 55: FLAG
    else: PASS

Herfindahl–Hirschman Index over aggregated user shares, on the
 antitrust 0–10000 scale [S]:

    HHI = Σ (share_i in %)²
    monopoly (one holder, 100%) → 10000
    ten equal holders (10% each) → 10 × 100 = 1000

Effective holder count — inverse Simpson over shares normalised
*among user holders* [S]:

    p_i = balance_i / Σ user balances
    effective_count = 1 / Σ p_i²

Two equal holders give exactly 2.0 whatever their combined share
of supply — it measures spread among users, not coverage of
supply. All-zero balances → None [S].

Nakamoto coefficient [S]:

    smallest k such that the top-k aggregated holders'
    cumulative share is STRICTLY greater than threshold_pct
    (default 50.0) — exactly 50.0% is not a majority.
    None when the threshold is never exceeded.

Input hardening shared by all functions: negative, NaN, or
infinite amounts are malformed rows (never negative
concentration); a NaN/infinite supply fails closed exactly like
a missing one; numeric-string amounts and supplies are accepted;
malformed rows are skipped but counted separately from excluded
(program) and unknown rows [S].

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Verbatim from `demo.py` [S]:

Scenario 1 — big pool, spread-out users:

- Naive top-1 (pool included): 40.0% of supply.
- User top-1: 6.0% | top-10 users: 12.0% → PASS (passed=True).
- Excluded=1 (the pool), unknown=0.

The naive number says "whale"; the honest number says "fine".
That 34-point gap is the entire reason this toolkit exists [R].

Scenario 2 — one user wallet quietly holds the majority:

- Naive top-1: 52.0%; user top-1: 52.0% | top-10: 57.0%.
- Verdict FAIL (passed=False). Here the whale is real: 52 > 50.

Scenario 3 — no data at all: top-1/top-10 n/a, verdict
`"unknown"`, passed=False. Fail-closed, not PASS [S].

Boundary fixtures from the tests [S]: top1 = 50.0 → FLAG,
50.01 → FAIL; top10 = 70.0 → FLAG, 70.01 → FAIL; 25.0 / 55.0 →
PASS. Top-10 alone can trigger FLAG and FAIL with a modest top-1
(e.g. top1 9% with top10 72% → FAIL) — a coordinated-looking
crowd, not a single whale. With a single user, top1 == top10.

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Reading raw largest-accounts as whales. The top account is
  normally a pool/curve PDA. Exclude program accounts first or
  every liquid token looks whale-controlled.
- T2. Assuming unregistered accounts are users. Absent from the
  registry means unknown, excluded and counted — never silently
  added to the user numerator.
- T3. Fuzzy boundaries. "About half" is not a rule: 50.0 is FLAG
  and 50.01 is FAIL, by strict comparison, on purpose.
- T4. Judging top-1 alone. Ten wallets at 7% each pass a top-1
  check and fail top-10 — distribution shape matters, not just
  the biggest hat.
- T5. Counting token accounts instead of wallets. Duplicate rows
  per address must be aggregated first, or split whales vanish.
- T6. Accepting an empty holder list as "no concentration".
  Empty/unmeasurable is unknown with passed=False.
- T7. Letting NaN/negative amounts through. They are malformed
  rows; a negative balance would fake *lower* concentration.
- T8. Confusing the metrics. HHI reacts to the whole tail,
  effective count normalises among users only, Nakamoto asks
  "how few collude for a majority" — different questions.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Classification rule (on-curve + not program-owned), strict
  verdict thresholds 50/70 and 25/55, aggregation, HHI /
  effective-count / Nakamoto definitions: [S] — 45 tests.
- Demo scenarios (40% naive vs 6% user top-1; 52% FAIL;
  no-data unknown): [S] — verbatim captured output.
- Live distribution proof check applying the same discipline —
  percentages only mean something with program accounts out of
  the numerator, unknown blocks: [M] — per README.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves concentration can be measured without the
pool-as-whale illusion: classify accounts, aggregate per wallet,
then apply exact strict thresholds — plus three distribution
lenses (HHI, effective count, Nakamoto) that agree with the
top-1/top-10 verdicts on hand-built fixtures across 45 tests
[S]. It does not prove the holders it sees are independent
people: the registry is an injected input, wallets under one
operator still count as many users here (that is the bundle
lab's problem), and a PASS says the *measured distribution* is
spread out — nothing about who funded those wallets [R].
