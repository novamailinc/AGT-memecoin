# Backtesting Rigor for Memecoin Research (lab)

A from-scratch, pure-stdlib toolkit of five defences against fooling yourself with a backtest: freeze the gate before you look, split time-ordered data with an embargo, prefer parameter plateaus over spikes, haircut expectancy for every variant you tried, and see exactly how much edge a single look-ahead bug fabricates from pure noise.

## What it does

- `PreRegistration` lets you write a gate down and freeze it; after the freeze every mutation route raises — attribute assignment (including flipping `_frozen` itself or adding a new field), attribute deletion, item assignment/deletion and the dict mutator methods on `thresholds`, `set_threshold`, and even rewriting the caller's original dict (the registration keeps its own copy, and the class is slotted so there is no `__dict__` back door) — and only a frozen registration can certify a result or evaluate metrics against the gate.
- `walk_forward` produces expanding-window train/test folds and drops an embargo of samples between train and test so adjacent bars cannot leak across the split.
- `parameter_plateau` measures the width of the contiguous region around the best parameter that keeps at least 80% of the best expectancy — width 1 is a spike, and a spike is fragile.
- `deflated_expectancy` subtracts a documented multiple-testing penalty that grows with trials tried and shrinks with sample size.
- `lookahead_experiment` is a dishonesty injector: on iid noise, a signal that peeks at the next bar manufactures roughly +0.40 expectancy while the honest current-bar signal finds nothing.

## Files

- `rigor.py` — `PreRegistration` slotted dataclass with fields `gate: str`, `thresholds: dict`, `registered_at: str`; methods `freeze()` (idempotent, returns self), `frozen() -> bool`, `set_threshold(key, value)`, `certify() -> bool`. Construction is fail-closed and defensive: empty gate/timestamp, a non-dict or empty `thresholds`, or a non-finite / boolean / non-numeric threshold value raises `ValueError`, and the thresholds dict is copied so the caller's original cannot mutate the registration. Fail-closed after `freeze()`: assigning *any* attribute raises `RuntimeError` (including `_frozen` itself and new attribute names), deleting any attribute raises `RuntimeError`, item assignment/deletion into `thresholds` raises `TypeError` (mapping proxy, which also exposes no `update`/`pop`/`clear`/`setdefault`), `set_threshold` raises `RuntimeError`, there is no `__dict__` to rewrite, and `certify()` on an unfrozen registration raises `RuntimeError`. Also `registration_fingerprint(reg) -> str` (canonical SHA-256 of the contents; identical contents fingerprint identically regardless of dict order, any content change changes it) and `evaluate_gate(reg, metrics) -> bool` (every threshold is a minimum the same-named metric must meet; unfrozen raises `RuntimeError`, a missing or non-finite metric raises `ValueError`). `walk_forward(data, n_folds=3, embargo=0)` — generator of `(train, test)` lists; negative embargo, non-positive folds, or a segment too small for the embargo raise `ValueError`. `chronological_split(data, train_fraction=0.7, embargo=0) -> (train, test)` — single split; a fraction outside (0, 1) or an empty side raises. `check_no_leakage(train_indices, test_indices) -> int` — returns the gap between disjoint, strictly time-ordered index sets; overlap, disorder, or an empty set raises. `parameter_plateau(results: dict) -> int` (empty dict raises; best expectancy <= 0 returns 0), `plateau_region(results) -> list` (the actual parameters in the region; non-finite expectancies raise), `is_robust_plateau(results, min_width=2) -> bool`. `multiple_testing_penalty(n_trials, n_samples=100) -> float` and `bonferroni_alpha(alpha, n_trials) -> float` = `alpha / n_trials`; `deflated_expectancy(observed, n_trials, n_samples=100) -> float` = `observed - sqrt(2*ln(max(n_trials,1))) / sqrt(n_samples)`, assuming per-sample std 1.0 (documented in the docstring); `n_samples <= 0` or a non-finite observed value raises. `lookahead_experiment(seed=42, n_bars=5000, sigma=0.5) -> (cheating_expectancy, honest_expectancy)` and `lookahead_bias(...) -> float` (cheating − honest); fewer than 100 bars or a non-positive/non-finite `sigma` raises.
- `demo.py` — walks through all five defences in order. Runs clean, exit 0.
- `test_rigor.py` — pytest suite (see below).

## Test suite

