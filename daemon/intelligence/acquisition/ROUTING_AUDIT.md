# Provider-Routing Audit (Phase 3, Master Directive §19 Phase 3)

**Date:** 2026-11-22. **Repo:** `~/workspace/Forex-agent/Forex-agent-main`.
**Method:** grep over `daemon/launch_scanner/`, `scripts/`, `broker/` +
re-read of the call sites Phase 1 listed. Line numbers are against
the tree at audit time.

## Verdict in one line

The central router (`daemon/intelligence/acquisition/provider_router.py`)
is now the single owner of routing DECISIONS for all ordinary
Solana JSON-RPC traffic: every live call through `reserves.rpc()` is
observed in shadow mode (decision logged vs the provider that
actually served it). The live EXECUTION path still runs on
`reserves.rpc()` itself — deliberately, in shadow mode — and seven
modules still own their own transports. No workload has cut over to
Alchemy or Chainstack. See §4 for what stays unrouted and why.

## 1. The single choke point (shadow-wired)

| Live entry | Method | Shadow status |
|---|---|---|
| `daemon/launch_scanner/reserves.py:rpc()` (wrapper; impl `_rpc_impl`) | all standard JSON-RPC (`getSlot`, `getSignaturesForAddress`, `getTransaction`, `getAccountInfo`, `getMultipleAccounts`, `getProgramAccounts`, `getTokenLargestAccounts`, …) | **SHADOW-WIRED** — after every call, `shadow_observer.observe_live_call()` records the router's pure `route()` decision vs `last_rpc_source()` (`helius` / `public-rpc` / None). The live call executes exactly as before; the hook never raises, never retries, never changes a result. Journal: `~/.forex-agent/acquisition_router_shadow.jsonl`. Summary: `scripts/router-shadow-summary`. |

Every module that calls `reserves.rpc()` is therefore shadow-covered
by construction — the router sees its decisions, even though the
execution still goes through the live transport:

`grad_select.py` (curve stats, creator, canonical pool, migration
price), `slot_clock.py` (getSlot), `grad.py`, `grad_raw.py`,
`tranche.py`, `tranche_protect.py`, `momentum_lane.py`, `mirror.py`,
`moon_lane.py`, `raw_tight.py`, `ceo.py`, `dossiers.py`,
`watcher_lane.py`, `smart_select.py` (dormant), `paper_live.py`,
`switches.py`, `armed_watch.py` (HTTP reads; its WSS is §3),
`helius_vault.py` (vault key issuance, not an RPC read).

## 2. Modules with their own transports (NOT routed through the router)

These build their own Helius URLs / keys / fallbacks and call
`urllib` directly. They are NOT covered by the shadow observer
(they never touch `reserves.rpc()`), and they are NOT migrated in
Phase 3. Each row records exactly why it cannot be routed yet.

| Module | Calls | Why not routed yet |
|---|---|---|
| `deployers.py:76,156-162` | `getSignaturesForAddress` (limit 100), `getTransaction` — own Helius URL | Own transport + own key file; migration means moving the deployer registry reads onto `reserves.rpc()` (or the router) and re-proving the serial-deployer filter. Phase 4/5 work; the filter's evidence depends on identical sig pages. |
| `instant_safety.py:100-126,292-294` | `getAccountInfo` (jsonParsed), `getTokenLargestAccounts`, `getMultipleAccounts` — own Helius pairs + public | Safety screening is entry-critical and latency-budgeted (8s); its fallback semantics (Helius pairs → public) must be proven identical under the router before migration. Phase 4/5. |
| `funding_clusters.py:464-494` | `getSignaturesForAddress`, `getTransaction` — own Helius URLs + public | Research-only (dossiers); own transport. Routable later with no lane impact, but unproven — stays unrouted. |
| `smartmoney.py:36,149-205` | `getSignaturesForAddress` (limit 10), `getTransaction` — own Helius URL | DORMANT by order (watchlist-only, zero score since 2026-10-20). No live decision consumes it; routing a dormant module proves nothing. Stays unrouted. |
| `bundle.py:38,85-91` | `getTokenLargestAccounts`, `getAccountInfo`, sigs, tx — public RPC direct | Deliberately public-only (bundle deep scan never spends Helius). Routing it would CHANGE its cost profile; it is not a router workload. Stays public-direct by design. |
| `mimic_curve.py:141-187,290-313` | `getAccountInfo` (base64), own Helius URLs + public | Dormant lane (mimic family killed 2026-11-02). Stays unrouted. |
| `logstream.py:489` | `getTransaction` (jsonParsed v0) — HTTP failover pool | Detection enrichment over the public logstream; its failover pool is a different shape (multi-URL HTTP pool). Routable in Phase 4 with the ingestion work. |
| `armed_watch.py:243-249` | WSS `accountSubscribe` — Helius WSS → public WSS | WebSocket subscriptions are not RPC calls: the router's `route_stream()` covers Parsed Streams, not raw `accountSubscribe`. A WSS migration needs the fixed WS lifecycle (Phase 2, D-02) wired to a supervised client. Stays unrouted. |
| `broker/memecoin/execution/rpc.py` | `getSlot`, `getTokenAccountBalance`, `getSignatureStatuses`, `sendTransaction`, `simulateTransaction`, `getProgramAccounts`, … — Alchemy/Chainstack/public allowlist | The LIVE executor path (never run, unsupervised, paper-only boundary). It already routes to Alchemy/Chainstack by allowlist, NOT through the central router. Unifying it with the router is Phase 7 work, gated on live-execution approval. Stays unrouted. |
| `scripts/helius-load-test` | Direct `reserves.rpc` calls in a loop | Manual load-test script, not live traffic. It calls `reserves.rpc`, so its calls ARE shadow-observed — but it is test traffic, not production; the runner sets `ROUTER_SHADOW_OFF=1` for suites, and this script should be run with that flag too. Noted, not migrated. |

