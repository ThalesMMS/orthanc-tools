#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

trap 'echo "[ERROR] Failure at line ${LINENO}." >&2' ERR

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
SUPPORTED_UBUNTU_VERSION="24.04"

usage() {
  cat <<'USAGE'
Usage:
  sudo ORTHANC_ADMIN_PASSWORD='http-password' ORTHANC_DB_PASSWORD='db-password' ./install-orthanc-native.sh

Optional variables:
  ORTHANC_PACKAGE_SOURCE=standalone
  ORTHANC_MIN_VERSION=1.12.10
  ORTHANC_NAME=Orthanc-PACS-main
  ORTHANC_ADMIN_USER=admin
  ORTHANC_DB_NAME=orthanc
  ORTHANC_DB_USER=orthanc
  ORTHANC_AET=ORTHANC
  ORTHANC_HTTP_PORT=8042
  ORTHANC_DICOM_PORT=4242
  ORTHANC_STORAGE_DIR=/var/lib/orthanc/storage
  ORTHANC_TMP_DIR=/var/lib/orthanc/tmp
  ORTHANC_REMOTE_ACCESS_ALLOWED=true
  ORTHANC_STORAGE_COMPRESSION=false
  ORTHANC_DICOM_CHECK_MODALITY_HOST=false
  ORTHANC_LIMIT_FIND_RESULTS=100
  ORTHANC_LIMIT_FIND_INSTANCES=100
  ORTHANC_STORAGE_MOUNTPOINT=
  ORTHANC_REQUIRE_STORAGE_MOUNT=false
  ORTHANC_MODALITIES_FILE=/root/modalities.json
  ORTHANC_ALLOW_UNSUPPORTED=false

ORTHANC_MODALITIES_FILE format (JSON):
{
  "CT01": {
    "AET": "CT01",
    "Host": "192.0.2.50",
    "Port": 104,
    "AllowEcho": true,
    "AllowFind": false,
    "AllowMove": false,
    "AllowGet": false,
    "AllowStore": true
  }
}
USAGE
}

if [[ "${1:-}" =~ ^(-h|--help)$ ]]; then
  usage
  exit 0
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root (for example: sudo bash install-orthanc-native.sh)." >&2
  exit 1
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

need_cmd apt-get
need_cmd systemctl
need_cmd python3
need_cmd sed
need_cmd grep
need_cmd install
need_cmd cp
need_cmd rm
need_cmd runuser
need_cmd findmnt

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

need_file() {
  local path="$1"
  [[ -f "$path" ]] || {
    echo "Required file not found: $path" >&2
    exit 1
  }
}

require_absolute_path() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" || "${value:0:1}" != "/" ]]; then
    echo "$name must be an absolute path." >&2
    exit 1
  fi
}

validate_storage_mountpoint() {
  local mountpoint_path="$1"
  local storage_path="$2"
  local storage_fstype

  require_absolute_path "ORTHANC_STORAGE_MOUNTPOINT" "$mountpoint_path"

  if [[ ! -d "$mountpoint_path" ]]; then
    echo "ORTHANC_STORAGE_MOUNTPOINT does not exist or is not a directory: $mountpoint_path" >&2
    exit 1
  fi

  python3 - "$mountpoint_path" "$storage_path" <<'PY'
import os
import sys

mountpoint_path = os.path.realpath(sys.argv[1])
storage_path = os.path.realpath(sys.argv[2])

if not os.path.ismount(mountpoint_path):
    raise SystemExit(
        f"ORTHANC_STORAGE_MOUNTPOINT is not currently mounted: {mountpoint_path}. "
        "Refusing to install Orthanc storage onto the root filesystem fallback."
    )

try:
    common = os.path.commonpath([mountpoint_path, storage_path])
except ValueError as exc:
    raise SystemExit(str(exc))

if common != mountpoint_path:
    raise SystemExit(
        "ORTHANC_STORAGE_DIR must live inside ORTHANC_STORAGE_MOUNTPOINT. "
        f"Got storage={storage_path} mountpoint={mountpoint_path}"
    )
PY

  storage_fstype="$(findmnt -n -o FSTYPE --target "$mountpoint_path" || true)"
  case "$storage_fstype" in
    exfat)
      echo "ORTHANC_STORAGE_MOUNTPOINT uses exfat: $mountpoint_path" >&2
      echo "Use ext4 or another Linux-native filesystem for Orthanc storage." >&2
      exit 1
      ;;
  esac
}

