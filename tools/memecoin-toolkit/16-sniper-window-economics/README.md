# Sniper-Window Economics (lab)

A from-scratch, pure-stdlib lab for the latency and age economics of launch entries: what a detection-plus-execution delay actually costs, in multiples. Everything here is synthetic — no network, no live bot code, no real token data — and the model is deliberately generous to the late entrant, so that its negative verdict on the 2–10 minute copy window cannot be argued away as pessimism.

## What it does

Module `sniperwindow.py`. The price model: a launch rises geometrically from 1x to `peak_multiple` over `rise_seconds` (`multiple(t) = peak ** (t / rise)`), then decays with a half-life (`peak * 0.5 ** ((t − rise) / half_life)`). Defaults: 10x peak, 120s rise, 180s half-life, 4% round-trip cost.

- `multiple_at_time(t_seconds, peak_multiple=10, rise_seconds=120, half_life_seconds=180) -> float` — the price multiple at a moment. Fail-closed: raises `ValueError` on negative time, non-positive parameters, or a "peak" below 1.
- `entry_multiple(delay_seconds, ...) -> float` — the multiple you actually pay when your fill lands `delay` seconds after launch.
- `fraction_of_move_remaining(delay_seconds, ...) -> float` — share of the (log) move still ahead: 1.0 at launch, 0.0 at/after the peak. Half the rise time gone means half the exponent gone — a 30s polling delay into a 120s rise already pays 10^0.25 ≈ 1.78x the launch price.
- `gross_return_pct(delay_seconds, exit_multiple_value) -> float` — % return entering at `delay` and exiting at a stated multiple.
- `net_return_at_peak_pct(delay_seconds, ...) -> float` — the *best case*: enter at `delay`, exit exactly at the peak, minus round-trip costs. Raises `ValueError` for a delay after the rise — it refuses to sell you a peak that happened before you entered.
- `late_entry_return_pct(delay_seconds, hold_seconds=60, ...) -> float` — the realistic late trade: enter at `delay`, exit `hold` seconds later, wherever the path (rise or decay) has taken the price, minus costs.
- `delay_bucket(delay_seconds) -> str` — names the entrant: `sniper` (≤ 2s, where real snipers fill at 0.4–2s), `fast_polling` (< 15s), `polling` (15–120s; polling detection alone is ≥ 15–30s), `copy_window_2_10min` (120–600s), `late` (> 600s). Raises `ValueError` on negative delays.
- `expectancy_by_delay(delay_seconds, seed=7, n_launches=400, ...) -> BucketResult` — mean net % and win rate over seeded synthetic launches (varying peaks and rise times, a 35% rug share exiting near zero, early entrants generously granted the peak exit, late entrants exiting after a fixed hold deep in the decay). Deterministic per seed. Raises `ValueError` on non-positive `n_launches`.
- `expectancy_table(delays=(1, 20, 60, 180, 300, 600), ...) -> list[BucketResult]` / `copy_window_expectancy(...) -> float` — the delay ladder, and the mean expectancy across the 2–10 minute window specifically. It is negative: the window is structurally negative for a late entrant, not merely risky.
- `break_even_delay(peak_multiple=10, rise_seconds=120, round_trip_cost_pct=4, tolerance_seconds=0.01) -> float | None` — binary-search solver for the latest entry whose best-case peak exit still covers costs (117.9s at the defaults — and that still assumes selling the exact peak). `None` when even an instant entry cannot cover costs.

`demo.py` walks the delay ladder (entry multiple, move remaining, best-case net), prices the copy window directly, prints the seeded expectancy table, and reports the break-even delay.

## Test suite

