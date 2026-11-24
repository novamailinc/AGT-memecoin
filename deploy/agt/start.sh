#!/usr/bin/env bash
# start.sh — AGT deployment start command for the paper bot.
#
# AGT runs this once under its process supervisor (bash -c, deployment
# work dir = HOME). It must be self-contained: the deployment operates
# independently once started; the development agent's computer does not
# need to remain online.
#
# Order:
#   1. Resolve paths (REPO = the deployed checkout, HOME = deployment
#      work dir, FOREX_AGENT_HOME = $HOME/.forex-agent).
#   2. Credential preflight — FAILS CLOSED (exit 1, clear message) when
#      a required credential env var is missing. Never a silent default.
#   3. Bridge env credentials into the runtime file paths that file-only
#      readers expect (0600, deployment-private, from env only).
#   4. Python environment validation (report only; never installs).
#   5. Database init + migrations.
#   6. Start the resident supervisor (background, owns all daemons),
#      the in-deployment scheduler (background, the cron replacements),
#      and the read-only status dashboard (foreground via wait — the
#      dashboard is the process AGT health-checks on the port).
#   7. Wait on the children; if any exits, exit non-zero so AGT
#      restarts the deployment.
#
# Modes (for local validation; not used by AGT):
#   start.sh --preflight-only   run steps 1-5, then exit 0.
#   start.sh --smoke-test       run steps 1-5, start the dashboard on
#                               an ephemeral port against a scratch
#                               home, fetch /health, run the scheduler
#                               --dry-run, exit 0. Never starts the
#                               supervisor or any trader.
set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DEPLOY_HOME="${HOME:-$REPO}"
FOREX_AGENT_HOME="${FOREX_AGENT_HOME:-$DEPLOY_HOME/.forex-agent}"
export FOREX_AGENT_HOME
PORT="${AGT_STATUS_PORT:-8765}"
AGT_PY="${AGT_PY:-python3}"

