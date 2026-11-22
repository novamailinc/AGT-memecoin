# Rug Taxonomy (lab)

Five named rug patterns — LP pull, mint-and-dump, can't-sell, wash skew, and holder crash — implemented as small detectors over one shared input shape: a token lifecycle, a list of snapshot dicts (timestamp, price, liquidity, holders, supply, mint authority, sell-simulation result, volumes, unique buyers, trades). Each detector returns `(triggered, evidence)` with the numbers that fired. Synthetic fixtures only; pure Python stdlib, no network.

## What it does

`ruglab.py` runs each detector independently over a lifecycle and reports all five results at once. Every threshold is a named constant at the top of the module, every rule is concrete (percentages, windows, and strict-vs-inclusive comparisons are stated per detector below), and missing fields never trigger a detector — a snapshot that lacks the data a rule needs is skipped, with the non-firing evidence saying so.

## Files

- `ruglab.py` — the module:
  - `lp_pull(lifecycle: list[dict]) -> (bool, str)` — fires when, within a short window (≤3600s, or ≤3 steps if timestamps are absent), liquidity falls ≥80% from an earlier snapshot while price falls ≥50% over the same pair. Fail-closed: unusable snapshots are skipped; a 79% drop, or an 80%+ drop without the price collapse, does not fire.
  - `mint_and_dump(lifecycle) -> (bool, str)` — fires when mint authority is active at the inflation pair, supply grows strictly >20% between snapshots, and price at/after the inflation snapshot has fallen >50% from before. Missing supply data → never fires.
  - `cant_sell(lifecycle) -> (bool, str)` — fires when any snapshot has `sell_sim_ok is False` explicitly. A missing field is unknown, not a failure, and never fires.
  - `wash_skew(lifecycle) -> (bool, str)` — fires on any snapshot where all of: buy_vol + sell_vol > $10,000, unique_buyers / trades < 0.1, and holder count moved <2% versus the first snapshot. Snapshots missing any field are skipped.
  - `holder_crash(lifecycle) -> (bool, str)` — fires when holders drop ≥40% between two consecutive snapshots. A slow bleed totalling 40%+ over many steps does not fire.
  - `run_detectors(lifecycle) -> dict[str, (bool, str)]` — all five results, always; an empty lifecycle yields five `(False, reason)` entries rather than an exception.
  - `triggered_names(lifecycle) -> list[str]` — sorted names of detectors that fired.
  - `rug_score(lifecycle) -> int` — combined 0–100 score: the sum of `DETECTOR_WEIGHTS` (lp_pull 35, mint_and_dump 30, cant_sell 25, holder_crash 20, wash_skew 15) over triggered detectors, capped at 100. The detectors stay independent and binary; this layer only weighs how many fired at once, and a clean lifecycle scores exactly 0.
  - `risk_grade(score) -> str` — bands a score: 0 `"clean"`, 1–24 `"watch"`, 25–49 `"elevated"`, 50–100 `"severe"`. Raises `ValueError` for a non-numeric or out-of-range score.
  - `assess(lifecycle) -> dict` — one-call summary `{"triggered", "score", "grade", "detectors_fired"}`, consistent by construction.
  - Shared parsing hardening: NaN/infinite snapshot fields are treated exactly like missing fields by every detector.
- `demo.py` — a healthy lifecycle (nothing fires) and an LP-pull lifecycle (only `lp_pull` fires).
- `test_ruglab.py` — the pytest suite below.

## Test suite

