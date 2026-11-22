# Holder Concentration (lab)

Top-1 / top-10 holder concentration done the honest way: the largest account on-chain is usually a liquidity pool or bonding curve — a program-derived address that sits off the ed25519 curve — not a person. This lab computes concentration over user wallets only, using synthetic `getTokenLargestAccounts`-style fixtures and an injected account registry. Pure Python stdlib, no network.

## What it does

Given holder rows, a total supply, and a registry classifying each account, `holders.py` sorts the user wallets, computes what share of supply the biggest one and the biggest ten hold, and maps that to PASS / FLAG / FAIL. Accounts missing from the registry are never assumed to be users; empty or unmeasurable input returns verdict `"unknown"` with `passed=False` (fail-closed).

## Files

- `holders.py` — the module:
  - `analyze(holders: list[dict], total_supply: float, registry: dict) -> HolderAnalysis` — top-1/top-10 user percentages and verdict. Fail-closed: empty holders, non-positive/missing supply, or zero classifiable user wallets → verdict `"unknown"`, `passed=False`, percentages `None`.
  - `classify_holder(address: str, registry: dict | None) -> str` — `"user"` iff the registry entry has `on_curve=True` and is not program-owned; off-curve/program accounts → `"program"`; absent or malformed entries → `"unknown"`, never `"user"`.
  - `is_user_wallet(address: str, registry) -> bool` — thin wrapper over `classify_holder`.
  - `user_holders_sorted(holders: list, registry) -> list[tuple[str, float]]` — user wallets only, biggest first; malformed rows are skipped.
  - `verdict_for(top1_pct: float, top10_pct: float) -> str` — FAIL if top1 > 50 or top10 > 70; FLAG if top1 > 25 or top10 > 55; else PASS. All comparisons are strict, so exactly 50.0 / 70.0 is FLAG and exactly 25.0 / 55.0 is PASS.
  - `aggregate_user_balances(holders, registry) -> list[tuple[str, float]]` — user balances summed per address (one wallet can appear in several token accounts), biggest first; program/unknown/malformed rows skipped.
  - `top_n_user_pct(holders, total_supply, registry, n) -> float | None` — the top-1/top-10 rule generalised to any n (`n=1`/`n=10` reproduce `analyze`); raises `ValueError` unless n is a positive int; unusable supply or no user wallets → `None`.
  - `herfindahl_index(holders, total_supply, registry) -> float | None` — HHI over aggregated user shares on the 0–10000 scale (monopoly 10000, ten equal holders 1000); fail-closed `None` on unusable supply or no user wallets.
  - `effective_holder_count(holders, total_supply, registry) -> float | None` — inverse Simpson index over shares normalised among user holders: the equal-sized-holder equivalent (two equal holders → 2.0 whatever their combined share).
  - `nakamoto_coefficient(holders, total_supply, registry, threshold_pct=50.0) -> int | None` — smallest number of top aggregated holders whose cumulative share is *strictly greater* than the threshold (exactly 50.0% is not a majority); `None` when the threshold is never exceeded.
  - `HolderAnalysis` — dataclass result: `top1_user_pct`, `top10_user_pct`, `verdict`, `passed`, holder/excluded/unknown counts, `reason`.
  - Amount hardening shared by all of the above: negative, NaN or infinite amounts are malformed rows (never negative concentration), and a NaN/infinite supply fails closed to `"unknown"` / `None` exactly like a missing one.
- `demo.py` — three scenarios: pool-dominated token with spread users, a genuine majority whale, and the no-data case.
- `test_holders.py` — the pytest suite below.

## Test suite

45 tests, all passing. Coverage: the pool-exclusion fixture where raw top-1 is the pool but user top-1 is far smaller; bonding-curve/escrow exclusion; top-10 summation over exactly ten users; every verdict boundary (50.0 = FLAG vs 50.01 = FAIL, 70.0/70.01, 25.0/55.0 strictness); top-10 alone triggering FLAG and FAIL; empty holders → unknown; missing/zero/negative/non-numeric supply → unknown; no user wallets → unknown (not PASS); unregistered accounts excluded and counted unknown; the `owner` key alias. Plus: classification edges (on-curve but program-owned → program, missing `owner_program` key → user, non-boolean `on_curve` / non-dict / `None` registry entries → unknown, `None`/empty registry → everything unknown); sorting order with address tie-breaks; every malformed-row shape skipped (missing address/amount, non-numeric/`None` amounts, non-dict rows); numeric-string amounts and supplies accepted; negative/NaN/infinite amounts counted malformed; `None` holders and NaN/infinite supply → unknown; excluded/unknown/malformed counted separately on a mixed fixture; top-10 with fewer than ten users, and the single-user top1 == top10 case; FLAG counts as passed; verdict corner values (0/0, 100/100, each metric alone at FAIL); aggregated balances summing duplicate rows per address; `top_n_user_pct` reproducing top-1/top-10, generalising to n=3 and n > holder count, and rejecting bad n; HHI at monopoly / equal-split / skewed distributions, pool exclusion, and fail-closed `None`s; effective holder counts for equal splits and the all-zero-balance `None`; Nakamoto coefficients for a majority whale, the exactly-50%-is-not-a-majority boundary, a never-exceeded threshold, aggregated duplicate rows, and bad-threshold `ValueError`s.

## Captured output

Verbatim output of `python demo.py` (exit 0):

```
Holder concentration lab -- pools are not whales
====================================================
Scenario 1: big pool, spread-out users
  naive top-1 (pool included): 40.0% of supply
  user top-1: 6.0% | top-10 users: 12.0% | verdict: PASS (passed=True)
  note: top1 user 6.00%, top10 users 12.00%; excluded=1, unknown=0

Scenario 2: one user wallet quietly holds the majority
  naive top-1 (pool included): 52.0% of supply
  user top-1: 52.0% | top-10 users: 57.0% | verdict: FAIL (passed=False)
  note: top1 user 52.00%, top10 users 57.00%; excluded=1, unknown=0

Scenario 3: no data at all (fail-closed)
  naive top-1 (pool included): n/a (no holders)
  user top-1: n/a | top-10 users: n/a | verdict: unknown
  note: no holder data; excluded=0, unknown=0

```

## What this mirrors in the live bot

The live bot's safety pipeline judges holder concentration from token reports where the biggest accounts include pool and curve accounts; reading those raw would mislabel almost every liquid token as whale-controlled. The live holder/distribution proof check applies the same discipline — top-holder percentages only mean something once program accounts are out of the numerator — and the same fail-closed posture: unknown safety data blocks rather than passes.

## Self-exam

1. A token's largest account holds 60% of supply and is an off-curve pool. What is its contribution to top-1 user concentration, and why?
2. Top-1 user share is exactly 50.0%. Which verdict results, and which single change of +0.01 would change it?
3. Why does an account missing from the registry fail closed instead of being counted as a user?
4. Top-10 user share is 72% but top-1 is only 9%. Which verdict results, and what does that distribution shape suggest?
5. An analysis returns verdict `"unknown"` with `passed=False`. Name two distinct input problems that produce it.
