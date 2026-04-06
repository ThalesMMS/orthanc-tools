#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

trap 'echo "[ERROR] Failure at line ${LINENO}." >&2' ERR

usage() {
  cat <<'USAGE'
Usage:
  sudo ./purge-orthanc-native.sh

Optional variables:
  ORTHANC_DB_NAME=orthanc
  ORTHANC_DB_USER=orthanc
  ORTHANC_CONFIG_DIR=/etc/orthanc
  ORTHANC_STORAGE_ROOT=/var/lib/orthanc
  ORTHANC_LOG_DIR=/var/log/orthanc
  ORTHANC_HELPER_DIR=/usr/local/sbin
  ORTHANC_EXAMPLE_DIR=/root
  ORTHANC_SYSTEMD_OVERRIDE_DIR=/etc/systemd/system/orthanc.service.d
  ORTHANC_STORAGE_MOUNT_OVERRIDE_FILE=/etc/systemd/system/orthanc.service.d/storage-mount.conf
  ORTHANC_STANDALONE_LIST_PATH=/etc/apt/sources.list.d/orthanc.list
  ORTHANC_STANDALONE_KEYRING_PATH=/usr/share/keyrings/orthanc-archive-keyring.gpg
  PURGE_DATA=true
  PURGE_POSTGRESQL_PACKAGE=false
  PURGE_ORTHANC_REPOSITORY=false
USAGE
}

if [[ "${1:-}" =~ ^(-h|--help)$ ]]; then
  usage
  exit 0
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root (for example: sudo bash purge-orthanc-native.sh)." >&2
  exit 1
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

bool_normalize() {
  local v="${1,,}"
  case "$v" in
    true|false) printf '%s' "$v" ;;
    1|yes|y|on) printf 'true' ;;
    0|no|n|off) printf 'false' ;;
    *) echo "Invalid boolean value: $1" >&2; exit 1 ;;
  esac
}

run_as_postgres() {
  runuser -u postgres -- "$@"
}

need_cmd apt-get
need_cmd systemctl
need_cmd runuser
need_cmd rm
need_cmd grep

ORTHANC_DB_NAME="${ORTHANC_DB_NAME:-orthanc}"
ORTHANC_DB_USER="${ORTHANC_DB_USER:-orthanc}"
ORTHANC_CONFIG_DIR="${ORTHANC_CONFIG_DIR:-/etc/orthanc}"
ORTHANC_STORAGE_ROOT="${ORTHANC_STORAGE_ROOT:-/var/lib/orthanc}"
ORTHANC_LOG_DIR="${ORTHANC_LOG_DIR:-/var/log/orthanc}"
ORTHANC_HELPER_DIR="${ORTHANC_HELPER_DIR:-/usr/local/sbin}"
ORTHANC_EXAMPLE_DIR="${ORTHANC_EXAMPLE_DIR:-/root}"
ORTHANC_SYSTEMD_OVERRIDE_DIR="${ORTHANC_SYSTEMD_OVERRIDE_DIR:-/etc/systemd/system/orthanc.service.d}"
ORTHANC_STORAGE_MOUNT_OVERRIDE_FILE="${ORTHANC_STORAGE_MOUNT_OVERRIDE_FILE:-$ORTHANC_SYSTEMD_OVERRIDE_DIR/storage-mount.conf}"
ORTHANC_STANDALONE_LIST_PATH="${ORTHANC_STANDALONE_LIST_PATH:-/etc/apt/sources.list.d/orthanc.list}"
ORTHANC_STANDALONE_KEYRING_PATH="${ORTHANC_STANDALONE_KEYRING_PATH:-/usr/share/keyrings/orthanc-archive-keyring.gpg}"
PURGE_DATA="$(bool_normalize "${PURGE_DATA:-true}")"
PURGE_POSTGRESQL_PACKAGE="$(bool_normalize "${PURGE_POSTGRESQL_PACKAGE:-false}")"
PURGE_ORTHANC_REPOSITORY="$(bool_normalize "${PURGE_ORTHANC_REPOSITORY:-false}")"

echo "[1/6] Stopping and disabling services..."
systemctl stop orthanc >/dev/null 2>&1 || true
systemctl disable orthanc >/dev/null 2>&1 || true

echo "[2/6] Removing the Orthanc database and role from PostgreSQL..."
if id postgres >/dev/null 2>&1 && command -v psql >/dev/null 2>&1; then
  systemctl start postgresql >/dev/null 2>&1 || true
  if run_as_postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${ORTHANC_DB_NAME}'" | grep -q 1; then
    run_as_postgres psql -v ON_ERROR_STOP=1 -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${ORTHANC_DB_NAME}' AND pid <> pg_backend_pid();" >/dev/null
    run_as_postgres psql -v ON_ERROR_STOP=1 -c "DROP DATABASE \"${ORTHANC_DB_NAME}\";"
  fi
  if run_as_postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${ORTHANC_DB_USER}'" | grep -q 1; then
    run_as_postgres psql -v ON_ERROR_STOP=1 -c "DROP ROLE \"${ORTHANC_DB_USER}\";"
  fi
fi

echo "[3/6] Purging Orthanc packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get purge -y orthanc orthanc-dicomweb orthanc-gdcm orthanc-postgresql dcmtk || true
apt-get autoremove -y || true

if [[ "$PURGE_POSTGRESQL_PACKAGE" == "true" ]]; then
  echo "[4/6] Purging the PostgreSQL package..."
  apt-get purge -y postgresql postgresql-client postgresql-common || true
  apt-get autoremove -y || true
else
  echo "[4/6] Keeping the PostgreSQL package installed."
fi

echo "[5/6] Cleaning configuration, data, and helper scripts..."
rm -f \
  "$ORTHANC_HELPER_DIR/orthanc-healthcheck.sh" \
  "$ORTHANC_HELPER_DIR/orthanc-start.sh" \
  "$ORTHANC_HELPER_DIR/orthanc-stop.sh" \
  "$ORTHANC_HELPER_DIR/orthanc-restart.sh"
rm -f "$ORTHANC_EXAMPLE_DIR/orthanc-modalities.example.json"
rm -f "$ORTHANC_STORAGE_MOUNT_OVERRIDE_FILE"
if [[ -d "$ORTHANC_SYSTEMD_OVERRIDE_DIR" ]] && [[ -z "$(find "$ORTHANC_SYSTEMD_OVERRIDE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  rmdir "$ORTHANC_SYSTEMD_OVERRIDE_DIR" || true
fi
rm -rf "$ORTHANC_CONFIG_DIR"

if [[ "$PURGE_DATA" == "true" ]]; then
  rm -rf "$ORTHANC_STORAGE_ROOT" "$ORTHANC_LOG_DIR"
fi

if [[ "$PURGE_ORTHANC_REPOSITORY" == "true" ]]; then
  rm -f "$ORTHANC_STANDALONE_LIST_PATH" "$ORTHANC_STANDALONE_KEYRING_PATH"
  apt-get update || true
fi

systemctl daemon-reload || true

echo "[6/6] Final state..."
systemctl is-active orthanc >/dev/null 2>&1 && echo "Orthanc still active" || echo "Orthanc removed/stopped"
echo "Purge completed."
