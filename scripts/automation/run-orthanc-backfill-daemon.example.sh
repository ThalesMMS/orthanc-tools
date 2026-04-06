#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ORTHANC_BACKFILL_ENV_FILE:-$SCRIPT_DIR/orthanc-backfill-daemon.env}"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

: "${REPO_ROOT:?Set REPO_ROOT in $ENV_FILE or the environment.}"
: "${BACKUP_DIR:?Set BACKUP_DIR in $ENV_FILE or the environment.}"
: "${STATE_DIR:?Set STATE_DIR in $ENV_FILE or the environment.}"
: "${ORTHANC_BASE_URL:?Set ORTHANC_BASE_URL in $ENV_FILE or the environment.}"
: "${ORTHANC_USER:?Set ORTHANC_USER in $ENV_FILE or the environment.}"
: "${ORTHANC_PASSWORD:?Set ORTHANC_PASSWORD in $ENV_FILE or the environment.}"
: "${REMOTE_AET:?Set REMOTE_AET in $ENV_FILE or the environment.}"
: "${REMOTE_HOST:?Set REMOTE_HOST in $ENV_FILE or the environment.}"
: "${REMOTE_PORT:?Set REMOTE_PORT in $ENV_FILE or the environment.}"
: "${START_DATE:?Set START_DATE in $ENV_FILE or the environment.}"

VENV_DIR="${VENV_DIR:-}"
CALLING_AET="${CALLING_AET:-ORTHANC}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-3600}"
LOG_DIR="${LOG_DIR:-$REPO_ROOT/logs}"
CLI_SCRIPT="$REPO_ROOT/scripts/workflows/orthanc-backfill-export-by-date.py"

if [[ -n "$VENV_DIR" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-$VENV_DIR/bin/python3}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

mkdir -p "$LOG_DIR" "$BACKUP_DIR" "$STATE_DIR"

echo "Starting example daemon. Edit $ENV_FILE or export variables before using it." >&2

while true; do
  end_date="$(date +%Y-%m-%d)"
  "$PYTHON_BIN" "$CLI_SCRIPT" \
    --start-date "$START_DATE" \
    --end-date "$end_date" \
    --remote-aet "$REMOTE_AET" \
    --remote-host "$REMOTE_HOST" \
    --remote-port "$REMOTE_PORT" \
    --base-url "$ORTHANC_BASE_URL" \
    --user "$ORTHANC_USER" \
    --password "$ORTHANC_PASSWORD" \
    --calling-aet "$CALLING_AET" \
    --backup-dir "$BACKUP_DIR" \
    --state-dir "$STATE_DIR" \
    >> "$LOG_DIR/backfill-daemon.log" 2>&1 || true
  sleep "$CHECK_INTERVAL_SECONDS"
done
