# Wash & Bot-Share Detection — study notes

## Core ideas (teach-back)

Early trade streams lie. Volume can be manufactured by the same
wallets buying and selling to each other, by swarms of dust-sized
bot transactions, and by repeated tiny buys meant to bump a token
up a trending list. This toolkit measures all three over a stream
of synthetic `Trade(wallet, side, amount_sol, ts, slot)` records,
with each share hand-checkable [S].

**Wash share** asks how much volume is the same wallets round
tripping. For one wallet, only the part of its flow that both buys
*and* sells can be wash; one-sided buying, however large, washes
nothing.

**Bot share** asks what fraction of *transactions* (count, not
volume) look machine-made. A trade is bot-like if **any** pattern
holds: its size is tiny (< **0.10 SOL**), its wallet is
high-frequency (≥ **8 trades** in the stream), or its slot is
crowded (≥ **3 trades** share one slot; `None` slots never
cluster). Crucially, bot share is a **negative signal only**: the
toolkit maps it to a penalty in **[−100, 0]** that can never go
positive. No stream ever earns points for bot-like activity [S].

**Bump share** isolates one specific farm shape: small *buys*
(< 0.10 SOL) from a wallet with ≥ **3 such small buys**. Small
sells never count — bumping is a buy-side performance.

The three combine into a **toxicity score** in [0, 100] and three
threshold flags. The verdict wrapper, `assess`, has a deliberate
**fail-open fingerprint** contract: with `None` input or fewer than
**10 trades**, it returns status `"unknown"` with all shares `None`
and no flags — missing data is **never a pass and never a veto**.
Only a big enough stream can earn `"clean"` or `"flagged"`. The
low-level share functions do the opposite: they *raise*
`ValueError` on empty/`None` input rather than returning a
plausible-looking zero [S]. Both choices are intentional, at
different layers.

This mirrors the live bot's fingerprint philosophy and its
wash-trade veto check: detectors run in measure mode where
unmeasured is "unknown", and bot share only ever counts against a
token [M].

## Key derivations & formulas

**Wash** (per wallet, then summed) [S]:

```
wash_volume(wallet) = 2 × min(buy_volume, sell_volume)
wash_share          = Σ wash_volume / total_volume        ∈ [0, 1]
```

The `min` is the matched, round-tripped size; the ×2 counts both
legs of the trip. A pure buyer contributes `2 × min(buy, 0) = 0`.
A wallet buying and selling 2 SOL each contributes 4 SOL of wash.

**Bot and bump shares** (count-based) [S]:

```
bot_like(trade) ⇔ amount < 0.10
                  OR wallet_trade_count ≥ 8
                  OR slot_trade_count ≥ 3   (slot not None)
bot_share       = #{bot_like} / #{trades}
bot_volume_share = Σ amounts of bot_like / total_volume   (can diverge!)
negative_signal_score = −100 × bot_share                  ∈ [−100, 0]

bump(trade) ⇔ side = buy AND amount < 0.10
              AND wallet_small_buy_count ≥ 3
bump_share  = #{bump} / #{trades}
```

**Toxicity and flags:**

```
toxicity = min(100, 50×wash + 30×bot + 20×bump)

WASH_SKEW  ⇔ wash ≥ 0.40
BOT_SWARM  ⇔ bot  ≥ 0.50
BUMP_FARM  ⇔ bump ≥ 0.50
```

A share exactly *at* its threshold flags; one epsilon below does
not. `flags_for` raises `ValueError` on any share outside [0, 1],
and flag order is stable (wash, bot, bump) [S]. Wash carries the
biggest weight (50) because matched round-trips are the most direct
evidence of manufactured volume [R].

## Worked example

Three demo streams, 12 trades each [S]:

- **Clean stream** — 12 distinct wallets, one buy each of
  1.0–2.1 SOL, distinct slots. No sells → wash **0.00**; no tiny
  sizes, no repeat wallets, no crowded slots → bot **0.00**;
  toxicity 0.0; `assess` → `"clean"`, no flags.