log() { printf '%s start.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

preflight() {
    mkdir -p "$FOREX_AGENT_HOME/log" "$FOREX_AGENT_HOME/run"
    log "repo=$REPO home=$FOREX_AGENT_HOME port=$PORT"
    # One durable storage path for EVERY process in the deployment
    # (2026-11-23 wiring fix, found by the independent validator):
    # storage.store's built-in default is repo-relative
    # (<repo>/storage/local.db) while db_init and the dashboard use
    # $FOREX_AGENT_HOME/storage/local.db, so without this export the
    # /halt latch, daemon Store writes and the initialized database
    # were three different files. Derived here, before any Python
    # child starts; an explicit operator override still wins.
    export FOREX_AGENT_STORAGE="${FOREX_AGENT_STORAGE:-$FOREX_AGENT_HOME/storage/local.db}"
    # Shared paper account (the owner-authorized $10 shared demo
    # account): without this, the grad/ceo/watcher/momentum lanes run
    # on private notional counters instead of the shared account.
    # Enable only: the ledger is created once at first use and is
    # NEVER reset by startup (paper_account refuses to overwrite an
    # existing ledger). Gate activation and thresholds stay
    # owner-owned - PAPER_ACCOUNT_GATES_ENABLED is untouched.
    export PAPER_ACCOUNT_ENABLED="${PAPER_ACCOUNT_ENABLED:-1}"
    "$AGT_PY" "$REPO/deploy/agt/credentials.py" --preflight || return 1
    # Deployment version record (Phase 13 finalize): the dashboard
    # reports exactly which commit is running. git may be absent in
    # a tarball deploy — then commit is null and the operator
    # records the HEAD by hand at deploy time; never guessed.
    COMMIT="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo "")"
    if [ -n "$COMMIT" ]; then
        COMMIT_SRC="git rev-parse HEAD"
    else
        COMMIT_SRC="no git checkout in this deploy (tarball); record main HEAD by hand"
    fi
    printf '{"branch":"main","commit":%s,"commit_source":"%s","generated_at_utc":"%s","repo":"https://github.com/novamailinc/AGT-memecoin"}\n' \
        "$( [ -n "$COMMIT" ] && printf '"%s"' "$COMMIT" || printf 'null' )" \
        "$COMMIT_SRC" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$FOREX_AGENT_HOME/agt_version.json"
    "$AGT_PY" "$REPO/deploy/agt/validate_env.py" > "$FOREX_AGENT_HOME/log/agt-env-report.json" || {
        log "FATAL: Python environment validation failed (see log/agt-env-report.json)"
        return 1
    }
    log "environment report: $(tail -n 1 "$FOREX_AGENT_HOME/log/agt-env-report.json")"
    "$AGT_PY" "$REPO/deploy/agt/db_init.py" --home "$FOREX_AGENT_HOME" || return 1
    return 0
}

smoke_test() {
    SMOKE_HOME="$(mktemp -d "${TMPDIR:-/tmp}/agt-smoke-home.XXXXXX")"
    export FOREX_AGENT_HOME="$SMOKE_HOME"
    export HOME="$SMOKE_HOME"
    log "smoke test home=$SMOKE_HOME"
    preflight || { rm -rf "$SMOKE_HOME"; return 1; }
    # Dashboard on an ephemeral port (0) is not addressable from the
    # shell, so use a fixed high port unlikely to collide locally.
    SMOKE_PORT="${AGT_SMOKE_PORT:-18765}"
    "$AGT_PY" "$REPO/deploy/agt/status_server.py" --port "$SMOKE_PORT" --home "$SMOKE_HOME" \
        > "$SMOKE_HOME/dashboard.log" 2>&1 &
    DASH_PID=$!
    trap 'kill $DASH_PID 2>/dev/null; rm -rf "$SMOKE_HOME"' EXIT
    ok=0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if "$AGT_PY" -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:$SMOKE_PORT/health', timeout=2)); print(d)" 2>/dev/null; then
            ok=1; break
        fi
        sleep 1
    done
    if [ "$ok" != "1" ]; then
        log "smoke test FAILED: dashboard did not answer on $SMOKE_PORT"
        cat "$SMOKE_HOME/dashboard.log"
        return 1
    fi
    "$AGT_PY" "$REPO/deploy/agt/scheduler.py" --dry-run > /dev/null || return 1
    log "smoke test OK: preflight + dashboard /health + scheduler --dry-run"
    return 0
}

case "${1:-}" in
    --preflight-only)
        preflight
        exit $?
        ;;
    --smoke-test)
        smoke_test
        exit $?
        ;;
    "")
        ;;
    *)
        echo "usage: start.sh [--preflight-only | --smoke-test]" >&2
        exit 2
        ;;
esac

preflight || exit 1

log "starting resident supervisor"
"$AGT_PY" "$REPO/scripts/supervisor" \
    > "$FOREX_AGENT_HOME/log/supervisor.log" 2>&1 &
SUP_PID=$!

log "starting in-deployment scheduler"
"$AGT_PY" "$REPO/deploy/agt/scheduler.py" --run \
    > "$FOREX_AGENT_HOME/log/agt-scheduler.log" 2>&1 &
SCHED_PID=$!

log "starting status dashboard on 127.0.0.1:$PORT"
"$AGT_PY" "$REPO/deploy/agt/status_server.py" --port "$PORT" --home "$FOREX_AGENT_HOME" \
    > "$FOREX_AGENT_HOME/log/agt-dashboard.log" 2>&1 &
DASH_PID=$!

cleanup() {
    kill "$SUP_PID" "$SCHED_PID" "$DASH_PID" 2>/dev/null
}
trap cleanup EXIT

# Wait for the first child to exit; a dead child means the deployment
# is degraded, so exit non-zero and let AGT restart us.
wait -n "$SUP_PID" "$SCHED_PID" "$DASH_PID"
rc=$?
log "child exited (rc=$rc); stopping deployment so AGT restarts it"
exit 1
