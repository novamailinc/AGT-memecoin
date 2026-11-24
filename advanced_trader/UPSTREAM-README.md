# advanced_trader — a deterministic trading brain for memecoin paper trading

A standalone decision layer for a Solana memecoin paper-trading bot. It
decides what to enter, how much to stake, and when to exit — and nothing
else. The owner of the AGT-memecoin repo can drop this package in as the
trading brain behind their lanes.

## What it is

* **Edge-gated entries.** A lane trades only when its measured,
  out-of-sample expectancy after costs is positive (rolling 100 closes,
  walk-forward train/test check). No edge, no trade.
* **Fractional-Kelly sizing.** Quarter-Kelly from measured win rate and
  average win/loss, hard-capped per trade and per bankroll, halved after
  3 consecutive losses, halted after 5 until a human re-enables.
* **Ladder exits with unconditional stops.** 25% at 1.5x, 25% at 3x, the
  remainder on a 30% trailing stop, breakeven stop once leg A banks, one
  dip buyback. The −50% hard stop and the 24h time stop fire no matter
  what — even with a dead quote (write-off at zero), even mid-crash.
* **Multi-provider pump signals.** Helius, Chainstack and Alchemy behind
  one `SignalSource` interface, merged by a `SignalAggregator` with
  failover (a down provider just contributes nothing) and corroboration
  (2+ providers score higher; lone signals need stronger evidence).
* **Experience learning.** Every closed trade is recorded by signal
  source, deployer, lane, entry pattern and holding time. Losing
  patterns are down-weighted and eventually blocked; winning patterns
  earn more size (inside the Kelly caps). Learning comes only from
  closed, journaled trades — never from open positions or guesses.
* **Risk governor.** Daily loss limits (per lane and global), max open
  positions, per-token and per-deployer exposure caps, a drawdown
  circuit breaker, and a kill-switch hook. It is a separate layer the
  decision code cannot bypass.
* **Price sanity guard.** Any quote ≤ 0 or > 100x entry is rejected,
  counted, and can never produce a fill.
* **Never-reused tickets.** Monotonic ticket numbers persisted with
  atomic writes; a ticket is issued exactly once, across restarts.

## What it is NOT

* Not a broker. It executes nothing. It returns decisions; the owner's
  execution layer carries them out.
* Not a price feed. Quotes and signals arrive as arguments from the
  owner's transports.
* Not live trading. Paper/demo only. There is no live-trading code path
  in this package, and none is planned here.
* Not an agent. No LLM calls, no natural-language reasoning, no
  deliberation, no discretion. Every decision is a pure deterministic
  function of market data + state: same inputs → same outputs.

## The design target (stated honestly)

Maximize profits, minimize losses — as **asymmetry**, not as zero
losses. Zero losses is impossible and is never claimed anywhere in this
package. Winners run via the ladder, the trailing stop and the
breakeven stop; losers are cut by the −50% hard stop, the 24h time
stop, the quote-dead write-off and the daily loss limit. The design
target is **expectancy > 0 with capped downside**: the largest single
loss can never exceed one max stake. The validation harness reports the
asymmetry (average win vs average loss, largest win vs largest loss,
expectancy) so the owner can see it instead of taking it on trust.

## Why it can beat a pro — and what that claim actually means

Three bot-only advantages, nothing mystical: **speed** (decisions in
milliseconds), **perfect memory** (the experience store never forgets a
losing pattern), and **discipline** (never overrides a stop, never
revenge-trades, never FOMOs). The edge is discipline + speed + memory,
proven by the validation numbers — never a claim of guaranteed profit.

## Layout

```
advanced_trader/
├── __init__.py      public surface
├── config.py        every threshold, with ADV_TRADER_* env overrides
├── edge.py          EdgeValidator — trade only positive-expectancy lanes
├── sizing.py        KellySizer — fractional Kelly + caps + streak throttle
├── exits.py         ExitManager — ladder + unconditional stops + write-off
├── risk.py          RiskGovernor — limits the trader cannot bypass
├── sanity.py        PriceGuard — reject bad quotes, never fill on them
├── tickets.py       TicketAllocator — monotonic, never reused
├── signals.py       SignalSource adapters + SignalAggregator (3 providers)
├── experience.py    ExperienceStore — learns from closed trades only
├── trader.py        AdvancedTrader — the orchestrator
└── validation.py    scenario + synthetic-backtest harness with report
tests/               plain scripts: python3 tests/test_<name>.py
```

## Quick start

```python
from advanced_trader import AdvancedTrader, TraderConfig

trader = AdvancedTrader(TraderConfig.from_env())

decision = trader.evaluate(
    {
        "lane": "grad_select",
        "token": "MINT...",
        "deployer": "DEP...",
        "quote": 0.0012,
        "corroboration": 2,        # providers confirming this signal
        "evidence_score": 0.9,     # owner's filter score, 0..1
        "pattern": "grad_select",
        "source": "helius",
    },
    bankroll=1000.0,
    day="2026-11-24",
)
if decision["decision"] == "enter":
    position = trader.enter(decision, "2026-11-24", now_ts=..., bankroll=1000.0)
    ...
    actions = trader.manage(position, quote_or_none, now_ts=...)
    # execute actions, then book the close:
    trader.on_close({"ticket": ..., "lane": ..., "pnl": ..., "day": ...,
                     "equity_after": ..., ...})
```

See [INTEGRATION.md](INTEGRATION.md) for the full hook points and the
safety model.

## Requirements

Python 3.12+, standard library only. No dependencies, no network calls,
no secrets, no private keys.

## Tests

```bash
for f in tests/test_*.py; do python3 "$f"; done
python3 -m advanced_trader.validation
```

## Safety model

Paper-first, fail-closed, human re-enable. Every halt (streak halt,
daily-loss halt, drawdown breaker, kill switch, experience block) needs
a human to clear it. Stops are unconditional. A bad quote can never
become a fill. A ticket is never reused. State is two small atomic JSON
files (ticket counter, experience stats) — no database.
```

## License

No license file is committed yet — add one to set the terms (e.g. MIT,
Apache-2.0). Demo/research software; not financial advice.
