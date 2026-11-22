# Feed Data Quality — study notes

## Core ideas (teach-back)

A mint almost never has one pair. It has several — different DEXes,
different quote tokens, and dust pools nobody trades — and a
DexScreener-style payload lists them in *some* order. The classic
bug is trusting `pairs[0]`: the first entry is whatever the API
happened to order first, and it is often a $40 dust pool quoting a
fantasy price. This toolkit builds the careful selector:
`select_pair(payload, mint, now_ms)` returns the one pair to trust,
or **None** — fail-closed in both directions (no eligible pair, or
an unverifiable price) [S].

The pipeline, in order:

1. **Parse.** Each raw entry becomes a `PairRecord` or is counted
   malformed and skipped — non-dict entries, missing blocks,
   non-positive price, negative liquidity, unparseable numbers.
   Malformed data is never trusted and never raised on. (A non-list
   *payload*, by contrast, raises `ValueError`: that is a caller
   bug, not data.) Volume defaults to 0 when missing; the data
   timestamp falls back to `pairCreatedAt` when no explicit
   timestamp is present.
2. **Filter to the mint.** Pairs for a different base token are
   ignored (counted as `wrong_mint`).
3. **Dust floor.** Pairs under **$1,000 liquidity** (default,
   configurable) are excluded *before* verification — their prices
   are noise, and a dust price must not be allowed to poison the
   cross-check in step 6.
4. **Dedupe.** One record per pair address; the **highest-liquidity
   copy wins**; drops are counted.
5. **Staleness.** A pair is stale if its data is older than
   **900 seconds** (default), timestamped in the future, or has **no
   timestamp at all**. Missing time is stale: fail-closed. Stale
   pairs are rejected and counted, however liquid they are.
6. **Rank and verify.** Survivors are ranked by liquidity, then the
   survivors' prices must agree: if the price disagreement exceeds
   **15%** (default), selection returns **None**. An unverifiable
   price is not priced by picking a side.

Ranking deserves care: liquidity descending is primary (24h volume
breaks exact liquidity ties). Quote preference — **USDC > SOL >
USDT > WETH/ETH > anything else** — applies *only inside a 10%
liquidity tie band*, so preference can reorder near-equals but
never beats a real liquidity gap [S].

## Key derivations & formulas

**Price disagreement** — the verification statistic:

```
disagreement_pct = (max_price − min_price) / median_price × 100
```

0.0 for a single pair (nothing to disagree with); raises
`ValueError` on an empty list. Selection fails closed when
`disagreement_pct > max_disagreement_pct` (default 15); a value
exactly at the threshold still verifies, since the check is
strictly-greater [S].

**Staleness.**

```
age_seconds = (now_ms − timestamp_ms) / 1000
stale ⇔ age is None (no timestamp) OR age < 0 (future) OR age > max_age
```

The 900s boundary is exact in the tests: at the threshold the pair
is still fresh; beyond it, stale.

**Quote tie band.** In `rank_pairs`, after sorting by liquidity
descending, an adjacent pair with a better `quote_rank` swaps ahead
only if:

```
back.liquidity × 1.10 ≥ front.liquidity   AND   back.liquidity < front.liquidity
```

i.e. the challenger is within 10% of the leader's liquidity and not
already ahead. `quote_rank` is case-insensitive; unlisted quotes
rank after all preferred ones [S].

**Selection accounting.** A successful `Selection` carries the
winning pair plus the audit counts: `candidates_considered`,
`malformed_skipped`, `stale_rejected`, `duplicates_dropped`, and
the final `disagreement_pct` — so a caller can see *how much* of
the payload was thrown away to trust the answer [S].

## Worked example

**The `pairs[0]` trap** (`dust_first_payload`) [S]:

- `pairs[0]`: DustPair111 — liquidity **$40**, price **$9.99**.
- RealPair222 — liquidity **$180,000**, price **$0.50**; plus
  MidPair333 — $60,000, price $0.51.
- The $40 pair is under the $1,000 dust floor and is excluded
  *before* verification. Disagreement over the survivors:
  `(0.51 − 0.50) / 0.505 × 100 ≈ 2.0%` — well under 15%.
- Selector returns RealPair222 at **$0.50** with
  `candidates_considered = 2`. The naive reader is off by ~20x on
  price because it trusted list order.

**Messy payload** (`messy_payload`): the selector picks GoodPair at
its richest duplicate copy, liquidity **$130,000**, with
`malformed_skipped = 2`, `duplicates_dropped = 2`,
`stale_rejected = 1`, and 1 wrong-mint pair ignored. Note the stale
pair quoted $400,000 of liquidity — the richest entry in the
payload — and still lost, because stale data is rejected before
ranking [S].

**Disagreement payload**: two liquid pairs at $0.50 and $0.90 —
median $0.70, disagreement `(0.90 − 0.50)/0.70 ≈ 57%` > 15% — so
`select_pair` returns **None**, even though both pairs are liquid,
fresh, and well-formed. Empty payload also returns None [S].

## Trap log

- T1. Trusting `pairs[0]`. List order is an API accident, not a
  market fact; in the fixture it is wrong on price by ~20x.
- T2. Ranking dust instead of excluding it. If the $9.99 dust quote
  entered the disagreement check, honest pairs would look
  "unverifiable" and selection would fail — dust must be removed
  *before* verification, not merely sorted last.
- T3. Treating a missing timestamp as "probably fresh". No
  timestamp means no evidence of freshness: stale, fail-closed.
  Future timestamps fail the same way.
- T4. Picking the more liquid side when pairs disagree. At 57%
  disagreement at least one feed is lying; choosing is guessing
  with extra steps. The honest answer is None.
- T5. Letting quote preference roam free. Unrestricted, a USDC dust
  pair would outrank the real SOL market; the 10% band confines
  preference to genuine near-ties.
- T6. Averaging or first-keeping duplicate pair records. The same
  address quoted twice is one market reported twice; keep the
  richest copy, count the drop.
- T7. Raising on malformed entries. One broken entry must not kill
  the whole payload — skip and count it. (But a non-list payload is
  a programming error and *does* raise.)

## Source flags

- Pipeline order, defaults (900s staleness, 15% disagreement,
  $1,000 dust floor, 1.10 tie band, quote order), the disagreement
  formula, and all fixture numbers are from `feedquality.py`,
  covered by `test_feedquality.py` (49 tests) [S].
- The ~20x mispricing figure is arithmetic on the fixture prices
  (9.99 vs 0.50) [R].
- The mirror to the live feeds' DexScreener pair picking and
  fail-closed habits is per the README [M].
- Flag for the report: the module docstring in `feedquality.py`
  paraphrases the tie rule as preference never beating a "10x
  liquidity gap", while the code constant is a 10% band (×1.10) and
  the README states 10% — the docstring wording is loose; the code
  and README agree [S].

## One-paragraph verdict

This toolkit proves that pair selection is a data-quality pipeline,
not an index lookup: parse defensively, exclude dust before it can
poison verification, reject stale data however liquid it looks,
rank by liquidity with quote preference confined to a 10% band, and
refuse to price when surviving pairs disagree by more than 15%. It
does **not** prove the selected price is *correct* — two fresh,
liquid pairs can agree on a wrong price — and its thresholds (900s,
15%, $1,000) are documented defaults, not validated optima; what it
guarantees is that whatever price you use survived an auditable
set of rejections, with the counts to show for it.
