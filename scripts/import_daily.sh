#!/usr/bin/env bash
# Cron-safe wrapper for importing every new snapshot pair from imports/.

set -Eeuo pipefail

# Cron commonly has a minimal PATH. Keep any caller additions while including
# the usual Docker installation locations.
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LOCK_FILE="${PROJECT_DIR}/.daily-import.lock"

timestamp() {
    date --iso-8601=seconds
}

log() {
    printf '%s %s\n' "$(timestamp)" "$*"
}

if ! command -v docker >/dev/null 2>&1; then
    log "ERROR: docker was not found in PATH"
    exit 127
fi

if ! command -v flock >/dev/null 2>&1; then
    log "ERROR: flock was not found in PATH"
    exit 127
fi

cd "${PROJECT_DIR}"

# Keep the descriptor open for the entire run. A second cron invocation exits
# harmlessly instead of running concurrent imports.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    log "SKIP: another daily import is already running"
    exit 0
fi

log "Starting daily snapshot scan"
if docker compose exec -T web python import_all.py /data; then
    log "Daily snapshot scan completed successfully"
else
    status=$?
    log "ERROR: daily snapshot scan failed with exit status ${status}"
    exit "${status}"
fi
