# Live Risk Controls — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

Statistics decide whether a lane deserves to live. This lab in
`risk.py` builds the three controls that enforce the decision
while the lane is running — a kill switch, a cost gate, and a
fixed bankroll — all synthetic, all paper-only [S].

Control 1: the rolling-expectancy kill switch. `KillSwitch`
(window=30, min_trades=15) records paper closes as realised
multiples. Once at least `min_trades` closes sit in the window
and their mean multiple is below 1.0 — negative expectancy —
the lane halts. Below 15 closes it refuses to judge: fourteen
straight losers do not halt, because a switch that fires on
noise trains everyone to ignore or bypass it [S].

Three properties make the halt trustworthy. It is *sticky*: a
later 50x winner never clears it. It is *human-only* to re-arm:
`rearm(operator, reason)` requires both a named operator and a
reason, journals every attempt (including refused anonymous
ones), and there is deliberately no auto re-arm method. And
re-arming *preserves the closes*, so the next negative close
can immediately re-halt — re-arm is a decision to watch one
more trade, not a pardon. Fail-closed at load: a missing or
corrupt JSON state file leaves the switch halted=True and
journals a `load_failed` event [S].

Control 2: the pre-registered cost gate. Before a $1 shot is
allowed, price its worst-case round trip: 4 transactions at the
chain's per-tx cost. Blocked iff total cost exceeds 25% of
notional (exactly 25% passes). Per-tx costs: Solana $0.001,
Base $0.03, Ethereum $1.00, Hyperliquid 0.035% of notional.
At $1 that makes Ethereum cost $4.00 — 400% of the position —
and the gate's refusal is not a warning but arithmetic: a $1
Ethereum shot can never pay for its own gas [S].

