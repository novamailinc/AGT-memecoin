# Helius Credential Roles (Phase 3)

**No key values appear in this file, in any report, log, or commit —
ever.** Key material lives only in its existing files / the vault
connector. This file records ROLES (which source serves which
workload), so a reader can audit attribution without touching a key.

## The three sources

| Source | Location (path only, never the value) | Role |
|---|---|---|
| Main disk key | `~/.forex-agent/helius/api_key` (0600) | The live leg's primary key. `reserves.rpc_url_pairs()` rotates it first; the central router records Helius usage against the `helius_main` account. |
| Discovery disk key | `~/.forex-agent/helius/discovery_api_key` (0600, per the Nov 22 rotation) | Second rotation key in `reserves.rpc_url_pairs()`. Same live leg, second source for pacer-bucket separation. |
| Vault surrogate | Vault connector `custom.helius` via `daemon/launch_scanner/helius_vault.py` (`vault_surrogate_url()`) | Third Helius source, appended after the disk keys in `rpc_url_pairs()`. The surrogate rotates on every issuance; the pacer bucket is keyed on the credential identity (`vault:custom.helius`), NOT on the surrogate value. |

All three were replaced/verified live on 2026-11-22 ~08:28–08:30 PST
(vault via secure card, main + discovery disk files from values
supplied in chat, used transiently, recorded nowhere; both verified
live via getSlot).

## Attribution caveat (standing)

`reserves.rpc()` rotates across the three sources internally and the
router/adapters cannot see which key served a given call. Usage is
therefore recorded against the single `helius_main` account with a
rotation caveat in the journal — per-key attribution is NOT visible
from the router layer, and **no second allowance is assumed**: the
two disk keys may belong to ONE Helius account (1M credits/month),
so budgets are enforced against the single `helius_main` account.

## Rules (standing, from the capability audit §7)

- NEVER rotate keys / accounts to evade rate limits, quotas,
  billing, or terms. A rate-limited account is backed off, quieted,
  or failed over — never replaced by a fresh key.
- A monthly-quota 429 disables the account until the allowance
  renews (operator re-enable); a throughput 429 never does.
- Do NOT rotate keys to evade limits (Phase 3 task 8 — restated,
  not changed).

## OPEN — not resolved in Phase 3

**Whether the vault and discovery keys are the same underlying
Helius key.** The discovery-only-leg experiment that would establish
it changes live routing and needs the parent's word; it was NOT run
in this phase. Until it is established, the program assumes the
conservative position (one account, one allowance) for all budget
enforcement.
