# Demo mode wiring — seam analysis and requirements

Status (corrected 2026-11-23): the wiring IS applied. mode="demo" is
constructed in broker/memecoin/adapter.py (validator accepts "demo";
bridge wiring in __init__; idempotency/UNKNOWN guard refusing blind
retry before reconcile_demo_execution(); _demo_fill dispatch). The
lifecycle matrix below is implemented and green in
tests/test_demo_adapter_lifecycle.py, plus tests/test_sim_executor.py
and tests/test_demo_bridge.py. SimulationExecutor + DemoExecutionBridge
run the same execute_swap contract as MainnetExecutor over REAL Jupiter
quotes with simulated settlement. The seam analysis below remains the
design record.

## User-grounded semantics (from authenticated messages 2026-11-23)

- DEMO is the operational target: live market data + shared simulated $10 +
  no real funds + no on-chain trades.
- DEMO must be closest-to-live: same market data, strategy, risk and order
  lifecycle; live migration changes the execution adapter only.
- Paper harness is TEST-ONLY. Demo is not the optimistic paper fill.
- Demo-to-live migration requires explicit user approval + qualification.

## The seam (broker/memecoin/adapter.py)

submit_order -> _submit_solana (under _state_lock):
  idempotency check/replay -> _verify_position_mints ->
  price_sol via (_observe_entry_fx + _validated_usd_quote) [shared BUY] |
  _shared_price_sol [shared SELL] | _price_sol [unshared]
  -> _devnet_fill [devnet] | _paper_buy/_paper_sell [paper]

_paper_buy today: tokens = cost_sol / price_sol (optimistic: no fees, no
slippage, no failure modes). _commit_native persists the fill.

Demo wiring replaces ONLY the fill step:
  price_sol-only fill -> bridge.buy/sell -> SimulationExecutor
Everything above the seam (idempotency, kill switch, shared-account
binding, position ledger, journaling, native intent machinery) is
reused unchanged.

## Load-bearing requirements (from main, 2026-11-23)

1. UNKNOWN fee exposure: when the executor raises confirm_timeout, a
   real send may have landed with the fee paid. The shared $10 ledger
   must expose the unresolved intent and possible fee loss — never
   silently treat UNKNOWN as success or as a clean failure. The existing
   _seal_intent/_recover_native_intent machinery is the vehicle: seal the
   intent (with max-fee exposure) BEFORE calling the executor, resolve on
   result, leave UNRESOLVED + surfaced on confirm_timeout. No reseed, no
   separate balance, no compensating rollback.
2. Reconciliation: a retried order after UNKNOWN must reconcile against
   the journal/idempotency store before any new executor call
   (existing _idempotent_recover_from_journal pattern).
3. Idempotence: bridge fills must flow through the same
   request_fingerprint + idempotency-key atomic section; a replayed key
   replays the original simulated fill, never re-executes.
4. Fees: the shared ledger debit must be volume + actual fee_lamports
   from the measured fill (buy) / proceeds net of fee (sell), matching
   the executor's balance-delta measurement.
5. No activation: wiring ships disabled (mode="demo" constructed only
   where the operator selects it); no default change to existing
   paper/devnet behavior. Mainnet stays qualification-gated.

## Lifecycle test matrix (to implement with the wiring)

- demo BUY: fill qty < optimistic qty (impact + latency move), fee
  deducted from shared ledger, entry_plan economics consistent.
- demo SELL: FIFO reduction against bridge proceeds net of fee.
- skip propagation: no_route / price_impact_over_cap / fee_over_cap do
  not touch ledger, positions, or idempotency store.
- UNKNOWN: inject_confirm_outage -> intent sealed, ledger unresolved,
  status surfaced; retry reconciles without double-debit.
- idempotent replay: same key + same fingerprint -> same ticket, one
  executor call (count calls).
- kill-switch engaged -> refused before any quote fetch.
- determinism: seeded executor replays identical fills across restarts.

## Explicitly not claimed yet

Class-swap-only equivalence with the live path is NOT established:
adapter configuration, risk checks, fee settlement and shared-ledger
reconciliation must validate end-to-end before that claim. Simulated
fills cannot prove real execution behavior.