prompt_secret() {
  local var_name="$1"
  local prompt="$2"
  if [[ -z "${!var_name:-}" ]]; then
    read -r -s -p "$prompt: " "$var_name"
    echo
    if [[ -z "${!var_name}" ]]; then
      echo "Required value not provided: $var_name" >&2
      exit 1
    fi
    export "$var_name"
  fi
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

safe_ident() {
  local name="$1"
  [[ "$name" =~ ^[a-z_][a-z0-9_]*$ ]]
}

sql_escape_literal() {
  printf '%s' "$1" | sed "s/'/''/g"
}

run_as_postgres() {
  runuser -u postgres -- "$@"
}

require_supported_os() {
  . /etc/os-release

  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "$SUPPORTED_UBUNTU_VERSION" ]]; then
    echo "This script was validated for Ubuntu ${SUPPORTED_UBUNTU_VERSION}. Detected: ${PRETTY_NAME:-unknown}." >&2
    echo "If you still want to force it, export ORTHANC_ALLOW_UNSUPPORTED=true." >&2
    exit 1
  fi
}

setup_orthanc_repository() {
  local repo_url="$1"
  local key_url="$2"
  local keyring_path="$3"
  local list_path="$4"

  . /etc/os-release
  if [[ -z "${VERSION_CODENAME:-}" ]]; then
    echo "Unable to determine Ubuntu codename from /etc/os-release." >&2
    exit 1
  fi

  apt-get install -y ca-certificates curl gnupg
  install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d

  local tmp_key
  local tmp_keyring
  tmp_key="$(mktemp)"
  tmp_keyring="$(mktemp)"
  curl -fsSL "$key_url" -o "$tmp_key"
  gpg --dearmor <"$tmp_key" >"$tmp_keyring"
  install -m 0644 "$tmp_keyring" "$keyring_path"
  printf 'deb [signed-by=%s] %s %s main\n' "$keyring_path" "$repo_url" "$VERSION_CODENAME" >"$list_path"
  rm -f "$tmp_key" "$tmp_keyring"
}

check_orthanc_min_version() {
  local orthanc_bin="/usr/sbin/Orthanc"
  local installed_version

  if [[ ! -x "$orthanc_bin" ]]; then
    orthanc_bin="$(command -v Orthanc || true)"
  fi
  if [[ -z "$orthanc_bin" ]]; then
    echo "Orthanc binary not found after package installation." >&2
    exit 1
  fi

  installed_version="$("$orthanc_bin" --version | awk 'NR==1 {print $2}')"
  if [[ -z "$installed_version" ]]; then
    echo "Unable to determine the installed Orthanc version." >&2
    exit 1
  fi
  if ! version_ge "$installed_version" "$ORTHANC_MIN_VERSION"; then
    echo "Installed Orthanc $installed_version is older than the required $ORTHANC_MIN_VERSION." >&2
    echo "Use the official standalone repository or lower ORTHANC_MIN_VERSION explicitly if you really want an older build." >&2
    exit 1
  fi
}

prompt_secret ORTHANC_ADMIN_PASSWORD "Orthanc HTTP password"
prompt_secret ORTHANC_DB_PASSWORD "Orthanc PostgreSQL user password"

