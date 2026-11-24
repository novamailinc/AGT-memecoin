# AMM and LP Lock Checks — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

After a token graduates off its bonding curve it trades in a real
pool: two piles of assets (say SOL and tokens) governed by the
same constant-product rule, x·y = k. This lab in `amm.py` answers
the two questions that matter about such a pool: what will a swap
actually fill at, and is the liquidity really locked? [S]

On the swap side, keep two losses separate, because the toolkit's
central lesson is that they are not the same number:

- Price impact is the pool's shape: how much worse than spot you
  fill purely because your trade moves the curve. Fee excluded.
- Slippage is what you actually pay versus the spot quote, fee
  included. Slippage is always ≥ price impact [S].

The default fee is 25 bps, taken from the input side of the swap.
A `Pool` is frozen; swaps return a new pool, so a sequence of
trades must be threaded through successive states — exactly like
the bonding-curve lab, and exactly like reality.

On the LP side: whoever provides liquidity receives LP tokens.
If the deployer still holds those LP tokens in their own wallet,
they can withdraw the pool's assets at any moment — the classic
rug. LP counts as "secured" only when it is burned (destroyed) or
held by a locker contract. LP in the deployer wallet is reported
but never counted as secured, whatever the deployer promises [S].

The verdict rule: secured ≥ threshold → `"locked"` (default
threshold 90.0%); otherwise `"unlocked"`. And there is a third
verdict beginners forget: `"unverifiable"`, returned with
passed=False whenever data is missing or contradictory — a None
supply, burn balance, or locker list, a non-positive supply, or
balances that together claim more than 100% of supply. Missing
data is never a guessed "locked" — and it is not "unlocked"
either, because "unlocked" is a positive claim too [S].

Two supporting ideas round out the lab. Impermanent loss prices
what an LP gives up versus just holding, as a function of the
price ratio. And a sandwich simulation shows why thin pools are
dangerous: the same attack costs a victim roughly 8× more in a
10× thinner pool at the same spot price [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

Constant-product quote with fee on the input [S]:

    in_after_fee = amount_in × (1 − fee_bps / 10,000)
    amount_out   = reserve_out × in_after_fee
                   / (reserve_in + in_after_fee)

Exact-out inverse — gross input needed for an exact output [S]:

    in_after_fee = reserve_in × amount_out
                   / (reserve_out − amount_out)
    amount_in    = in_after_fee / (1 − fee_bps / 10,000)

An output at or beyond the whole reserve can never be bought and
raises `ValueError`: the denominator would hit zero or go
negative [S].

Impact vs slippage, for an x-in trade [S]:

    spot_out        = amount_x_in × (reserve_y / reserve_x)
    price_impact %  = 100 × (spot_out_at_zero_fee_fill gap)
                      — curve movement alone
    slippage %      = 100 × (spot_out − actual_out) / spot_out
                      — fee included; always ≥ impact

Hand values pinned by tests: impact is 50% when amount_in equals
the input reserve, 20% at a quarter of it [S]. With a fee, k
strictly grows after a swap (the fee stays in the pool); at zero
fee k is preserved exactly [S].

LP secured percentage [S]:

    secured % = 100 × (burn_balance + Σ locker_balances)
                / lp_total_supply
    locked  iff secured % ≥ threshold_pct   (default 90.0)

`threshold_pct` must lie in (0, 100]; anything else raises [S].

Impermanent loss for price ratio r = new_price / old_price [S]:

    IL % = 100 × ( 2·√r / (1 + r) − 1 )

Always ≤ 0, exactly 0 at r = 1, symmetric under r → 1/r:
r = 4 and r = 0.25 both give exactly −20% [S].

Multi-hop routes (`chained_quote`) feed each hop's output into
the next hop's `get_amount_out`, so every leg charges its own fee
and impact; an empty or None route raises [S].

Sandwich estimate: run attacker buy → victim buy → attacker
sell-all across successive pool states; report the victim's extra
loss % versus the same trade alone, plus attacker profit [S].

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Demo pool: 100 SOL / 1,000,000 tokens, spot 10,000 tokens/SOL,
fee 25 bps [S]:

- 1 SOL in → 9,876.5 tokens out. Those tokens back in → 0.9756
  SOL out. A round trip loses; it never gains.
- Impact/slippage by size: 0.1 SOL → 0.10% / 0.35%; 1 SOL →
  0.99% / 1.24%; 10 SOL → 9.09% / 9.30%; 50 SOL → 33.33% /
  33.44%. Watch slippage sit just above impact throughout.
- Min-out guard: demanding 9,975.246912 out for the 1 SOL swap is
  refused — actual output 9,876.482091 is below min_out.
- LP verdicts: 95% burned → locked (95.0% ≥ 90%). 50% secured
  with the rest deployer-held → unlocked. Missing supply →
  unverifiable, secured n/a.
- Sandwich: deep pool (100 SOL) — victim normally gets 9,876,
  sandwiched 9,495, extra loss 3.86%, attacker profit +0.0290
  SOL. Thin pool (10 SOL) — normally 9,070, sandwiched 6,398,
  extra loss 29.46%, attacker profit +0.3052 SOL.

Boundary fixtures from the tests: exactly 90% secured is locked;
a locker balance tipping 89.9% to exactly 90.0% flips the verdict;
60% secured + 60% deployer-held claims 120% of supply and returns
unverifiable; a custom threshold flips the same 60%-secured
fixture between locked and unlocked [S].

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Conflating impact and slippage. Impact is curve shape;
  slippage is the trader's real cost including the fee. A min-out
  guard must be built from the number the trader actually gets.
- T2. Counting deployer-held LP as locked. A promise is not a
  burn. Deployer LP is reported, never counted as secured.
- T3. Returning "unlocked" when data was never fetched. Missing
  locker data is unverifiable with passed=False — fail-closed,
  distinct from a measured unlock.
- T4. Trusting totals that exceed supply. Balances summing past
  100% of LP supply are a contradiction → unverifiable.
- T5. Swapping a sequence against one stale pool state. Each
  swap must run on the state the previous swap left (the
  sandwich sim makes the cost of forgetting this visible).
- T6. Assuming depth doesn't matter at the same spot price. The
  thin pool's sandwich loss is 29.46% vs 3.86% deep — spot is
  the same, the danger is not.
- T7. Expecting a round trip to break even. Fees plus impact
  guarantee a loss; any backtest showing a gain has a bug.
- T8. Trying to buy the whole reserve. Exact-out quotes at or
  beyond the reserve raise — the curve never fully drains.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Quote formula, 25 bps default, impact hand values (50% at
  in == reserve), IL = −20% at r = 4 and r = 0.25: [S] — pinned
  by the 45-test suite.
- ≥90% secured rule, deployer exclusion, unverifiable shapes:
  [S] — including the 120%-claims contradiction case.
- Demo numbers (9,876.5 out, sandwich 3.86% vs 29.46%): [S] —
  verbatim captured output.
- The same ≥90% / fail-closed shape gating live locked-liquidity
  cards, and honest-fill quoting for paper entries: [M] — per
  README; the lab shares no code with the live bot.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves the mechanics of post-graduation trading:
constant-product quotes in both directions (exact-in and
exact-out), the impact/slippage split, multi-hop compounding,
impermanent loss, sandwich cost as a function of depth, and a
fail-closed LP-lock verdict at a 90% default threshold — all
hand-checked in 45 tests [S]. It does not prove any real pool is
safe: locker balances here are synthetic inputs, a "locked"
verdict is only as good as the on-chain data fed in, and the lab
verifies lock arithmetic, not the honesty of whoever reports the
balances [R].