## 3. Out of scope for the router (not Solana JSON-RPC providers)

| Source | Why out of scope |
|---|---|
| `pumpportal.py` (PumpPortal free WSS: `subscribeNewToken`, `subscribeMigration`) | A WebSocket event stream product, not a JSON-RPC provider. No router workload covers it. |
| `jup_price.py`, `sources.py` (GeckoTerminal), `ds_ticker.py` (DexScreener), `social_radar.py`, `sentiment.py` | Third-party REST price/social APIs, not Solana RPC. Different products, different budgets; the router owns Solana providers only. |
| `broker/memecoin/execution/jupiter.py`, `jito.py` | Jupiter swap API / Jito bundles — execution products, not RPC reads. Phase 7. |
| `broker/memecoin/chains/evm.py`, `hyperliquid.py` | Non-Solana chains. Out of this program's Solana provider scope. |
| `helius_vault.py` (vault surrogate issuance) | Credential issuance, not a data call. The router never handles key material. |

## 4. What stays unrouted and why (Phase 3 close-out)

1. **All seven own-transport modules (§2)** — migration is Phase 4/5
   work per workload, each behind the shadow harness with the
   removal criterion (sample size + agreement + zero 429s + latency
   budget). None was migrated in Phase 3 because no shadow evidence
   existed for them at phase start, and migrating an entry-critical
   read (instant safety, curve stats) on zero evidence would violate
   the shadow-first constraint.
2. **The live execution path stays on `reserves.rpc()`** — by design.
   The router decides in shadow; execution follows only after the
   shadow program proves agreement per workload.
3. **No hidden fallbacks.** Every fallback in the live path is the
   documented one: Helius disk keys (rotating) + vault surrogate →
   public mainnet-beta once under the 1-call/2s fleet budget
   (`reserves.py`). The router records `fallback_difference` on
   every served result so a fallback serve is never reported as a
   primary serve.
4. **Chainstack unsupported methods are never routed there:**
   `getSignaturesForAddress` / `getProgramAccounts` /
   `getTokenLargestAccounts` are free-tier refusals in
   `provider_capabilities.py` and the router's capability gate
   refuses them without touching the network (re-probed live in
   Phase 3 — see `master-phase3-d16-probe.json`).

## 5. Routing rules (the router is the only copy)

- Shadow mode (live today): Helius leads P0–P2, last resort for P3;
  `INDEPENDENT_VERIFICATION` never includes Helius (it is the
  subject, not the verifier).
- End-state mode (built, NOT cut over): Alchemy-first for routine
  account/tx reads, signature history, routine recovery; Chainstack
  for recent reads + independent verification of known signatures;
  Helius ONLY for Parsed Streams / Parsed Events. Preprocessed
  observations are REFUSED in both modes (paid-gated).
- `provider_router.end_state_order()` is the public view of the
  end-state rules for the shadow program's cutover decisions.
