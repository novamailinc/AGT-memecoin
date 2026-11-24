"""Provider capability configuration (three-provider program, Phases 1-2).

Config-gated DATA ONLY. This module performs no network I/O, reads no
secret, imports no live module (in particular it never imports the
launch_scanner monolith or ``reserves``), and is never on a live path:
adapters in ``daemon/intelligence/acquisition/`` read these records to
decide what a provider *may* be asked to do, never to bypass a limit.

Every value below is either:

* cited from official provider documentation read 2026-11-22 (see
  ``~/workspace/reports/provider-capability-audit-2026-11-22.md`` for the
  per-claim citations), or
* measured by a live probe run 2026-11-22 (probe time recorded per
  record), or
* explicitly marked ``UNVERIFIED`` with the reason.

Quotas are stored here as CONFIG, not as architectural assumptions:
nothing in this file may be treated as permanent. When a plan changes,
edit the record (and the audit report), not the callers.

Compliance (task section 7): multiple Helius key sources are recorded
separately and may be disabled individually (``enabled``). Nothing here
authorises rotating accounts or distributing requests to evade rate
limits, quotas, billing, or terms. Each account's usage is tracked
independently by the scheduler/health monitor; a disabled account is
never called.

No API key, endpoint id, or credential appears in this file. Key
material lives only in the existing key files / env vars / vault
connector and is resolved by the adapters, never here.
"""

from __future__ import annotations

# Schema version for this file. Bump when a field is added/renamed so
# the schema test and any consumer can refuse a stale shape.
SCHEMA_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Capability vocabulary
# ---------------------------------------------------------------------------
# "yes"         -- confirmed by official docs and/or a live probe.
# "no"          -- confirmed absent / not on the plan in the docs.
# "paid"        -- exists, but only on a paid plan/add-on (free-only
#                  program must not enable it).
# "unverified"  -- no doc or live evidence found; do NOT assume.
# "blocked"     -- live probe attempted and blocked (key/account missing
#                  or rejected); docs-only until the blocker clears.
SUPPORT_YES = "yes"
SUPPORT_NO = "no"
SUPPORT_PAID = "paid"
SUPPORT_UNVERIFIED = "unverified"
SUPPORT_BLOCKED = "blocked"

PROVIDERS = ("helius_main", "helius_discovery", "helius_vault",
             "alchemy", "chainstack", "public")

# Standard Solana JSON-RPC methods the bot uses (see the provider
# dependency report). Cost is per call in the provider's own unit.
# ``None`` cost means the cost is UNVERIFIED for that provider.
METHOD_COSTS = {
    # method: (helius credits, alchemy CU, chainstack RU, public cost)
    "getSlot": (1, 20, 1, None),
    "getHealth": (1, 20, 1, None),
    "getBalance": (1, 10, 1, None),
    "getAccountInfo": (1, 10, 1, None),
    "getMultipleAccounts": (1, 20, 1, None),
    "getTokenAccountBalance": (1, 20, 1, None),
    "getTokenAccountsByOwner": (1, 10, None, None),  # Alchemy CU +
    # Chainstack RU UNVERIFIED (router-owner addition 2026-11-22;
    # Helius 1 credit follows the standard-read convention).
    "getTokenLargestAccounts": (1, 20, None, None),  # Alchemy official CU table verified 2026-11-23;
    # Chainstack docs conflict (methods page: listed; limits page:
    # 0 RPS / Dedicated Nodes only). Do not route until verified.
    "getSignaturesForAddress": (10, 40, 2, None),  # Helius archival 10cr;
    # Chainstack always archive (2 RU) and NOT served on Developer.
    "getTransaction": (10, 40, 1, None),  # Chainstack 1 RU recent only;
    # 2 RU below firstAvailable + 5000.
    "getBlock": (10, 40, 2, None),
    "getSignatureStatuses": (1, 20, 1, None),  # Chainstack 2 RU if old.
    "getProgramAccounts": (10, 20, None, None),  # Chainstack paid-only.
    "getLatestBlockhash": (1, 20, 1, None),
    "getRecentPrioritizationFees": (1, 20, 1, None),
    "sendTransaction": (1, 20, 1, None),  # Helius Free send limit 1/s.
    "simulateTransaction": (1, 20, 1, None),
    "getTokenSupply": (1, 20, 1, None),  # Alchemy official CU table verified 2026-11-23.
}

