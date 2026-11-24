#!/bin/sh
# Refresh AgentLink's proxy configuration from this VM's current
# egress-proxy credential, before every AgentLink run.
#
# VM network facts (this machine):
# - All egress goes through hatch-egress-proxy:3128, with a
#   credential that the runtime rotates and injects fresh into
#   each new shell's HTTPS_PROXY / https_proxy env.
# - Direct egress is blocked. TLS is MITM'd; the CA bundle is
#   /run/hatch/egress-tls/ca-bundle.pem, already in SSL_CERT_FILE
#   / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE in a normal shell.
# - AgentLink's proxycreds.py races proxy candidates harvested
#   from /proc, most of which are already stale (407) and one
#   of which intermittently returns a 405 from the login Edge
#   and stops the race. The current shell's proxy is last in
#   that race, so registration failed until it was pinned first.
#
# This script writes the CURRENT shell's proxy into the two
# files proxycreds reads first:
#   ~/workspace/agentlink-deploy/proxy-current.env (canonical, outranks all)
#   ~/.vm-agent/proxy.env
# It must run in the SAME shell invocation as the AgentLink
# command that follows it, because the credential rotates
# between shells. Never copy an old proxy-current.env into a
# new shell and expect it to work; re-run this script instead.
# The file holds a live proxy credential: mode 0600, never
# print it, never commit it, never paste it in chat.
set -eu
PROXY="${HTTPS_PROXY:-${https_proxy:-}}"
if [ -z "$PROXY" ]; then
  echo "refresh-proxy: no HTTPS_PROXY/https_proxy in this shell" >&2
  exit 1
fi
mkdir -p "$HOME/workspace/agentlink-deploy"
printf 'https_proxy=%s\nHTTPS_PROXY=%s\nhttp_proxy=%s\nHTTP_PROXY=%s\n' "$PROXY" "$PROXY" "$PROXY" "$PROXY" > "$HOME/workspace/agentlink-deploy/proxy-current.env"
printf 'https_proxy=%s\n' "$PROXY" > "$HOME/.vm-agent/proxy.env"
chmod 0600 "$HOME/workspace/agentlink-deploy/proxy-current.env" "$HOME/.vm-agent/proxy.env"
