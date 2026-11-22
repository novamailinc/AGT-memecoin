# Live Risk Controls (lab)

A from-scratch, pure-stdlib lab for the three controls that keep a paper memecoin account honest: a rolling-expectancy kill switch that only a named human can re-arm, a pre-registered per-chain cost gate that proves a $1 Ethereum shot can never pay for its own gas, and a fixed $100 bankroll with $1 shots and deliberately no way to refill it. Everything is synthetic; nothing here touches a live bot, a network, or real funds.

## What it does

- Feeds paper closes into a kill switch and halts the lane the moment the last window of closes averages below a 1.0x realised multiple — but only once enough closes exist to judge.
- Prices the worst-case round trip on each chain before a shot is allowed, and blocks any chain whose costs would eat more than a quarter of the position.
- Tracks a $100 bankroll shot by shot, refuses an 11th concurrent position, and flips permanently to `bankroll_exhausted` when less than one shot remains.

## Files

- `risk.py` — `KillSwitch(state_path=None, window=30, min_trades=15)`: `record_close(realized_mult) -> bool` appends a close and halts when at least `min_trades` closes sit in the window and `mean(realized_mult) - 1 < 0`; `expectancy() -> float | None`; `recent_closes() -> list` (a copy of the window); `loss_streak() -> int` (trailing closes below 1.0x); `would_halt_if(realized_mult) -> bool` (non-mutating prediction; an already-halted switch stays halted); `is_halted() -> bool`; `rearm(operator: str, reason: str) -> bool` raises `ValueError` on an empty operator or reason and journals every attempt. A halt is sticky: later winners never clear it, only `rearm` does, and `rearm` preserves the closes so the next negative close can re-halt. Negative realised multiples are rejected with `ValueError`. There is no auto re-arm method. Fail-closed: a missing or corrupt JSON state file on load leaves `halted=True` and journals a `load_failed` event.
- `risk.py` — `cost_gate(chain, notional=1.0) -> (allowed: bool, cost_usd, pct)`, built on `CHAIN_COSTS = {solana: 0.001, base: 0.03, ethereum: 1.00}` per transaction and Hyperliquid at 0.035% of notional per transaction (`per_tx_cost(chain, notional=1.0)`, chain names normalised for case/whitespace); worst case is 4 transactions, blocked iff cost > 25% of notional (exactly 25% passes), so Ethereum at $1 returns `(False, 4.00, 400.0)`. Also `min_notional_to_pass(chain) -> float` (Ethereum $16.00, Base $0.48, Solana $0.016, Hyperliquid 0.0) and `chains_passing(notional) -> list` (sorted). Fail-closed: an unknown chain or a non-positive notional raises `ValueError` instead of passing.
- `risk.py` — `Bankroll(start=100.00)` with `MAX_OPEN = 10`: `open_position() -> bool` debits $1.00 and raises when exhausted, full, or underfunded; `settle(realized_mult) -> float` credits `1.00 * realized_mult` for one open position (negative multiples rejected); `can_open() -> bool` is the non-raising predicate; `shots_affordable() -> int` floors the cash to whole shots; `net_pnl()` counts cash plus stakes still open against the start; `summary() -> dict` reports cash, open count, total debited/credited, settled count, P/L and state; `state` is `"active"` or `"bankroll_exhausted"` (permanent once flipped, including a bankroll that starts below one shot). There is deliberately no `top_up` and no `reset` attribute.
- `demo.py` — scenario walkthrough: cost gate on four chains, a kill-switch halt and re-arm, and bankroll exhaustion rules.
- `test_risk.py` — the pytest suite below.

## Test suite

45 tests, all passing. Coverage: halt after exactly 15 negative closes and no halt at 14, no halt with positive or zero expectancy, rolling-window behaviour (including expectancy computed only over the window once old closes fall out), a sticky halt that a later 50x winner does not clear, a re-armed switch re-halting on the next negative close because closes are preserved, re-arm requiring both operator and reason (with failed attempts journaled), no auto re-arm attribute, invalid `window`/`min_trades` rejected, negative realised multiples rejected without recording, `expectancy()` is `None` and the loss streak is 0 before any close, trailing loss-streak counting with an exactly-breakeven close breaking the streak, `would_halt_if` predicting both the tripping and the non-tripping next close without mutating state and staying True once halted, corrupt and missing state files loading halted (with the `load_failed` journal entry), state persistence across instances, exact cost-gate values for Ethereum (blocked, $4.00 = 400%), Solana ($0.004), Base and Hyperliquid, percentage-chain scaling, chain-name normalisation, the exactly-25% boundary passing at a $16 Ethereum notional and failing one cent below, `min_notional_to_pass` values for all four chains plus the boundary actually passing/halving through the gate, `chains_passing` at $1 and $16, unknown-chain failure, bankroll open/settle arithmetic, `MAX_OPEN` enforcement, `can_open` blocked separately by exhaustion and by the open cap, a sub-$1 starting bankroll born exhausted, `shots_affordable` flooring, `summary`/`net_pnl` accounting with a stake still open, a negative settle rejected without closing the position, the exhaustion flip at exactly $0 after 100 lost shots, exhaustion permanence after a later winner, and the asserted absence of `top_up`/`reset`.

## Captured output

Verbatim output of `python demo.py` (exit 0):

```
=== Live risk controls demo (synthetic, paper only) ===

-- Cost gate at $1 notional, worst case = 4 transactions --
solana       cost $0.0040 = 0.40% of $1 -> ALLOWED
base         cost $0.1200 = 12.00% of $1 -> ALLOWED
ethereum     cost $4.0000 = 400.00% of $1 -> BLOCKED
hyperliquid  cost $0.0014 = 0.14% of $1 -> ALLOWED

-- Kill switch: 15 losing paper closes --
fresh state file missing -> halted=True (fail-closed)
after human re-arm -> halted=False
close 14: realized 0.5x -> halted=False
close 15: realized 0.5x -> halted=True
expectancy after 15 closes: -0.50
anonymous re-arm refused: rearm requires a non-empty operator and reason
after named re-arm -> halted=False

-- Bankroll: $100, $1 shots, no top-up, no reset --
one 3.0x winner: cash $102.00, state=active
10 shots open at once: cash $92.00, open=10
11th shot refused: MAX_OPEN positions already open
all 10 lost: cash $92.00, state=active
has top_up=False, has reset=False

Demo complete.
```

## What this mirrors in the live bot

The live bot runs the same shape of controls around its paper lanes: a per-lane kill switch that halts paper entries on negative rolling expectancy and can only be re-enabled by a human, a pre-registered cost gate that blocks $1 Ethereum mirror positions outright because modelled round-trip gas is roughly 400% of notional while Solana, Base and Hyperliquid pass, and a dedicated fixed-size paper account traded at $1 a shot with honest costs and no resets or top-ups, so its ledger can serve as untainted evidence. This lab re-implements those ideas from scratch on synthetic numbers only.

## Self-exam

1. Why does the kill switch refuse to halt on 14 losing closes, and what failure mode is that minimum protecting against?
2. A re-armed switch halts again after one more losing close — what design choice in `rearm` causes that, and why is it the safer choice?
3. At what notional, if any, would the Ethereum cost gate start allowing positions, and how do you derive it from the fixtures?
4. Why is a missing state file treated the same as a corrupt one, rather than as a fresh start?
5. If `settle` can still credit cash after exhaustion, in what exact sense is the exhaustion permanent?
