# Wash & Bot-Share Detection (lab)

A from-scratch, pure-stdlib lab for the manipulation detectors that run over a token's early trade stream: wash trading (the same wallets round-tripping volume to fake demand), bot share (transactions showing machine patterns), and bump farming (repeated dust buys meant to push a token up a trending list). Everything here is synthetic — no network, no live bot code, no real token data — so every share can be checked by hand and every failure mode can be exercised safely.

## What it does

Module `wash.py` — one frozen dataclass, `Trade(wallet, side, amount_sol, ts=0.0, slot=None)`, plus stream functions over lists of trades:

- `Trade(...)` — a single synthetic trade. Fail-closed: construction raises `ValueError` on an empty wallet, a side other than `"buy"`/`"sell"`, a non-positive/`None` amount, a negative timestamp, or a negative slot.
- `total_volume(trades) -> float` / `trade_count(trades) -> int` / `distinct_wallets(trades) -> int` — stream basics. All stream functions raise `ValueError` on `None` or empty input rather than returning a plausible-looking zero.
- `wallet_volumes(trades) -> dict` — `{wallet: (buy_volume, sell_volume)}`.
- `roundtrip_volume(trades) -> float` — summed over wallets as `2 * min(buy, sell)`: the round-tripped part of each wallet's flow, both legs counted.
- `wash_share(trades) -> float` — round-trip volume / total volume, in `[0, 1]`. Pure one-sided buying scores 0; a stream where every wallet buys and sells the same size scores 1.
- `is_tiny(amount_sol) -> bool` — strictly below 0.10 SOL. Raises `ValueError` on non-positive input.
- `bot_like_mask(trades) -> list[bool]` — a trade is bot-like when *any* bot pattern holds: tiny size (< 0.10 SOL), a high-frequency wallet (≥ 8 trades in the stream), or a crowded slot (≥ 3 trades sharing one slot; `None` slots never cluster).
- `bot_share(trades) -> float` — the *count* share of bot-like transactions. This is a NEGATIVE signal only: `negative_signal_score(trades) -> float` maps it to a penalty in `[-100, 0]` and can never go positive.
- `bot_volume_share(trades) -> float` — the volume-weighted counterpart, which can diverge sharply from the count share (one whale trade plus nine dust bots: 90% by count, < 1% by volume).
- `bump_mask(trades) -> list[bool]` / `bump_share(trades) -> float` — a bump trade is a small buy (< 0.10 SOL) from a wallet with ≥ 3 such small buys; small *sells* never count.
- `same_slot_clusters(trades) -> int` — number of slots carrying ≥ 3 trades.
- `toxicity_score(trades) -> float` — combined burden in `[0, 100]`: `50·wash + 30·bot + 20·bump`, capped at 100.
- `flags_for(wash, bot, bump) -> list[str]` — threshold flags `WASH_SKEW` (≥ 0.40), `BOT_SWARM` (≥ 0.50), `BUMP_FARM` (≥ 0.50); a share exactly *at* the threshold flags. Raises `ValueError` on shares outside `[0, 1]`.
- `assess(trades, min_trades=10) -> dict` — the fingerprint verdict, with fail-open semantics: `None` or fewer than `min_trades` trades returns `{"status": "unknown", "flags": [], ...}` with all shares `None` — missing data is **never a pass and never a veto**. Only a sufficiently large stream can return `"clean"` (fires nothing) or `"flagged"`.

`demo.py` runs a clean stream, a wash stream, a bot stream, and the missing-data case through the detectors.

## Test suite

39 tests, all passing (`pytest -q`: "39 passed"). Covered: trade validation (bad side, zero/negative/`None` amounts, empty wallet, bad slot/timestamp); every share function raising on empty/`None`; the tiny-size boundary at exactly 0.10 SOL; wash hand-values (full round-trip = 1.0, partial round-trip = 8/24, sell-heavy wallets washed on the smaller leg); bot patterns individually (tiny size, the ≥ 8 high-frequency boundary with 7 trades just below it, same-slot clusters with 2-in-a-slot just below, `None` slots never clustering); count share vs volume share divergence; the negative signal never going positive; bump farming requiring ≥ 3 small *buys* from one wallet; threshold boundaries for all three flags (exactly at = flags, one epsilon below = nothing) plus stable flag ordering and out-of-range rejection; and the fail-open fingerprint contract (`None`/empty/9-trade streams are `"unknown"`, a clean 10-trade stream is `"clean"` and fires nothing, wash/bot/bump streams each flag their own fingerprint).

## Captured output

Verbatim output of `python demo.py`:

```
=== Wash & bot-share lab (synthetic trade streams) ===

-- Clean stream (12 distinct real buyers) --
wash_share=0.00 bot_share=0.00
assess -> {'status': 'clean', 'flags': [], 'wash_share': 0.0, 'bot_share': 0.0, 'bump_share': 0.0, 'toxicity_score': 0.0, 'trade_count': 12}

-- Wash stream (6 wallets buy and sell the same size) --
wash_share=1.00 bot_share=0.00
assess -> {'status': 'flagged', 'flags': ['WASH_SKEW'], 'wash_share': 1.0, 'bot_share': 0.0, 'bump_share': 0.0, 'toxicity_score': 50.0, 'trade_count': 12}

-- Bot stream (12 dust-sized buys) --
wash_share=0.00 bot_share=1.00
assess -> {'status': 'flagged', 'flags': ['BOT_SWARM'], 'wash_share': 0.0, 'bot_share': 1.0, 'bump_share': 0.0, 'toxicity_score': 30.0, 'trade_count': 12}

-- Missing / thin data (fail-open fingerprint) --
assess(None) -> {'status': 'unknown', 'flags': [], 'wash_share': None, 'bot_share': None, 'bump_share': None, 'toxicity_score': None, 'trade_count': 0}
assess(3 trades) -> {'status': 'unknown', 'flags': [], 'wash_share': None, 'bot_share': None, 'bump_share': None, 'toxicity_score': None, 'trade_count': 3}
unknown is neither a pass nor a veto: no score is reported.

-- Fail-closed --
Empty stream correctly rejected: trades must not be empty
```

## What this mirrors in the live bot

This mirrors the live bot's manipulation layer: the `toxicity.py` fingerprints (the `BUMP_FARM` / `WASH_SKEW` / `BOT_SWARM`-style patterns measured over early trade streams) and the veto engine's wash-trade check, which treats a high same-wallet round-trip share as hard evidence of manufactured volume. It also mirrors the bot's fingerprint philosophy: these detectors run in measure/fingerprint mode where missing data reads as "unknown" — a token is never cleared *or* condemned on evidence that was never collected — and bot share only ever counts *against* a token, never for it. It is a teaching/checking model of those detectors only; it shares no code with the live bot.

## Self-exam

1. Why is wash volume for one wallet `2 * min(buy, sell)` rather than the wallet's total volume, and what does a wallet that only buys contribute?
2. Why can bot share by transaction count and bot share by volume tell opposite stories about the same stream, and which one does the toxicity score use?
3. A stream has 9 trades, all obvious wash round-trips. Why does `assess` still return `"unknown"`, and why is that the safe answer in *both* directions?
4. The high-frequency bot pattern flags every trade from a wallet with ≥ 8 trades — including its large, ordinary-looking ones. What false positive does that create, and why is the pattern still useful?
5. Why must bot share be a negative-only signal — what would go wrong if a scoring system ever *rewarded* a stream for bot-like activity?
6. A bump farmer splits dust buys across 20 wallets, 2 buys each. Which detector here goes blind, and what stream feature would you add to catch it?
