# Request audit — sections 4/5/6

Audit of the repo against the engineering request (26-section AGT trading-system
brief), sections 4 (intelligence/memory), 5 (early discovery), 6 (market
intelligence). Status vocabulary: IMPLEMENTED (code evidence cited) / PARTIAL /
MISSING / PENDING (not yet verified). Repo baseline: 4226728 (main, 2026-11-23).
Items marked FIXED name the landing commit.

## 5. Early token discovery

- On-chain discovery via program subscriptions: IMPLEMENTED — daemon/intelligence/acquisition
  (websocket_manager.py, subscription_manager.py, helius_streams.py, providers/),
  decoders for pump.fun + PumpSwap + token program (daemon/intelligence/decoders/).
  PENDING: verify Pump.fun V2 coverage claim, pool-creation and liquidity-initialization
  event coverage.
- Every discovery event has timestamp/slot/tx-sig/program/provenance/validation/confidence:
  IMPLEMENTED — LogicalEvent (daemon/intelligence/events/event_models.py) carries
  signature, slot, block_time, per-field provenance, verification_status
  (verified/unverified/conflicting), lifecycle_status, providers, decoder_version.
  FIXED (b45ad4a): (a) the event record now carries an explicit program-identity
  field with merge semantics; (b) confidence/completeness remain categorical
  (verification_status) — no invented numeric scores; documented as the chosen
  representation.
- Discovery latency measurement: IMPLEMENTED — daemon/launch_scanner/latency.py
  measures first_seen_ts -> first_card_ts (discovery-to-signal), persisted,
  median/p90 surfaced. FIXED (b45ad4a): LogicalEvent now carries
  recognition_latency_s (on-chain event block_time -> bot recognition) with merge
  semantics. REMAINING: acquisition/benchmark.py receipt-vs-decision harness is a
  keyed live benchmark, currently blocked (keyed VM run held); not continuous
  production measurement.
- "Do not confuse fast discovery with good opportunity": design posture present
  (measurement-only module, never trades/gates).

## 4. Intelligence and memory

- 4.1 Market memory: PARTIAL — dossiers.py (per-mint birth dossiers, 24h TTL),
  regime.py (regime tagging), backtest REGIME_RULES (contemporaneous-only),
  fingerprints.py (durable fp_series_journal). GAP: TTL'd dossiers are not long-term
  memory of launches/patterns; "remember failed launches, liquidity collapses,
  historical signal outcomes" partially via verdict_ledger/lessons; depth check PENDING.
  FIXED (0f7c1da lineage): durable launch archive - every expired dossier archives
  before eviction with birth facts, safety verdict, three-valued migration
  provenance (explicit / validated migrated_ts stamp / unrecorded; contradictions
  labeled conflict), bounded preliminary curve-history digests (actual retained
  samples with totals), and per-deployer-year rollups with honest counter merges
  (corrupt counters marked partial, never silently summed; partial surfaces in
  _intel_fields deployer_history). Curve-history digests are retained on raw
  records only - rollups keep counts, and curve-kind labels remain
  sample-count categories; no full-curve or verified pattern claim.
