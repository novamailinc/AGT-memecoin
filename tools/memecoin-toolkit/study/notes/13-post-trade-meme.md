# Post-Trade Ledger Analytics — study notes

## Core ideas (teach-back)

After trades close, every lane wants to tell a flattering story.
This toolkit is the referee: it reads close records of one fixed
shape — `{lane, mint, realized_mult, peak_mult, exit_reason,
was_rug}` — and produces the same statistics, with the same
definitions, for every lane, so lanes can be compared honestly [S].

One win definition is used everywhere and never quietly moved:

- **Win:** `realized_mult ≥ 1.5` (`WIN_MULT = 1.5`).
- **Breakeven rate** is reported separately: share with
  `realized_mult ≥ 1.0`.
- **Expectancy:** mean realised multiple minus 1 — profit per **$1
  shot**, matching the paper ledger's fixed sizing.
- **Rug rate:** share of closes flagged `was_rug`.

The number the toolkit cares about most is **leakage**. A
**round-tripper** is a close that *peaked* at ≥ 2.0x (`PEAK_MULT`)
but *realised* < 1.0x: the position saw a double and still lost
money. Round-trippers are an exit-policy diagnosis, not an entry
diagnosis — the entry found a 2x, the exit gave it back. The
leakage report counts how many closes peaked ≥ 2x, how many closed
as wins, how many round-tripped, and what the round-trippers
actually realised on average. A companion, **capture rate**, asks:
of the closes that peaked ≥ 2x, what share closed as wins? [S]

Supporting analytics round out the scorecard: total P/L in dollars,
median realised, best/worst close, profit factor (gross gains /
gross losses), the cumulative P/L curve and its max drawdown,
win/loss streaks, an exit-reason breakdown, per-lane scorecards and
rankings, and a **pre-registered verdict gate** — enough closes
*and* expectancy strictly above the bar — so a lane cannot be
declared good on five lucky closes [S].

The empty-ledger contract is deliberate: an empty ledger never
crashes. Rates return 0.0; averages with nothing to average return
`None`; the scorecard is `{}`; the rendered table says
"no closes recorded". But a record *missing a required field*
raises `KeyError` from every public function — a corrupt row must be
loud, never silently averaged away [S].

## Key derivations & formulas

All rates are simple shares over `n` closes; the substance is in
the fixed boundaries (exactly-at-the-line behaviour is tested):

```
win_rate        = #{realized ≥ 1.5} / n          (exactly 1.5 wins)
breakeven_rate  = #{realized ≥ 1.0} / n          (exactly 1.0 counts)
expectancy      = (Σ realized) / n − 1           (per $1 shot)
pnl_usd         = Σ (realized − 1) × shot_usd    (shot_usd > 0 else ValueError)
rug_rate        = #{was_rug} / n
```

Leakage, step by step:

```
peaked_2x     = #{peak ≥ 2.0}
round_tripped = #{peak ≥ 2.0 AND realized < 1.0}
                (peak exactly 2.0 counts; realized exactly 1.0 does NOT —
                 0.999 round-trips, 1.0 does not; peak 1.999 does not)
capture_rate  = #{peaked AND realized ≥ 1.5} / peaked_2x
                (None when peaked_2x = 0: "nothing to capture" ≠
                 "captured nothing", which is 0.0)
```

Profit factor:

```
gains  = Σ (realized − 1) over realized > 1.0
losses = Σ (1 − realized) over realized < 1.0     (exactly 1.0 feeds neither)
profit_factor = gains / losses
                → inf if gains > 0 and losses = 0
                → None if both sides are empty (empty or perfectly flat)
```

Drawdown: build the cumulative P/L curve in ledger order (starting
from an implicit 0 before the first close), track the running peak,
and take the largest `peak − current` fall. An opening losing streak
therefore draws down *from zero* [S].

Streaks use the same definitions: a win is ≥ 1.5, a loss is < 1.0,
and the 1.0–1.5 breakeven band **breaks both streaks** — it is
neither. Lane ranking is deterministic: expectancy descending, then
close count, then lane name. `verdict_ready(closes, min_closes,
min_expectancy=0.0)` requires `len ≥ min_closes` AND
`expectancy > min_expectancy` — exactly at the bar fails [S].

