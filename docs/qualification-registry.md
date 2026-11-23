# Qualification status and evidence registry

Original requirement: user message `wamid.HBgNMjM0NzA0NDcxOTI1MRUCABIYFDNBNjA4MzdENEMxRDFBQzYyQTZGAA==`, November 22, 2026, sections 12, 13 and 22.

`core/qualification.py` implements persistent, explicit status/evidence bookkeeping. It is NOT connected to execution or a runtime trading gate. A stored DEMO_QUALIFIED state means the supplied checklist is complete for its exact binding, not that this program independently measured an edge. Every status labels caller-recorded evidence, bookkeeping-only scope, runtime_gate_connected=false and execution_authorized=false. No actual bot qualification is claimed.

## States and transitions

All ten original names are exposed: DEVELOPMENT, HISTORICAL_VALIDATION, DEMO_QUALIFICATION, DEMO_QUALIFIED, MAINNET_READY_PENDING_AUTHORIZATION, MAINNET_CANARY, MAINNET_RESTRICTED, MAINNET_EXPANDED, PAUSED, EMERGENCY_STOP.

- Missing files are `availability=missing`, not DEVELOPMENT. Corrupt/unreadable state is unavailable, never silently reset.
- Initialization creates DEVELOPMENT once, bound to strategy_version, config_hash and data_period.
- Explicit DEVELOPMENT -> HISTORICAL_VALIDATION is allowed.
- Explicit HISTORICAL_VALIDATION -> DEMO_QUALIFICATION requires recorded passing historical criteria.
- Explicit DEMO_QUALIFICATION -> DEMO_QUALIFIED requires historical and demo criteria. No record insertion automatically advances state.
- PAUSED and EMERGENCY_STOP are available from any stored non-corrupt state. Resuming goes explicitly to HISTORICAL_VALIDATION, never directly back to qualified status.
- Revoking a criterion while DEMO_QUALIFIED records PAUSED. Corrupt or mismatched criterion evidence cannot produce qualified=true.
- Every mainnet target is refused by this implementation. Naming states grants no authority. Owner permission/signing infrastructure/canary policy are intentionally not simulated by booleans or a command flag.

State, evidence, and append-only-in-the-snapshot audit history are saved together under flock with atomic replace, file fsync and directory fsync. This is not a tamper-proof or cryptographically authenticated ledger. Local file editing can change caller evidence. Record references/justifications are locators, not independently fetched or validated content. Audit growth is not bounded.

## CLI

From repository root:

```
python3 -m core.qualification --home /path/to/simulated-home status
python3 -m core.qualification --home /path/to/simulated-home initialize \
  --strategy-version reviewed-version --config-hash reviewed-hash \
  --data-period explicit-start/end
python3 -m core.qualification --home /path/to/simulated-home record evidence.json
python3 -m core.qualification --home /path/to/simulated-home transition \
  HISTORICAL_VALIDATION --reason 'Begin offline evaluation'
```

An evidence JSON object has criterion, passed (exact boolean), source (actual artifact locator), justification, and binding (the exact three initialization fields). Wrong binding, missing source/justification or string/integer truth values are refused. Evidence may record a failed result without changing any trading policy.

## Criteria and unresolved policy

Historical checklist: timestamp_correct, realistic_costs, chronological_holdout, multiple_regimes, reproducible_advantage. Demo checklist follows original section 22.1: trade_sample, observation_period, net_expectancy, profit_factor, drawdown, out_of_sample, data_integrity, provider_routing, entry_exit, execution_realism, restart_recovery, risk_controls, frozen_strategy.

No thresholds are invented or evaluated here. The operator must supply measured artifacts and justify sample size/period, calibrated profitability/risk gates, dependence/uncertainty, exact strategy/config/data, and execution assumptions. Current tests supply synthetic fixture evidence only. The original 500-trade/30-day initial target is not silently installed as a universal calibrated pass rule.

Remaining work: artifact schema/digest verification, measured qualification evaluator, structured threshold/version configuration, binding to actual runtime strategy and provider state, reviewed mainnet authorization mechanism and explicit runtime admission integration. Historical data, holdout results, live demo, AGT operation and mainnet evidence still do not exist merely because this registry can store a status.

Frozen lane verdict accounting is unchanged. The separately proposed correction for unresolved estimated P/L remains unwired pending review. PAUSED/EMERGENCY_STOP here are bookkeeping states, NOT a substitute for the existing operational kill switch, and do not send commands to it.
