# Integration guide — wiring advanced_trader into AGT-memecoin

This package is the **decision layer**. It does not fetch prices, does
not place trades, and does not talk to any provider. You wire your
existing lanes, feeds and execution around it at four hook points.

## The four hook points

### 1. Candidate evaluation — where a signal becomes a decision

Your launch scanner already produces candidates (new graduations,
momentum coins). For each one, build a candidate dict and call
`evaluate()` **immediately, as the signal arrives** — no batching, no
second pass. The decision is deterministic and takes milliseconds.

```python
from advanced_trader import AdvancedTrader, TraderConfig
from advanced_trader.signals import (
    HeliusSignalAdapter, ChainstackSignalAdapter,
    AlchemySignalAdapter, SignalAggregator,
)

trader = AdvancedTrader(TraderConfig.from_env())
aggregator = SignalAggregator(trader.config)
adapters = {
    "helius": HeliusSignalAdapter(),
    "chainstack": ChainstackSignalAdapter(),
    "alchemy": AlchemySignalAdapter(),
}

# On each raw payload from any provider transport:
signal = adapters[source].normalize(raw_payload, received_ts=time.time())
if signal is None:
    continue  # malformed payload: fail closed, no trade
count, sources = aggregator.corroboration(signal.token, time.time()) \
    if aggregator.add(signal) else (0, set())

decision = trader.evaluate(
    {
        "lane": "grad_select",          # your lane
        "token": signal.token,
        "deployer": signal.deployer,
        "quote": signal.price,
        "corroboration": count,         # distinct providers, in-window
        "evidence_score": your_filter_score(signal),  # 0..1
        "pattern": "grad_select",       # your entry-pattern tag
        "source": signal.source,
    },
    bankroll=ledger.balance(),
    day=today_str(),
)
```

`evaluate()` runs the fixed gauntlet: risk pre-check → edge check →
evidence bar (corroborated signals need 0.5, lone signals 0.8) →
price guard → Kelly sizing → experience weight → risk final gate.
It returns `{"decision": "enter", "stake": ...}` or
`{"decision": "reject", "reason": ...}`. Reasons are stable strings
(`no_edge`, `weak_evidence`, `quote_spike`, `streak_halt`,
`experience_blocked`, `daily_loss_lane`, `daily_loss_global`,
`drawdown_breaker`, `lane_halted`, `max_positions`, `token_exposure`,
`deployer_exposure`, `stake_zero`) — journal them, they are your audit
trail for why trades did *not* happen.

### 2. Entry — ticket allocation and position creation

```python
if decision["decision"] == "enter":
    position = trader.enter(decision, today_str(), time.time(), bankroll)
    # position is a plain dict: ticket, token, entry_price, entry_ts,
    # peak_price, volume (stake), remaining, legs, breakeven_armed...
    # Execute the buy through YOUR broker, then journal the fill.
```

`enter()` re-checks the risk governor and allocates a ticket (the only
file I/O in the trading path). If the ticket cannot be persisted, it
raises — fail-closed, no position is opened. Tickets are never reused,
across restarts or crashes.

### 3. Exit management — one call per quote

```python
actions = trader.manage(position, quote_or_none, time.time())
for action in actions:
    if action["action"] in ("sell", "close_all"):
        execute_sell(position, action["fraction"], action["close_price"])
    elif action["action"] == "write_off":
        # Quote dead past the 24h time stop: close at zero value.
        # Journal it as a write-off, never as a market fill.
        journal_write_off(position, action)
    elif action["action"] == "buyback":
        # One dip buyback per position; re-enter action["fraction"].
        execute_buyback(position, action)
```

A bad quote (≤ 0 or > 100x entry) is routed to the dead-quote path
inside `manage()`: only the time-stop write-off can fire there, so a
5000x spike can never trigger a leg sale. A `None` quote means the feed
is dead — the position holds until its time stop, then writes off at
zero. Positions can no longer rot open for weeks.

### 4. Close journaling — where the bot learns

```python
trader.on_close({
    "ticket": position["ticket"],
    "lane": position["lane"],
    "token": position["token"],
    "deployer": position["deployer"],
    "pnl": realized_pnl_net_of_costs,   # REQUIRED: net of fees/slippage
    "day": today_str(),
    "equity_after": ledger.balance(),
    "reason": action["reason"],         # e.g. "leg_b", "hard_stop"
    "write_off": action["action"] == "write_off",
    "source": signal_source,            # for experience keys
    "pattern": entry_pattern,           # for experience keys
    "hold_seconds": close_ts - position["entry_ts"],
})
```

`on_close()` updates the edge stats, the sizing streak, the risk
governor, and the experience store — and fires the kill switch if the
lane's rolling expectancy has turned negative. **Only closed, journaled
trades teach.** Open positions never update experience.

## Feeding edge history

A fresh lane has no closes, so it is not tradable until it earns trust
(30 closes by default). To bootstrap from your existing journals,
import them directly:

```python
for close in journal.iterate_closes(lane="grad_select"):
    trader.edge.record_close("grad_select", close.net_pnl)
```

`net_pnl` must be net of all costs. Inflated pnl (phantom fills,
un-charged fees) poisons the edge check — the guard against that is
your honest-fill accounting, not this package.