- **Wash stream** — 6 wallets each buy 2.0 SOL and sell 2.0 SOL.
  Per wallet wash volume `2 × min(2, 2) = 4`; total wash 24 of
  total volume 24 → wash_share **1.00**. No tiny/repeat/slot
  patterns → bot 0.00. Toxicity `50 × 1.0 = 50.0`; `assess` →
  `"flagged"`, `['WASH_SKEW']`.
- **Bot stream** — 12 wallets, one dust buy of 0.01 SOL each. Every
  trade is tiny → bot_share **1.00**, toxicity `30 × 1.0 = 30.0`,
  `['BOT_SWARM']`. But bump_share is **0.00**: each wallet made
  only *one* small buy, below the ≥ 3-repeat bump rule. Dust alone
  is bot-like; it is not yet bump *farming*.

Hand-check from the tests: a partial round-trip stream totals
wash 8 of volume 24 → wash_share `8/24 ≈ 0.333` [S].

Missing data: `assess(None)` → `"unknown"`, trade_count 0;
`assess` on 3 trades → `"unknown"`, trade_count 3, all shares
`None` — even if those 3 trades are obvious wash [S].

Count-vs-volume divergence (from the tests' shape): one whale trade
plus nine dust bots gives bot share **90% by count** but **< 1% by
volume** — the two statistics answer different questions, and the
toxicity score uses the *count* share [S].

## Trap log

- T1. Counting a one-sided whale as wash. Wash is `2 × min(buy,
  sell)`; buying only washes exactly zero, however aggressive.
- T2. Confusing bot count share with bot volume share. A stream can
  be 90% bots by count and < 1% by volume; quoting the wrong one
  either panics or reassures you falsely.
- T3. Verdict-hunting on thin data. Nine obvious wash trades still
  return `"unknown"` — `assess` refuses both a pass and a veto
  below 10 trades, because a fingerprint built on nine prints is a
  coin flip wearing a lab coat.
- T4. Rewarding bot activity. If a scorer ever *added* points for
  bot-like flow, farmers would manufacture the reward directly.
  Negative-only is a structural defence, not a style choice.
- T5. Calling every dust buy bump farming. Bump needs ≥ 3 small
  *buys* from the *same wallet*; one dust buy per wallet (the bot
  demo) is swarm, not bump — and small sells never count at all.
- T6. Missing the high-frequency false positive. The ≥ 8-trade rule
  flags *every* trade from that wallet, including its large,
  ordinary-looking ones; a genuine active trader trips it. The
  pattern is still useful as a share, ruinous as a per-trade
  conviction.
- T7. Splitting to evade. A bump farmer using 20 wallets × 2 buys
  each goes invisible to the bump detector (and weakens the
  high-frequency pattern) — these detectors see per-wallet shapes,
  and Sybil splitting is their known blind spot.

## Source flags

- All definitions, thresholds (0.10 SOL tiny, ≥8 high-frequency,
  ≥3 crowded slot, ≥3 bump repeats, flags at 0.40/0.50/0.50,
  weights 50/30/20, minimum 10 trades), and demo numbers are from
  `wash.py`, covered by `test_wash.py` (39 tests) [S].
- The count-vs-volume divergence example is a tested shape in the
  suite [S]; the "why wash weighs most" gloss is my reading [R].
- The live-bot fingerprint/veto parallel and measure-mode stance
  are per the README [M].

## One-paragraph verdict

This toolkit proves that three cheap, hand-checkable statistics —
matched round-trip share, bot-like count share, and repeated-dust
buy share — can fingerprint manufactured flow, combine into a
bounded toxicity score, and, just as importantly, refuse to judge
at all when the stream is too thin to judge. It does **not** prove
any flagged stream is fraudulent (each pattern has an innocent
explanation, which is why flags inform and only the live veto
layer convicts), it is blind to Sybil-split manipulation across
many wallets, and its thresholds are documented lab constants, not
calibrated live optima.