# ---------------------------------------------------------------------------
# Per-account capability records.
#
# "account" is a key SOURCE as the repo sees it, not a dashboard plan
# name: the plan tier of each Helius key file cannot be read from disk
# and is recorded UNVERIFIED until the operator confirms it in the
# provider dashboard. Never infer a plan from a key file.
# ---------------------------------------------------------------------------
ACCOUNTS = {
    "helius_main": {
        "provider": "helius",
        "role": "live leg today — all standard RPC workloads until "
                "benchmarks prove the new allocation",
        "key_source": "~/.forex-agent/helius/api_key (0600, disk file)",
        "plan": "UNVERIFIED — dashboard check required; do not assume "
                "Free. Observed behaviour (10x429/10min 2026-11-22 "
                "08:44 UTC, auto-quiet) is consistent with a flagged "
                "Free-tier key but is NOT proof of plan.",
        "enabled": True,
        "networks": ["solana-mainnet"],
        "networks_unverified": ["solana-devnet"],
        "https_endpoint": "https://mainnet.helius-rpc.com/?api-key=<key>",
        "wss_endpoint": "wss://mainnet.helius-rpc.com/?api-key=<key>",
        "http_rpc": SUPPORT_YES,
        "websocket": SUPPORT_YES,
        "yellowstone_grpc": SUPPORT_PAID,  # LaserStream gRPC mainnet:
        # Business ($499/mo) or Professional ($999/mo). Devnet only on
        # Developer. Free: not available.
        "historical_archive": SUPPORT_YES,  # full archive on standard
        # RPC (getTransaction/getSignaturesForAddress served; 10 cr each).
        "archive_depth": "full (genesis) per docs; UNVERIFIED by live probe",
        "enhanced_tx_api": SUPPORT_YES,  # 2 req/s on Free; NOT USED by repo.
        "das_api": SUPPORT_YES,  # 2 req/s on Free; NOT USED by repo.
        "parsed_streams": SUPPORT_YES,  # Included on Free, 1 credit/event.
        # NOT USED by repo yet (Phase 7 integration).
        "parsed_events": SUPPORT_YES,  # Included on Free, 10 credits/req.
        # NOT USED by repo yet (Phase 7 integration).
        "laserstream_wss_standard": SUPPORT_YES,  # Included on Free.
        "laserstream_wss_extensions": SUPPORT_PAID,  # Developer+ only.
        "laserstream_grpc": SUPPORT_PAID,  # Mainnet Business+; see above.
        "preprocessed_transactions": SUPPORT_PAID,  # All paid plans,
        # 0.1 credits/message; NOT on Free. Best-effort, no replay.
        "preconfirmations": SUPPORT_PAID,  # Developer+ only, 10 cr/msg.
        "webhooks": SUPPORT_YES,  # 1 webhook on Free V3 / plan-dependent;
        # NOT USED by repo.
        "monthly_quota": {"unit": "credits", "amount": 1_000_000,
                          "note": "Free-plan figure from official pricing; "
                          "applies ONLY if this account is on Free — "
                          "plan UNVERIFIED, so treat as provisional."},
        "rate_limit_rps": 10,  # Free-plan figure; provisional as above.
        "per_method_limits": {"getProgramAccounts": 5, "sendTransaction": 1,
                              "das_enhanced": 2},
        "concurrent_connections": "UNVERIFIED — no published figure found",
        "data_retention_replay": "Parsed Streams: at-most-once delivery, "
                                 "no replay; backfill via RPC on reconnect. "
                                 "Preprocessed tx: no historical replay. "
                                 "Standard RPC archive: full history.",
        "restrictions": ["Auto-quiet engaged 2026-11-22 08:44:53 UTC after "
                         "10x429/10min; fleet on public fallback until "
                         "~09:44 UTC. 429 flapping is the live constraint, "
                         "not the monthly quota (~7% of 1M at ~73k calls/mo)."],
        "last_live_probe": "2026-11-22 09:18:45 UTC: reserves.rpc getSlot "
                           "failed closed on Helius (auto-quiet active) and "
                           "was answered by public fallback, slot 472487966 "
                           "in 1.40s. Last successful Helius probe "
                           "2026-11-22 08:34:55 UTC.",
        "citations": ["helius.dev/docs/billing/plans (updated 2026-11-20)",
                      "helius.dev/docs/preprocessed-transactions/overview",
                      "helius.dev/docs/laserstream"],
    },
    "helius_discovery": {
        "provider": "helius",
        "role": "research-only second key (wallet discovery); wired via "
                "/sethelius2, NOT in the live entry/exit path",
        "key_source": "~/.forex-agent/helius/api_key_discovery (0600)",
        "plan": "UNVERIFIED — dashboard check required; do not assume Free, "
                "and do not assume it is a different account from "
                "helius_main (two key files may belong to one account).",
        "enabled": True,
        "networks": ["solana-mainnet"],
        "networks_unverified": ["solana-devnet"],
        "https_endpoint": "https://mainnet.helius-rpc.com/?api-key=<key>",
        "wss_endpoint": "wss://mainnet.helius-rpc.com/?api-key=<key>",
        "http_rpc": SUPPORT_YES,
        "websocket": SUPPORT_YES,
        "yellowstone_grpc": SUPPORT_PAID,
        "historical_archive": SUPPORT_YES,
        "archive_depth": "full (genesis) per docs; UNVERIFIED by live probe",
        "enhanced_tx_api": SUPPORT_YES,
        "das_api": SUPPORT_YES,
        "parsed_streams": SUPPORT_YES,
        "parsed_events": SUPPORT_YES,
        "laserstream_wss_standard": SUPPORT_YES,
        "laserstream_wss_extensions": SUPPORT_PAID,
        "laserstream_grpc": SUPPORT_PAID,
        "preprocessed_transactions": SUPPORT_PAID,
        "preconfirmations": SUPPORT_PAID,
        "webhooks": SUPPORT_YES,
        "monthly_quota": {"unit": "credits", "amount": 1_000_000,
                          "note": "Free-plan figure; provisional — plan "
                          "UNVERIFIED. If both disk keys share one "
                          "account, this is NOT an additional 1M credits."},
        "rate_limit_rps": 10,
        "per_method_limits": {"getProgramAccounts": 5, "sendTransaction": 1,
                              "das_enhanced": 2},
        "concurrent_connections": "UNVERIFIED — no published figure found",
        "data_retention_replay": "Same as helius_main.",
        "restrictions": ["Research-only by design; never route entry/exit "
                         "traffic here. Usage must be tracked separately "
                         "from helius_main."],
        "last_live_probe": "No dedicated live probe 2026-11-22; key file "
                           "presence only. Probing it would spend its "
                           "quota — probe only via the repo's own paths "
                           "when needed.",
        "citations": ["helius.dev/docs/billing/plans (updated 2026-11-20)"],
    },
    "helius_vault": {
        "provider": "helius",
        "role": "third Helius source via vault surrogate (custom.helius); "
                "appended after disk keys in reserves.rpc_url_pairs()",
        "key_source": "vault connector custom.helius via helius_vault.py "
                      "(surrogate hsurr:*, api-key query-param placement)",
        "plan": "UNVERIFIED — credential validity UNVERIFIED.",
        "enabled": True,  # code appends it when the surrogate is available;
        # fail-soft to disk keys when it is not.
        "networks": ["solana-mainnet"],
        "networks_unverified": ["solana-devnet"],
        "https_endpoint": "https://mainnet.helius-rpc.com/?api-key=<surrogate>",
        "wss_endpoint": "wss://mainnet.helius-rpc.com/?api-key=<surrogate>",
        "http_rpc": SUPPORT_BLOCKED,  # 2026-11-18 probe: getHealth with the
        # surrogate returned HTTP 401 in 1.06s; surrogate issuance works,
        # the exchange does not produce a valid Helius key from this VM.
        "websocket": SUPPORT_UNVERIFIED,
        "yellowstone_grpc": SUPPORT_PAID,
        "historical_archive": SUPPORT_UNVERIFIED,
        "archive_depth": "UNVERIFIED",
        "enhanced_tx_api": SUPPORT_UNVERIFIED,
        "das_api": SUPPORT_UNVERIFIED,
        "parsed_streams": SUPPORT_UNVERIFIED,
        "parsed_events": SUPPORT_UNVERIFIED,
        "laserstream_wss_standard": SUPPORT_UNVERIFIED,
        "laserstream_wss_extensions": SUPPORT_PAID,
        "laserstream_grpc": SUPPORT_PAID,
        "preprocessed_transactions": SUPPORT_PAID,
        "preconfirmations": SUPPORT_PAID,
        "webhooks": SUPPORT_UNVERIFIED,
        "monthly_quota": {"unit": "credits", "amount": None,
                          "note": "UNVERIFIED — no usable key confirmed."},
        "rate_limit_rps": None,
        "per_method_limits": {},
        "concurrent_connections": "UNVERIFIED",
        "data_retention_replay": "UNVERIFIED",
        "restrictions": ["BLOCKED 2026-11-18: surrogate exchange returns "
                         "HTTP 401 on getHealth. Do NOT treat as a live "
                         "third account until a re-probe returns HTTP 200. "
                         "Never extract or handle the raw key."],
        "last_live_probe": "2026-11-18: getHealth via surrogate -> HTTP 401 "
                           "in 1.06s (helius_vault.py module docstring). "
                           "No re-probe 2026-11-22.",
        "citations": ["daemon/launch_scanner/helius_vault.py",
                      "custom.legacy-helius CONNECTOR.md"],
    },
    "alchemy": {
        "provider": "alchemy",
        "role": "END-STATE primary routine/live acquisition (initial "
                "allocation, to be validated by benchmarks). ALSO carries "
                "deep-history retrieval on Free — see note.",
        "key_source": "custom.alchemy connector (api_key, url_path_segment "
                      "/v2/{}) + $ALCHEMY_API_KEY / $HOME/alchemy/api_key "
                      "file fallback in the acquisition adapters. "
                      "NO key file exists on disk yet.",
        "plan": "Free (per connector registration; the account itself is "
                "UNVERIFIED live because the key is rejected — see below).",
        "enabled": True,  # adapters exist; live use BLOCKED on the key.
        "networks": ["solana-mainnet", "solana-devnet"],
        "networks_unverified": [],
        "https_endpoint": "https://solana-mainnet.g.alchemy.com/v2/<key>",
        "wss_endpoint": "wss://solana-mainnet.streaming.alchemy.com/v2/<key>",
        "http_rpc": SUPPORT_BLOCKED,  # key returns HTTP 401; docs only.
        "websocket": SUPPORT_YES,  # docs: all 6 core subscriptions; live
        # handshake behaviour UNMEASURED (no valid key).
        "yellowstone_grpc": SUPPORT_PAID,  # usage-priced $75/TB, NOT part
        # of the free CU allowance; no monthly minimum. Free plan: no gRPC.
        "historical_archive": SUPPORT_YES,  # full archive back to genesis
        # on Free (docs). NOTE: this corrects the initial allocation —
        # Chainstack Developer has NO archive, so deep-history retrieval
        # on the free tier can only be Alchemy (or Helius while live).
        "archive_depth": "full (genesis) per docs; UNVERIFIED by live probe",
        "enhanced_tx_api": SUPPORT_YES,  # NFT/Token/Transfers on Free;
        # Solana DAS listed, Beta, gating UNVERIFIED.
        "das_api": SUPPORT_UNVERIFIED,  # Solana DAS Beta; gating unverified.
        "parsed_streams": SUPPORT_NO,  # Helius-only product; Alchemy has
        # no equivalent. Closest: WebSocket subscriptions + Token API.
        "parsed_events": SUPPORT_NO,  # Helius-only product.
        "laserstream_wss_standard": SUPPORT_NO,  # Helius-only product.
        "laserstream_wss_extensions": SUPPORT_NO,
        "laserstream_grpc": SUPPORT_NO,  # Alchemy's own Yellowstone gRPC
        # is the equivalent, priced $75/TB (paid, above).
        "preprocessed_transactions": SUPPORT_NO,  # Helius-only product.
        "preconfirmations": SUPPORT_NO,
        "webhooks": SUPPORT_YES,  # 5 webhooks on Free; NOT USED by repo.
        "monthly_quota": {"unit": "compute units", "amount": 30_000_000,
                          "note": "Free plan, official pricing. Shared CU "
                          "pool across chains on the account. Bot burn at "
                          "current demand ~2.3-2.9M CU/mo (7.5-9.7%); "
                          "~77-97% at 10x demand."},
        "rate_limit_rps": "UNVERIFIED — conflict: Alchemy docs say 300 CU/s "
                          "account-level (10s rolling window), ~15 req/s; "
                          "secondary sources say 25 RPS. Scheduler uses "
                          "5/s (burst 10), under either figure.",
        "per_method_limits": {"throughput_cu_per_s": 300},
        "concurrent_connections": "100 WS connections, 1,000 subs/connection "
                                  "(secondary — UNVERIFIED against Alchemy's "
                                  "own docs).",
        "data_retention_replay": "Full archive on Free; WebSocket delivery "
                                 "is live-only (no replay); gap recovery "
                                 "via RPC backfill (event_backfill.py). "
                                 "WS billing per byte delivered — rate "
                                 "UNVERIFIED (0.0002 CU/byte in prior audit "
                                 "vs 0.04 CU/byte in a secondary source; "
                                 "do not budget on either).",
        "restrictions": ["BLOCKED live: key returns HTTP 401 Unauthorized "
                         "(re-probed 2026-11-22 09:19 UTC via the connector "
                         "helper). User re-entering the key. Do NOT treat "
                         "Alchemy as verified until a health call returns ok.",
                         "getTokenLargestAccounts documented at 20 CU, rechecked 2026-11-23.",
                         "Historical account-state reads are paid-only; the "
                         "bot does not use them.",
                         "Jito bundle methods are paid-only; the live "
                         "executor's Jito path has no free Alchemy route."],
        "last_live_probe": "2026-11-22 09:19 UTC: connector helper "
                           "`alchemy.py health` -> HTTPError 401 Unauthorized.",
        "citations": ["alchemy.com/pricing (fetched 2026-11-22)",
                      "Alchemy Solana compute-unit costs (via published "
                      "Solana CU table; secondary confirmation "
                      "docs.rpcplane.dev/providers/alchemy/)",
                      "custom.alchemy CONNECTOR.md"],
    },
    "chainstack": {
        "provider": "chainstack",
        "role": "END-STATE historical retrieval, event recovery, "
                "independent verification (initial allocation). QUALIFIED "
                "by this audit: on Developer Free, Chainstack can only do "
                "the RECENT-read subset — it has no archive data, so "
                "deep-history retrieval stays with Alchemy (Free, full "
                "archive) until/unless Chainstack is upgraded to Growth "
                "($49/mo, paid — out of free-only scope).",
        "key_source": "CHAINSTACK_ENDPOINT env var / "
                      "$HOME/.forex-agent/chainstack/endpoint file. "
                      "NO endpoint configured; key outstanding (user "
                      "completing Cloudflare check in the live browser).",
        "plan": "Developer Free (intended; account UNVERIFIED — no console "
                "access yet).",
        "enabled": True,  # adapters exist; live use BLOCKED on the key.
        "networks": ["solana-mainnet"],
        "networks_unverified": ["solana-devnet"],
        "https_endpoint": "https://solana-mainnet.core.chainstack.com/"
                          "<endpoint-id>",
        "wss_endpoint": "https://solana-mainnet.core.chainstack.com/"
                        "<endpoint-id> (same host as HTTPS)",
        "http_rpc": SUPPORT_BLOCKED,  # no endpoint configured; docs only.
        "websocket": SUPPORT_YES,  # docs: listed, no plan restriction found;
        # live handshake behaviour UNMEASURED. WS metering UNVERIFIED
        # except blockSubscribe (1 RU/push).
        "yellowstone_grpc": SUPPORT_PAID,  # Yellowstone Geyser gRPC add-on,
        # $49/mo on Growth+ (2 streams). Not on Developer.
        "historical_archive": SUPPORT_NO,  # Developer has NO archive data.
        # Archive-class calls return -32002 on Developer. Full node
        # retains a 400M-shred ledger; no published slot/time depth.
        "archive_depth": "none on Developer; Growth ($49/mo) is the first "
                         "tier with archive access.",
        "enhanced_tx_api": SUPPORT_NO,  # No DAS equivalent; Subgraphs
        # sunset (disabled 2026-02-01).
        "das_api": SUPPORT_NO,
        "parsed_streams": SUPPORT_NO,  # Helius-only product.
        "parsed_events": SUPPORT_NO,
        "laserstream_wss_standard": SUPPORT_NO,
        "laserstream_wss_extensions": SUPPORT_NO,
        "laserstream_grpc": SUPPORT_NO,  # Chainstack's own Yellowstone
        # gRPC is the equivalent, paid add-on (above).
        "preprocessed_transactions": SUPPORT_NO,
        "preconfirmations": SUPPORT_NO,  # Flashblocks preconfirmations are
        # a separate Chainstack product; availability on Developer
        # UNVERIFIED — do not assume.
        "webhooks": SUPPORT_NO,  # No webhook product found in docs/pricing.
        "monthly_quota": {"unit": "request units", "amount": 3_000_000,
                          "note": "Developer Free, official pricing. "
                          "Services STOP at quota (no auto-overage charge "
                          "on Developer). Bot burn at current demand "
                          "~110k RU/mo (3.7%) IF every call were 1 RU — "
                          "moot for the entry path, which Chainstack "
                          "cannot serve (archive)."},
        "rate_limit_rps": 5,  # Solana Mainnet carve-out on Developer
        # (general plan figure is 25 RPS; Solana mainnet is 5 RPS per
        # Chainstack's Throughput guidelines, updated 2026-11-17).
        "per_method_limits": {"getSupply": "2 RPS, paid plans only",
                              "getProgramAccounts": "paid-only (not served)",
                              "getTokenLargestAccounts": "CONFLICT — methods "
                              "page lists no restriction; limits page says "
                              "0 RPS / Dedicated Nodes only"},
        "concurrent_connections": "500 concurrent WS connections "
                                  "(Chainstack docs; EVM-framed, apply with "
                                  "caution). HTTP: 60s idle timeout, max "
                                  "1,000 requests per connection. WS: 3,600s "
                                  "idle timeout.",
        "data_retention_replay": "Full node retains 400M-shred ledger; "
                                 "archive NOT included on Developer "
                                 "(-32002). No replay product; gap recovery "
                                 "via RPC backfill within the retained window "
                                 "only. Nodes auto-deleted after 30 days of "
                                 "inactivity (notices day 23/28/30).",
        "restrictions": ["BLOCKED live: no endpoint configured; key "
                         "outstanding (Cloudflare check in the live browser).",
                         "getSignaturesForAddress ALWAYS archive-billed "
                         "(2 RU) and NOT served on Developer — Chainstack "
                         "cannot be the fallback for the entry path.",
                         "getProgramAccounts paid-only — live executor ALT "
                         "lookup cannot run on Chainstack.",
                         "getTokenLargestAccounts: docs conflict; do not rely.",
                         "Warp fast-routing (Jito equivalent) is a paid "
                         "add-on ($0.15/tx, Growth+).",
                         "1 project / 1 node on Developer."],
        "last_live_probe": "BLOCKED — no endpoint configured (Phase 2 probe, "
                           "2026-11-22 ~08:55 UTC; unchanged 09:19 UTC).",
        "citations": ["chainstack.com/pricing",
                      "docs.chainstack.com/docs/request-units",
                      "docs.chainstack.com/docs/quotas",
                      "docs.chainstack.com/docs/handle-real-time-data-using-"
                      "websockets-with-javascript-and-python"],
    },
    "public": {
        "provider": "public",
        "role": "tertiary fallback ONLY — never a primary. Keyless Solana "
                "Foundation endpoint plus two alternates in logstream's "
                "failover pool (solana.public-rpc.com, rpc.ankr.com/solana).",
        "key_source": "none (keyless)",
        "plan": "n/a — no account, no SLA",
        "enabled": True,
        "networks": ["solana-mainnet"],
        "networks_unverified": ["solana-devnet"],
        "https_endpoint": "https://api.mainnet-beta.solana.com",
        "wss_endpoint": "wss://api.mainnet-beta.solana.com",
        "http_rpc": SUPPORT_YES,
        "websocket": SUPPORT_YES,
        "yellowstone_grpc": SUPPORT_NO,
        "historical_archive": SUPPORT_UNVERIFIED,  # recent only in practice;
        # getTransaction 403 claim DISPROVED by live probe (HTTP 200).
        "archive_depth": "UNVERIFIED — recent only; no published depth.",
        "enhanced_tx_api": SUPPORT_NO,
        "das_api": SUPPORT_NO,
        "parsed_streams": SUPPORT_NO,
        "parsed_events": SUPPORT_NO,
        "laserstream_wss_standard": SUPPORT_NO,
        "laserstream_wss_extensions": SUPPORT_NO,
        "laserstream_grpc": SUPPORT_NO,
        "preprocessed_transactions": SUPPORT_NO,
        "preconfirmations": SUPPORT_NO,
        "webhooks": SUPPORT_NO,
        "monthly_quota": {"unit": "requests", "amount": None,
                          "note": "Undocumented; documented limit 100 req/10s "
                          "total, 40 req/10s per method (Solana docs). "
                          "Repo budget: 1 call/2s fleet-wide."},
        "rate_limit_rps": 10,  # 100 req/10s per IP (Solana docs).
        "per_method_limits": {"per_method": "40 req/10s"},
        "concurrent_connections": "UNVERIFIED — public pubsub limit '5' is "
                                  "secondary-only.",
        "data_retention_replay": "Recent only; no replay product; gap "
                                 "recovery via RPC backfill within the "
                                 "retained window only.",
        "restrictions": ["No SLA; 429 + 403 (IP block) possible. 'Not "
                         "intended for production' (Solana docs).",
                         "getTransaction 403 claim DISPROVED 2026-11-22 "
                         "~08:55 UTC (HTTP 200, 0.698s, single sample — "
                         "not a load guarantee)."],
        "last_live_probe": "2026-11-22 09:18:45 UTC: getSlot via reserves.rpc "
                           "answered by public fallback, slot 472487966 in "
                           "1.40s (Helius auto-quiet active).",
        "citations": ["Solana docs (public RPC limits)",
                      "Phase 2 probe report 2026-11-22 ~08:55 UTC"],
    },
}

