# Bonding Curve Math — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

A pump.fun-style launch does not trade against real buyers and sellers
at first. It trades against a formula. The formula keeps two pretend
piles — virtual reserves — and insists their product stays constant.
The default curve in `curve.py` starts with 1.073 billion virtual
tokens against 30 virtual SOL, a total supply of 1 billion tokens,
a fee of 100 bps (1%), and a graduation target of 85 real SOL [S].

"Virtual" is the key word. There are not really 30 SOL sitting in
the pool at launch. The virtual piles exist so the curve has a
starting price and rises smoothly as people buy. Real SOL collected
is tracked separately, and only that real number decides graduation:
when real SOL reaches the target (default ~85 SOL), the token is
ready to leave the curve and trade on a DEX pool instead [S].

Buying pushes the price up, selling pushes it down — not by opinion,
but by arithmetic. If you add SOL to one side of a constant product,
the other side must shrink so the product is unchanged, so each
token left behind is worth more SOL. That is the whole price
mechanism. There is no order book and no counterparty to negotiate
with.

Three consequences follow that beginners routinely miss:

1. The spot price is a quote for an infinitesimal trade only. Any
   real-sized trade moves the price while it fills, so the average
   fill is always worse than spot: above spot for buys, below spot
   for sells. The gap grows with trade size [S].
2. The per-SOL rate degrades with size. The first SOL buys at near
   spot; later SOL in the same order buy into a curve you have
   already pushed up. Cost is convex: the second 1% of supply costs
   more than the first 1% [S].
3. A round trip priced honestly costs about the fee twice. The fee
   is skimmed from the SOL side on both the buy and the sell, so a
   buy-then-immediately-sell loses ~1.99% at 100 bps and exactly 0%
   at zero fee. There is no hidden spread in this model [S].

The curve object is a frozen dataclass: trades return a *new*
`Curve`, the original never moves. That immutability mirrors how
you should reason — a fill must be priced on the state the previous
trade left behind, never on a stale snapshot [S].