Control 3: a bankroll that cannot lie to itself. `Bankroll`
starts at $100.00, fires $1.00 shots, allows at most
`MAX_OPEN = 10` concurrent positions, settles a position for
`1.00 × realized_mult`, and flips permanently to
`bankroll_exhausted` when less than one shot remains. There is
deliberately no `top_up` and no `reset` attribute — the tests
assert their absence — so the ledger's record stays untainted
evidence rather than a story retold after each refill [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

Kill-switch expectancy and halt rule [S]:

    expectancy = mean(last `window` closes) − 1
                 (None before any close)
    halt iff len(window closes) ≥ min_trades (15)
             AND expectancy < 0
    loss_streak = trailing closes strictly below 1.0x
                 (an exactly-1.0 close breaks the streak)
    would_halt_if(m) predicts the next close's effect
                 without mutating state; True once halted.
    Negative realised multiples are rejected (ValueError)
    and never recorded.

Cost gate [S]:

    per_tx(chain, notional): fixed chains from CHAIN_COSTS
        = {solana: 0.001, base: 0.03, ethereum: 1.00};
        hyperliquid = 0.00035 × notional
        (chain names normalised for case/whitespace)
    worst_case_cost = 4 × per_tx
    pct = 100 × worst_case_cost / notional
    allowed iff worst_case_cost ≤ 0.25 × notional

    At $1: solana $0.004 (0.40%) ALLOWED; base $0.120
    (12.00%) ALLOWED; ethereum $4.00 (400%) BLOCKED;
    hyperliquid $0.0014 (0.14%) ALLOWED.

Minimum notional to pass — invert the inequality [S]:

    min_notional = 4 × per_tx / 0.25 = 16 × per_tx
    ethereum $16.00; base $0.48; solana $0.016;
    hyperliquid 0.0 (percentage cost scales with notional,
    so it passes at any size). Verified through the gate:
    $16.00 Ethereum passes, one cent below fails.

Bankroll accounting [S]:

    open_position: cash −= 1.00, open_count += 1
        (raises when exhausted, full at 10, or underfunded)
    settle(mult):  cash += 1.00 × mult, open_count −= 1
        (negative multiples rejected, position stays open)
    shots_affordable = floor(cash / 1.00)
    net_pnl = (cash + 1.00 × open_count) − start
    state flips to bankroll_exhausted when cash < 1.00 —
        permanent, including a bankroll born below $1;
        later settles can still credit cash, but the
        state never flips back and opening stays refused
        by the exhausted check where applicable.

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Verbatim from `demo.py` [S]:

Cost gate at $1, worst case = 4 transactions:

    solana       $0.0040 = 0.40%   -> ALLOWED
    base         $0.1200 = 12.00%  -> ALLOWED
    ethereum     $4.0000 = 400.00% -> BLOCKED
    hyperliquid  $0.0014 = 0.14%   -> ALLOWED

Kill switch over 15 losing closes at 0.5x:

- Fresh state file missing → halted=True (fail-closed).
- After a named human re-arm → halted=False.
- Close 14 at 0.5x → halted=False (below min_trades).
- Close 15 at 0.5x → halted=True; expectancy −0.50.
- Anonymous re-arm refused: "rearm requires a non-empty
  operator and reason". Named re-arm → halted=False.

Bankroll ($100, $1 shots, no top-up, no reset):

- One 3.0x winner: cash $102.00, state active.
- 10 shots open at once: cash $92.00, open=10.
- 11th shot refused: MAX_OPEN positions already open.
- All 10 lost: cash $92.00, state active.
- has top_up=False, has reset=False.

Test-pinned edges [S]: rolling window forgets old closes (a
halt decision uses only the last 30); exactly 100 lost shots
take cash to $0 and flip exhaustion; a winner settling after
exhaustion credits cash but cannot resurrect the account;
`chains_passing(1)` excludes Ethereum while
`chains_passing(16)` includes it.

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Halting on too few closes. Fourteen losers is a bad
  week, not a verdict; min_trades=15 is the guard against a
  switch everyone learns to override.
- T2. Auto-clearing halts. If a later winner could un-halt,
  the switch is a suggestion. Sticky + named-human re-arm
  only.
- T3. Re-arm as amnesty. Preserving closes means one more
  loser re-halts instantly — wiping history at re-arm would
  let a dead lane live forever on paperwork.
- T4. Fresh-start on missing state. A lost state file treated
  as "new switch" silently un-halts a killed lane; missing
  and corrupt both load halted.
- T5. Ignoring cost because the token "might 100x". At 400%
  round-trip cost the $1 Ethereum shot loses before any
  market move; the gate blocks arithmetic, not opinions.
- T6. Fuzzy cost boundaries. Exactly 25% passes, one cent of
  notional below the $16 Ethereum minimum fails — boundaries
  are computed, not vibes.
- T7. Refillable bankrolls. A top-up or reset turns a ledger
  into fiction; their absence is asserted by tests.
- T8. Treating exhaustion as a pause. The state is permanent
  even though settles still credit — "permanent" describes
  the account's eligibility, not its arithmetic.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Kill-switch parameters (window 30, min 15), stickiness,
  re-arm journaling, fail-closed load; CHAIN_COSTS, 25% cap,
  $16.00/$0.48/$0.016 minima; Bankroll $100 / $1 / MAX_OPEN
  10 / no top_up / no reset: [S] — 45 tests.
- Demo figures (400% Ethereum block, halt at close 15,
  $102.00 after one 3x winner): [S] — verbatim output.
- Live per-lane kill switches, the $1 Ethereum mirror block,
  and the fixed $1-a-shot CEO-style paper account: [M] —
  per README; the lab re-implements the shape synthetically.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves the three controls compose into an honest
paper account: a kill switch that waits for 15 closes, halts
on negative rolling expectancy, and yields only to a named
human; a cost gate whose $1 Ethereum refusal is pure arithmetic
(400% > 25%); and a $100 bankroll whose no-refill design keeps
its record admissible as evidence — 45 tests including
persistence, corruption, and boundary cases [S]. It does not
prove the live bot is safe to trade real money: costs here are
fixed synthetic constants (real gas varies), the kill switch
judges only the closes it is fed, and paper controls say
nothing about execution, custody, or keys [R].
