# Provider assigned-job live validation — 2026-11-23

Scope: read-only, free-tier/shared-quota live execution of each provider's
assigned job from the acquisition VM. No trading, signing, deployment, paid
usage, or plan changes. Credentials came from the operator vault and were
never logged; all endpoints below are key-redacted. Failed-before /
success-after evidence preserved. Harness artifacts are separated from
production defects.

## Result matrix (live)

| Assigned job | Provider | Result | Evidence |
| --- | --- | --- | --- |
| Normal scanning (HTTPS) | Alchemy | PASS | Alchemy-first hint (`purpose=normal_scanning`) served by Alchemy, 5 sigs returned. HTTPS scanning only — this says nothing about Alchemy push/stream performance. |
| Routine RPC (getSlot) | Helius | PASS | Primary route, 89ms (key slot 2). |
| Transaction read (getTransaction) | Helius | PASS | Primary route, 134ms, with repo `TX_FETCH_OPTIONS` (`maxSupportedTransactionVersion: 1`). |
| Relevant streams/events (Parsed Streams) | Helius | PASS after fix | See "Defect 1". Live event via `HeliusParsedStreamClient`, 430ms to first `parsedTransactionNotification`, slot 473214591, no 429s. |
| Enriched intelligence (parsed events API) | Helius | PASS (guarded) | 10 credits consumed; guard refused non-qualifying requests by design. |
| Complementary recent tx/block/account reads | Chainstack | PASS | getSlot 117ms; getBlock(slot 473214790, signatures detail) 149ms, 222 signatures, blockhash prefix `BaT2bzFMs3pa`. |
| Independent verification | Chainstack | PASS | `verify_transaction` on a Helius-fetched tx: `verified:true, match:true` in 156ms, served by chainstack. |
| Normal scanning push (WSS logs) | Alchemy | BLOCKED (plan/app-side) | See "Blocker 1". |

## Defect 1 (production, fixed): Parsed Streams pointed at the wrong host

- Symptom (failed before): `HeliusParsedStreamClient` against
  `wss://mainnet.helius-rpc.com/?api-key=...` → subscribe rejected
  `-32601 Method not found`, 0 events over 75s.
- Root cause: Helius serves `parsedTransactionSubscribe` (Parsed Streams,
  available on all plans) only on the Gatekeeper host
  `wss://beta.helius-rpc.com`. `routine.websocket_url()` had no
  parsed-streams purpose and no caller supplied the Gatekeeper URL; the
  only construction of the client in-tree used a fake test socket, so the
  miswiring never surfaced offline.
- Fix (this patch): `HELIUS_PARSED_STREAMS_WSS_TMPL` in
  `providers/base.py` + a `parsed_streams` purpose in
  `routine.websocket_url()`. `routine` and `normal_scanning` purposes
  unchanged (offline regression asserts mainnet host, Alchemy host, and
  unchanged no-arg default).
- Causal retest (success after): raw probe to the Gatekeeper host ACKed
  the same subscribe and delivered a notification within 30s; the actual
  client then received a real pump.fun event in 430ms (slot 473214591,
  verification_status `parsed_unverified`, 0 undecoded, 0 handshake 429s).

## Harness artifacts (not product defects)

- Earlier all-provider getTransaction errors came from the test driver
  sending `maxSupportedTransactionVersion: 0`; repo code uses 1 and passes.
- One "client silent, 0 events" run was a driver that called `connect()`
  without `run_forever()` (connect only constructs the transport;
  documented in the code).
- SharedQuota `lock_error` runs happened when `FOREX_AGENT_HOME` did not
  exist (fail-closed by design); `mkdir` first.

## Blocker 1 (unchanged): Alchemy WSS 403

- Live evidence (single bounded check, not repeated):
  `wss://solana-mainnet.streaming.alchemy.com/v2/<key>` →
  `WebSocketBadStatusException: Handshake status 403 Forbidden`,
  response body "this endpoint is not available", Alchemy trace id
  `940fa69f-ff15-4cba-b6a5-e2dba3b97fbc`, 2026-11-24 04:35:36 UTC.
- The endpoint matches Alchemy's current Solana PubSub documentation
  exactly (`wss://solana-mainnet.streaming.alchemy.com/v2/<APP KEY>`),
  so this is not a code/endpoint defect. Whether the cause is plan tier
  or an app-side capability/setting cannot be distinguished from here —
  it needs an Alchemy dashboard/console check. No paid upgrade is
  recommended or authorized on this evidence alone.
- Impact is scoped: Alchemy HTTPS scanning works; only the Alchemy-side
  scanning *stream* is blocked. The revised split already assigns the
  streaming role to Helius.

## Finding (policy, not weakened): Helius account-wide pacer

- One account-wide token bucket across the 3 keys: 1 token / 20s,
  burst 2 (`helius_ratelimit.py`, deliberately conservative comment).
- Effect under revised-role traffic: after ~2 Helius calls/min, routine
  calls fall back to Alchemy with ~5s added latency. This is the repo's
  own configured guard, not a vendor 429 — the vendor's actual free
  quota was not hit during validation (0 vendor 429s observed).
- Retuning for the speed requirement is a config-policy decision needing
  the authorized live load-test (`scripts/helius-load-test`). Left
  untouched.

## Provenance

All latencies/ids above come from bounded one-shot live calls on
2026-11-23/24 (UTC-8/UTC) from the acquisition VM; request+response
records retained in the operator's test reports. No secret material is
present in this document or the exported patch.
