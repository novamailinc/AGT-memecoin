#!/bin/sh
# contained.sh CMD [ARGS...]
# Runs CMD in a fresh user+network+PID namespace: no interface but lo, no addresses, no routes,
# scratch HOME. The namespace is verified BEFORE CMD starts. If it cannot be created or verified,
# this exits nonzero (coverage UNQUALIFIED); there is no unwrapped fallback and no bypass switch.
[ "$#" -ge 1 ] || { echo "usage: contained.sh CMD [ARGS...]" >&2; exit 64; }
command -v unshare >/dev/null 2>&1 || { echo "CONTAINMENT UNAVAILABLE: unshare missing; coverage UNQUALIFIED" >&2; exit 90; }
SCRATCH_HOME=$(mktemp -d) || exit 91
trap 'rm -rf "$SCRATCH_HOME"' EXIT
unshare -rnpf --kill-child sh -c '
  ip link set lo up || exit 94
  [ "$(ip -o link | wc -l)" = 1 ] || exit 95
  [ -z "$(ip -o addr | grep -v " lo ")" ] || exit 96
  [ -z "$(ip -4 route)" ] || exit 97
  [ "$(tail -n +2 /proc/net/route | wc -l)" = 0 ] || exit 98
  [ -z "$(ip -6 route | grep -v " dev lo \|^unreachable\|^local\|^fe80\|^ff00")" ] || exit 99
  echo "contained: links=lo v4routes=0 v6routes=lo-only"
  H="$1"; shift
  export AGT_CONTAINED=1 HOME="$H"
  exec "$@"
' sh "$SCRATCH_HOME" "$@"
RC=$?
case "$RC" in 90|94|95|96|97|98|99)
  echo "CONTAINMENT UNAVAILABLE/UNVERIFIED (rc=$RC): coverage UNQUALIFIED" >&2;; esac
exit $RC
