#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'USAGE'
Usage:
  ./orthanc-delete-all-studies.sh [options]

Delete all studies stored in Orthanc using the REST API.

Options:
  --base-url URL             Orthanc base URL, for example http://127.0.0.1:8042
  --user USER                Orthanc HTTP user
  --password PASSWORD        Orthanc HTTP password
  --config-dir DIR           Configuration directory. Default: /etc/orthanc
  --orthanc-config FILE      Explicit path to orthanc.json
  --credentials-config FILE  Explicit path to credentials.json
  --dry-run                  Only show how many studies would be deleted
  --yes                      Skip the interactive confirmation
  -h, --help                 Show this help text

Equivalent environment variables:
  ORTHANC_BASE_URL
  ORTHANC_ADMIN_USER
  ORTHANC_ADMIN_PASSWORD
  ORTHANC_CONFIG_DIR
  ORTHANC_MAIN_CONFIG_FILE
  ORTHANC_CREDENTIALS_CONFIG_FILE

Examples:
  sudo ./orthanc-delete-all-studies.sh --dry-run
  sudo ./orthanc-delete-all-studies.sh --yes
  ./orthanc-delete-all-studies.sh \
    --base-url http://127.0.0.1:8042 \
    --user admin \
    --password 'your-password' \
    --yes
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

need_cmd curl
need_cmd python3

