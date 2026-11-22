# Feed Data Quality (lab)

A from-scratch, pure-stdlib lab for choosing the correct pair for one mint out of a DexScreener-style multi-pair payload. Everything here is synthetic — no network, no live bot code, no real token data — so the classic failure (blindly trusting `pairs[0]`) can be reproduced exactly and every rejection path can be exercised safely.

## What it does

Module `feedquality.py` — frozen dataclasses `PairRecord(pair_address, base_address, quote_symbol, price_usd, liquidity_usd, volume_h24_usd, timestamp_ms)` and `Selection(pair, status, candidates_considered, malformed_skipped, stale_rejected, duplicates_dropped, disagreement_pct)`:

- `select_pair(payload, mint, now_ms, max_age_seconds=900, max_disagreement_pct=15, min_liquidity_usd=1000) -> Selection | None` — the one pair to trust, or `None`. Fail-closed throughout: `None` when no eligible fresh pair survives, and `None` when the surviving pairs disagree on price by more than the threshold — an unverifiable price is not priced by picking a side.
- `parse_pair(raw) -> PairRecord | None` — one raw entry parsed, or `None` for malformed data (non-dict, missing blocks, non-positive price, negative liquidity, unparseable numbers). Malformed entries are skipped and counted, never trusted and never raised on.
- `parse_payload(payload) -> (list[PairRecord], int)` — parsed pairs plus the malformed count. Raises `ValueError` on a non-list payload (that is a caller bug, not data).
- `dedupe_pairs(pairs) -> (list[PairRecord], int)` — one record per pair address, the highest-liquidity copy wins; returns the drop count.
- `is_stale(pair, now_ms, max_age_seconds=900) -> bool` / `pair_age_seconds(pair, now_ms) -> float | None` — stale means older than the threshold, timestamped in the future, or carrying no timestamp at all (the data timestamp falls back to `pairCreatedAt` when no explicit one is present). Missing time is stale: fail-closed.
- `rank_pairs(pairs) -> list[PairRecord]` — liquidity descending (volume breaks exact ties); quote preference (`USDC` > `SOL` > `USDT` > `WETH`/`ETH` > anything else, via `quote_rank()`) applies only inside a 10% liquidity tie band, so preference never beats a real liquidity gap.
- `price_disagreement_pct(pairs) -> float` — `(max − min) / median` price across pairs, in %; 0.0 for a single pair, raises `ValueError` on an empty list.
- `eligible_pairs(payload, mint, now_ms, ...) -> (ranked_pairs, stats)` — the full pipeline short of the disagreement check: wrong-mint pairs ignored, pairs under the dust floor ($1,000 liquidity by default) excluded before verification so a dust pool's fantasy price cannot poison the check, duplicates dropped, stale rejected; `stats` reports each count. Raises `ValueError` on an empty mint.
- Synthetic fixtures: `dust_first_payload()` (where `pairs[0]` is a $40 dust pool quoting $9.99 and the real $180k pair quoting $0.50 sits second), `disagreement_payload()` (two liquid pairs that flatly disagree on price), `messy_payload()` (duplicates, two malformed entries, another mint's pair, and a stale pair).

`demo.py` shows the `pairs[0]` trap and the selector disagreeing with it, cleans the messy payload with its skip/drop/reject counts, and shows both fail-closed `None` results.

## Test suite

49 tests, all passing (`pytest -q`: "49 passed"). Covered: the headline fixture proving `pairs[0]` is the dust pair while the selector picks the real one, at the real price; highest-liquidity selection; string and numeric prices; malformed entries (parametrised shapes, zero/negative/non-numeric price, negative liquidity) parsing to `None` and being counted; non-list payloads raising; missing volume defaulting to zero; the `pairCreatedAt` timestamp fallback; staleness — fresh, old, the exact 900s boundary, missing timestamp, and future timestamp all fail-closed correctly; an all-stale payload and a stale richest pair never winning; a configurable staleness threshold; quote-rank ordering and case-insensitivity; liquidity ranking; quote preference breaking a near-tie but never beating a big liquidity gap; volume tie-breaking; de-duplication keeping the richest copy; disagreement math against a hand value, the single-pair and empty cases; wild disagreement selecting `None`; close prices verifying; a configurable disagreement threshold; the dust price not poisoning verification; the messy fixture end-to-end with its exact malformed/duplicate/stale/wrong-mint counts; and empty, wrong-mint-only, all-dust, and empty-mint inputs.

## Captured output

Verbatim output of `python demo.py`:

```
=== Feed data quality lab (synthetic DexScreener-style payloads) ===

-- The pairs[0] trap --
pairs[0]: DustPair111 liquidity $40 price $9.99
selector: RealPair222 liquidity $180,000 price $0.50
candidates considered: 2, price disagreement: 2.0%

-- Messy payload --
selected: GoodPair liquidity $130,000
malformed skipped: 2, duplicates dropped: 2, stale rejected: 1

-- Fail-closed: unverifiable price --
Disagreeing pairs select: None
Empty payload selects: None
```

## What this mirrors in the live bot

This mirrors the DexScreener pair picking in the live bot's feeds: a mint almost always has several pairs — different DEXes, different quote tokens, and dust pools nobody trades — and the feeds must price a token from the pair that actually carries its market, not from whichever entry the API listed first. It also mirrors the live feeds' fail-closed habits: stale data is rejected rather than priced from, malformed entries are skipped and counted rather than trusted, and when two liquid pairs tell different price stories the honest answer is "unverifiable", not a coin flip. It is a teaching/checking model of that selection logic only; it shares no code with the live feeds.

## Self-exam

1. Why is `pairs[0]` an unsafe default even when the API usually lists a sensible pair first?
2. Why are dust pairs excluded *before* the price-disagreement check rather than merely ranked last?
3. A pair has no timestamp at all. Why is that treated as stale instead of as "probably fresh"?
4. Two liquid pairs disagree on price by 57%. Why does the selector return nothing instead of trusting the more liquid one?
5. Why does quote preference (USDC over SOL) only apply inside a 10% liquidity band — what failure does an unrestricted preference create?
6. When a duplicate pair address appears twice with different liquidity, why keep the richer copy rather than the first or an average?
