#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

CONFIG_DIR="${ORTHANC_CONFIG_DIR:-/etc/orthanc}"
MAIN_CONFIG_FILE="${ORTHANC_MAIN_CONFIG_FILE:-$CONFIG_DIR/orthanc.json}"
CREDENTIALS_CONFIG_FILE="${ORTHANC_CREDENTIALS_CONFIG_FILE:-$CONFIG_DIR/credentials.json}"
POSTGRESQL_CONFIG_FILE="${ORTHANC_POSTGRESQL_CONFIG_FILE:-$CONFIG_DIR/postgresql.json}"
LEGACY_CONFIG_FILE="${ORTHANC_CONFIG_FILE:-}"
ORTHANC_MIN_VERSION="${ORTHANC_MIN_VERSION:-1.12.10}"
ORTHANC_REQUIRE_GETSCU="${ORTHANC_REQUIRE_GETSCU:-true}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root or with sudo." >&2
  exit 1
fi

bool_normalize() {
  local v="${1,,}"
  case "$v" in
    true|false) printf '%s' "$v" ;;
    1|yes|y|on) printf 'true' ;;
    0|no|n|off) printf 'false' ;;
    *) echo "Invalid boolean value: $1" >&2; exit 1 ;;
  esac
}

version_ge() {
  python3 - "$1" "$2" <<'PY'
import re
import sys

def parse(value):
    numbers = [int(part) for part in re.findall(r'\d+', value)]
    return tuple(numbers or [0])

sys.exit(0 if parse(sys.argv[1]) >= parse(sys.argv[2]) else 1)
PY
}

ORTHANC_REQUIRE_GETSCU="$(bool_normalize "$ORTHANC_REQUIRE_GETSCU")"

if [[ -n "$LEGACY_CONFIG_FILE" ]]; then
  if [[ ! -f "$LEGACY_CONFIG_FILE" ]]; then
    echo "Configuration file not found: $LEGACY_CONFIG_FILE" >&2
    exit 1
  fi

  readarray -t CFG < <(python3 - "$LEGACY_CONFIG_FILE" <<'PY'
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    cfg = json.load(f)
users = cfg.get('RegisteredUsers', {})
if not users:
    raise SystemExit('No user registered in RegisteredUsers.')
user, password = next(iter(users.items()))
print(user)
print(password)
print(cfg.get('HttpPort', 8042))
print(cfg.get('DicomPort', 4242))
print(cfg.get('Name', 'Orthanc'))
pg = cfg.get('PostgreSQL', {})
print(pg.get('Host', '127.0.0.1'))
print(pg.get('Port', 5432))
print(pg.get('Database', 'orthanc'))
print(pg.get('Username', 'orthanc'))
print(pg.get('Password', ''))
PY
  )
else
  for f in "$MAIN_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" "$POSTGRESQL_CONFIG_FILE"; do
    if [[ ! -f "$f" ]]; then
      echo "Configuration file not found: $f" >&2
      exit 1
    fi
  done

  readarray -t CFG < <(python3 - "$MAIN_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" "$POSTGRESQL_CONFIG_FILE" <<'PY'
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    cfg = json.load(f)
with open(sys.argv[2], 'r', encoding='utf-8') as f:
    credentials = json.load(f)
with open(sys.argv[3], 'r', encoding='utf-8') as f:
    pg_root = json.load(f)
users = credentials.get('RegisteredUsers', {})
if not users:
    raise SystemExit('No user registered in RegisteredUsers.')
user, password = next(iter(users.items()))
print(user)
print(password)
print(cfg.get('HttpPort', 8042))
print(cfg.get('DicomPort', 4242))
print(cfg.get('Name', 'Orthanc'))
pg = pg_root.get('PostgreSQL', {})
print(pg.get('Host', '127.0.0.1'))
print(pg.get('Port', 5432))
print(pg.get('Database', 'orthanc'))
print(pg.get('Username', 'orthanc'))
print(pg.get('Password', ''))
PY
  )
fi

ADMIN_USER="${CFG[0]}"
ADMIN_PASS="${CFG[1]}"
HTTP_PORT="${CFG[2]}"
DICOM_PORT="${CFG[3]}"
ORTHANC_NAME_EXPECTED="${CFG[4]}"
PG_HOST="${CFG[5]}"
PG_PORT="${CFG[6]}"
PG_DB="${CFG[7]}"
PG_USER="${CFG[8]}"
PG_PASS="${CFG[9]}"

ok() { echo "[OK] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || fail "curl is not installed."
command -v jq >/dev/null 2>&1 || fail "jq is not installed."
command -v nc >/dev/null 2>&1 || fail "netcat is not installed."
command -v python3 >/dev/null 2>&1 || fail "python3 is not installed."

if [[ "$ORTHANC_REQUIRE_GETSCU" == "true" ]]; then
  command -v getscu >/dev/null 2>&1 || fail "getscu is not installed. Install dcmtk."
  ok "getscu present"
fi

systemctl is-active --quiet postgresql || fail "PostgreSQL is not active."
ok "PostgreSQL active"

systemctl is-active --quiet orthanc || fail "Orthanc is not active."
ok "Orthanc active"

if command -v pg_isready >/dev/null 2>&1; then
  PGPASSWORD="$PG_PASS" pg_isready -q -h "$PG_HOST" -p "$PG_PORT" -d "$PG_DB" -U "$PG_USER" || fail "PostgreSQL did not respond to pg_isready."
  ok "PostgreSQL responded to pg_isready"
fi

HTTP_JSON="$(curl -fsS --max-time 10 -u "${ADMIN_USER}:${ADMIN_PASS}" "http://127.0.0.1:${HTTP_PORT}/system")" || fail "Failed GET /system from Orthanc."
NAME_FROM_API="$(printf '%s' "$HTTP_JSON" | jq -r '.Name // empty')"
VERSION_FROM_API="$(printf '%s' "$HTTP_JSON" | jq -r '.Version // empty')"
[[ -n "$NAME_FROM_API" ]] || fail "The /system response is missing the Name field."
[[ "$NAME_FROM_API" == "$ORTHANC_NAME_EXPECTED" ]] || fail "Expected Name '$ORTHANC_NAME_EXPECTED', got '$NAME_FROM_API'."
ok "REST API responded and Name matched"
[[ -n "$VERSION_FROM_API" ]] || fail "The /system response is missing the Version field."
version_ge "$VERSION_FROM_API" "$ORTHANC_MIN_VERSION" || fail "Orthanc version $VERSION_FROM_API is older than required $ORTHANC_MIN_VERSION."
ok "Orthanc version $VERSION_FROM_API meets minimum $ORTHANC_MIN_VERSION"

curl -fsS --max-time 10 -u "${ADMIN_USER}:${ADMIN_PASS}" "http://127.0.0.1:${HTTP_PORT}/tools/metrics-prometheus" >/dev/null || fail "Failed /tools/metrics-prometheus."
ok "Metrics endpoint responded"

nc -z 127.0.0.1 "$DICOM_PORT" >/dev/null 2>&1 || fail "DICOM port ${DICOM_PORT} is not accepting local TCP connections."
ok "DICOM port ${DICOM_PORT} open"

echo "Health check completed successfully."