DRY_RUN=false
ASSUME_YES=false
CONFIG_DIR="${ORTHANC_CONFIG_DIR:-/etc/orthanc}"
MAIN_CONFIG_FILE="${ORTHANC_MAIN_CONFIG_FILE:-}"
CREDENTIALS_CONFIG_FILE="${ORTHANC_CREDENTIALS_CONFIG_FILE:-}"
ORTHANC_BASE_URL="${ORTHANC_BASE_URL:-}"
ORTHANC_ADMIN_USER="${ORTHANC_ADMIN_USER:-}"
ORTHANC_ADMIN_PASSWORD="${ORTHANC_ADMIN_PASSWORD:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      [[ $# -ge 2 ]] || { echo "Missing value for --base-url" >&2; exit 1; }
      ORTHANC_BASE_URL="$2"
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || { echo "Missing value for --user" >&2; exit 1; }
      ORTHANC_ADMIN_USER="$2"
      shift 2
      ;;
    --password)
      [[ $# -ge 2 ]] || { echo "Missing value for --password" >&2; exit 1; }
      ORTHANC_ADMIN_PASSWORD="$2"
      shift 2
      ;;
    --config-dir)
      [[ $# -ge 2 ]] || { echo "Missing value for --config-dir" >&2; exit 1; }
      CONFIG_DIR="$2"
      shift 2
      ;;
    --orthanc-config)
      [[ $# -ge 2 ]] || { echo "Missing value for --orthanc-config" >&2; exit 1; }
      MAIN_CONFIG_FILE="$2"
      shift 2
      ;;
    --credentials-config)
      [[ $# -ge 2 ]] || { echo "Missing value for --credentials-config" >&2; exit 1; }
      CREDENTIALS_CONFIG_FILE="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --yes)
      ASSUME_YES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Invalid option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$MAIN_CONFIG_FILE" ]]; then
  MAIN_CONFIG_FILE="$CONFIG_DIR/orthanc.json"
fi
if [[ -z "$CREDENTIALS_CONFIG_FILE" ]]; then
  CREDENTIALS_CONFIG_FILE="$CONFIG_DIR/credentials.json"
fi

load_from_config() {
  local main_cfg="$1"
  local cred_cfg="$2"
  [[ -f "$main_cfg" && -f "$cred_cfg" ]] || return 1

  readarray -t CFG < <(python3 - "$main_cfg" "$cred_cfg" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    main_cfg = json.load(handle)
with open(sys.argv[2], "r", encoding="utf-8") as handle:
    cred_cfg = json.load(handle)

users = cred_cfg.get("RegisteredUsers", {})
if not isinstance(users, dict) or not users:
    raise SystemExit("RegisteredUsers is missing or empty in credentials.json.")

user = next(iter(users))
password = users[user]
if not isinstance(password, str):
    raise SystemExit("Invalid Orthanc password in credentials.json.")

port = main_cfg.get("HttpPort", 8042)
print(user)
print(password)
print(port)
PY
  )

  if [[ ${#CFG[@]} -lt 3 ]]; then
    echo "Could not parse the Orthanc configuration files." >&2
    exit 1
  fi

  if [[ -z "$ORTHANC_ADMIN_USER" ]]; then
    ORTHANC_ADMIN_USER="${CFG[0]}"
  fi
  if [[ -z "$ORTHANC_ADMIN_PASSWORD" ]]; then
    ORTHANC_ADMIN_PASSWORD="${CFG[1]}"
  fi
  if [[ -z "$ORTHANC_BASE_URL" ]]; then
    ORTHANC_BASE_URL="http://127.0.0.1:${CFG[2]}"
  fi
}

if [[ -z "$ORTHANC_ADMIN_USER" || -z "$ORTHANC_ADMIN_PASSWORD" || -z "$ORTHANC_BASE_URL" ]]; then
  load_from_config "$MAIN_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" || true
fi

ORTHANC_BASE_URL="${ORTHANC_BASE_URL:-http://127.0.0.1:8042}"

if [[ -z "$ORTHANC_ADMIN_USER" || -z "$ORTHANC_ADMIN_PASSWORD" ]]; then
  echo "Credentials not provided. Use --user/--password or export ORTHANC_ADMIN_USER and ORTHANC_ADMIN_PASSWORD." >&2
  exit 1
fi

api_get() {
  local path="$1"
  local response
  if ! response="$(curl -fsS --max-time 60 -u "${ORTHANC_ADMIN_USER}:${ORTHANC_ADMIN_PASSWORD}" \
    "${ORTHANC_BASE_URL}${path}")"; then
    echo "Failed to access ${ORTHANC_BASE_URL}${path}." >&2
    echo "Check whether Orthanc is running and whether the URL and credentials are correct." >&2
    exit 1
  fi

  printf '%s' "$response"
}

api_delete() {
  local path="$1"
  if ! curl -fsS --max-time 60 -X DELETE -u "${ORTHANC_ADMIN_USER}:${ORTHANC_ADMIN_PASSWORD}" \
    "${ORTHANC_BASE_URL}${path}" >/dev/null; then
    echo "Failed to delete resource ${ORTHANC_BASE_URL}${path}." >&2
    exit 1
  fi
}

parse_study_ids() {
  local json_payload="$1"
  if ! python3 -c '
import json
import sys

path = sys.argv[1]
payload = sys.stdin.read()
if not payload:
    raise SystemExit(f"Empty response from API {path}.")

try:
    data = json.loads(payload)
except json.JSONDecodeError as exc:
    preview = payload.strip().replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."
    raise SystemExit(
        f"Invalid response from API {path}: {exc}. Received content: {preview!r}"
    )

if not isinstance(data, list):
    raise SystemExit(f"Unexpected response from API {path}.")

for item in data:
    if isinstance(item, str):
        print(item)
  ' "/studies" <<<"$json_payload"
  then
    exit 1
  fi
}

parse_list_count() {
  local json_payload="$1"
  local path="$2"
  if ! python3 -c '
import json
import sys

path = sys.argv[1]
payload = sys.stdin.read()
if not payload:
    raise SystemExit(f"Empty response from API {path}.")

try:
    data = json.loads(payload)
except json.JSONDecodeError as exc:
    preview = payload.strip().replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."
    raise SystemExit(
        f"Invalid response from API {path}: {exc}. Received content: {preview!r}"
    )

if not isinstance(data, list):
    raise SystemExit(f"Unexpected response from API {path}.")

print(len(data))
  ' "$path" <<<"$json_payload"
  then
    exit 1
  fi
}

validate_system_endpoint() {
  local system_json
  if ! system_json="$(api_get "/system")"; then
    exit 1
  fi

  if ! python3 -c '
import json
import sys

base_url = sys.argv[1]
payload = sys.stdin.read()
if not payload:
    raise SystemExit(
        f"Empty response from API /system at {base_url}. "
        "That endpoint should return Orthanc JSON."
    )

try:
    data = json.loads(payload)
except json.JSONDecodeError as exc:
    preview = payload.strip().replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."
    raise SystemExit(
        f"Invalid response from API /system at {base_url}: {exc}. "
        f"Received content: {preview!r}"
    )

if not isinstance(data, dict):
    raise SystemExit(f"Unexpected response from API /system at {base_url}.")

name = data.get("Name")
version = data.get("Version")
if not name or not version:
    raise SystemExit(
        f"Incomplete response from API /system at {base_url}. "
        "Expected fields: Name and Version."
    )

print(f"Orthanc detected: {name} {version}")
  ' "$ORTHANC_BASE_URL" <<<"$system_json"
  then
    exit 1
  fi
}

validate_system_endpoint
study_json="$(api_get "/studies")"
study_ids_raw="$(parse_study_ids "$study_json")"

STUDY_IDS=()
if [[ -n "$study_ids_raw" ]]; then
  while IFS= read -r study_id; do
    [[ -n "$study_id" ]] && STUDY_IDS+=("$study_id")
  done <<<"$study_ids_raw"
fi

study_count="${#STUDY_IDS[@]}"

if [[ "$study_count" -eq 0 ]]; then
  echo "No studies found at ${ORTHANC_BASE_URL}."
  exit 0
fi

echo "Target Orthanc: ${ORTHANC_BASE_URL}"
echo "Studies found: ${study_count}"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run mode enabled. No studies were deleted."
  exit 0
fi

if [[ "$ASSUME_YES" != "true" ]]; then
  if [[ ! -t 0 ]]; then
    echo "Non-interactive session. Use --yes to confirm the mass deletion." >&2
    exit 1
  fi

  echo
  echo "WARNING: this operation will delete ALL studies from Orthanc."
  read -r -p "Type DELETE to continue: " confirmation
  if [[ "$confirmation" != "DELETE" ]]; then
    echo "Operation canceled."
    exit 1
  fi
fi

deleted=0
for study_id in "${STUDY_IDS[@]}"; do
  deleted=$((deleted + 1))
  echo "Deleting study ${deleted}/${study_count}: ${study_id}"
  api_delete "/studies/${study_id}"
done

remaining_json="$(api_get "/studies")"
remaining_count="$(parse_list_count "$remaining_json" "/studies")"

if [[ "$remaining_count" != "0" ]]; then
  echo "Deletion finished with pending items. Remaining studies: ${remaining_count}" >&2
  exit 1
fi

echo "Deletion finished. All studies were removed."