# ---------------------------------------------------------------------------
# End-state workload assignment (initial allocation, validated by the
# Phase 2 capability audit — see the audit report §6 for the one
# correction: deep-history retrieval moves to Alchemy on Free because
# Chainstack Developer has no archive).
# ---------------------------------------------------------------------------
END_STATE_ASSIGNMENT = {
    "routine_live_acquisition": "alchemy",  # primary; Helius stays live
    # until benchmarks prove the allocation per workload.
    "recent_read_verification": "chainstack",  # recent-read subset only.
    "event_recovery_recent_window": "chainstack",
    "deep_history_retrieval": "alchemy",  # CORRECTED: Chainstack Developer
    # cannot serve archive; Alchemy Free has full archive.
    "independent_verification": "chainstack",  # recent reads only.
    "specialized_parsed_streams": "helius_main",  # Free-included, 1 cr/event.
    "specialized_parsed_events": "helius_main",  # Free-included, 10 cr/req.
    "specialized_laserstream_wss": "helius_main",  # standard methods, Free.
    "specialized_laserstream_grpc": None,  # PAID (Business $499/mo) — NOT
    # in the free-only program. Revisit only on an explicit paid decision.
    "specialized_preprocessed_tx": None,  # PAID plans only — not Free.
    "tertiary_fallback": "public",
}