44 tests, all passing. Coverage: one trigger fixture per detector (5); the shared clean lifecycle fixture triggering none of the detectors; per-detector edges — 79% liquidity drop does not fire `lp_pull`, a liquidity drop without price collapse does not fire, a drop outside the short window does not fire, mint-and-dump without an active mint authority does not fire, exactly +20% supply inflation does not fire (rule is strictly >20%), a missing `sell_sim_ok` field never fires `cant_sell`, each wash-skew condition removed in turn prevents firing, a 39% holder drop and a slow multi-step bleed do not fire `holder_crash`; a single-detector fixture reporting exactly that detector via `run_detectors`; empty lifecycle returning five non-firing results. Plus: `lp_pull` inclusive thresholds (exactly −80% / −50% fires), price 0.1% short of the threshold not firing, the window edge at exactly 3600s vs 3601s, the ≤3-step fallback window at gap 3 vs gap 4 with timestamps absent, the window measured per pair (a later in-window pair fires when the first pair is out of window), and zero/missing liquidity never firing; `mint_and_dump` price drop of exactly 50% not firing (strictly >50%), +20.1% inflation firing, mint authority active on either snapshot of the pair counting, a price drop that happens *before* the inflation not counting, and missing/zero supply never firing; `cant_sell` strictness (only a real boolean `False` fires — `None`, `0`, `"false"` and `1` do not) and index-0 evidence; `wash_skew` boundaries (volume exactly $10,000 vs $10,001, unique ratio exactly 0.1 vs 0.099, holder change exactly 2.0% vs 1.8%), holder *drops* counting as flat via the absolute change, and zero-trades/missing-field snapshots skipped safely; `holder_crash` at a second scale (1000 → 600 fires, → 601 does not), unusable pairs skipped with a later pair still firing, and zero/rising holder counts not firing; `run_detectors(None)` returning five non-firing results; a mixed fixture firing exactly three detectors with sorted names; a crafted lifecycle firing all five detectors at once; combined scoring (single-detector weights 15/25/35, all-five raw 125 capped at 100, `assess` agreeing with `triggered_names`/`rug_score` on clean, single-detector and all-five fixtures); and `risk_grade` at every band boundary plus `ValueError` on out-of-range/non-numeric/NaN scores.

## Captured output

Verbatim output of `python demo.py` (exit 0):

```
Rug taxonomy lab -- five patterns, synthetic lifecycles
========================================================
Scenario 1: healthy lifecycle (expect: nothing fires)
  [ ] lp_pull: no >=80% liquidity drop with >=50% price collapse in window
  [ ] mint_and_dump: no active-mint supply inflation >20% followed by price drop >50%
  [ ] cant_sell: sell simulation ok (or unreported) at every snapshot
  [ ] wash_skew: no snapshot with high volume, <0.1 unique ratio and flat holders
  [ ] holder_crash: no consecutive holder drop >=40%

Scenario 2: liquidity pulled, price collapses
  [X] lp_pull: liquidity -92.0% ($100,000 -> $8,000) and price -80.0% within short window (snapshots 0->2)
  [ ] mint_and_dump: no active-mint supply inflation >20% followed by price drop >50%
  [ ] cant_sell: sell simulation ok (or unreported) at every snapshot
  [ ] wash_skew: no snapshot with high volume, <0.1 unique ratio and flat holders
  [ ] holder_crash: no consecutive holder drop >=40%

```

## What this mirrors in the live bot

The live bot spreads these same patterns across layers rather than one module: sell-simulation failures are the EVM honeypot/can't-sell hard blocks, wash patterns and insider concentration feed the unified veto engine, and liquidity/holder collapses are what its post-entry monitoring and exit ladders react to. This lab isolates the pattern definitions themselves — the thresholds, windows, and conjunctions — so each can be studied and edge-tested without the live pipeline around it.

## Self-exam

1. Liquidity falls 85% in twenty minutes but price only falls 20%. Does `lp_pull` fire, and which condition saves the token?
2. Supply inflates exactly 20% with the mint authority active, then price halves. Does `mint_and_dump` fire? State the comparison that decides it.
3. A lifecycle has no `sell_sim_ok` field anywhere. What does `cant_sell` return, and why is that the fail-closed choice here?
4. Holders fall from 500 to 290 over five snapshots, never more than 15% in one step. Does `holder_crash` fire? What pattern would catch this instead?
5. A snapshot shows $50,000 of volume, 30 unique buyers across 900 trades, and holders up 0.5% since launch. Which detector fires, and which of its three conditions is closest to its threshold?
