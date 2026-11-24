# Liquidity Velocity (lab)

A from-scratch, pure-stdlib lab for measuring how fast SOL flows into a launch in its first minutes — and for the uncomfortable follow-up question of whether predicting graduation that way is worth any money. Everything here is synthetic — no network, no live bot code, no real token data — so every rate can be checked by hand and the cost math can be pinned by tests instead of argued in prose.

## What it does

Module `liqvelocity.py` — one frozen dataclass, `Flow(ts, sol_in)` (seconds since launch, SOL in), plus functions over event lists:

- `Flow(...)` — one inflow event. Fail-closed: construction raises `ValueError` on a negative/`None` timestamp or a non-positive/`None` amount.
- `total_inflow(events) -> float` — total SOL in. `duration_minutes(events) -> float` — span from first to last event (input is sorted first). The rate functions raise `ValueError` on `None`, empty, single-event, or zero-duration streams rather than dividing by nothing.
- `inflow_rate(events) -> float` — SOL per minute over the stream's span.
- `trade_rate(events) -> float` — trades per minute over the same span.
- `trades_normalized_velocity(events) -> float` — inflow per minute per 100 trades (`inflow_rate / count * 100`): a size-adjusted pace comparable between launches with different trade counts.
- `mean_trade_size(events) -> float` — total inflow / trade count.
- `progress_rate(events, target_sol=85.0) -> float` — bonding-curve completion percentage points per minute at the current inflow rate. Raises `ValueError` on a non-positive target.
- `minutes_to_graduation(events, target_sol=85.0) -> float | None` — minutes to the target at the current rate; `None` once the target is already collected.
- `velocity_band(rate) -> str` — `"slow"` (< 0.5), `"steady"` (< 2.0), `"fast"` (< 5.0), `"igniting"` (≥ 5.0) SOL/min. Raises `ValueError` on negative/`None` rates.
- `graduation_score(events) -> float | None` — a 0–100 predictor score from inflow pace (up to 60 points across the calibrated bands) plus trade pace (up to 40). Fail-closed on sparse data: fewer than `MIN_TRADES` (10) events returns `None` — no confident score from thin air.
- `predict_graduation(events, threshold=60.0) -> dict` — `{"status": "unknown", "score": None, "will_graduate": None}` on sparse/`None` input; otherwise a scored call, graduating iff score ≥ threshold. Raises `ValueError` on a threshold outside `[0, 100]`.
- `calibration_samples(seed=7, per_class=60) -> list` — seeded synthetic `(inflow_rate, graduated)` pairs: graduates drawn around 4.0 SOL/min, non-graduates around 0.8. `calibration_accuracy(threshold_rate=2.0) -> float` — how well a rate cut classifies them (0.97 on the default seed: velocity genuinely predicts the *event*).
- `calibrated_gross_edge() -> float` — the gross expectancy of following the predictor, computed from the embedded calibration outcome (55 winners at +30%, 45 losers at −30% → exactly +0.030).
- `edge_after_costs(gross_edge=None, round_trip_cost=0.04) -> float` — gross edge minus the ~4% all-in round-trip cost (fees + slippage + impact). For the calibrated predictor this is **−0.010**.
- `clears_breakeven(...) -> bool` — strictly greater than zero after costs; exactly at cost does not clear. `breakeven_gross_edge(round_trip_cost=0.04) -> float` — the gross edge a predictor would need just to break even.

`demo.py` contrasts a fast and a slow launch, shows the sparse-data verdict, and prints the calibration-versus-costs reckoning.

## Test suite

39 tests, all passing (`pytest -q`: "39 passed"). Covered: `Flow` validation; rate functions raising on empty/`None`/single-event/zero-duration streams; hand-computed inflow rate, trade rate, normalized velocity, progress rate, and minutes-to-graduation on a 20-event fixture (including out-of-order input being sorted, and `None` once the target is reached); every velocity-band boundary (0.5 / 2.0 / 5.0, at = next band up); the sparse-data contract (`MIN_TRADES − 1` events → score `None` and an `"unknown"` prediction that makes no call; exactly `MIN_TRADES` → scored); fast-stream graduation calls vs slow-stream rejections; score monotonicity in pace; calibration determinism, class separation, and ≥ 0.85 classification accuracy; and the pinned cost verdict — `ROUND_TRIP_COST` is 0.04, the calibrated gross edge is exactly +0.030, `edge_after_costs()` is exactly −0.010, `clears_breakeven()` is `False`, and a predictor would need a gross edge above the full 4% cost to change that.

## Captured output

Verbatim output of `python demo.py`:

```
=== Liquidity velocity lab (synthetic launch inflows) ===

-- Fast launch --
inflow rate: 18.95 SOL/min (igniting)
trade rate: 6.32 trades/min
curve progress rate: 22.29 pts/min
prediction: {'status': 'scored', 'score': 78.0, 'will_graduate': True, 'trade_count': 20}

-- Slow launch --
inflow rate: 0.11 SOL/min (slow)
trade rate: 1.05 trades/min
curve progress rate: 0.12 pts/min
prediction: {'status': 'scored', 'score': 5.0, 'will_graduate': False, 'trade_count': 20}

-- Sparse data fails closed --
3 events -> {'status': 'unknown', 'score': None, 'will_graduate': None, 'trade_count': 3}

-- Calibration vs costs (the honest part) --
Velocity predicts graduation with accuracy 0.97
Calibrated gross edge of following it: +0.030
Round-trip cost: ~4%
Edge after costs: -0.010
Clears breakeven: False
Predicting graduation is not the same as profiting from it.
```

## What this mirrors in the live bot

This mirrors the ignition metrics on the live bot's spark/degen feeds — the first-minutes inflow pace, trade pace, and curve-progress rate used to decide whether a launch is actually running — and one of the bot program's core research findings: liquidity velocity *does* predict graduation (the calibration accuracy here is real), but graduation is an event, not a profit. Tokens that graduate still mostly bleed afterward, so a predictor whose gross edge is ~3% is underwater the moment the ~4% round trip is charged — the same arithmetic that keeps the live lanes paper-only until a ledger, not a predictor, shows positive expectancy after costs. It is a teaching/checking model only; it shares no code with the live bot.

## Self-exam

1. Why does `inflow_rate` divide by the span from first to last event instead of a fixed window, and what stream shape does that choice flatter?
2. A launch has 3 events totalling 90 SOL in 20 seconds. Its raw inflow rate is enormous — why does the predictor still refuse to score it?
3. The calibration accuracy is 0.97 and the predictor still loses money. Explain, in one sentence each, what the accuracy measures and what it does not.
4. `minutes_to_graduation` returns `None` once the target is collected rather than 0. Why is that distinction worth a separate value?
5. If round-trip costs fell to 2%, would this predictor clear breakeven? What gross edge would a predictor need at a 6% cost, and why does `clears_breakeven` require *strictly* more than the cost?
6. Trades-normalized velocity and raw inflow rate disagree about which of two launches is "faster". Construct the two streams and say which measure you would trust for spotting manufactured pace, and why.