## Worked example

The demo fixture: 10 synthetic closes, 5 `degen` + 5 `steady` [S].

Overall (hand-checkable from the fixture rows):

- Realised sum = 12.3 → expectancy `12.3/10 − 1 = +0.230` per $1.
- Wins (≥1.5): 2.6, 1.8, 1.6, 3.2 → win rate **0.40**.
- Breakeven (≥1.0): those four plus 1.1 → **0.50**.
- Rugs: 2 of 10 → rug rate **0.20**. P/L **+$2.30** at $1 shots,
  **+$11.50** at $5 shots.

Per lane — same win rate, different expectancy:

- `degen`: realised 2.6, 0.4, 0.0, 1.8, 0.7 → sum 5.5, expectancy
  **+0.100**, breakeven 0.40, rug 0.20.
- `steady`: 1.6, 1.1, 0.9, 0.0, 3.2 → sum 6.8, expectancy **+0.360**,
  breakeven 0.60, rug 0.20.

Leakage: 5 closes peaked ≥ 2x; 2 round-tripped — MINT_B (peak 2.6,
realised 0.4) and MINT_H (peak 2.4, realised 0.9) — averaging
`(0.4 + 0.9)/2 = 0.65x`. Capture rate `3/5 = 0.60`. Profit factor:
gains 5.3 / losses 3.0. Max drawdown **$1.60** (the curve peaks at
+1.6 after the first close, then falls back to 0.0 by the third).
Longest win streak 1, longest loss streak 2 [S].

## Trap log

- T1. Defining "win" at 1.0x. That flatters every lane; the fixed
  1.5x bar separates "didn't lose" (breakeven rate) from "won".
- T2. Comparing lanes on win rate alone. The fixture lanes tie at
  0.40 yet differ by 0.26 of expectancy — win *size* matters, which
  is why expectancy, not win rate, is the verdict statistic.
- T3. Blaming entries for round-trippers. A close that peaked at
  2.6x and realised 0.4x had a fine entry and a broken exit;
  mixing the two sends the fix to the wrong place.
- T4. Reporting capture rate 0.0 when nothing peaked. That case is
  `None` — no opportunity existed. 0.0 means opportunities existed
  and all were missed; the two demand opposite responses.
- T5. Skipping corrupt rows silently. A missing `peak_mult` raises
  `KeyError` on purpose: one skipped rug can flip a lane's
  expectancy sign, so the ledger must fail loud.
- T6. Declaring a verdict early. `verdict_ready` needs the close
  count *and* strictly-above-bar expectancy; exactly-at-bar fails,
  and five hot closes are not a verdict.
- T7. Reading drawdown only from the first peak. It is measured
  from the 0 start, so an opening losing streak counts in full.

## Source flags

- Win/peak constants (1.5 / 2.0), all formulas, boundary behaviour,
  the empty-ledger contract, `KeyError` on missing fields, and every
  fixture number above are implemented and hand-checked in
  `posttrade.py` / `test_posttrade.py` (40 tests) [S].
- The interpretation of round-trippers as exit leakage rather than
  entry failure follows the module's documented definitions [S],
  and mirrors the live ledger's repeated pattern of >2x peaks
  closing as losers [M].
- The streak/drawdown conventions (breakeven band breaks streaks;
  drawdown measured from 0) are source behaviour [S]; calling them
  "the conservative choice" is my gloss [R].

## One-paragraph verdict

This toolkit proves that a small, fixed-definition scorecard —
win rate at 1.5x, expectancy per $1, rug rate, and especially the
leakage/capture pair — can separate entry quality from exit quality
and compare lanes without moving goalposts, including honest
empty-ledger and corrupt-row behaviour. It does **not** prove any
lane is profitable (the fixture is synthetic and its +0.23
expectancy is an arithmetic demonstration), it says nothing about
whether 1.5x is the *right* win bar for live decisions, and it
trusts the `peak_mult` and `was_rug` fields it is fed — garbage
peaks in, garbage leakage out.
