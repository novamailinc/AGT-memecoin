# Development Telegram control room

This is the real Telegram daemon connected without starting trading or market-data processes. It is not an AGT deployment, a live-market demo, or qualification evidence.

## Start

Use a persistent development state directory, not a fixture home. Configure:

- `FOREX_AGENT_HOME`: private control-state directory.
- `FOREX_AGENT_STORAGE`: its SQLite database path. All later connected components must use this same store for shared controls.
- `TELEGRAM_ALLOWED_CHAT_ID` and `TELEGRAM_ALLOWED_USER_ID`: the approved owner pair. Both must be set.
- Bot token through `TELEGRAM_BOT_TOKEN` or the existing private `telegram/bot_token` file. Never put it in a command argument or commit it.

Run `python3 scripts/supervisor --telegram-control-only` from the repository. This explicit roster includes only `daemon.telegram --control-only`. It cannot start the normal scanner/trader fleet. Do not use the ordinary supervisor command for this limited setup.

The normal Telegram long-poll cadence remains unchanged. Shared supervision retains its existing restart backoff and storm guard. The supervisor can adopt an existing Telegram process; ensure no unrelated bot poller is running first. This mode inherits the configured state paths and owner allowlist; it does not pair an arbitrary first sender.

## Available

- `/start`, `/help`: owner pairing and accurate limited command list.
- `/status`: stored runtime/gate state, with control-only and unpassed-stage labels. No broker/worker network health probes.
- `/account`, `/positions`, `/perf`: the local memecoin simulated account only. Missing ledger is unavailable, never test money or proof of zero trading history. No legacy forex account is substituted.
- `/halt`: persist the actual local global-entry latch and verify its state, without constructing a broker/executor. All connected components must share the same configured database.

Trade, resume, configuration mutation, research/price commands and inline callbacks cannot execute in this mode. Price/blink/thesis side loops do not run. Normal authorized commands remain unchanged outside this mode.

## Delivery and lifetime

Command sends record `last_command_delivery` in durable Telegram daemon state only after Telegram's API accepts the send, including returned message ID and acceptance time. This is not a read receipt. Failed sends increment `command_send_failures`, not a success record. Older replies without this instrumentation have no stored acceptance proof.

The supervisor restores a stopped Telegram child within its restart policy; durable pairing and update offset survive a process restart. Neither process by itself proves boot persistence. If the development host shuts down, service stops unless an independently configured host boot service restores it. Do not promise permanent hosting from a detached process or a supervised child restart.

No token, user message body or private account contents belong in logs or this document. The production AGT install/reboot/restore acceptance is still separate work.
