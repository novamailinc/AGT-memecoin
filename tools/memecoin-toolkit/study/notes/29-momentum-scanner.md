# Momentum Scanner — study notes

## Core ideas (teach-back)

A momentum scanner does not hunt newborns. It hunts pools that are *already running* and asks one
question of each: has this pool earned a card? Just as important, it must give a **counted reason** for
every pool that has not — because a scanner that silently drops pools cannot tell you whether it is
being selective or being broken.

`momentumscan.py` is a qualification funnel. A `Pool` carries `mint, liquidity_usd, volume_24h,
change_h24_pct, change_h1_pct`, and optionally `change_m5_pct`. Construction is fail-closed: an empty
mint or any non-numeric (or boolean) required field raises `ValueError`, so a pool with missing data
never reaches the funnel at all.

The thresholds:

- `MIN_LIQUIDITY = 50_000` — at least $50k liquidity
- `MIN_VOLUME_24H = 100_000` — at least $100k 24h volume
- `MIN_GAIN_24H = 20.0` — at least +20% gain over 24h
- `DUMP_GUARD_H1 = -15.0` — reject if the last hour is worse than −15%
- `COOLDOWN_S = 6h` — one card per mint per 6 hours

The funnel's checks run in a fixed order, and the order is part of the design (see the derivations):

1. cooldown
2. liquidity
3. volume
4. 24h gain
5. dump guard
6. positive momentum present
7. safety

Safety deserves its own paragraph. It enters the funnel as an *injected predicate*, `safety_ok(mint)`,
so the funnel can be tested without any API. And it fails closed **three** ways:

- no predicate supplied → reject, reason `safety`
- predicate returns `False` → reject, reason `safety`
 
- predicate *raises* → reject, reason `safety`

A crashed safety check never waves a pool through "just this once". Unknown safety is unsafe.

The lab's defining regression: **there is no upper ceiling on 24h gain.** `MIN_GAIN_24H` is a floor and
only a floor. The fixture that pins this is the Doomsday-shaped pool: h24 **+2540%**, liquidity $96k,
volume $900k, h1 +19.8%. It meets every legitimate rule, and it *must* qualify — a test also pushes an
extreme +100,000% gain through, so any future "sanity cap" on gain turns the suite red.

Why is a ceiling so dangerous here, specifically? A momentum scanner exists to find the pools running
hardest. A gain ceiling selectively removes exactly that population — the biggest runners — while
leaving everything mediocre untouched. It is a filter pointed at the target.

Two bookkeeping rules complete the pattern:

- **Only a qualifying pool records a cooldown entry.** A rejection never mutes a mint, so a pool rejected during a transient safety outage is not silenced for 6 hours afterwards.
- **Every scan fills a stats dict.** `new_stats()` starts zeroed with every reject reason plus `qualified` and `scanned`; each `qualify` call increments `scanned` and exactly one outcome key. A silent blockage then shows up as a counted reason instead of as an absence nobody notices.

## Key derivations & formulas

There is no continuous math; the derivation is the funnel itself — a state machine over one pool, plus
the cooldown invariant.

### The qualification predicate

```text
qualify(pool) <=> pool is a Pool
                  AND mint not on cooldown
                  AND liquidity_usd  >= 50_000
                  AND volume_24h     >= 100_000
                  AND change_h24_pct >= 20.0        (no maximum)
                  AND change_h1_pct  >= -15.0       (dump guard)
                  AND has_positive_momentum(pool)
                  AND safety_ok(mint) is True
```

Each failed conjunct maps to exactly one reject reason: `bad_pool, cooldown, liq_low, vol_low,
gain24_low, dump_guard, no_momentum, safety` — the full `REJECT_REASONS` tuple.

Boundary arithmetic, pinned by tests:

- gain 20.0 passes; 19.9 rejects (`gain24_low`)
- liquidity $50,000 passes; $49,999 rejects
- volume $100,000 passes; $99,999 rejects
- h1 exactly −15.0 passes the guard; −15.01 rejects (`dump_guard` fires on `h1 < -15.0`, strictly)

### Priority by order

Because checks short-circuit in order, the reported reason is the *first* failure. A thin pool with a
huge gain counts as `liq_low`, never `gain24_low` — it never reaches the gain check. This is deliberate:
the stats then tell you which bar is actually doing the rejecting at each stage, and a funnel whose
biggest reject bucket suddenly changes is a funnel whose behaviour changed.

### Positive momentum

```text
has_positive_momentum(pool) <=> change_h1_pct > 0
                                OR (change_m5_pct is not None
                                    AND change_m5_pct > 0)
```

Zero is not positive. When m5 is absent, the check falls back to h1 alone. Note the interplay with the
dump guard: a pool can survive the guard (−15.0 ≤ h1 ≤ 0) yet still fail here if neither h1 nor m5 is
positive. Conversely, a strongly positive m5 does *not* rescue a pool failing the dump guard — the guard
runs first.

### The cooldown invariant

```text
on_cooldown(mint, now) <=> a card was recorded for mint
                           AND (now − last_card_time) < 6 * 3600
```