- 4.2 Wallet memory: IMPLEMENTED (measurement posture) — wallet_grader.py
  (statistical gates), funding_clusters.py (union-find funder clustering, explicitly
  non-gating), deployer_tracker.py / deployers.py, smartmoney.py, follower_grader.py.
  FIXED (persistence depth): deployers.py now archives an entry before registry
  eviction (append-only deployer_archive.jsonl under flock; eviction only after a
  successful append - failed archives keep the entry, never a silent drop; torn
  tails repaired under lock before append and counted), per-outcome bounded
  timestamps (outcome_ts) alongside the bounded launch_ts, and segments with
  stable ids (deployer + registry first_seen): a re-appearing deployer's later
  segment never erases an earlier one - reads and compaction keep PER-SEGMENT
  detail per segment id (the dedup identity compaction cannot erase). Append
  time is NOT the logical version (an unlocked registry read-modify-write can
  archive a stale snapshot after a fresh one), so same-id snapshots merge by
  monotonic maximum per cumulative counter; a decrease is an explicit
  regression_conflict label, never an invented total and never a silent
  overwrite, and any regression conflict makes the row partial (the max
  combination may never have existed as an actual snapshot) so no consumer
  can read it as a verified record. Entries without a verifiable first_seen have no deterministic
  identity: archiving fails closed (entry retained) rather than fabricating
  recency. An unarchivable candidate does not block eviction liveness: the
  bounded eviction pass skips it (counted as eviction_skips_blocked), tries
  the remaining eligible entries, and never deletes unarchivable history
  silently; a pass that evicts nothing is an explicit overflow event
  (eviction_overflow_events), and no hard registry-size bound is claimed.
  Every numeric boundary (counters, timestamps, archived_ts) rejects bools
  and NaN/inf explicitly: a bool is not a count and NaN can never be ordered
  or merged safely. Records with corrupt archived_ts or no id are isolated and counted
  (unusable_records / dropped_unreadable_total), never poisoning the read.
  Totals sum only over verifiable counters (corrupt counters mark the row
  partial); segment detail grows with distinct evictions (bounded in practice
  by activity, no enforceable-constant or lifetime-dedup claim); live risk
  counters are never rehydrated from the archive. Compaction
  past the byte cap rewrites one row per deployer as above; dropped corrupt or
  unidentifiable lines, compaction failures and torn-tail repairs persist in a
  sidecar meta file surfaced by deployer_archive() (missing file =
  unknown_missing, absence cannot prove zero history). Archived
  counters/timestamps are labeled recorded values, not verified skill;
  advisory memory only, no gate/entry/exit reads it (tests in
  test_deployers.py).
- 4.3 Trading memory: IMPLEMENTED — recorder.py, tap_journal.py, decision_journal.py
  (daemon/intelligence/signals/), verdict_ledger.py, exit_provenance.py, paper_account
  ledgers. decision journal + lessons.py settle records with outcomes.
- 4.4 Strategy memory: PARTIAL (advisory) - lessons.py persists entry features
  and settled outcomes, generates deterministic per-band summaries, and journals
  context IDs in grad_select. No lesson-based verdict gate or point-in-time lesson
  application is built. lane_bandit + lane-bandit-shadow are diagnostic allocations,
  not verified entry consumers. ceo.lessons_context has no production caller found
  (definition ceo.py:117, only tests/test_lessons.py references it); the auto-tune
  comment describes intent, not a wired consumer. PARTIAL progress (outcome-event
  cutoff): lessons.generate_lessons_as_of computes the lesson payload only from
  decisions whose close_ts (outcome event time) is at or before as_of; non-finite
  as_of rejected, future as_of labeled. mode="knowledge_time" filters on
  settled_ts (the append time stamped by the settle paths): records closed
  before the cutoff but recorded after it are excluded, records recorded after
  their close (any settled_ts > close_ts, no lateness threshold) are counted
  in recorded_after_close_included, records recorded before their close are
  excluded as inconsistent_time_excluded (counted in both modes), and records
  lacking a finite settled_ts (ledger-rebuilt/legacy) are excluded and counted
  in unknown_availability_excluded - never silently treated as known.
  Settled-looking records of unknown status are counted in
  status_unknown_excluded, never assumed pending. Non-dict or close_ts-less
  records are counted in malformed_excluded (both modes); any exclusion sets
  partial_history=True. Per-feature availability: log_decision stamps
  feature_ts[k] at first record (append and merge fills; merge fills only
  None values, so recorded values are immutable and first-recorded times are
  exact); in knowledge_time mode a record with a banded feature recorded
  after as_of is excluded (feature_recorded_after_as_of_excluded) and one
  lacking feature_ts is excluded (feature_availability_unknown_excluded).
  Numeric-string values follow the same numeric coercion as generate_lessons.
  knowledge_replay_limits labels the remaining gap: post-hoc file edits are
  unversioned, so this is an append-time replay of the known-availability
  subset, NOT a verified leak-free reconstruction guarded against file
  tampering. Unknown modes rejected (tests in test_lessons.py).
