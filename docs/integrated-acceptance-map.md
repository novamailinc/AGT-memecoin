# Pinned integrated acceptance map

Snapshot: candidate immediately after 7b2ef6f, November 23, 2026. This is an engineering acceptance map, not qualification or a completed-bot report.

Original source: user's WhatsApp message `wamid.HBgNMjM0NzA0NDcxOTI1MRUCABIYFDNBNjA4MzdENEMxRDFBQzYyQTZGAA==`, November 22, 2026, 14:25 Pacific. The message includes numbered sections 1–26 and a final section 27. Requirements are not sequentially completed milestones.

## Implementation versus evidence

| Original section | Current acceptance position | What still prevents acceptance |
|---|---|---|
| 1 Mission | Programmed components and fixture lifecycle exist | Whole standalone live-data system not demonstrated |
| 2 Operating philosophy | Safety/account controls and explicit provenance implemented in parts | No integrated proof that all sources and lanes satisfy it |
| 3 Loss forensics | New-trade journals and accounting diagnostics exist | Original historical trade records unavailable; historical loss/provider correlation unresolved |
| 4 Intelligence/memory | Durable launch/outcome/deployer archives and retrieval tested | Memory decision consumption and controlled candidate cycle incomplete; advisory storage is not autonomous learning |
| 5 Discovery | Wallet polling, event/subscription/decoder components exist | Full V2, pool creation and liquidity-initialization coverage not established |
| 6 Market intelligence | Multiple production modules; reserve capture independently fixture-checked through 7b2ef6f | Token-2022 behavior, multi-window overlap and complete pool coverage incomplete; heuristics are unqualified |
| 7 Entry | Actual sighting-to-bus-to-watcher entry path fixture-tested | Real source completeness, all-lane integration and strategy effectiveness unproved |
| 8 Exit | Actual exit polling, tranche/account settlement and adverse-close fixtures | Real executable liquidity and adverse-market reliability unproved |
| 9 Providers | Shared reserve RPC supports paced Alchemy-first fallback; candidate wires wallet transaction acquisition to it | Broader router/Chainstack/workload integration and live operational evidence incomplete |
| 10 Event correctness | Dedup/timestamp/event-store tests; wallet parser rejects failed/unknown execution and invalid amounts atomically, aggregates same-mint accounts | No complete acquisition-to-decision causal coverage across every source |
| 11 Unified decision/risk | Shared paper account, kill switches and lane controls | Full unified policy proof absent; frozen verdict still includes stale estimated close P/L |
| 12A Historical/offline | Timestamp-correct replay v2 and fixture tests | Representative historical data, realistic execution coverage, untouched chronological holdout and advantage evidence missing |
| 12B Live-market demo | Shared simulated account machinery | No completed authorized continuous real-mainnet-data lifecycle demonstration |
| 12C Controlled mainnet | Not activated | Qualification and explicit execution authorization required |
| 13 Performance/qualification | Some metrics and diagnostics implemented | Frozen verdict contamination and missing qualified sample/period/uncertainty report |
| 14 Adaptation | Dry-run learner/promotion records | Tested candidate development, holdout comparison, rollback and controlled production promotion not integrated |
| 15 Portfolio | Shared cash/account gates and position persistence fixture-tested | All-lane exposure/risk acceptance and live execution evidence missing |
| 16 Telegram | Real handler/account/position and stop/resume component tests | Current live pairing/polling/control-room acceptance not demonstrated |
| 17 Signing security | No real signing exercised in this work | Secure signing/mainnet readiness must be separately demonstrated; no secrets in tests |
| 18 Jupiter/real execution | Pricing/execution components and fixtures | Actual simulation/submission/confirmation/reconciliation proof not established by fixtures |
| 19 AGT runtime | Prior installer/startup/restart component evidence | Current actual AGT install, reboot, persistent operation, restore and backup proof absent |
| 20 Architectural completion | Concrete production seams progressively repaired | Separate routers/intelligence/advisory modules remain incompletely integrated |
| 21 Testing/proof | Unit, script and limited realpath integration batteries | A build or mock result is not whole runtime, historical qualification or live demo proof |
| 22 Graduation | Explicit fail-closed state/evidence registry and CLI added, original names/checklist | Bookkeeping only: measured artifact evaluator, runtime admission, verified authority and qualification evidence remain missing |
| 23 Emergency | Kill switch/global halt and recovery components tested | Full source/provider/restart fault matrix across running system incomplete |
| 24 Deliverables | Prior partial status reports and component audit | Final evidence-backed architecture/loss/historical/demo/AGT/security reports incomplete |
| 25 Restrictions | Current work offline, simulated only | Preserve no live activation/spend/silent safety weakening |
| 26 Development autonomy | Integration work continues | Does not waive money, activation or policy authority limits |
| 27 Engineering standard | Incomplete | Cannot claim autonomous profitable or deploy-ready bot from fixture passes |