Strict inequality: at exactly 6h the cooldown has expired and the mint may card again. Cooldowns are
per-mint — one mint's card never mutes another. `CooldownBook.record` validates (non-empty mint, `now >=
0`).

And the recording rule ties the state machine shut:

```text
cooldown.record(mint, now) happens ONLY on the success path,
after every check including safety has passed.
```

`scan(pools, ...)` runs the batch over one shared cooldown book, so a duplicate mint inside a single
scan dedupes via `cooldown` — the second occurrence rejects with reason `cooldown`.

## Worked example

From `demo.py`: eight pools scanned with `safety = lambda mint: mint != "RugMint"`, a fresh cooldown
book, at `now = 1000.0`.

Qualified: `['GoodMint', 'DoomsdayMint']`.

- `GoodMint` — the `good_pool()` defaults: liquidity $120k, volume $500k, h24 +80%, h1 +10%, m5 +3%.
- `DoomsdayMint` — liquidity $96k, volume $900k, h24 **+2540%**, h1 +19.8%, m5 +5%. Qualifies. This is the regression case walking through the demo.

The six rejections, one per reason:

| pool | defect | reason |
|------|--------|--------|
| ThinMint | liquidity $10,000 | `liq_low` |
| QuietMint | volume $5,000 | `vol_low` |
| FlatMint | h24 +5% | `gain24_low` |
| DumpMint | h1 −40% | `dump_guard` |
| RugMint | safety predicate False | `safety` |
| StaleMint | h1 −5%, m5 −1% | `no_momentum` |

Resulting stats:

```text
{'gain24_low': 1, 'liq_low': 1, 'vol_low': 1, 'dump_guard': 1,
 'no_momentum': 1, 'safety': 1, 'cooldown': 0, 'bad_pool': 0,
 'qualified': 2, 'scanned': 8}
```

Every pool is accounted for: 2 qualified + 6 rejected = 8 scanned.

Cooldown, demonstrated on the Doomsday pool:

- re-checked on a *fresh* book → `(True, None)`
- re-checked within 6h on the *same* book that carded it (now = 2000) → `(False, 'cooldown')`

StaleMint deserves a second look: h24 +50%, $200k liquidity, $300k volume — it passes the size and gain
bars, survives the dump guard (h1 −5% > −15%), and is still rejected, because neither its hour nor its 5
minutes is rising. Big and *currently falling* is not momentum.

## Trap log

- T1. **The "sanity" gain ceiling.** Capping 24h gain at, say, 300% sounds prudent and selectively deletes the biggest runners — the exact population a momentum scanner exists to find. This is the lab's founding regression, pinned by the +2540% and +100,000% fixtures.
- T2. **Judging a runner by its 24h number alone.** +2540% on the day says nothing about the last hour. The dump guard (h1 < −15%) and the positive-momentum check are what stop a collapsing pool wearing a huge daily gain from carding.
- T3. **Fail-open safety.** A safety API outage, a missing predicate, or an exception must all read as "not proven safe". Any path where a *crashed* check admits the pool converts an infrastructure failure into a safety failure.
- T4. **Cooldown on rejection.** Muting a mint for 6h because it was rejected — e.g. during a transient safety outage — punishes pools for the scanner's own downtime and hides real runners when service recovers.
- T5. **Silent dropping.** Returning only the qualified pools makes a broken funnel indistinguishable from a selective one. Counted reject reasons are the funnel's instrumentation; without them the 300%-ceiling bug produced no signal at all.
- T6. **Reading reject reasons as order-free.** The first-failure rule means buckets are not independent tallies of defects. A pool counted `liq_low` may also have low gain — the stats describe the funnel's decision path, not the pools' full defect lists.
- T7. **Zero treated as momentum.** An h1 of exactly 0 with no positive m5 is flat, not rising. "Not falling" is not the claim a momentum card makes.
- T8. **Trusting the predicate's exceptions to be rare.** The raising-predicate case is tested because in production the safety source *will* throw — timeouts, malformed replies — and the funnel's behaviour in that moment is the safety property.

## Source flags

- All thresholds, the reject-reason tuple, check order, and boundary behaviours are from `momentumscan.py`. [S]
- Coverage claims — 38 tests passing, including the Doomsday regression, an extreme +100,000% gain, all boundary pairs, the three safety fail-closed modes, and cooldown expiry at exactly 6h — are the README's test-suite account. [S]
- The historical claim — an invented 300% ceiling in the live scanner silently blocked a real runner on 2026-10-19 and was removed, with this lab's fixture as the guard against its return — is the README's mirroring account of the live bot. [M]
- The reading that reject-reason *order* is instrumentation design, not an accident, is reasoned here from the funnel's implementation and the README's self-exam. [R]

## One-paragraph verdict

This toolkit proves a funnel can be both strict and legible: fixed size and gain floors ($50k liquidity, $100k volume, +20% h24), a −15% hourly dump guard, a strict positive-momentum requirement, fail-closed safety in all three failure modes, and a 6h per-mint cooldown that only successes trigger — with every rejection counted by reason and the no-ceiling property on 24h gain pinned by a +2540% regression fixture so it cannot silently regress. It does not prove these thresholds are optimal, that the injected safety predicate resembles a real safety source's accuracy, or that qualifying pools are profitable; it proves the scanner applies its stated rules, at their stated boundaries, and can show its working for every pool it refused.