ORTHANC_PACKAGE_SOURCE="${ORTHANC_PACKAGE_SOURCE:-standalone}"
ORTHANC_MIN_VERSION="${ORTHANC_MIN_VERSION:-1.12.10}"
ORTHANC_NAME="${ORTHANC_NAME:-Orthanc-PACS-main}"
ORTHANC_ADMIN_USER="${ORTHANC_ADMIN_USER:-admin}"
ORTHANC_DB_NAME="${ORTHANC_DB_NAME:-orthanc}"
ORTHANC_DB_USER="${ORTHANC_DB_USER:-orthanc}"
ORTHANC_AET="${ORTHANC_AET:-ORTHANC}"
ORTHANC_HTTP_PORT="${ORTHANC_HTTP_PORT:-8042}"
ORTHANC_DICOM_PORT="${ORTHANC_DICOM_PORT:-4242}"
ORTHANC_STORAGE_DIR="${ORTHANC_STORAGE_DIR:-/var/lib/orthanc/storage}"
ORTHANC_TMP_DIR="${ORTHANC_TMP_DIR:-/var/lib/orthanc/tmp}"
ORTHANC_REMOTE_ACCESS_ALLOWED="$(bool_normalize "${ORTHANC_REMOTE_ACCESS_ALLOWED:-true}")"
ORTHANC_STORAGE_COMPRESSION="$(bool_normalize "${ORTHANC_STORAGE_COMPRESSION:-false}")"
ORTHANC_DICOM_CHECK_MODALITY_HOST="$(bool_normalize "${ORTHANC_DICOM_CHECK_MODALITY_HOST:-false}")"
ORTHANC_LIMIT_FIND_RESULTS="${ORTHANC_LIMIT_FIND_RESULTS:-100}"
ORTHANC_LIMIT_FIND_INSTANCES="${ORTHANC_LIMIT_FIND_INSTANCES:-100}"
ORTHANC_STORAGE_MOUNTPOINT="${ORTHANC_STORAGE_MOUNTPOINT:-}"
ORTHANC_REQUIRE_STORAGE_MOUNT="$(bool_normalize "${ORTHANC_REQUIRE_STORAGE_MOUNT:-false}")"
ORTHANC_MODALITIES_FILE="${ORTHANC_MODALITIES_FILE:-}"
ORTHANC_ALLOW_UNSUPPORTED="$(bool_normalize "${ORTHANC_ALLOW_UNSUPPORTED:-false}")"
ORTHANC_CONFIG_DIR="${ORTHANC_CONFIG_DIR:-/etc/orthanc}"
ORTHANC_HELPER_DIR="${ORTHANC_HELPER_DIR:-/usr/local/sbin}"
ORTHANC_EXAMPLE_DIR="${ORTHANC_EXAMPLE_DIR:-/root}"
ORTHANC_STANDALONE_REPOSITORY_URL="${ORTHANC_STANDALONE_REPOSITORY_URL:-https://orthanc.uclouvain.be/debian}"
ORTHANC_STANDALONE_KEY_URL="${ORTHANC_STANDALONE_KEY_URL:-https://orthanc.uclouvain.be/debian/archive.key}"
ORTHANC_STANDALONE_KEYRING_PATH="${ORTHANC_STANDALONE_KEYRING_PATH:-/usr/share/keyrings/orthanc-archive-keyring.gpg}"
ORTHANC_STANDALONE_LIST_PATH="${ORTHANC_STANDALONE_LIST_PATH:-/etc/apt/sources.list.d/orthanc.list}"
ORTHANC_SYSTEMD_OVERRIDE_DIR="${ORTHANC_SYSTEMD_OVERRIDE_DIR:-/etc/systemd/system/orthanc.service.d}"
ORTHANC_STORAGE_MOUNT_OVERRIDE_FILE="${ORTHANC_STORAGE_MOUNT_OVERRIDE_FILE:-$ORTHANC_SYSTEMD_OVERRIDE_DIR/storage-mount.conf}"

ORTHANC_CONFIG_FILE="$ORTHANC_CONFIG_DIR/orthanc.json"
CREDENTIALS_CONFIG_FILE="$ORTHANC_CONFIG_DIR/credentials.json"
POSTGRESQL_CONFIG_FILE="$ORTHANC_CONFIG_DIR/postgresql.json"
DICOMWEB_CONFIG_FILE="$ORTHANC_CONFIG_DIR/dicomweb.json"
LEGACY_CONFIG_FILE="$ORTHANC_CONFIG_DIR/99-local.json"
INSTALL_HEALTHCHECK_PATH="$ORTHANC_HELPER_DIR/orthanc-healthcheck.sh"
INSTALL_START_PATH="$ORTHANC_HELPER_DIR/orthanc-start.sh"
INSTALL_STOP_PATH="$ORTHANC_HELPER_DIR/orthanc-stop.sh"
INSTALL_RESTART_PATH="$ORTHANC_HELPER_DIR/orthanc-restart.sh"
INSTALL_MODALITIES_EXAMPLE_PATH="$ORTHANC_EXAMPLE_DIR/orthanc-modalities.example.json"
BACKUP_SUFFIX="$(date +%Y%m%d-%H%M%S)"

