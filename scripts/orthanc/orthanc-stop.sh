#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

WAIT_SECONDS="${ORTHANC_STOP_WAIT_SECONDS:-30}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root or with sudo." >&2
  exit 1
fi

systemctl stop orthanc

for _ in $(seq 1 "$WAIT_SECONDS"); do
  if ! systemctl is-active --quiet orthanc; then
    echo "Orthanc stopped."
    exit 0
  fi
  sleep 1
done

echo "Orthanc did not stop after ${WAIT_SECONDS}s." >&2
systemctl status orthanc --no-pager -l || true
exit 1
