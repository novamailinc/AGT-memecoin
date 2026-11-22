# Verdict Protocol (lab)

A from-scratch, pure-stdlib lab for the lane-verdict procedure: given a lane's record and the four live gates, decide FUND-ELIGIBLE, KEEP-FIXING, KEEP-MEASURING, or KILL — and show the reasons. Everything is synthetic: lanes are fixtures, gates are booleans, and no real ledger is read. The protocol's spine is a refusal: below a minimum sample there is no verdict to give, only more measuring.

## What it does

Module `verdict.py` — thresholds `REQUIRED_N_FULL = 534` (closes needed to distinguish a 35% win rate from a 30% base rate), `MIN_N_ANY_VERDICT = 20`, `MIN_N_KILL = 50`, `DEEP_NEGATIVE_EXPECTANCY = -0.20` per $1, and gate names `sustained_positive_expectancy, proven_entry_strategy, top_picks_winning, hardened_execution_safety`:

- `required_n(p1=0.35, p0=0.30, alpha=0.05, power=0.8) -> int` — the normal-approximation one-sided sample size, `ceil((z_a·√(p0(1−p0)) + z_b·√(p1(1−p1)))² / (p1−p0)²)` with a stdlib inverse-normal approximation; the defaults return exactly **534**. Fail-closed: any probability outside `(0, 1)` or `p1 <= p0` raises `ValueError`.
- `LaneRecord(name, closes, expectancy, win_rate, weeks_of_data, structural_dead=False)` — fail-closed: empty name, negative/non-int/boolean closes, non-numeric expectancy, win rate outside `[0, 1]`, negative weeks, or a non-bool structural flag raise `ValueError`. `Gates(...)` — the four booleans, with `as_dict()`, `all_green()`, and `red_gates()`.
- `Verdict(decision, lane, reasons)` — `render()` (also `__str__`) prints `"<lane>: <DECISION>"` followed by one `  - <reason>` line per reason; a verdict never renders without reasons.
- `sample_sufficient(closes, level) -> bool` for levels `full` (534), `any` (20), `kill` (50); unknown levels and negative closes raise.
- `decide(lane, gates) -> Verdict` — the matrix, in precedence order: (1) `structural_dead` → **KILL** regardless of sample or gates; (2) closes < 20 → **KEEP-MEASURING** (verdict refused); (3) expectancy ≤ −0.20 at ≥50 closes → **KILL**; (4) negative expectancy below the full sample → KEEP-MEASURING (below kill sample) or **KEEP-FIXING**; (5) all four gates green → **FUND-ELIGIBLE** only with ≥534 closes *and* positive expectancy, otherwise KEEP-MEASURING / KEEP-FIXING with the shortfall named; (6) anything else → **KEEP-FIXING** with the red gates listed. Fail-closed: non-`LaneRecord` / non-`Gates` inputs raise `ValueError`. Calendar time never substitutes for closes — 52 weeks at 5 closes is still KEEP-MEASURING.
- Fixtures: `degen_v2_fixture()` (structural window-dead lane → KILL), `small_sample_fixture()` (18 closes → KEEP-MEASURING), `halted_394_fixture()` (394 closes, expectancy −0.375 → KILL), `all_green_fixture()` (600 closes, +0.18 expectancy, all gates green → FUND-ELIGIBLE).

`demo.py` prints the required sample size and renders verdicts for the four fixtures plus a promising lane with one red gate.

## Test suite

38 tests, all passing (`pytest -q`: "38 passed"). Covered: `required_n` pinned at exactly 534 (and inside 500–700), growing as the WR gap shrinks, and rejecting degenerate probabilities; the pinned constants and four gate names; `LaneRecord` validation for every field (including boolean closes and a string structural flag) and the `Gates` helpers (all-green, red-gate list order, dict view); the structural kill firing over a tiny sample *and* over green gates; the sample boundary (19 closes measures, 20 earns a verdict, 0 measures); the 394-close deep-negative fixture killing, deep-negative at exactly 50 closes killing, deep-negative at 30 closes *measuring* instead (below kill sample), and mild negatives fixing at 100 and 600 closes; FUND-ELIGIBLE requiring *each* gate individually (dropping any one of the four revokes it), the full sample (533 measures, 534 funds), and positive expectancy; positive lanes with red gates fixing with reasons rendered; render/`__str__` containing the decision and ≥2 reasons; wrong-type inputs raising; `sample_sufficient` at all three levels and its error paths; every fixture landing in the verdict set; negative expectancy never funding at 1000 closes; and weeks never substituting for closes.

## Captured output

Verbatim output of `python demo.py`:

```
=== Lane verdict protocol lab (synthetic lanes) ===
Required closes to separate 35% WR from 30%: 534
degen_v2: KILL
  - structural kill: the lane's trading window is proven dead
  - closes=120 expectancy=-0.100 win_rate=0.28
steady_new: KEEP-MEASURING
  - sample too small: 18 closes < 20 minimum — verdict refused
  - full verdict needs 534 closes (35% vs 30% WR separation)
main_lane: KILL
  - deep negative expectancy -0.375 per $1 at 394 closes (>= 50)
  - halt: continued measuring spends attention, not information
proven_lane: FUND-ELIGIBLE
  - all four live gates green
  - sample sufficient: 600 closes >= 534
  - expectancy +0.180 per $1, win_rate 0.41, 10.0 weeks
promising: KEEP-FIXING
  - red gates: top_picks_winning
  - expectancy positive (+0.080) — fix the red gates, do not fund yet
```

## What this mirrors in the live bot

This mirrors the live lane-verdict procedure twice over: the Oct 22 lane verdict day (every ledger pulled, a plain live-or-die recommendation per lane) and the Dec 20 CEO-mode fund/fix/kill verdict. The four gates are the live-mode gates verbatim — sustained positive paper expectancy over weeks, a proven entry strategy, Top Picks winning more than losing, and hardened execution/safety — and FUND-ELIGIBLE demands all four *plus* a sample big enough for the win-rate claim to mean anything, in the spirit of the statistical-validity lab (toolkit 08): telling 35% from 30% takes ~534 closes, so a hot 40-close month funds nothing. The fixtures are the real shapes: a structurally dead sniper window (the degen_v2 lane, killed on structure, not statistics), an 18-close lane where the only honest verdict is "not yet", and a 394-close lane at −0.375 expectancy where the kill is arithmetic, not opinion. It is a teaching/checking model only; it shares no code with the live bot and reads no live ledger.

## Self-exam

1. Why does a structural kill outrank the sample-size rules — what would "keep measuring" a proven-dead window actually buy?
2. The 394-close lane is killed at 394 closes even though a *funding* verdict needs 534. Why is the evidence bar for killing lower than the bar for funding — is that asymmetry justified?
3. A lane has expectancy −0.50 at 30 closes and is told to KEEP-MEASURING, not die. What disaster does the 50-close kill floor prevent, and what does waiting cost?
4. Dropping any single gate revokes FUND-ELIGIBLE even at 600 closes and +0.20 expectancy. Pick the gate you would most want to waive "just this once" and argue against yourself.
5. Why can't 52 weeks of calendar data substitute for closes — what is the sample in a win-rate claim actually made of?
6. A lane at 533 closes, all gates green, positive expectancy, gets KEEP-MEASURING. Is the one-close cliff at 534 defensible, or should the protocol grade confidence continuously? What breaks either way?
7. Every verdict renders with reasons, including the refusals. Why is a reasoned KEEP-MEASURING more useful to the Dec 20 decision than a bare "insufficient data"?