Fail-closed is everywhere: non-positive reserves, supply, target,
or amounts, a fee outside [0, 10000), or a buy that would drain the
virtual reserve all raise `ValueError`. Nothing returns a
plausible-looking zero for bad input [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

Invariant:

    k = virtual_sol × virtual_tokens
    default k = 30.0 × 1,073,000,000 = 3.219e10

Spot price (SOL per token) and its reciprocal [S]:

    spot = virtual_sol / virtual_tokens
         = 30 / 1,073,000,000 ≈ 0.0000000280 SOL
    tokens_per_sol = virtual_tokens / virtual_sol ≈ 35,766,667

Tokens received for a gross SOL buy, step by step [S]:

    1. fee_rate = fee_bps / 10,000        (100 bps → 0.01)
    2. net_sol  = sol_amount × (1 − fee_rate)
    3. new_sol  = virtual_sol + net_sol
    4. new_tokens = k / new_sol           (product preserved)
    5. tokens_out = virtual_tokens − new_tokens

Cost to buy exactly T tokens is the exact inverse [S]:

    1. new_tokens = virtual_tokens − T   (must stay > 0)
    2. new_sol    = k / new_tokens
    3. net_sol    = new_sol − virtual_sol
    4. gross_sol  = net_sol / (1 − fee_rate)

The tests pin the pair as exact inverses: buying with X SOL and
asking the cost of the tokens received returns X [S].

Sell proceeds: add the tokens to the token reserve, read the new
SOL reserve off k, take the difference, then skim the fee:

    gross_out = virtual_sol − k / (virtual_tokens + T)
    proceeds  = gross_out × (1 − fee_rate)

Price impact and slippage are numerically identical here [S]:

    impact = (effective_price − spot) / spot × 100%   (buys)
    effective_buy_price = gross SOL paid / tokens received

Both are ≥ 0 and grow monotonically with size. A dust trade's
impact is approximately the fee alone [S].

SOL needed to push spot to a target price P — derived from
k = S × T and P = S / T, so S = sqrt(k × P) [S]:

    new_sol_reserve = sqrt(k × P)
    net_sol_needed  = new_sol_reserve − virtual_sol
    gross_sol       = net_sol_needed / (1 − fee_rate)

The target must be strictly above current spot: buys cannot move
the price down or sideways, so at/below-spot targets raise [S].

Graduation progress [S]:

    completion_pct = clamp(100 × real_sol / target, 0, 100)
    is_graduated   = real_sol ≥ target
    remaining      = max(target − real_sol, 0)

Market cap in SOL is simply spot × total supply. On a fixture
where supply equals reserves it equals the virtual SOL exactly [S].

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

All figures verbatim from `demo.py` on the default curve [S]:

- Spot: 0.0000000280 SOL per token; 35,766,667 tokens per SOL.
- 1.0 SOL buys 34,277,832 tokens (fee + impact included) — note
  this is *less* than the 35,766,667 spot quote. Asking the cost
  of those exact tokens back returns 1.0000 SOL.
- 1% of supply = 10,000,000 tokens costs 0.2851 SOL, with price
  impact 1.96% and slippage vs spot +1.96%.
- After a 10 SOL buy, spot rises from 0.0000000280 to
  0.0000000495 SOL.
- Round trip: 5.0 SOL → 151,969,957 tokens → sell all → 4.9005 SOL
  back. Loss 1.99% — fees only, exactly as the fee model predicts.
- Graduation: 0 SOL = 0.0%, not graduated; 42.5 SOL = 50.0%, not
  graduated; 85.0 SOL = 100.0%, graduated; 120 SOL clamps to
  100.0%, graduated.
- Zero buy is rejected: "sol_amount must be positive, got 0".

Hand-check fixture from the tests: a zero-fee curve with 1M tokens
and 100 SOL — buying 1% of supply costs 1.010101… SOL by the
constant-product formula. Buying 100% of supply on the default
curve succeeds only with reserve headroom (~415.11 SOL); after a
1B-SOL buy ~32.5 tokens remain — the reserve approaches zero but
never drains [S].

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Quoting spot as the fill. Spot is the marginal price, not
  your average price; real fills are worse and worsen with size.
- T2. Pricing a sell-back on the pre-buy curve. The sell must run
  on the post-buy state, or the round-trip cost is understated.
- T3. Assuming linear cost. The second 1% costs more than the
  first; cost in percent-of-supply is strictly increasing, convex.
- T4. Blaming a "spread" for round-trip loss. In this model the
  loss is the fee charged twice — ~1.99% at 100 bps, 0 at zero fee.
- T5. Thinking you can buy the whole reserve. Tokens out approach
  the reserve asymptotically; a draining amount raises instead.
- T6. Confusing virtual SOL with real SOL collected. Only real SOL
  counts toward the 85 SOL graduation target.
- T7. Expecting buys to lower the price. `sol_to_reach_spot_price`
  rejects targets at or below spot — direction is one-way.
- T8. Treating bad input as zero. Zero/negative/None amounts and
  out-of-range fees raise `ValueError`; silence would be a lie.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Defaults (1.073e9 / 30 SOL / 1e9 supply / 100 bps / 85 SOL),
  inverse buy/cost pair, 1.99% round trip, convex percent cost:
  [S] — all pinned by the 43-test suite.
- Demo numbers (34,277,832 tokens per 1 SOL, 0.2851 SOL for 1%,
  4.9005 SOL round trip): [S] — verbatim captured demo output.
- The sqrt(k·P) derivation for reaching a target price: [S] for
  the method and the 41.421356… SOL test value; the algebra shown
  here is [R] exposition of it.
- "Quoted prices overstate what a position would really get" in
  launch discovery: [M] — README ties this to the live scanner's
  bonding-curve reasoning; the lab shares no code with it.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves the arithmetic of a virtual-reserve
constant-product curve: how spot, fills, impact, convex cost,
fee-only round-trip loss, and graduation progress follow from
k = x·y, with 43 passing tests including hand-computed values [S].
It does not prove anything about any real token: reserves, fees,
and the graduation target are synthetic pump.fun-style defaults,
real curves add platform quirks and competing traders, and knowing
what a buy *would* fill at says nothing about whether the token is
safe or worth buying [R].
