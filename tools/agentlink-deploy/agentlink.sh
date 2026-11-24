#!/bin/sh
# Run AgentLink with this VM's network configuration applied.
# Refreshes the proxy file from the current shell's fresh
# credential, then execs the venv binary. Use this wrapper
# for every agentlink command on this VM, e.g.:
#   ~/workspace/agentlink-deploy/agentlink.sh register --username @lean
#   ~/workspace/agentlink-deploy/agentlink.sh doctor --for-install
set -eu
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
sh "$DIR/refresh-proxy.sh"
exec "$HOME/.agentlink/venv/bin/agentlink" "$@"
