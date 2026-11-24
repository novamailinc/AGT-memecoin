#!/usr/bin/env bash
# healthcheck.sh — operator health check for the AGT deployment.
#
# Usage: bash deploy/agt/healthcheck.sh [--home DIR]
# Exit 0 when every check passes, 1 otherwise. One line per check.
# Read-only: never starts, stops, or edits anything.
#
# Checks (each is a fact from live state, never an assumption):
#   1. supervisor_state.json fresh (< 120s) — the resident supervisor
#      is ticking.
#   2. run/heartbeat.json fresh (< 600s) — the watchdog loop is firing.
#   3. status dashboard answers /health on the deployment port.
#   4. killswitch/state.json parses.
#   5. local.db exists and its expected tables are present.
#   6. No active Helius 429 spike (helius_429_alert.json).
#   7. Latest backup younger than 2h (backup loop is firing).
#   8. Disk free on the state volume above 2 GB.
set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOME_DIR="${FOREX_AGENT_HOME:-${HOME:-$REPO}/.forex-agent}"
PORT="${AGT_STATUS_PORT:-8765}"
FAIL=0

if [ "${1:-}" = "--home" ] && [ -n "${2:-}" ]; then
    HOME_DIR="$2"
fi

check() {
    # check <name> <ok: 0|1> <detail>
    if [ "$2" = "0" ]; then
        printf 'OK   %s: %s\n' "$1" "$3"
    else
        printf 'FAIL %s: %s\n' "$1" "$3"
        FAIL=1
    fi
}

fresh() {
    # fresh <path> <max_age_s> -> 0 when the file exists and is newer
    [ -f "$1" ] || return 1
    age=$(( $(date +%s) - $(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1") ))
    [ "$age" -lt "$2" ]
}

# 1. Supervisor ticking.
if fresh "$HOME_DIR/supervisor_state.json" 120; then
    check "supervisor" 0 "state fresh"
else
    check "supervisor" 1 "supervisor_state.json missing or older than 120s"
fi

# 2. Watchdog heartbeat.
if fresh "$HOME_DIR/run/heartbeat.json" 600; then
    check "watchdog-heartbeat" 0 "heartbeat fresh"
else
    check "watchdog-heartbeat" 1 "run/heartbeat.json missing or older than 600s"
fi

# 3. Dashboard answers.
if python3 -c "import json,sys,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=5)); sys.exit(0 if d.get('status') in ('ok','degraded') else 1)" 2>/dev/null; then
    check "dashboard" 0 "GET /health answers on 127.0.0.1:$PORT"
else
    check "dashboard" 1 "no /health answer on 127.0.0.1:$PORT"
fi

# 4. Killswitch state parses.
if python3 -c "import json; d=json.load(open('$HOME_DIR/killswitch/state.json')); assert 'lanes' in d" 2>/dev/null; then
    halted=$(python3 -c "import json; d=json.load(open('$HOME_DIR/killswitch/state.json')); print(','.join(k for k,v in d['lanes'].items() if v.get('halted')) or 'none')" 2>/dev/null)
    check "killswitch" 0 "state parses; halted lanes: $halted"
else
    check "killswitch" 1 "killswitch/state.json missing or unparsable"
fi

# 5. Database present with expected tables.
if python3 "$REPO/deploy/agt/db_init.py" --home "$HOME_DIR" --check >/dev/null 2>&1; then
    check "database" 0 "local.db present with expected tables"
else
    check "database" 1 "local.db missing or tables incomplete"
fi

# 6. No active 429 spike.
if [ -f "$HOME_DIR/helius_429_alert.json" ]; then
    spike=$(python3 -c "import json; print(json.load(open('$HOME_DIR/helius_429_alert.json')).get('active', 'unknown'))" 2>/dev/null || echo unknown)
    if [ "$spike" = "True" ] || [ "$spike" = "true" ]; then
        check "helius-429" 1 "429 spike ACTIVE (see helius_429_alert.json)"
    else
        check "helius-429" 0 "no active spike (alert file says active=$spike)"
    fi
else
    check "helius-429" 0 "no alert file (no spike recorded)"
fi

# 7. Backup freshness.
latest=$(ls -t "$HOME_DIR/backups"/forex-backup-*.tar.gz 2>/dev/null | head -n 1)
if [ -n "$latest" ] && fresh "$latest" 7200; then
    check "backup" 0 "latest backup younger than 2h: $(basename "$latest")"
else
    check "backup" 1 "no backup younger than 2h (latest: ${latest:-none})"
fi

# 8b. Latest backup verified (backup_verify.py runs right after the
# hourly backup step and writes backups/verify-latest.json).
if [ -f "$HOME_DIR/backups/verify-latest.json" ]; then
    verdict=$(python3 -c "import json; print(json.load(open('$HOME_DIR/backups/verify-latest.json')).get('passed'))" 2>/dev/null || echo unknown)
    if [ "$verdict" = "True" ]; then
        check "backup-verify" 0 "latest archive verified (checksum, no credential material, ledgers parse, DB integrity)"
    else
        check "backup-verify" 1 "latest backup verify report says passed=$verdict — inspect backups/verify-latest.json before trusting the archive"
    fi
else
    check "backup-verify" 1 "no backups/verify-latest.json yet (hourly backup+verify has not run once)"
fi

# 8. Disk space.
free_kb=$(df -k "$HOME_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$free_kb" ] && [ "$free_kb" -gt 2097152 ]; then
    check "disk" 0 "$((free_kb / 1048576)) GB free on the state volume"
else
    check "disk" 1 "less than 2 GB free on the state volume (free: ${free_kb:-unknown} KB)"
fi

if [ "$FAIL" = "0" ]; then
    echo "healthcheck: ALL OK"
else
    echo "healthcheck: FAILURES PRESENT (see FAIL lines above)"
fi
exit "$FAIL"
