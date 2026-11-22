# Momentum Scanner (lab)

A from-scratch, pure-stdlib lab for the trending-pool qualification funnel: which already-running pools earn a card, and — just as important — a counted reason for every pool that does not. Everything is synthetic: pools are hand-built fixtures and safety is an injected predicate, so the funnel's logic is testable without any API. The lab carries one regression in its bones: there is **no upper ceiling** on 24h gain.

## What it does

Module `momentumscan.py` — thresholds `MIN_LIQUIDITY = 50_000`, `MIN_VOLUME_24H = 100_000`, `MIN_GAIN_24H = 20.0` (percent, no maximum), `DUMP_GUARD_H1 = -15.0`, `COOLDOWN_S = 6h`, and reject reasons `gain24_low, liq_low, vol_low, dump_guard, no_momentum, safety, cooldown, bad_pool`:

- `Pool(mint, liquidity_usd, volume_24h, change_h24_pct, change_h1_pct, change_m5_pct=None)` — a frozen-shape dataclass. Fail-closed: construction raises `ValueError` on an empty mint or any non-numeric (or boolean) required field — a pool with missing data never reaches the funnel.
- `has_positive_momentum(pool) -> bool` — positive-momentum *presence*: `h1 > 0`, or `m5 > 0` when present. Zero is not positive.
- `qualify(pool, safety_ok=None, cooldown=None, now=0.0, stats=None) -> (bool, str | None)` — the funnel, in order: cooldown → liquidity ≥ $50k → 24h volume ≥ $100k → 24h gain ≥ +20% (**no upper bound**) → dump guard (`h1 < −15%` rejects) → positive momentum required → safety. Fail-closed on safety: a missing predicate, a `False` return, *or a predicate that raises* all reject with reason `safety`. A non-`Pool` input rejects as `bad_pool`. Only a qualifying pool records a cooldown entry — a rejection never mutes a mint. When a `stats` dict is passed, every call increments `scanned` plus exactly one of `qualified` or the reject reason.
- `CooldownBook()` — `on_cooldown(mint, now) -> bool` (strictly inside 6h) and `record(mint, now)` (raises `ValueError` on empty mint / negative time). Cooldowns are per-mint.
- `new_stats() -> dict` — a zeroed stats dict with every reject reason plus `qualified`/`scanned`; `scan(pools, ...) -> (qualified_pools, stats)` runs the funnel over a batch sharing one cooldown book, so a duplicate mint in one scan dedupes via `cooldown`.
- Fixtures: `good_pool(**over)` (a qualifying pool with overridable fields) and `doomsday_pool()` — the regression shape: h24 **+2540%**, liquidity $96k, high volume, h1 +19.8%.

`demo.py` scans eight pools (one per reject reason plus two qualifiers), prints the stats dict, then re-checks the Doomsday pool on a fresh book and inside its cooldown.

## Test suite

38 tests, all passing (`pytest -q`: "38 passed"). Covered: the happy path; **the Doomsday regression — a +2540% pool must qualify** — plus an extreme +100,000% gain, pinning that no ceiling exists; boundary arithmetic on every bar (gain 20.0 passes / 19.9 rejects, liquidity $50k / $49,999, volume $100k / $99,999, dump guard at exactly −15.0 vs −15.01); the dump guard firing even with a positive m5; momentum via m5 only, zero-not-positive, and m5-absent fallbacks; safety failing closed three ways (false, missing predicate, raising predicate); `bad_pool` for non-Pool input and `Pool` construction validation; cooldown blocking a second card, expiring at exactly 6h, per-mint isolation, record validation, rejections never starting a cooldown, and scan-level dedup; reject-reason priority (liquidity before gain); and the stats dict — every reason counted, all keys present on an empty scan, `new_stats` zeroed, and the pinned threshold constants.

## Captured output

Verbatim output of `python demo.py`:

```
=== Momentum scanner lab (no upper gain ceiling) ===
Qualified: ['GoodMint', 'DoomsdayMint']
Stats: {'gain24_low': 1, 'liq_low': 1, 'vol_low': 1, 'dump_guard': 1, 'no_momentum': 1, 'safety': 1, 'cooldown': 0, 'bad_pool': 0, 'qualified': 2, 'scanned': 8}
Doomsday re-check (fresh book): (True, None)
Doomsday again within 6h (same book): (False, 'cooldown')
```

## What this mirrors in the live bot

This mirrors `daemon/launch_scanner/momentum.py` — the scanner that hunts already-running trending pools (known mcap lanes, $100k volume / $50k liquidity floors, ≥+20% 24h gain, an hourly dump guard, the 1/100 safety bar behind the injected predicate here, and a 6h per-mint card cooldown) — *after* the 2026-10-19 ceiling removal. Before that fix, an invented 300% maximum on 24h gain sat in the funnel, and it silently blocked a real runner (the Doomsday-shaped case: +2540% on the day, $96k liquidity, positive hourly) that met every legitimate rule. The regression fixture exists so that ceiling can never creep back in unnoticed: any future "sanity cap" on gain turns this suite red. The stats dict mirrors the live funnel counters that make a silent blockage visible as a counted reject reason instead of an absence. It is a teaching/checking model only; it shares no code with the live bot.

## Self-exam

1. Why is an upper ceiling on 24h gain especially dangerous in a *momentum* scanner — what population does a ceiling selectively remove?
2. The Doomsday pool's h1 was +19.8% while its h24 was +2540%. Which checks besides the gain bar protect the funnel from a pool that is actually collapsing right now?
3. The dump guard rejects at h1 < −15% but a pool at exactly −15.0 can still qualify. What else must be true for that pool to earn a card, and is that the right combination?
4. Safety fails closed three different ways in this lab (false, missing, raising). Why should a *crashed* safety check reject rather than let the pool through "just this once"?
5. A rejected pool never starts its 6h cooldown. What user-visible harm would the opposite rule cause after a transient safety-API outage?
6. Why does every scan fill a stats dict of reject reasons instead of just returning the qualified pools — what failure mode stays invisible without it?
7. Liquidity is checked before gain, so a thin pool with a huge gain counts as `liq_low`, never `gain24_low`. Why does the *order* of checks matter for reading the funnel stats?