- 4.5 Operational memory: IMPLEMENTED — acquisition/provider_health.py
  (ok/degraded/down/unconfigured, latency/error/429 surfacing, public-fallback-only
  alert), health_monitor.py, event_recovery.py, helius_ratelimit.py,
  provider_health.read_durable_statuses (cross-process read-only view; missing file =
  unknown, never "ok"). FIXED (10cc2d7): the "operational memory must inform the
  decision engine" requirement is now wired — paper_account's entry debit consults
  the durable health record when PROVIDER_HEALTH_GATE_ENABLED=1 (default OFF;
  activation and the PROVIDER_HEALTH_GATE_PROVIDERS list stay owner-owned). Enabled
  with an incomplete provider list, a missing/corrupt/stale record, or any listed
  provider not "ok" blocks the entry fail-closed: uncertain market information is
  never treated as reliable. Exits never gated. tests/test_provider_health_gate.py
  (12 checks).
- 4.6 Controlled learning: PARTIAL - advisory lesson statistics exclude non-current
  price provenance and nonfinite/inconsistent core outcomes; all-settled and
  excluded recorded economics remain separate diagnostic figures, not executable
  truth or qualified performance. Selective missingness can bias the included
  simulated subset. walk_forward.py is a chronological classifier harness, not a
  general promotion controller. Existing degen-learner mutation is now contained:
  mandatory dry-run, save_weights refuses, scheduler invokes --dry-run explicitly.
  No runtime LLM dependency added. PARTIAL (research plumbing only):
  promotion.py writes durable research-only promotion_decision records -
  verdicts NEED-CRITERIA / NEED-DATA / EVALUATED, never a promote
  recommendation; without source-approved criteria every record is
  not_eligible. Enforced discipline: chronological splits on settled time
  only (no archive-time fallback), entry-time-only scorer schema (outcome
  peeking fails loudly), all-settled economics with excluded outcomes
  labeled and reported separately (never qualified performance), and the
  unresolved requirements (capital protection, selection noise, fresh
  holdout, overlap, missingness bias, counterfactual coverage) recorded on
  every decision. Eligibility is never caller-conferred: records stay
  not_eligible_research_only regardless of flags. Entry schema hygiene
  enforced (finite entry_ts, dict features, entry not after settle).
  Storage: fail-closed locked append with torn-tail repair, file fsync
  (directory fsync best-effort, the stated limit), reader counts
  unreadable lines (tests/test_promotion.py, 13 checks incl. negative
  probes). PENDING and owner-owned: approved promotion criteria,
  candidate wiring from the degen-learner dry-run, demo validation;
  NO activation authorized.

## 6. Advanced market intelligence

- 6.1 Launch intelligence: IMPLEMENTED (partial depth) — safety.py (RugCheck +
  instant_safety on-chain fallback: authorities, freeze, mint characteristics),
  dossiers.py, lp_lock.py, locked_liq.py. PARTIAL: Token-2022 extension checks - presence-based traps (transferHook/permanentDelegate/nonTransferable fail; defaultAccountState-frozen fail; transferFeeConfig >10% fail) plus NAMED review classification of the canonical UiExtension serde names (pausableConfig, scaledUiAmountConfig, interestBearingConfig, memoTransfer, confidentialMintBurn, unparseableExtension, authority-mutable fee schedules), same REVIEW class (flag, pass, -5). PENDING: behavioral evaluation (hook program semantics, maximumFee/withheld modeling, pausable state tracing); named classification is not semantic safety.
