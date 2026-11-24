# Secrets mapping — AGT deployment

Phase 13 skeleton. This document names every credential the paper bot
uses, where it comes from on AGT, and where it came from on the VM.
**No credential value appears in this document, in any file under
`deploy/`, or in any log.** Values live only in the AGT vault and are
injected into the deployment process environment.

## Why this mapping exists

On the VM, credentials reach the bot through two paths that do not
exist on AGT:

1. **Disk files** under `~/.forex-agent/` (`helius/api_key`,
   `helius/api_key_discovery`, `telegram/bot_token`). AGT deployments
   are git-tarball checkouts; there is no seeded home directory.
2. **Vault-surrogate bridges** (`daemon/launch_scanner/helius_vault.py`
   via authd, and the `custom.github` / `custom.alchemy` /
   `custom.chainstack` connectors). Those depend on the VM's authd
   socket (`/run/hatch/auth/authd.sock`) and egress proxy. Neither
   exists on the phone, so the bridges are **replaced by env reads**,
   not ported. A missing surrogate on AGT is expected, never an error
   (`deploy/agt/credentials.py::vault_surrogate_available()`).

## Inventory (names only)

| Env var | Required | Purpose | File fallback on VM | Env-first in live code? |
|---|---|---|---|---|
| `HELIUS_API_KEY` | **yes** | Helius main RPC key | `helius/api_key` | Yes — `reserves.py`, `smartmoney.py` |
| `HELIUS_API_KEY_DISCOVERY` | no | Second Helius key (research/discovery traffic) | `helius/api_key_discovery` | Yes — `reserves.py`, `smartmoney.py`, `funding_clusters.py`, `instant_safety.py` |
| `ALCHEMY_API_KEY` | no* | Alchemy API key — **key only, never the endpoint URL** | `alchemy/api_key` | Yes — `acquisition/providers/base.py` |
| `CHAINSTACK_ENDPOINT` | no* | Full Chainstack Solana mainnet endpoint URL (the key is its final path segment) | `.forex-agent/chainstack/endpoint` | Yes — `acquisition/providers/base.py` |
| `TELEGRAM_BOT_TOKEN` | **yes** | Telegram bot token (alerts + control bot) | `telegram/bot_token` | Yes — `daemon/telegram/__init__.py` |
| `HELIUS_ALERT_CHAT_ID` | no | Telegram chat id for Helius 429-spike alerts | — (env only) | Yes — `helius_alerts.py` |
| `GITHUB_TOKEN` | no† | GitHub token for the repo sync loop | — | No (finalize pass) |

\* Required only when the acquisition router enables that provider
(Phases 5–12 cutover). Helius is the live leg today; neither is
required for the skeleton deploy.

† Deferred to the finalize pass: the VM sync script
(`workspace/github-deploy/push_all.sh`) lives outside the repo and
authenticates via the VM authd vault credential. On AGT it needs a
GitHub token in the vault plus a vendored push script. The scheduler
records `github-repo-sync` as `blocked` until then — it never runs
half-authenticated.

## How each credential is consumed on AGT

**Env-first readers (no change needed).** `reserves.py`,
`smartmoney.py`, `funding_clusters.py`, `instant_safety.py`,
`daemon/telegram/__init__.py`, and `acquisition/providers/base.py`
all check the env var before the file. With the AGT vault injecting
the env vars, these work unchanged.

**File-only readers (bridged at start).** Five live readers take only
a file and have no env fallback:

- `grad_select.py`, `grad_raw.py`, `raw_tight.py` — card senders read
  `telegram/bot_token`.
- `helius_alerts.py` — reads `telegram/bot_token`.
- `launch_scanner/__init__.py` — reads `telegram/bot_token`.
- `telegram/commands.py` — reads `helius/api_key_discovery`.

`deploy/agt/credentials.py::bridge_files_from_env()` writes those env
values into the expected runtime paths at deployment start, mode
0600, inside the deployment's private `FOREX_AGENT_HOME`, from env
only. The files exist only on the phone, only while the deployment
runs. They are never committed, never logged, and never copied into
backups (`scripts/forex-backup` already excludes secrets; the bridge
files live under the same exclusion — see `backup_recovery.md`).

The long-term fix — adding env fallbacks to those five readers — is a
live-code change and is **not** made in this skeleton pass (no live
files are edited in Phase 13). It is a finalize-pass candidate.

## Startup check (fail closed)

`deploy/agt/credentials.py --preflight` (run by `start.sh` before
anything else) raises a clear error naming every missing **required**
variable and the deployment refuses to start. There is never a silent
default, never a fallback to a placeholder key, and the error message
contains names only — never values.

## Rotation

Rotate a credential by updating it in the AGT vault and restarting
the deployment (AGT app → deployment → Restart). The bridge files are
rewritten from env on every start, so no stale file can survive a
rotation. Revoking an agent's vault in AGT wipes that agent's secrets;
the deployment then fails closed at preflight until the vault is
re-seeded — by design.

## What is NOT a credential here

- `FOREX_AGENT_HOME` — a path, not a secret.
- `AGT_STATUS_PORT` — a port number, not a secret.
- The deployment manifest (`manifest.json`) — env **names** only.
- The live-trading keypair (`live/keypair.json`) — **does not exist
  and must never be created on AGT**. Mainnet stays disabled; there is
  no mainnet wallet on this deployment, and the backup explicitly
  deletes any key file from staged copies.