# Methods no provider may be asked to serve on its free tier.
# The scheduler/provider manager must treat these as hard refusals.
FREE_TIER_REFUSALS = {
    "chainstack": {"getSignaturesForAddress", "getProgramAccounts",
                   "getTokenLargestAccounts"},
    "alchemy": set(),
    "helius_main": set(),
    "helius_discovery": set(),
    "helius_vault": set(),
    "public": set(),
}

# Alchemy official support and CU source rechecked 2026-11-23:
# https://www.alchemy.com/docs/reference/compute-unit-costs.md
# /docs/chains/solana/solana-api-endpoints/get-token-largest-accounts.md
# /docs/chains/solana/solana-api-endpoints/get-token-supply.md
# /docs/chains/solana/solana-api-endpoints/get-token-accounts-by-owner.md
# These verify documented methods/costs, not the operator's credentials or uptime.
# Methods whose cost/support is UNVERIFIED on a provider. A call may be
# attempted only in shadow/probe mode and must be flagged provisional.
UNVERIFIED_METHODS = {
    "alchemy": set(),  # Official method + CU docs rechecked 2026-11-23.
    "chainstack": {"getTokenLargestAccounts", "getTokenAccountsByOwner"},
    "helius_main": set(),
    "helius_discovery": set(),
    "helius_vault": {"getTransaction", "getSignaturesForAddress",
                     "getAccountInfo", "getSlot"},
    "public": set(),
}