LOCAL_HEALTHCHECK_SCRIPT="$REPO_ROOT/scripts/orthanc/orthanc-healthcheck.sh"
LOCAL_START_SCRIPT="$REPO_ROOT/scripts/orthanc/orthanc-start.sh"
LOCAL_STOP_SCRIPT="$REPO_ROOT/scripts/orthanc/orthanc-stop.sh"
LOCAL_RESTART_SCRIPT="$REPO_ROOT/scripts/orthanc/orthanc-restart.sh"
LOCAL_MODALITIES_EXAMPLE="$REPO_ROOT/deploy/native/examples/orthanc-modalities.example.json"

need_file "$LOCAL_HEALTHCHECK_SCRIPT"
need_file "$LOCAL_START_SCRIPT"
need_file "$LOCAL_STOP_SCRIPT"
need_file "$LOCAL_RESTART_SCRIPT"
need_file "$LOCAL_MODALITIES_EXAMPLE"

if [[ "$ORTHANC_ALLOW_UNSUPPORTED" != "true" ]]; then
  require_supported_os
fi

if ! safe_ident "$ORTHANC_DB_USER"; then
  echo "ORTHANC_DB_USER must contain only [a-z0-9_] and start with a letter or _" >&2
  exit 1
fi

if ! safe_ident "$ORTHANC_DB_NAME"; then
  echo "ORTHANC_DB_NAME must contain only [a-z0-9_] and start with a letter or _" >&2
  exit 1
fi

if ! [[ "$ORTHANC_HTTP_PORT" =~ ^[0-9]+$ && "$ORTHANC_DICOM_PORT" =~ ^[0-9]+$ ]]; then
  echo "ORTHANC_HTTP_PORT and ORTHANC_DICOM_PORT must be numeric." >&2
  exit 1
fi

if ! [[ "$ORTHANC_LIMIT_FIND_RESULTS" =~ ^[0-9]+$ && "$ORTHANC_LIMIT_FIND_INSTANCES" =~ ^[0-9]+$ ]]; then
  echo "ORTHANC_LIMIT_FIND_RESULTS and ORTHANC_LIMIT_FIND_INSTANCES must be numeric." >&2
  exit 1
fi

require_absolute_path "ORTHANC_STORAGE_DIR" "$ORTHANC_STORAGE_DIR"
require_absolute_path "ORTHANC_TMP_DIR" "$ORTHANC_TMP_DIR"

if [[ "$ORTHANC_REQUIRE_STORAGE_MOUNT" == "true" && -z "$ORTHANC_STORAGE_MOUNTPOINT" ]]; then
  echo "ORTHANC_REQUIRE_STORAGE_MOUNT=true requires ORTHANC_STORAGE_MOUNTPOINT to be set." >&2
  exit 1
fi

if [[ -n "$ORTHANC_STORAGE_MOUNTPOINT" ]]; then
  validate_storage_mountpoint "$ORTHANC_STORAGE_MOUNTPOINT" "$ORTHANC_STORAGE_DIR"
fi

case "$ORTHANC_PACKAGE_SOURCE" in
  standalone|ubuntu)
    ;;
  *)
    echo "ORTHANC_PACKAGE_SOURCE must be either 'standalone' or 'ubuntu'." >&2
    exit 1
    ;;
esac

