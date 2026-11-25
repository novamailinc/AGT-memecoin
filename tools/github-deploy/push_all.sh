#!/bin/bash
# Push ALL project code to olamoreA/meme-project: bot code at repo root,
# supporting code under tools/. Pass-through args (e.g. --dry-run) appended.
# Secrets are never pushed: .env/.key/.pem files, keypair/bot_token/api_key
# filenames, and any file containing a private-key block are excluded
# (see EXCLUDE_* and SECRET_MARKERS in push_repo.py).
set -e
W=/home/hatch/workspace
# tools/phone-test is intentionally NOT synced: frozen unqualified live-wallet bundle (guard in push_repo.py).
exec python3 "$W/github-deploy/push_repo.py" "$W/Forex-agent/Forex-agent-main" \
  --message "Sync all project code" \
  --extra "$W/memecoin-toolkit:tools/memecoin-toolkit" \
  --extra "$W/memecoin-toolkit-realdata:tools/memecoin-toolkit-realdata" \
  --extra "$W/temporal-edge-validator:tools/temporal-edge-validator" \
  --extra "$W/custom-connectors/custom.legacy-helius:tools/connectors/helius" \
  --extra "$W/custom-connectors/custom.legacy-telegram:tools/connectors/telegram" \
  --extra "$W/custom-connectors/custom.alchemy:tools/connectors/alchemy" \
  --extra "$W/custom-connectors/custom.chainstack:tools/connectors/chainstack" \
  --extra "$W/reports/shadow-live-2026-11-22/*.py:tools/scripts/reports/shadow-live-2026-11-22" \
  --extra "$W/github-deploy:tools/github-deploy" \
  --extra "$W/agentlink-deploy:tools/agentlink-deploy" \
  --extra "$W/reports/*.py:tools/scripts/reports" \
  --extra "$W/wallet-hunt/*.py:tools/scripts/wallet-hunt" \
  --extra "$W/exit-backtest/*.py:tools/scripts/exit-backtest" \
  --extra "$W/exit-replay/*.py:tools/scripts/exit-replay" \
  --extra "$W/exit-replay/control.json:tools/scripts/exit-replay/control.json" \
  --extra "$W/entry-learner-watch/check.py:tools/scripts/entry-learner-watch/check.py" \
  --extra "$W/devnet-demo-watch.py:tools/scripts/devnet-demo-watch.py" \
  --extra "$W/diag_chain_funnel.py:tools/scripts/diag_chain_funnel.py" \
  --extra "$W/lane-runners.py:tools/scripts/lane-runners.py" \
  --extra-skip-dir data \
  "$@"
