# Sniper-Window Economics — study notes

## Core ideas (teach-back)

Every launch entry is a race you mostly cannot see: the price rises
from 1x to its peak in about two minutes, and *when* your fill lands
decides what is left for you. This toolkit prices delay itself, in
multiples, under a model deliberately **generous to the late
entrant** — so its negative verdict on the 2–10 minute copy window
cannot be argued away as pessimism [S].

The price model is two clean pieces. On the way up, growth is
**geometric**: `multiple(t) = peak^(t / rise)`, defaults peak **10x**
over a **120s** rise. After the peak, price decays with a **180s
half-life**: every 180 seconds, half of the multiple is gone. A
round trip costs **4%** (fees + slippage + adverse fills), charged
on every trade [S].

Entrants are named by delay in `delay_bucket`:

- `sniper` — ≤ **2s** (real snipers fill at 0.4–2s),
- `fast_polling` — > 2s and < 15s,
- `polling` — 15s up to < 120s (polling detection alone is ≥ 15–30s),
- `copy_window_2_10min` — 120s to 600s inclusive,
- `late` — > 600s [S].

Two return lenses are kept separate, and the distinction matters.
The **best case** (`net_return_at_peak_pct`) grants the entrant an
exit exactly at the peak — nobody times the peak, so this is an
upper bound, not an expectation. The **realistic late trade**
(`late_entry_return_pct`) enters at the delay and exits a fixed hold
later, wherever the path has gone — for post-peak entries, deeper
into the decay. A seeded simulation (400 launches per delay, peaks
and rise times varied, **35% rug share** exiting near zero) then
adds reality back: early entrants are *still* generously granted
the peak exit, late entrants exit after a 60s hold in the decay —
and the copy window still loses [S].

This is the structural math behind the live bot's degen_v2 2–10
minute sniper lane being killed on 2026-10-16: not "the lane traded
badly", but "this window cannot pay a late entrant" [M].

## Key derivations & formulas

**Entry multiple and the log scale.** Because growth is geometric,
equal *time* slices consume equal *exponent* slices:

```
entry_multiple(d)           = peak^(d / rise)          (for d ≤ rise)
fraction_of_move_remaining  = 1 − d / rise             (0 at/after peak)
```

Step by step at the defaults (peak 10, rise 120s):

- d = 30s: exponent used = 30/120 = 0.25 → entry `10^0.25 ≈ 1.78x`;
  move remaining = 0.75.
- d = 60s: entry `10^0.5 = √10 ≈ 3.16x`; half the *log* move is
  already gone, even though "only" half the rise time passed.
- d = 120s: entry 10.00x; move remaining 0.0.

Measuring the move linearly ("price still has 9 of its 10x left")
would be the dishonest scale; multiples compound, so the exponent
is the honest unit [S].

**Best-case net at peak.**

```
net_at_peak(d) = (peak / entry(d)) × (1 − cost) × 100 − 100
```

Instant entry: `(10/1) × 0.96 → +860%`. Peak-time entry: exactly
**−4%** (you pay the full round-trip cost to buy the top). For
d > rise the function **raises `ValueError`** — it refuses to sell
you a peak that happened before you entered [S].

**Late-entry return** (any delay, hold `h`):

```
net = ( multiple(d + h) / multiple(d) ) × (1 − cost) × 100 − 100
```

Post-peak, both multiples sit on the decay curve, so a 60s hold
always multiplies price by `0.5^(60/180) ≈ 0.794`, giving
`0.794 × 0.96 − 1 ≈ −23.8%` regardless of which post-peak delay you
pick — the decay ratio over a fixed hold is constant [R, from S].

**Break-even delay** — binary search (tolerance 0.01s) on the
monotonically falling `net_at_peak`, solving for the latest entry
that still covers costs:

```
need peak/entry × 0.96 ≥ 1  →  entry ≤ 10 × 0.96 = 9.6
10^(d/120) = 9.6  →  d = 120 × log10(9.6) ≈ 117.9s
```