if [[ -n "$ORTHANC_MODALITIES_FILE" ]]; then
  if [[ ! -f "$ORTHANC_MODALITIES_FILE" ]]; then
    echo "Modalities file not found: $ORTHANC_MODALITIES_FILE" >&2
    exit 1
  fi
  ORTHANC_DICOM_CHECK_MODALITY_HOST="$ORTHANC_DICOM_CHECK_MODALITY_HOST" python3 - "$ORTHANC_MODALITIES_FILE" <<'PY'
import json
import os
import sys
path = sys.argv[1]
host_check_enabled = os.environ.get('ORTHANC_DICOM_CHECK_MODALITY_HOST', 'false').lower() == 'true'

def reject_duplicates(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise SystemExit(f'ORTHANC_MODALITIES_FILE contains a duplicate key: {key!r}.')
        data[key] = value
    return data

with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f, object_pairs_hook=reject_duplicates)
if not isinstance(data, dict):
    raise SystemExit('ORTHANC_MODALITIES_FILE must contain a JSON object.')
aet_to_names = {}
for name, cfg in data.items():
    if not isinstance(cfg, dict):
        raise SystemExit(f'Modality {name!r} must be a JSON object.')
    aet = cfg.get('AET')
    if not isinstance(aet, str) or not aet.strip():
        raise SystemExit(f'Invalid AET in {name!r}.')
    if len(aet) > 16:
        raise SystemExit(f'AET in {name!r} exceeds 16 characters.')
    host = cfg.get('Host')
    if not isinstance(host, str) or not host.strip():
        raise SystemExit(f'Invalid Host in {name!r}.')
    port = cfg.get('Port')
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise SystemExit(f'Invalid Port in {name!r}.')
    for flag in ('AllowEcho', 'AllowFind', 'AllowMove', 'AllowGet', 'AllowStore'):
        if flag in cfg and not isinstance(cfg[flag], bool):
            raise SystemExit(f'{flag} must be boolean in {name!r}.')
    aet_to_names.setdefault(aet, []).append(name)

if host_check_enabled:
    duplicate_aets = {aet: names for aet, names in aet_to_names.items() if len(names) > 1}
    if duplicate_aets:
        rendered = ', '.join(f"{aet}: {', '.join(names)}" for aet, names in sorted(duplicate_aets.items()))
        raise SystemExit(
            'ORTHANC_MODALITIES_FILE contains repeated remote AET values while '
            'ORTHANC_DICOM_CHECK_MODALITY_HOST=true. This combination is brittle with '
            f'duplicate peers: {rendered}. Use unique remote AETs or keep '
            'ORTHANC_DICOM_CHECK_MODALITY_HOST=false.'
        )
PY
fi

echo "[1/8] Updating APT indexes and installing packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
if [[ "$ORTHANC_PACKAGE_SOURCE" == "standalone" ]]; then
  setup_orthanc_repository \
    "$ORTHANC_STANDALONE_REPOSITORY_URL" \
    "$ORTHANC_STANDALONE_KEY_URL" \
    "$ORTHANC_STANDALONE_KEYRING_PATH" \
    "$ORTHANC_STANDALONE_LIST_PATH"
  apt-get update
fi
apt-get install -y \
  orthanc orthanc-dicomweb orthanc-gdcm orthanc-postgresql \
  dcmtk postgresql curl jq netcat-openbsd ca-certificates
check_orthanc_min_version

echo "[2/8] Enabling PostgreSQL at boot..."
systemctl enable --now postgresql

echo "[3/8] Creating data directories..."
install -d -m 0750 -o orthanc -g orthanc "$ORTHANC_STORAGE_DIR"
install -d -m 0750 -o orthanc -g orthanc "$ORTHANC_TMP_DIR"

echo "[4/8] Stopping Orthanc before rewriting the configuration..."
systemctl stop orthanc >/dev/null 2>&1 || true

echo "[5/8] Creating/updating the PostgreSQL role and database..."
DB_PASSWORD_SQL="$(sql_escape_literal "$ORTHANC_DB_PASSWORD")"
if ! run_as_postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${ORTHANC_DB_USER}'" | grep -q 1; then
  run_as_postgres createuser --login "$ORTHANC_DB_USER"