40 tests, all passing. Coverage: the freeze raising on all three original mutation routes (and mutation being legal before it), unfrozen registrations refusing to certify, the embargo verifiably dropping exactly 5 samples that appear in neither train nor test with time order preserved, plateau width on a plateau-shaped dict (3) versus a spike (1) versus an all-negative dict (0), the deflation haircut (zero at one trial, growing with trials, shrinking with samples), and the look-ahead injector inflating expectancy by more than 0.2 points over an honest signal that stays at ~0, with seed determinism. Added: a freeze-hardening battery covering every further mutation route constructible — flipping `_frozen` back, `setattr` on each field, inventing a new attribute, deleting each attribute, item assignment and deletion on the frozen thresholds, the proxy's missing mutator methods (`update`/`pop`/`clear`/`setdefault`/`popitem`), mutating the caller's original dict before and after the freeze (aliasing), and the `__dict__`/`vars()` back door (slotted class) — with the registration's fingerprint verified unchanged after failed tampering, plus freeze idempotence, construction validation (empty/non-finite/boolean thresholds and fields), and `set_threshold` value validation; `registration_fingerprint` determinism, dict-order independence, format, and sensitivity to gate/threshold/timestamp/extra-key changes; `evaluate_gate` pass/fail with inclusive boundaries and its fail-closed paths; walk-forward structure on 100 bars (test segments tiling bars 25–99), the trailing remainder bar that no fold uses, embargo shrinking tests to 20 with a gap of 10, and fold/segment validation; `chronological_split` exact contents (the 5 embargoed bars belong to neither side) and every empty-side/fraction failure; `check_no_leakage` gap values matching walk-forward's embargo and rejection of overlap, disorder, interleaving, and empty sets; `plateau_region` contents, best-at-the-edge sweeps, tie expansion from the first argmax, the exact 80% boundary (with best = 1.0 so the float threshold is exact), `is_robust_plateau` verdicts, and non-finite rejection; `multiple_testing_penalty` exact values and its identity with the deflated haircut, `bonferroni_alpha` exact values and validation, and deflated-expectancy input rejection; and the look-ahead bias scaling linearly in sigma (doubling sigma doubles the fake edge, matching σ·√(2/π) theory), honest-signal nulls for three other seeds, and sigma/bar-count validation.

## Captured output

```
Backtesting Rigor — five defences against fooling yourself

registered: momentum lane: WR >= 35% and expectancy > 0 over 200 closes at 2026-10-20, frozen=False
after freeze(): frozen=True, certify()=True
  post-freeze tweak refused: pre-registration is frozen: cannot set thresholds

walk-forward over 120 bars, 3 folds, embargo=5:
  train 0..29 (30 bars), test 35..59 (25 bars), gap 5 embargoed
  train 0..59 (60 bars), test 65..89 (25 bars), gap 5 embargoed
  train 0..89 (90 bars), test 95..119 (25 bars), gap 5 embargoed

parameter plateau width: plateau-shaped 3, spike-shaped 1 (1 = fragile)

deflated expectancy for an observed +0.30 (100 samples):
  after    1 trials tried: +0.300
  after   10 trials tried: +0.085
  after  100 trials tried: -0.003
  after 1000 trials tried: -0.072

look-ahead injector (pure noise, seed 42):
  cheating signal (next bar) expectancy: +0.403
  honest signal (current bar) expectancy: -0.002
  fabricated edge: +0.405 expectancy points from nothing
```

## What this mirrors in the live bot

The live operation runs on pre-registered gates: kill switches halt a lane on negative rolling expectancy after a fixed number of closes, the momentum lane has a written 200-trade go/no-go gate, and verdicts are due on a set date with the criteria fixed in advance. This lab is that discipline in miniature — the freeze is the pre-registration, the embargo is why replay and shadow-scan results are kept separate from the data that tuned them, the plateau test is the standing suspicion of any single magic threshold, and the look-ahead injector quantifies the classic replay bug of letting a signal see the bar it is supposed to predict.

## Self-exam

1. Name the ways of mutating a frozen `PreRegistration` — direct assignment, `set_threshold`, item assignment into `thresholds`, deleting an attribute, flipping `_frozen`, mutating the dict you originally passed in — and the exception (or non-effect) each one produces. Which route did the original version leave open?
2. With 120 bars, 3 folds, and embargo 5, which bar indices appear in no fold's train or test set at all?
3. A parameter sweep's best expectancy is 0.15 at one setting and 0.02 at both neighbours. What is the plateau width, and what should you conclude?
4. An observed expectancy of +0.30 from 100 samples survives 10 trials but not 100. What changed — the strategy, or your belief in it?
5. In `lookahead_experiment`, both strategies earn the next bar's return. What is the single difference between them, and why does it create +0.40 from noise?