54 tests, all passing (`pytest -q`: "54 passed"). Covered: the geometric path (1x at launch, peak at rise end, √peak halfway, quarter-point value, custom peak/rise); post-peak decay at one and two half-lives; monotonicity on both sides of the peak; invalid times and parameters raising; fraction-of-move at launch, halfway, peak, and beyond; the sniper delay keeping > 98% of the move; the 30s polling delay's exact 0.75 fraction and 1.78x entry; best-case net returns (instant entry = 860% at the defaults, peak-time entry = exactly −costs, monotonic decline with delay, post-peak entry raising); late entries during decay losing, a one-half-life hold costing exactly −52% after costs, an early short hold winning, and higher costs always hurting; every delay-bucket boundary (parametrised); seeded expectancy determinism, trade/bucket bookkeeping, and zero-launch rejection; the structural verdict — copy-window expectancy negative overall and at each of 120s/300s/600s individually (parametrised), sniper and polling delays both beating the copy window, win rate falling with delay; and the break-even solver against its closed-form value, its near-zero net at the solution, the impossible-costs `None` case, and its movement with costs and peak size.

## Captured output

Verbatim output of `python demo.py`:

```
=== Sniper-window economics lab (synthetic launches) ===
Model: 10x peak over a 120s geometric rise, 180s decay half-life,
4% round-trip cost. Peak exits are granted generously where used.

-- What your delay costs you (best case: you exit at the peak) --
delay    1.0s  entry  1.02x  move left  99.2%  net at peak  +841.8%
delay    2.0s  entry  1.04x  move left  98.3%  net at peak  +823.9%
delay   15.0s  entry  1.33x  move left  87.5%  net at peak  +619.9%
delay   30.0s  entry  1.78x  move left  75.0%  net at peak  +439.8%
delay   60.0s  entry  3.16x  move left  50.0%  net at peak  +203.6%
delay  120.0s  entry 10.00x  move left   0.0%  net at peak    -4.0%

-- The copy window (enter late, hold 60s) --
delay  120.0s  net  -23.8% (price is decaying the whole hold)
delay  300.0s  net  -23.8% (price is decaying the whole hold)
delay  600.0s  net  -23.8% (price is decaying the whole hold)

-- Seeded expectancy over synthetic launches (400 per delay) --
delay    1.0s  [sniper             ] mean net   +297.6%  win rate  62.0%
delay   20.0s  [polling            ] mean net   +111.2%  win rate  62.0%
delay   60.0s  [polling            ] mean net    -18.8%  win rate  36.2%
delay  180.0s  [copy_window_2_10min] mean net    -52.5%  win rate   0.0%
delay  300.0s  [copy_window_2_10min] mean net    -52.3%  win rate   0.0%
delay  600.0s  [copy_window_2_10min] mean net    -51.3%  win rate   0.0%
Copy-window (2-10 min) mean expectancy: -52.1%

-- Break-even solver --
Latest best-case break-even entry: 117.9s (and that still assumes you sell the exact peak)
```

## What this mirrors in the live bot

This mirrors why the live bot's degen_v2 2–10 minute sniper lane was structurally killed on 2026-10-16. That lane copied entries minutes after launch — exactly the `copy_window_2_10min` bucket here — while real snipers fill at 0.4–2 seconds and even polling-based detection cannot land before 15–30 seconds. The math in this lab is the structural part of that verdict: by 2–10 minutes the rise is spent, the entrant is buying near the peak and holding through the decay, and no improvement in scoring fixes an entry that starts after the move. The kill was not "the lane traded badly"; it was "this window cannot pay a late entrant", which is what the seeded expectancy table shows even while granting early entrants the peak exit for free. It is a teaching/checking model of that economics only; it shares no code with the live bot.

## Self-exam

1. Why is the move measured on a log (exponent) scale in `fraction_of_move_remaining` rather than as a linear share of the peak multiple?
2. The break-even solver says a 117.9s entry can still break even. Why does that not rescue the 2–10 minute window?
3. Which generous assumption does the expectancy simulation grant early entrants, and why does granting it make the copy window's negative verdict *stronger*, not weaker?
4. A polling detector improves from 30s to 15s. What does it gain in entry multiple on the default path, and why is it still not competing with a 1s sniper?
5. Why does `net_return_at_peak_pct` raise for delays past the rise instead of returning a number?
6. If the round-trip cost rose from 4% to 20%, what happens to the break-even delay — and why does the copy window barely notice?
