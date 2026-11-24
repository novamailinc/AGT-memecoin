# Native scanner broker and shared demo capital

Status: integration gap confirmed offline, not implemented or admitted.

The owner's approved goal is one configurable $10 simulated starting balance for
all strategies. The existing scanner direct-entry route submits to
MemecoinAdapter. Its native SOL paper_state balance is separate from the USD
paper_account ledger used by watcher/grad lanes and Telegram demo reports.
These books must not be summed or called one capital pool.

## Reproduced boundary

With a scratch home, actual MemecoinAdapter and shared account, fixture SOL/USD
100 and a 0.1 SOL native paper buy: native balance changes 10 to 9.9 SOL with one
position, shared cash remains $10 with no positions, and Telegram demo snapshot
still shows $10. No real transaction, provider or runtime service is involved.

## Required implementation constraints

- Preserve broker order volumes and scanner risk PnL in SOL. Capture validated,
  finite positive SOL/USD at the entry or settlement being booked, preserving
  the exact price/timestamp/provenance used in durable intent state.
- The USD shared account is authoritative for available capital, exposure and
  cash. Native 10 SOL genesis must never independently fund shared-mode entries.
  Broker account reporting must not label converted USD as a native SOL book.
- Preserve the existing execution-cost/fill contract. Wiring capital is not
  permission to change fee/slippage assumptions or trade sizes. Native adapter
  costs currently differ from cost-aware lane fills and need explicit evidence
  labels, not a hidden fee-policy rewrite.
- Intent must precede the first cross-ledger effect. Exact immutable parameters,
  stable entry/partial-close IDs and shared position-instance IDs are required.
  Replay converges debit/credit plus native state/fills, or freezes on conflict.
  Failure after replace/fsync can mean visible state, never automatic rollback.
- Two-tier A/B tickets, same-mint scale-ins and FIFO partial sells must not alias
  one shared operation or lose per-instance basis. Existing shared account
  permits one open record per lane/mint; do not evade lane exposure caps by
  inventing one lane per ticket without a defined aggregation contract.
- Existing native positions without shared bindings require exact migration
  evidence or an explicit unavailable/frozen state. No synthetic historical
  funding, unsupported conversion, or frozen qualification rewrite.
- Reporting, marks and recovery must use the same shared instances. A native
  scanner close must not be absent from demo reports, or counted twice.
- No new providers, modes, signing keys, deployment or live trading are enabled
  by implementing or testing this integration.

## Acceptance needed

Actual scanner entry with real paper adapter and shared ledger, competition with
another lane for the same $10, insufficient cash with no partial debit, two-tier
same-mint positions, partial and full close basis, currency movement, exact
operation replay, before/after each cross-ledger write failure, fresh-process
continuation, corrupt/legacy state freeze, and exact Telegram demo account and
positions. Existing per-lane and frozen qualification policies stay unchanged.

The separate close_intents module demonstrates the project's current
cross-ledger recovery pattern but is close-specific. Reusing it blindly for
native entry intents would not establish entry recovery correctness.

### Scanner close recovery boundary

Shared native scanner closes now persist an immutable SQLite close context before
calling the broker. Recovery binds account UUID, native book UUID and ticket,
requires the actual durable native CLOSE receipt, and runs before token quotes.
The event uses native realized SOL PnL and verifies its single ticket allocation.
Scanner context is retained for provenance, not used as economic authority.
The event queue, pollable log, delivery journal and committed evidence marker are
one exact-id SQLite transaction. Repeated recovery does not duplicate them.
Notification fanout after this commit is best effort, not guaranteed delivery.

New committed shared closes remain in the scoreboard after the delivery queue is
consumed. Legacy queue-only closes still use the old reader contract and remain
subject to destructive consumption. No historical migration or reconstruction is
performed. Existing intent retries preserve the original reason. A previously
uncommitted pool-gone zero-price decision whose premise no longer holds waits for
reconciliation instead of closing against a recovered quote or feed outage.

The independent legacy learning writers are not an atomic part of this close
transaction. Recovery does not blindly replay wallet increments or other learning
side effects. Committed evidence labels that projection unverified. This boundary
is not complete learning recovery, Stage A qualification, phone reboot proof,
physical power-loss proof, market performance evidence, or activation authority.

Offline checks include an abrupt fresh-interpreter exit immediately after native
close followed by two fresh-interpreter recoveries, stale scanner-basis mismatch,
SQLite projection rollback, repeated stale-state recovery, wrong-book refusal,
legacy queue-only fallback and mixed durable/legacy deduplication.
