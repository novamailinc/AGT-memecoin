# Admission + revocation root-cause design — 2026-11-23

Scope: why live execution admission and revocation work the way they do
today, the exact gaps, and the fix order. Analysis only — no behavior
changed by this document. Grounded in the preserved tree at commit
ae742e4. All limits unchanged: no mainnet activation, no signing keys,
no weakening of fail-closed behavior.

## Current architecture (as built)

1. **Deployment marker** (`{live_home}/live/deployment.json`,
   `{machine, armed}`): file presence + non-empty `machine` name =
   "this is the deployment machine"
   (`mainnet_executor.read_deployment_marker`). The marker is not
   secret; it is the machine-identity check that keeps live sends off
   any other host (grad-select-live-trader refuses to connect without
   it — fail closed, verified by reading the refusal path).
2. **Arming**: `MainnetExecutor` construction is always `dry_run=True`
   (non-True construction raises). Only `arm_live()` flips
   `_dry_run=False`, and only when the marker carries `armed: true`.
   `disarm()` is in-process.
3. **Send-time check**: `execute_swap()` calls
   `require_deployment()` (marker EXISTS + has a machine name) and then
   gates the actual send on `self._dry_run` (line ~577).
4. **Entry route**: live memecoin entries do NOT pass through
   `core/execution/gateway.py`. `live_select.honest_entry()` calls
   `executor.execute_swap()` directly. The gateway's request_trade
   decision record, idempotency cache, audit trail and safety checks
   (market-open, spread, min-equity) apply only to callers that use the
   gateway.
5. **Qualification**: `core/qualification.py` emits
   `execution_authorized=False` and `runtime_gate_connected=False` into
   its verdict dict. Nothing at runtime consumes the verdict to gate
   execution — the field names themselves admit the gate is not
   connected.

## Gap 1 — post-arm revocation does not work (root cause)

`arm_live()` reads `armed: true` ONCE and caches the result in
`_dry_run=False`. `execute_swap()` re-reads the marker but checks only
presence + machine name, never `armed`. Consequence: an operator who
revokes by editing the marker to `armed: false` (or removing the key)
does NOT stop an already-armed process — it keeps sending until
restarted or until the whole marker file is deleted (which trips
`require_deployment`). Revocation semantics depend on how the operator
guesses to revoke. Root cause: arm state is cached at arm time; the
send path checks identity, not authority.

**Fix B (smallest, do first)**: at send time, re-check
`marker.get("armed") is True` alongside presence. One marker edit then
revokes within one swap attempt. Fail closed: unreadable/malformed
marker = refuse. This changes only the executor's send gate; dry-run
simulation paths unchanged.

## Gap 2 — LiveFills bypasses the admission authority

Two parallel admission systems exist: the gateway (decision record,
idempotency, audit, safety checks) and the LiveFills route (direct
executor calls with its own fail-streak/dedup logic). Fixes to
admission policy made in one do not reach the other; audit of a live
memecoin entry has no gateway decision record. Root cause: the
memecoin path was built executor-first and the gateway was never wired
as its front door.

**Fix A (largest, do last)**: route `honest_entry` (and close paths)
through a memecoin-scoped gateway request so every entry gets one
auditable admit/deny decision with an idempotency key. Until then,
document the LiveFills route as a separate admission surface whose
policy must be kept in lock-step manually.

## Gap 3 — qualification verdict is bookkeeping, not a gate

`execution_authorized` is written into the qualification dict but no
runtime check reads it; a lapsed or failed qualification cannot revoke
live capability. Root cause: no consumer — the verdict flows to
reports, not to the executor/arm path.

**Fix C (after B)**: make qualification a hard precondition in two
places — `arm_live()` refuses when the current qualification verdict
is not execution-authorized, and the send-time check (from Fix B)
re-validates it cheaply (cached verdict with explicit staleness bound,
fail closed on unreadable). This converts post-qualification decay
into automatic revocation.

## Candidate fix order, NOT an implementation plan

1. **B** (send-time `armed` re-check): executor-local, fail-closed,
   closes the live revocation hole with the least surface change.
2. **C** (qualification as runtime gate): builds on B's send-time
   check; needs a defined verdict staleness policy.
3. **A** (route LiveFills through the gateway): the structural fix;
   requires a memecoin adapter for the gateway's market-data safety
   checks (tick age/spread/min-equity are forex-shaped today) so it
   must not be rushed ahead of B/C.

Non-goals for this pass: no mainnet activation, no marker or key
material creation, no change to shared-quota/fail-closed limits, no
weakening of any existing refusal to obtain a green test.


## Blocking design constraints, unresolved

The B -> C -> A ordering above is withdrawn as an executable plan.
A blanket executor denial could strand verified reductions and exits.
execute_swap currently has no trustworthy action/position/amount contract
that distinguishes exposure increases from bounded reductions. Resolve
that contract and recover the original revocation/exit policy before any
send-time gate implementation. Do not invent qualification staleness,
risk thresholds, exit authority or an automatic-close policy. This
analysis grants no activation or implementation authority and changes
no runtime gate. Entries and reductions need separate grounded decisions.