def get_account(name):
    """Return the capability record for one account, or None."""
    return ACCOUNTS.get(name)


def enabled_accounts():
    """Names of accounts currently enabled (individually disableable)."""
    return [n for n, a in ACCOUNTS.items() if a.get("enabled")]


def supports(account, capability):
    """Support value ('yes'/'no'/'paid'/'unverified'/'blocked') for one
    capability on one account, or None if the account/capability is unknown.
    """
    rec = ACCOUNTS.get(account)
    if rec is None:
        return None
    return rec.get(capability)


def method_cost(provider, method):
    """(helius_credits, alchemy_CU, chainstack_RU, public) cost tuple for
    a method, or None if the method is unknown. A None element inside the
    tuple means that provider's cost is UNVERIFIED — never assume 1."""
    return METHOD_COSTS.get(method)


def validate():
    """Schema validation. Returns a list of problems (empty = valid).

    Checks: every account has the required fields, support values are
    from the vocabulary, quotas are numbers-or-None with a note, and
    the end-state assignment names known accounts (or None for
    paid-gated work that is explicitly out of scope).
    """
    problems = []
    required = ("provider", "role", "key_source", "plan", "enabled",
                "networks", "https_endpoint", "wss_endpoint", "http_rpc",
                "websocket", "yellowstone_grpc", "historical_archive",
                "enhanced_tx_api", "das_api", "parsed_streams",
                "parsed_events", "laserstream_wss_standard",
                "laserstream_wss_extensions", "laserstream_grpc",
                "preprocessed_transactions", "preconfirmations", "webhooks",
                "monthly_quota", "rate_limit_rps", "concurrent_connections",
                "data_retention_replay", "restrictions", "last_live_probe",
                "citations")
    vocab = (SUPPORT_YES, SUPPORT_NO, SUPPORT_PAID, SUPPORT_UNVERIFIED,
             SUPPORT_BLOCKED)
    support_fields = ("http_rpc", "websocket", "yellowstone_grpc",
                      "historical_archive", "enhanced_tx_api", "das_api",
                      "parsed_streams", "parsed_events",
                      "laserstream_wss_standard", "laserstream_wss_extensions",
                      "laserstream_grpc", "preprocessed_transactions",
                      "preconfirmations", "webhooks")
    for name, rec in ACCOUNTS.items():
        for field in required:
            if field not in rec:
                problems.append("%s: missing field %s" % (name, field))
        for field in support_fields:
            if rec.get(field) not in vocab:
                problems.append("%s: %s=%r not in support vocabulary"
                                % (name, field, rec.get(field)))
        quota = rec.get("monthly_quota") or {}
        amount = quota.get("amount")
        if amount is not None and not isinstance(amount, (int, float)):
            problems.append("%s: monthly_quota.amount not a number" % name)
        if not quota.get("note"):
            problems.append("%s: monthly_quota.note missing" % name)
        if not rec.get("citations"):
            problems.append("%s: no citations" % name)
        if not isinstance(rec.get("enabled"), bool):
            problems.append("%s: enabled not a bool" % name)
    for workload, account in END_STATE_ASSIGNMENT.items():
        if account is not None and account not in ACCOUNTS:
            problems.append("assignment %s -> unknown account %s"
                            % (workload, account))
    for provider, methods in FREE_TIER_REFUSALS.items():
        if provider not in ACCOUNTS:
            problems.append("refusals for unknown account %s" % provider)
    for method, costs in METHOD_COSTS.items():
        if len(costs) != 4:
            problems.append("METHOD_COSTS %s: expected 4 elements" % method)
    return problems
