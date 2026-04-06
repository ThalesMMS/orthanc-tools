#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

HEALTHCHECK_CMD="${ORTHANC_HEALTHCHECK_CMD:-/usr/local/sbin/orthanc-healthcheck.sh}"
WAIT_SECONDS="${ORTHANC_RESTART_WAIT_SECONDS:-60}"
QUIET_WAIT_SECONDS="${ORTHANC_RESTART_QUIET_WAIT_SECONDS:-10}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root or with sudo." >&2
  exit 1
fi

systemctl restart orthanc

for attempt in $(seq 1 "$WAIT_SECONDS"); do
  if systemctl is-active --quiet orthanc; then
    if [[ "$attempt" -le "$QUIET_WAIT_SECONDS" ]]; then
      if "$HEALTHCHECK_CMD" >/dev/null 2>&1; then
        exec "$HEALTHCHECK_CMD"
      fi
    else
      if "$HEALTHCHECK_CMD"; then
        exit 0
      fi
    fi
  fi
  sleep 1
done

echo "Orthanc did not become active after ${WAIT_SECONDS}s." >&2
systemctl status orthanc --no-pager -l || true
exit 1