## Provider configuration

Adapters read endpoint URLs from the environment only:

| Provider    | Env var            |
| ----------- | ------------------ |
| Helius      | `HELIUS_RPC_URL`   |
| Chainstack  | `CHAINSTACK_RPC_URL` |
| Alchemy     | `ALCHEMY_RPC_URL`  |

No API keys appear in this code. If your endpoint URL embeds a key,
that key lives in your environment, never in the repo. An unconfigured
provider simply contributes no signals; the aggregator keeps flowing
on the others, and lone signals face the stricter evidence bar.

## Configuration knobs

Every threshold lives in `TraderConfig` with an `ADV_TRADER_*` env
override. Defaults are deliberately safe:

| Knob | Default | Meaning |
| ---- | ------- | ------- |
| `bankroll_start` | 1000.0 | Starting bankroll (drawdown baseline) |
| `edge_window` | 100 | Rolling closes kept per lane |
| `edge_min_trades` | 30 | Closes before a lane is trusted |
| `edge_min_expectancy` | 0.0 | Lane tradable only above this |
| `kelly_fraction` | 0.25 | Quarter-Kelly |
| `max_stake` | 25.0 | Hard cap per trade |
| `max_bankroll_pct_per_trade` | 0.025 | Hard cap as % of bankroll |
| `streak_halve_after` | 3 | Halve stake after N straight losses |
| `streak_halt_after` | 5 | Halt lane after N straight losses |
| `leg_a_mult` / `leg_b_mult` | 1.5 / 3.0 | Ladder legs |
| `trailing_pct` | 0.30 | Trailing stop from peak |
| `hard_stop_mult` | 0.5 | −50% hard stop |
| `time_stop_hours` | 24.0 | 24h time stop (unconditional) |
| `daily_loss_limit_lane` | 50.0 | Lane halts for the day at this loss |
| `daily_loss_limit_global` | 100.0 | All trading halts at this loss |
| `max_open_positions` | 20 | Simultaneous positions cap |
| `max_exposure_per_token` | 50.0 | Open-stake cap per token |
| `max_exposure_per_deployer` | 100.0 | Open-stake cap per deployer |
| `max_drawdown_pct` | 0.20 | Circuit breaker at 20% drawdown |
| `max_quote_multiple` | 100.0 | Reject quotes above 100x entry |
| `corroboration_window_s` | 5.0 | Signal corroboration window |
| `min_evidence_corroborated` | 0.5 | Evidence bar, 2+ providers |
| `min_evidence_single` | 0.8 | Evidence bar, lone signal |
| `experience_weight_max` | 1.25 | Max size boost for winning patterns |
| `experience_min_trades` | 5 | Trades before a key moves weight |
| `experience_block_streak` | 5 | Straight losses that block a pattern |
| `ticket_state_file` | `tickets.json` | Ticket counter persistence |
| `experience_state_file` | `experience.json` | Experience persistence |

## Safety model

* **Paper-first.** This package has no live-trading path. Wire it to
  paper/demo execution and prove it there.
* **Fail-closed.** Bad data → no trade. Corrupt state file → raise,
  never silently reset (a reset would reuse tickets). Unpersisted
  ticket → no position.
* **Human re-enable.** Every halt (streak halt, daily-loss halt,
  drawdown breaker, kill switch, experience block) is cleared only by
  an explicit human call. The bot never re-enables itself.
* **Stops are unconditional.** Nothing in the trader may delay or
  override the −50% stop or the 24h time stop.
* **Tiny state.** Two small atomic-JSON files (ticket counter,
  experience stats). No database, no shared-DB assumptions — the
  sibling bot's SQLite lock-ups cannot happen here.
* **Deterministic.** Same inputs → same outputs. No randomness, no
  clock reads inside a decision (timestamps arrive as arguments).

## Why this can beat a pro — stated honestly

The advantage is three bot-only properties, not a secret signal:

1. **Speed.** Signal arrival to entry decision in milliseconds (see
   `validation.py` timing). A human cannot react that fast, and the
   30s entry-age rule is an outer bound, not the target.
2. **Perfect memory.** The experience store never forgets a losing
   pattern — a human forgets, forgives, or re-tries a setup that has
   lost five times in a row. This bot blocks it.
3. **Discipline.** It never overrides a stop, never revenge-trades
   after a loss (the streak throttle halves size, then halts), never
   FOMOs into a lone uncorroborated signal (stricter evidence bar),
   and never trades a lane with no measured edge.

That is the whole edge: discipline + speed + memory. It is proven by
the validation numbers (`python3 -m advanced_trader.validation`),
not by a promise. There is no claim of guaranteed profit or zero
losses anywhere in this package — losses are expected, capped, and
reported. If the validation report ever shows a lane with no edge, the
correct action is to let it stay dark, not to override the check.

## Running the checks

```bash
for f in tests/test_*.py; do python3 "$f"; done
python3 -m advanced_trader.validation
```

The validation harness replays winning/losing streaks, dead-quote
rugs, 5000x spikes and flash crashes, runs a seeded synthetic
backtest, and prints expectancy, win rate, profit factor, max
drawdown, the win/loss asymmetry, and the trades the edge check
refused. Read it before trusting the trader — and re-run it after
any change.
