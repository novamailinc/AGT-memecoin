# Live benchmarks — what is UNMEASURED here, and how to measure it on AGT

Phase 13 finalize (2026-11-22). This file does NOT re-measure
anything. It references the Phase 10 benchmark report
(`~/workspace/reports/phase10-benchmarks-2026-11-22.md`, §8) and
records the exact on-phone procedures for the items that could
only ever be measured with real keys on the phone. Nothing below
is estimated; every number the dashboard or a report shows for
these items must come from a run of the procedure named here.

## Status

- **AGT admission: DONE 2026-11-22 ~02:34 PST.** Agent `nova`
  admitted (signed by Phoenix, main key); echo test passed (job
  27d74efc, exit 0); runtime check passed (job fda7ff77, exit 0):
  Python 3.12.10, Node v22.23.5, npm 10.9.9, git 2.51.0, curl
  8.14.1. Add-ons: xfce 0.1.14, playwright 0.1.1.
- **Still open: the Alchemy/Chainstack key values must reach a
  vault on the phone before any live benchmark can run there.**
  They cannot be read out of secure storage on this VM. The
  route now being tried (2026-11-22 ~02:44 PST, user: "figure it
  out yourself"): Nova asked Phoenix via AgentLink (msg 429bbb91)
  to put both keys in Phoenix's vault on the phone and run
  getHealth for each, then report back. Phoenix needs the key
  values from the user (or already has a route to them).
  Awaiting Phoenix's reply. No service_role key ever.
- **Placements from this VM are exhausted:** bearer-header and
  query-param placements for BOTH Alchemy and Chainstack return
  401 ("Must be authenticated!" / "Access token missing or
  invalid"). Both providers document path-only auth, so no
  placement works from here. Do not re-try placements from the
  VM; the next proof must come from the phone.
- **Helius stays the live leg** until benchmarks prove the new
  allocation. Preprocessed → ALWAYS REFUSED (paid-gated).

## UNMEASURED items + AGT procedures (Phase 10 §8, restated)

| Benchmark | Why unmeasured here | AGT procedure |
|---|---|---|
| Live provider latency (Alchemy / Chainstack / public, per method) | Keys unproven (401 from this VM); live calls forbidden by the Phase 10 hard rules | On AGT with real keys: `run_benchmark(provider, [(method, params)...], calls_per_method=100, rpc_call=live)` per provider per method; record p50/p95/p99 + 429/timeout rates + CU/RU spent |
| Helius adapter call path | `HeliusAdapter.call()` delegates to `daemon.launch_scanner.reserves` (live transport — dual-import hazard); calling it would hammer the fleet's live leg, flapping in auto-quiet | On AGT with the live transport: build `HeliusAdapter`, call getSlot / getTransaction / getSignaturesForAddress 100× each via `benchmark_adapter`, record p50/p95/p99 + 429 counts. Same for Alchemy/Chainstack with real keys |
| Live stage latency (preprocessed / processed / confirmed / finalized) | Inter-stage latency is provider + network time; no live stream runs here; preprocessed is paid-gated (refused) | On AGT: subscribe the Helius parsed stream + Alchemy WSS, record `observed_at` per stage per signature in the event store, then run `stage_latencies()` over the stored observations (n≥100 per stage pair) and report each transition separately |
| Live WSS: handshake / reconnect / completeness | No live WSS connection from this VM; handshake 429s and reconnect timing need the real endpoints | On AGT: connect each provider WSS, record handshake time, 429 counts, reconnect time after a forced drop, and missed-event counts vs backfill recovery over 24 h |
| Live backfill rate | Backfill rate against a live archive depends on provider paging latency, not driver overhead | On AGT: run `BackfillDriver.recover_gap` over a real 24 h gap via `measure_backfill_performance`; record events/s, pages, and unrecovered ranges |
| Live quota consumption | CU/RU spend under live load cannot be observed from mocked transport | On AGT: run the routine workload for 1 h, read the usage journal (`usage_report.py`), compare CU/RU spent vs the free-tier budgets |

## What Phase 10 DID prove here (not to be re-run, only re-checked)

- Router dispatch overhead, adapter overhead vs a mocked
  transport, decoder parse rates, event-pipeline throughput,
  backfill primitive rates (a ceiling, not a forecast). Those
  numbers are in the Phase 10 report §§1–7. Small-VM timing
  noise inflates p95/p99 here; re-run on AGT for tighter tails.
- The mocked transport is not the provider. A provider that is
  slow, 429s, or returns partial data will look nothing like the
  Phase 10 numbers — that is exactly what the table above exists
  to measure.

## Recording a live benchmark run

Each run writes its results (provider, method, n, p50/p95/p99,
429/timeout counts, CU/RU spent, AGT job id, timestamp) into the
deployment's usage journal and the coordinator's report. A
benchmark that is run without its n, its provider, and its
429 counts is not a measurement — record all three or do not
report it.