fi
run_as_postgres psql -v ON_ERROR_STOP=1 -c "ALTER USER \"${ORTHANC_DB_USER}\" WITH PASSWORD '${DB_PASSWORD_SQL}';"
if ! run_as_postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${ORTHANC_DB_NAME}'" | grep -q 1; then
  run_as_postgres createdb -O "$ORTHANC_DB_USER" "$ORTHANC_DB_NAME"
fi

echo "[6/8] Writing Orthanc configuration files..."
install -d -m 0755 "$ORTHANC_CONFIG_DIR"
install -d -m 0755 "$ORTHANC_HELPER_DIR"
install -d -m 0755 "$ORTHANC_EXAMPLE_DIR"

for f in "$ORTHANC_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" "$POSTGRESQL_CONFIG_FILE" "$DICOMWEB_CONFIG_FILE" "$LEGACY_CONFIG_FILE"; do
  if [[ -f "$f" ]]; then
    cp -a "$f" "${f}.bak.${BACKUP_SUFFIX}"
  fi
done

export ORTHANC_NAME ORTHANC_ADMIN_USER ORTHANC_ADMIN_PASSWORD ORTHANC_AET
export ORTHANC_HTTP_PORT ORTHANC_DICOM_PORT ORTHANC_STORAGE_DIR ORTHANC_TMP_DIR
export ORTHANC_REMOTE_ACCESS_ALLOWED ORTHANC_STORAGE_COMPRESSION ORTHANC_DICOM_CHECK_MODALITY_HOST
export ORTHANC_LIMIT_FIND_RESULTS ORTHANC_LIMIT_FIND_INSTANCES
export ORTHANC_DB_NAME ORTHANC_DB_USER ORTHANC_DB_PASSWORD ORTHANC_MODALITIES_FILE

python3 - "$ORTHANC_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" "$POSTGRESQL_CONFIG_FILE" "$DICOMWEB_CONFIG_FILE" <<'PY'
import json, os, sys

def env_bool(name, default=False):
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ('1', 'true', 'yes', 'on', 'y')

def reject_duplicates(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise SystemExit(f'ORTHANC_MODALITIES_FILE contains a duplicate key: {key!r}.')
        data[key] = value
    return data

def load_modalities():
    path = os.getenv('ORTHANC_MODALITIES_FILE', '').strip()
    if not path:
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f, object_pairs_hook=reject_duplicates)
    if not isinstance(data, dict):
        raise SystemExit('ORTHANC_MODALITIES_FILE must contain a JSON object.')
    return data

orthanc_config_path, credentials_config_path, postgresql_config_path, dicomweb_config_path = sys.argv[1:5]
modalities = load_modalities()

orthanc_cfg = {
    'Name': os.environ['ORTHANC_NAME'],
    'StorageDirectory': os.environ['ORTHANC_STORAGE_DIR'],
    'TemporaryDirectory': os.environ['ORTHANC_TMP_DIR'],
    'Plugins': [
        '/usr/share/orthanc/plugins/',
    ],

    'HttpServerEnabled': True,
    'HttpPort': int(os.environ['ORTHANC_HTTP_PORT']),
    'RemoteAccessAllowed': env_bool('ORTHANC_REMOTE_ACCESS_ALLOWED', True),

    'WebDavEnabled': False,
    'ExecuteLuaEnabled': False,
    'RestApiWriteToFileSystemEnabled': False,

    'DicomServerEnabled': True,
    'DicomAet': os.environ['ORTHANC_AET'],
    'DicomPort': int(os.environ['ORTHANC_DICOM_PORT']),
    'DicomCheckCalledAet': True,
    'DicomCheckModalityHost': env_bool('ORTHANC_DICOM_CHECK_MODALITY_HOST', False),
    'DicomAlwaysAllowEcho': False,
    'DicomAlwaysAllowStore': False,
    'DicomAlwaysAllowFind': False,
    'DicomAlwaysAllowGet': False,
    'DicomAlwaysAllowMove': False,
    'DicomModalities': modalities,

    'DefaultEncoding': 'Utf8',
    'StorageCompression': env_bool('ORTHANC_STORAGE_COMPRESSION', False),
    'OverwriteInstances': False,
    'StoreMD5ForAttachments': True,
    'LimitFindResults': int(os.environ['ORTHANC_LIMIT_FIND_RESULTS']),
    'LimitFindInstances': int(os.environ['ORTHANC_LIMIT_FIND_INSTANCES']),
    'MetricsEnabled': True,
}

