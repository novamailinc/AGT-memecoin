# Pinned integrated acceptance map

Snapshot: integrated candidate with durable wallet intake, November 23, 2026. This is an engineering acceptance map, not qualification or a completed-bot report.

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
| 16 Telegram | Real owner-only development control-only poller connected; supervised process restart demonstrated; local status/account/positions/perf/halt only | Next real owner-command API receipt awaited; no whole connected trading runtime, AGT hosting or reboot proof |
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

1. Actual `smart-watcher.poll_wallet` discovers fixture signatures and atomically
   stages its durable intake. A detail outage survives a reconstructed intake;
   next ordinary cycle recovers through real `get_buy_details`.
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
matching response/request signature, unique in-range account indices and
explicit owner identity, finite nonnegative native/token amounts and positive
block time before producing any buys. It validates the wallet-acquisition evidence before
returning output, aggregates multiple token accounts for the same mint, and
records signature/provenance. Aggregate and derived arithmetic must remain finite.
Block time is finite-positive only, not future-time or freshness qualified. This prevents failed chain transactions and
malformed later records from becoming partial sighting evidence. Suffix-based
Pump mint selection and equal split of SOL outflow remain existing approximations.
No missing execution status is guessed successful. Native arrays are checked
at the wallet index, not every unrelated balance; non-wallet amounts are not
validated. Loaded-address account indexing, raw amount/decimal consistency and
instruction-proven swap attribution remain unsupported.

## Durable wallet acquisition follow-up

The actual main wallet loop now calls `poll_wallet` with a locked atomic/fsynced
`wallet_intake.json` journal. New signatures are stored in the same snapshot as
the advanced acquisition cursor. Processing acknowledgement is separate: transport
error, null, invalid evidence and failed downstream persistence retain pending
work for the next ordinary cycle. Definitive chain failure and valid no-buy
responses can acknowledge without fabricating a sighting. One shared typed detail
fetch feeds buy/sell parsing; the existing six-worker concurrency, shared pacer,
30-second poll/event loop and inter-wallet pause remain, no extra retry timer.

A normal batch attempts at most the existing page budget (30*4 details); failed
items rotate durably so a poison item cannot starve later signatures. Pending
storage is deliberately not hard-capped or evicted, so prolonged outages can grow
it. Corruption is unavailable, never rebaselined. First-time bootstrap explicitly
records no pre-start backfill; pageback overflow or an absent previous cursor
records incomplete coverage, never a full-history claim. Page fetch failure does
not advance. Convergence write failure prevents acknowledgement; proposed in-memory seen
changes are applied only after that save, so the next poll cannot bypass the
failed handler. Bus publish false/exception is a failed fire, not a resolved
sighting; successful bus/convergence/seen snapshots fsync before acknowledgement.

Tests at the actual poll seam cover failed detail then fresh-interpreter retry,
valid empty versus invalid evidence, failed chain execution, disk/persistence
failure, corruption, stale cursor writer, bounded overflow, poison rotation and
crash-after-sighting replay dedup by wallet. Delivery is at least once: a crash
between downstream effects and acknowledgement can replay. This does not prove
exactly-once sell/notification/execution effects, abrupt AGT crash recovery,
provider completeness or a qualified strategy. Existing sell attribution and
convergence-policy approximations are not repaired by acquisition persistence.


## Remaining durability boundaries

Bus, convergence, seen and intake are separate commits, not one transaction.
Crashes can replay downstream effects. Shared bus publication now holds flock over the entire read/merge/replace
and uses private unique temporary snapshots with file/directory fsync. A
12-process/144-publication fixture checks complete retention below the existing
5000-line bound; replace failure preserves the previous snapshot and returns
false. A directory fsync failure after replace can return false with the new
signal already visible: durability is uncertain, not rollback/no publication,
and a retry can duplicate it. This does not make duplicate replay exactly once.
Convergence production sighting/expiry/retry paths now serialize whole RMW under
flock, use unique private atomic snapshots and reject existing unreadable/invalid
state instead of silently resetting it. Eight processes recording64distinct mints
check actual handle_sighting retention. Sell grading effects remain non-idempotent
by transaction signature; separate-stage commits and replay remain open. Passing poll recovery is not proof of exactly-once
signal/notification/sell execution or full acquisition coverage.


## Causal fire-intent replay

Before smart-watcher can publish a converged fire, its intent ID and creation
time are saved to convergence state. Available wallet transaction signatures
travel with sightings and the signal evidence. A retry after bus publication but
failed convergence completion reuses that intent, rather than inventing a new
retry-time event. The locked bus publication recognizes matching source/chain/ID
within retained history and refuses rebinding it to another mint; watcher and
smart-select fire keys use explicit identity when present, legacy timestamps
otherwise. This is bounded retained-history dedup, not lifetime exactly-once,
not instruction-level swap evidence, and not cross-file atomic execution.
Notifications, mirrors and sell grading may still replay. The same-mint paper
entry cash guard remains separate and unchanged.


## Causal wallet decrease history

The sell-side parser now uses the same successful wallet-response validation as
buy acquisition, sums each wallet/mint across token accounts, and includes fully
closed accounts as zero post-balance. Same-wallet transfers are not decreases.
The existing >50% selector is unchanged. A balance decrease is not proof of a
swap. Details carry response-bound signature, block time and provenance.