## Current cohesive fixture path

`tests/test_realpath_e2e.py` now starts with a provider-shaped transaction response at the actual HTTP-attempt seam, not a preconstructed buy:

1. Actual `smart-watcher.get_buy_details` calls its actual `rpc` wrapper.
2. Wrapper delegates to shared `reserves.rpc`; paced primary/fallback selection executes. HTTP attempts and pacing are fixtures, no actual requests.
3. Real transaction balance parser produces wallet buys; real wallet grouping/convergence produces the signal bus record.
4. Actual watcher intake consumes the bus into the shared $10 simulated account. Duplicate intake and negative safety are tested.
5. Actual exit polling settles a tranche. Module reload preserves account and position.
6. Real Telegram handlers render the stored position/account, with send captured locally.
7. Remaining position closes through exit polling; the real verdict funnel archives its simulated, unqualified outcome.
8. Memory module reload and `launch_memory` retrieve that outcome.
9. Total provider outage yields no fabricated wallet buy.

Still substituted: HTTP transport, safety, enrichment, tape/price inputs and Telegram delivery. `tests/test_realpath_process_restart.py` additionally drives three separate
interpreters: first runs acquisition to partial settlement and exits; second
recovers exact cash/pinned position, rejects duplicate intake, closes the
remainder and reads memory; third verifies completed cash, no open position,
and no duplicate outcome or intake. This is actual interpreter exit/restart,
not module reload. Its orderly checkpoint is not an abrupt-kill or reboot test.
No scheduler process, live websocket subscription, actual signing, AGT boot or memory-based entry decision is proved. Connecting the wallet RPC path does not connect the separate intelligence event stack automatically.

## Qualification accounting correction, proposed separately

Do not edit frozen lane verdict mathematics silently. `tests/test_paper_e2e.py` deliberately demonstrates that registered expectancy still includes an estimated stale close. Its passing assertion establishes a known activation blocker.

Proposed correction for review: retain the original registered verdict as an immutable legacy diagnostic; introduce a separately versioned qualification evaluator that reports incomplete/NOT_QUALIFIED while any required close contains unresolved estimated/write-down/invalid economic evidence. Do not delete those losses from risk accounting, fabricate zero P/L, or treat a current-price-only subset as qualified. Once original records can be reconciled, evaluate the full eligible chronological cohort with exact execution/model/config provenance and dependence-aware uncertainty. This proposal changes qualification semantics and needs explicit review before wiring; it is not the pending invalid-PnL new-entry-stop decision.

## Shortest next sequence

1. Pin and independently check this integrated acquisition/exit/memory/failure/restart batch, including full test entry points and live CI state.
2. Use demonstrated failures in the acceptance matrix to drive implementation, not new disconnected advisory modules.
3. Complete the formal qualification state/evidence contract without enabling live execution. Resolve the separate frozen-policy correction explicitly.
4. Obtain/validate historical data and run timestamp-correct chronological evaluation. Missing data remains missing.
5. Only with grounded cadence/provider-usage authority, run the requested mainnet-data simulated lifecycle. Original §13.2 sets an initial engineering target of at least 500 completed demo trades and 30 calendar days, extending for insufficient frequency/diversity. This is an evidence target, not a promised delivery date.
6. Actual AGT installation/reboot/restore and mainnet readiness remain separate demonstrations. No automatic graduation.

## Transaction evidence follow-up

The wallet-buy acquisition parser now requires meta.err to be explicitly null,
valid wallet identity, finite nonnegative native/token amounts and positive
block time before producing any buys. It validates the whole response before
returning output, aggregates multiple token accounts for the same mint, and
records signature/provenance. This prevents failed chain transactions and
malformed later records from becoming partial sighting evidence. Suffix-based
Pump mint selection and equal split of SOL outflow remain existing approximations.
The polling cursor still advances before transaction-detail success, so lost
acquisitions on transient detail failure remain an unresolved production gap.
No missing execution status is guessed successful.