`None` when even an instant entry cannot cover costs (e.g. costs
≥ 900% at a 10x peak — in general, when `peak × (1 − cost) < 1`).

## Worked example

Demo ladder, best case (exit granted at the peak), defaults [S]:

| delay | entry | move left | net at peak |
|------:|------:|----------:|------------:|
| 1s    | 1.02x | 99.2%     | +841.8%     |
| 2s    | 1.04x | 98.3%     | +823.9%     |
| 15s   | 1.33x | 87.5%     | +619.9%     |
| 30s   | 1.78x | 75.0%     | +439.8%     |
| 60s   | 3.16x | 50.0%     | +203.6%     |
| 120s  | 10.00x| 0.0%      | −4.0%       |

The realistic copy window (enter at delay, hold 60s): **−23.8%** at
120s, 300s, and 600s alike — the price decays through the whole
hold [S].

Seeded expectancy (seed 7, 400 launches per delay) [S]:

- 1s sniper: **+297.6%**, win rate 62.0%
- 20s polling: **+111.2%**, win rate 62.0%
- 60s polling: **−18.8%**, win rate 36.2%
- 180s / 300s / 600s (copy window): **−52.5% / −52.3% / −51.3%**,
  win rate **0.0%** at each
- Copy-window mean expectancy: **−52.1%**

Break-even delay at defaults: **117.9s** — and that still assumes
selling the exact peak [S].

## Trap log

- T1. Reading the break-even delay as rescue. 117.9s breaks even
  only under a perfect peak exit; the realistic hold-through-decay
  trade at 120s is already −23.8%. An upper bound is not a plan.
- T2. Measuring the remaining move linearly. Half the rise time
  gone means half the *exponent* gone — you have paid √10 ≈ 3.16x,
  not "half of 10x".
- T3. Blaming scoring for a copy window's losses. By 2–10 minutes
  the rise is spent; no signal quality fixes an entry that starts
  after the move. The window is structural, not informational.
- T4. Assuming faster polling competes with snipers. Going 30s →
  15s improves entry from 1.78x to 1.33x — real money, but a 1s
  sniper paid 1.02x; the gap is a different sport, not a tuning
  problem.
- T5. Forgetting costs scale with the trade, not the delay. At the
  peak-time entry the entire P/L is −costs (−4%); raise costs to
  20% and the break-even delay collapses toward the launch, while
  the copy window — already deep in decay — barely notices.
- T6. Asking for a "peak exit" after the peak. The toolkit raises
  instead of returning a fantasy number; any model that happily
  prices it is lying to you.
- T7. Treating the simulation's generosity as neutral. Early
  entrants are *given* the peak exit for free — and the copy window
  still prints −52%. Generosity toward the opposing case makes a
  negative verdict stronger, not weaker.

## Source flags

- Price model, defaults (10x / 120s / 180s / 4%), bucket boundaries,
  the 35% rug share, seeded expectancy figures, demo ladder, and
  the 117.9s break-even are from `sniperwindow.py`, pinned by
  `test_sniperwindow.py` (54 tests) [S].
- The constant −23.8% across post-peak delays is derived above from
  the decay formula [R] and matches the demo output [S].
- Bucket-boundary note: per the source, `polling` is 15s ≤ d <
  120s and the copy window starts *at* 120s; the README's prose
  ("polling (15–120s)") is shorthand for that [S].
- The degen_v2 lane kill and its structural rationale are per the
  README [M].

## One-paragraph verdict

This toolkit proves, inside its stated model, that delay is a price:
a geometric rise spends its exponent early, so a 30s polling delay
already pays 1.78x, the 2–10 minute window enters at or after the
peak and holds through decay, and its seeded expectancy (−52.1%) is
negative at every point in the window even when early entrants are
gifted perfect peak exits. It does **not** prove real launches
follow a clean 10x/120s path, that all post-peak entries lose (a
later, separate runner phase is a different trade), or that fast
entries are safe — the sniper bucket still eats the 35% rug share;
it proves the *window* cannot pay, not that any particular trade
must lose.