Actual poll recording now uses exact full wallet identity and retains one event
per signature/mint, rejecting conflicting replays. It does not mutate a guessed
most-recent buy or compute a fictional holding period from observation time.
Holding period is explicitly unavailable without a causal buy match. Existing
buy grading/reconciliation and sell recording share whole-RMW locking and unique
fsynced snapshots; unreadable existing grade state is not silently reset. A sell
save failure keeps intake pending. Retained sell events are not currently bounded,
so storage grows; this is not a complete buy/sell attribution or loss-forensics
solution. A failed post-replace fsync can leave visible state, with retry dedup and
resave, not rollback. No new grading or trading threshold is activated.


## Acquisition caller coverage audit

The repaired `scripts/smart-watcher.poll_wallet` is NOT the only wallet source.
`daemon.launch_scanner.smartmoney.poll_wallets` is called directly by the actual
scanner cycle, then `_smart_fasttrack` consumes returned activity. Inspection
previously found that separate path advanced its saved cursor before transaction
success and used a legacy unvalidated last-account balance parser. It now shares
the pure successful wallet-evidence parser with smart-watcher (including
jsonParsed pubkey normalization), keeping its original unrestricted mint selector.
Its separate scanner_wallet_intake journal stages signatures before detail reads,
retains invalid/unavailable details for bounded ordinary-cadence retry, and saves
mint deliveries before returning activity. Actual scanner fasttrack, grade
recording and co-buy handoff require a direct successful state write and readback
before explicit outbox ACK. The committed snapshot carries exact batch references
so restart can finish a missed ACK without duplicating grade/co-buy effects.
Partial fasttrack failures roll back in-memory handoff markers so the cycle's
finally-save cannot consume them. Unverified/uncertain state writes may replay. This is at-least-once intake, not atomic external
publication, lifetime exactly-once, or full scanner lifecycle acceptance. Its configured cadence
is max(smart_poll_seconds default120, registry_count*12) in the actual caller,
while the standalone poll function's default is180; do not silently speed either.

Current RPC acquisition code search also finds reads in bundle, curve_cache,
deployers, exit_copy, funding_clusters, grad_raw, grad_select, logstream, lp_lock,
moon_lane, raw_tight, reserves, tx_guards and wallet_discovery. Presence is not an
audited equivalent source or proof of safe parsing. Each relevant production
caller needs classification and failure/causal evidence before any all-source
completion claim. The separate daemon/intelligence acquisition/decoder stack
remains another integration boundary.

Grade recording counts now require successful snapshot save; buy/reconciliation
persistence failure raises to the existing scanner error logger rather than
reporting a fictional committed count. The scanner fasttrack now propagates that
failure to retain its outbox. Tests cover its real parser, pending retry,
fresh-interpreter delivery replay, successful-empty no-buy acceptance, corrupt
journal preservation, downstream write/readback failure and partial state rollback.
An actual run_once cycle fixture joins discovery to grade/co-buy state commit and
second-cycle dedup, with unrelated network/drain functions stubbed and stop-before-
scan enabled. This is not whole scan/qualification or AGT runtime acceptance.
The broader acquisition inventory remains open.


## Shared unknown execution status

The wider acquisition audit found tx_guards.tx_succeeded accepted an explicit
meta object without an err field as success. It now requires an explicit err=None;
missing status reports tx_unknown. The same gate is used by logstream transaction
mint resolution, deployer resolution, wallet exit-copy measurement and migration
price readers. Actual reader fixtures refuse unknown before parsing balances or
naming a signer. This is execution-status validation only, not response signature
binding, full account identity validation or all-source acquisition coverage.

Classification from actual call sites: wallet_discovery mines proposal-only
candidates, exit_copy measures exits, bundle/funding_clusters read wallet behavior,
grad_raw/moon_lane/raw_tight/grad_select read migration prices, deployers resolves
creator/funder data, and logstream is a separate launch-event acquisition path.
Logstream has its separate received-signature handoff described below; these
readers are not covered by scanner wallet outbox acceptance.


## Received logstream signatures and handoff

Logstream stages successfully reported envelope signatures in a separate atomic,
locked intake before fetching details. Unknown/failed/mismatched transaction
responses remain pending. Existing HTTP pool, pacer and 0.5s per-detail politeness
are unchanged; retry checks every30s process at most10 unresolved signatures and
rotate unavailable ones. Received/resolved work survives process restart; pending
work is not evicted. Accepted signature history retains5000 IDs, not lifetime dedup.

Actual scanner drain no longer removes the whole batch before consideration.
Resolved snapshots stay pending through stop, consideration exception, state-write
or readback failure. Direct scanner state commit/readback precedes intake ACK;
committed references finish missed ACK on restart. In-memory partial-state rollback
does not undo already published notifications or other external side effects.
No global scanner writer serialization, network-disconnection backfill, semantic
instruction ownership proof, whole-bot crash safety or source completeness is
claimed. Valid successful no-mint and definitive failed fetched details currently
remain pending too, so diagnostic backlog can grow. Websocket receipt still has a
small receive-to-stage crash gap; messages not durably received are not recovered.


## LogStream consumer compatibility and Spark watchlist handoff

Constructor/caller search finds scanner and scripts/spark-feed instantiate
LogStream. smart-watcher uses WSClient helpers; armed_watch and PumpPortal also use
WSClient, not LogStream.drain. Package and top-level module import paths are tested.
Spark uses a separate spark_logstream_intake journal: scanner ACK cannot consume
Spark work. Actual Spark collector atomically fsyncs its watchlist before ACK,
preserving full signature and original receipt time. Watchlist save/corruption
failure leaves delivery pending; replay after ACK failure preserves the first
time rather than making an old launch fresh. Existing ignition-window pruning
and signal processing remain downstream, unchanged and not made crash-atomic by
this handoff. Spark is not activated by these changes or fixtures. Existing
load_seen, daily-cap and final signal bookkeeping remain separate audit work.