credentials_cfg = {
    'AuthenticationEnabled': True,
    'RegisteredUsers': {
        os.environ['ORTHANC_ADMIN_USER']: os.environ['ORTHANC_ADMIN_PASSWORD'],
    },
}

postgresql_cfg = {
    'PostgreSQL': {
        'EnableIndex': True,
        'EnableStorage': False,
        'Host': '127.0.0.1',
        'Port': 5432,
        'Database': os.environ['ORTHANC_DB_NAME'],
        'Username': os.environ['ORTHANC_DB_USER'],
        'Password': os.environ['ORTHANC_DB_PASSWORD'],
        'Lock': True,
        'EnableSsl': False,
    },
}

dicomweb_cfg = {
    'DicomWeb': {
        'Enable': True,
        'Root': '/dicom-web/',
        'EnableWado': True,
        'WadoRoot': '/wado',
        'Host': 'localhost',
        'Ssl': False,
    },
}

for path, data in (
    (orthanc_config_path, orthanc_cfg),
    (credentials_config_path, credentials_cfg),
    (postgresql_config_path, postgresql_cfg),
    (dicomweb_config_path, dicomweb_cfg),
):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write('\n')
PY

rm -f "$LEGACY_CONFIG_FILE"
chown root:orthanc "$ORTHANC_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" "$POSTGRESQL_CONFIG_FILE" "$DICOMWEB_CONFIG_FILE"
chmod 0640 "$ORTHANC_CONFIG_FILE" "$CREDENTIALS_CONFIG_FILE" "$POSTGRESQL_CONFIG_FILE" "$DICOMWEB_CONFIG_FILE"

echo "[7/8] Installing helper scripts..."
install -m 0755 "$LOCAL_HEALTHCHECK_SCRIPT" "$INSTALL_HEALTHCHECK_PATH"
install -m 0755 "$LOCAL_START_SCRIPT" "$INSTALL_START_PATH"
install -m 0755 "$LOCAL_STOP_SCRIPT" "$INSTALL_STOP_PATH"
install -m 0755 "$LOCAL_RESTART_SCRIPT" "$INSTALL_RESTART_PATH"
install -m 0644 "$LOCAL_MODALITIES_EXAMPLE" "$INSTALL_MODALITIES_EXAMPLE_PATH"

echo "[8/8] Installing systemd mount dependency for Orthanc storage..."
install -d -m 0755 "$ORTHANC_SYSTEMD_OVERRIDE_DIR"
cat >"$ORTHANC_STORAGE_MOUNT_OVERRIDE_FILE" <<EOF
[Unit]
RequiresMountsFor=$ORTHANC_STORAGE_DIR $ORTHANC_TMP_DIR
EOF
systemctl daemon-reload

echo "[9/9] Enabling services at boot and restarting Orthanc..."
systemctl enable orthanc postgresql
"$INSTALL_RESTART_PATH"

echo
echo "Installation completed."
echo "Configuration files: $ORTHANC_CONFIG_FILE, $CREDENTIALS_CONFIG_FILE, $POSTGRESQL_CONFIG_FILE, $DICOMWEB_CONFIG_FILE"
echo "Helper scripts: $INSTALL_HEALTHCHECK_PATH, $INSTALL_START_PATH, $INSTALL_STOP_PATH, $INSTALL_RESTART_PATH"
echo "Orthanc logs: /var/log/orthanc/"
echo "Modalities example: $INSTALL_MODALITIES_EXAMPLE_PATH"
echo "Orthanc package source: $ORTHANC_PACKAGE_SOURCE"
echo "Orthanc minimum version enforced: $ORTHANC_MIN_VERSION"
echo "If you leave DicomModalities empty, Orthanc will reject C-STORE/C-FIND/C-MOVE from unknown modalities until you add valid IPs and restart the service."
