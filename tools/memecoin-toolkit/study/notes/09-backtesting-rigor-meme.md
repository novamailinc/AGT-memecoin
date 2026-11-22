# Backtesting Rigor for Memecoin Research — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

The statistics lab showed *how much* data a claim needs. This
lab in `rigor.py` covers the five process failures that make
even big backtests lie — and builds a defence against each [S].

Defence 1: freeze the gate before you look. A `PreRegistration`
records the gate, its thresholds, and a timestamp — then
`freeze()` makes it immutable in depth. Not just polite
immutability: after the freeze, attribute assignment (including
flipping `_frozen` itself or inventing a new field), attribute
deletion, item assignment into `thresholds`, the dict mutator
methods, `set_threshold`, and even rewriting the caller's
original dict all fail, because the class is slotted (no
`__dict__` back door), thresholds are stored as a mapping proxy
over a private copy, and only a frozen registration can
`certify()` or be used by `evaluate_gate` [S].

Defence 2: split time-ordered data with an embargo.
`walk_forward` builds expanding-window train/test folds and
*deletes* a gap of samples between train and test, so adjacent
bars — which are correlated in any real series — cannot leak
across the split. `check_no_leakage` proves the gap between two
index sets and raises on overlap, disorder, or emptiness [S].

Defence 3: prefer plateaus over spikes. `parameter_plateau`
finds the best parameter, then measures the width of the
contiguous region around it keeping at least 80% of the best
expectancy. Width 1 is a spike — a magic number that works at
exactly one setting and dies live. Width ≥ 2 is the default
robustness bar (`is_robust_plateau`) [S].

Defence 4: haircut for every variant you tried.
`deflated_expectancy` subtracts a multiple-testing penalty
that grows with trials tried and shrinks with sample size. An
observed +0.30 survives 10 trials and is gone by 100 [S].

Defence 5: know what one look-ahead bug is worth.
`lookahead_experiment` injects dishonesty into pure noise: a
signal that peeks at the next bar manufactures roughly +0.40
expectancy while the honest current-bar signal finds ~0. If
your backtest shows a beautiful edge, this is the size of the
artefact you must first rule out [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

Multiple-testing penalty and deflation [S]:

    penalty(n_trials, n_samples)
        = sqrt(2 × ln(max(n_trials, 1))) / sqrt(n_samples)
    deflated = observed − penalty
    (assumes per-sample std 1.0 — documented in the docstring)

    observed +0.30, 100 samples:
      1 trial   → +0.300   (ln 1 = 0, no penalty)
      10 trials → +0.085
      100 trials → −0.003
      1000 trials → −0.072

`bonferroni_alpha(alpha, n_trials) = alpha / n_trials` is the
classical companion correction [S].

Plateau measurement [S]:

    best = max expectancy over the swept parameters
    if best ≤ 0 → width 0 (nothing to defend)
    region = contiguous parameters around the argmax with
             expectancy ≥ 0.8 × best
    width = len(region); ties expand from the first argmax

Walk-forward layout, 120 bars / 3 folds / embargo 5 [S]:

    fold 1: train 0..29  (30), test 35..59  (25), gap 5
    fold 2: train 0..59  (60), test 65..89  (25), gap 5
    fold 3: train 0..89  (90), test 95..119 (25), gap 5

Bars 30–34, 60–64, 90–94 appear in *no* fold's train or test —
they are the embargo, spent on separation. (One trailing
remainder bar can also go unused by the fold tiling.) [S]

Look-ahead bias [S]:

    bias = cheating_expectancy − honest_expectancy
    On iid noise the cheat earns E|next return|-style money:
    bias scales linearly in sigma, matching σ·√(2/π) theory —
    doubling sigma doubles the fake edge (test-pinned).

Registration fingerprint [S]:

    SHA-256 over a canonical serialisation of the contents;
    identical contents fingerprint identically regardless of
    dict order; any content change changes the fingerprint.

Gate evaluation: `evaluate_gate(reg, metrics)` requires every
threshold to be a *minimum* the same-named metric meets
(inclusive boundary passes); an unfrozen registration raises
`RuntimeError`, a missing/non-finite metric `ValueError` [S].

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Verbatim from `demo.py` [S]:

    registered: momentum lane: WR >= 35% and expectancy > 0
    over 200 closes at 2026-10-20, frozen=False
    after freeze(): frozen=True, certify()=True
      post-freeze tweak refused: pre-registration is frozen:
      cannot set thresholds

    walk-forward over 120 bars, 3 folds, embargo=5:
      (the three folds listed in the formulas section)

    parameter plateau width: plateau-shaped 3, spike-shaped 1
    (1 = fragile)

    deflated expectancy for observed +0.30 (100 samples):
      1 trial +0.300; 10 trials +0.085; 100 trials −0.003;
      1000 trials −0.072

    look-ahead injector (pure noise, seed 42):
      cheating signal expectancy: +0.403
      honest signal expectancy:   −0.002
      fabricated edge: +0.405 points from nothing

Freeze-hardening battery from the tests [S]: flipping
`_frozen` back, `setattr` on each field, inventing a new
attribute, deleting each attribute, item assignment/deletion
on thresholds, calling the proxy's absent mutators
(`update`/`pop`/`clear`/`setdefault`/`popitem`), mutating the
caller's original dict before *and* after freezing, and the
`vars()` back door — after every failed attempt the
registration's fingerprint is verified unchanged.

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Moving the gate after seeing results. Any post-hoc
  threshold tweak converts a test into a story; the freeze
  exists to make the tweak *impossible*, not discouraged.
- T2. Shallow immutability. Freezing the object but leaving the
  thresholds dict, the `_frozen` flag, or the caller's dict
  mutable leaves the back door open — the battery tests each.
- T3. Contiguous train/test splits. Adjacent bars leak; an
  embargo of samples belonging to neither side is the price
  of an honest fold.
- T4. Reporting the spike. A best parameter whose neighbours
  collapse (0.15 vs 0.02/0.02) has plateau width 1 — treat as
  fragile, however pretty the peak.
- T5. Forgetting your own trial count. Trying 100 variants and
  reporting the winner's raw +0.30 is reporting −0.003 with
  extra steps. Deflate, or Bonferroni, but do something.
- T6. One look-ahead line. Peeking a single bar ahead is worth
  ~+0.40 on pure noise — larger than most claimed edges.
- T7. Certifying with an unfrozen gate. `certify()` and
  `evaluate_gate` refuse unfrozen registrations outright.
- T8. Judging the strategy when only belief changed. At 100
  trials the strategy is identical; the *evidence* deflated.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Freeze routes and exception types, fingerprint properties,
  walk-forward tiling and embargo counts, plateau 80% rule,
  deflation values, look-ahead +0.403/−0.002 and sigma
  scaling: [S] — 40 tests.
- Demo walkthrough figures: [S] — verbatim captured output.
- Live pre-registered gates (kill-switch close counts,
  200-trade momentum go/no-go, fixed verdict date): [M] —
  per README; this lab is that discipline in miniature.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves five defences can be enforced in code
rather than promised in prose: a genuinely tamper-proof
pre-registration, leakage-checked time splits with a real
embargo, plateau measurement, a deflation haircut with exact
values, and a calibrated look-ahead injector (+0.405
fabricated expectancy) — across 40 tests [S]. It does not
prove any particular backtest is now trustworthy: the deflation
assumes unit per-sample std, real series violate the noise
model in both labs, and discipline tools only work if the
researcher actually freezes the gate *before* looking — the
code can refuse a late freeze's certification, but it cannot
make anyone register early [R].