- 6.2 Liquidity intelligence: IMPLEMENTED — reserves.py, reserves_curve_price.py,
  curve_cache.py, liquidity_mirage.py, exit_liquidity_clock.py, price-impact modeling
  in backtest.py. FIXED (monitoring depth, measurement-only): reserve_monitor.py
  keeps a durable per-pool reserve-snapshot history (append-only JSONL under flock,
  torn-tail repair counted, missing file = unknown_missing never clean empty;
  bool/NaN/negative reserves rejected and counted at the boundary; optional
  quote_mint/decimals/slot/per-side observation ts recorded only when the caller
  grounds them, supplied-but-invalid rejects the record). Retention honesty: the
  byte cap is a compaction TRIGGER, not a hard bound (compaction keeps per-pool
  newest samples, which many pools can still push past the cap); dropped samples
  and unreadable lines are counted and propagated into EVERY history/detector
  result (retention block, partial flag), never vanishing into a separate status;
  meta writes are best-effort without fsync. Detections, all advisory:
  abnormal movement (drain/spike vs the pool's OWN trailing median, explicit
  insufficient_history / baseline_unusable labels, never a fabricated baseline;
  every result carries last_observed_ts + observed_age_s, no imposed freshness
  policy) and pool migration (causal: baselines use ONLY pre-transition samples,
  post-transition state is the LATEST observation so drained-then-recovered is a
  labeled conflict; markers bind to one transition only within a time tolerance;
  provenance explicit > inferred_from_reserves > unrecorded/unknown - pool
  appearance alone is never canonical proof and absent evidence fails unknown,
  not inference). All ratios/windows are UNVALIDATED ADVISORY HEURISTICS, labeled
  in every result; no trading threshold approval exists or is claimed. Runtime
  wiring: armed_watch records a snapshot from each vault resolution it ALREADY
  performs (arming + reconnect reconcile) AND from websocket accountNotification
  pushes (identity from the watcher's own subscription map, raw units identical
  to resolve_vaults, arrival time; both sides required with per-side observation
  ts, an unobserved side is unrecorded never zero-filled) - no new RPC, no added
  polling, cadence unchanged, monitor failure can never break the watch loop
  (negative test). BOUNDARY: detection output is advisory surface only; no gate,
  score, entry, or exit reads it, and continuous COVERAGE is bounded by armed
  positions (tests test_reserve_monitor.py, 28 checks incl. push-notification
  fixtures, causality probes, wiring + negative tests; armed_watch guard suite
  unchanged green).
- 6.3 Wallet intelligence: see 4.2 + insiders.py, holder_tracker.py, holder_quality.py,
  dust_holders.py, anticrowd.py, funding_clusters.py.
- 6.4 Trading intelligence: IMPLEMENTED — momentum.py, smartgrade.py, fusion.py
  (daemon/intelligence/signals/), saturation.py, spoof_walls.py. PENDING: multi-window
  evaluation + double-counting guard verification.
- 6.5 Manipulation intelligence: IMPLEMENTED — toxicity.py (BUMP_FARM, WASH_SKEW,
  BOT_SWARM), bundle.py, insiders.py, funding_clusters.py, spoof_walls.py.
  FIXED (b45ad4a): check_toxicity_detailed now returns per-fingerprint coverage
  labels (clear/veto/unverifiable with reasons); the smart-watcher journals
  toxicity_coverage with every card, so each veto carries its evidence and every
  "clear" is distinguishable from "did not run". Legacy veto outcomes preserved.
  FIXED (18b2c6d): watcher_lane re-runs the same fingerprints on its own fresh tape
  at entry — the bus label is not proof the upstream stack ran; veto = final skip,
  unverifiable = fail-closed transient skip (regression tests in test_watcher_lane;
  validator forge probe opens 0 positions).

## Remaining verification work
1. Each PENDING item above (depth checks, not docstring trust).
2. Keyed live benchmark run (acquisition/benchmark.py) — held pending account facts.
3. Anything requiring live mainnet data stays labeled unverifiable until a keyed run.
