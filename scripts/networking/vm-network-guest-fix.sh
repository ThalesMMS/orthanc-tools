#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

trap 'echo "[ERROR] Failure at line ${LINENO}." >&2' ERR

usage() {
  cat <<'USAGE'
Usage:
  sudo ./vm-network-guest-fix.sh [install|remove|status]

Optional variables:
  VM_UPLINK_GATEWAY=192.168.100.2
  VM_UPLINK_INTERFACE=auto
  VM_DNS_SERVERS="1.1.1.1 8.8.8.8"
  VM_FORCE_APT_IPV4=true
  VM_UPLINK_SERVICE_NAME=vm-uplink-fix
USAGE
}

if [[ "${1:-}" =~ ^(-h|--help)$ ]]; then
  usage
  exit 0
fi

ACTION="${1:-install}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root (for example: sudo bash vm-network-guest-fix.sh)." >&2
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

detect_interface() {
  local detected
  detected="$(ip route show default 2>/dev/null | awk '/default/ {for (i=1; i<=NF; ++i) if ($i == "dev") {print $(i+1); exit}}')"
  if [[ -n "$detected" ]]; then
    printf '%s' "$detected"
    return 0
  fi
  detected="$(ip -o -4 addr show scope global up | awk '!/ lo / {print $2; exit}')"
  if [[ -n "$detected" ]]; then
    printf '%s' "$detected"
    return 0
  fi
  echo "Unable to auto-detect a network interface. Set VM_UPLINK_INTERFACE explicitly." >&2
  exit 1
}

need_cmd ip
need_cmd systemctl
need_cmd install
need_cmd rm

VM_UPLINK_GATEWAY="${VM_UPLINK_GATEWAY:-192.168.100.2}"
VM_UPLINK_INTERFACE="${VM_UPLINK_INTERFACE:-auto}"
VM_DNS_SERVERS="${VM_DNS_SERVERS:-1.1.1.1 8.8.8.8}"
VM_FORCE_APT_IPV4="$(bool_normalize "${VM_FORCE_APT_IPV4:-true}")"
VM_UPLINK_SERVICE_NAME="${VM_UPLINK_SERVICE_NAME:-vm-uplink-fix}"

HELPER_PATH="/usr/local/sbin/${VM_UPLINK_SERVICE_NAME}"
SERVICE_PATH="/etc/systemd/system/${VM_UPLINK_SERVICE_NAME}.service"
APT_FORCE_IPV4_PATH="/etc/apt/apt.conf.d/99force-ipv4"

install_fix() {
  local interface="$VM_UPLINK_INTERFACE"
  if [[ "$interface" == "auto" ]]; then
    interface="$(detect_interface)"
  fi

  install -d -m 0755 /usr/local/sbin /etc/systemd/system /etc/apt/apt.conf.d

  cat >"$HELPER_PATH" <<EOF
#!/bin/sh
set -eu
DNS_SERVERS="${VM_DNS_SERVERS}"
/usr/sbin/ip route replace default via "${VM_UPLINK_GATEWAY}" dev "${interface}"
# shellcheck disable=SC2086
/usr/bin/resolvectl dns "${interface}" \$DNS_SERVERS
EOF
  chmod 0755 "$HELPER_PATH"

  cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=Route guest traffic through Proxmox uplink
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${HELPER_PATH}

[Install]
WantedBy=multi-user.target
EOF

  if [[ "$VM_FORCE_APT_IPV4" == "true" ]]; then
    cat >"$APT_FORCE_IPV4_PATH" <<'EOF'
Acquire::ForceIPv4 "true";
EOF
  else
    rm -f "$APT_FORCE_IPV4_PATH"
  fi

  systemctl daemon-reload
  systemctl enable --now "${VM_UPLINK_SERVICE_NAME}.service"

  echo "Installed ${VM_UPLINK_SERVICE_NAME}.service on interface ${interface}."
  ip route show default
  resolvectl dns "$interface" || true
}

remove_fix() {
  systemctl disable --now "${VM_UPLINK_SERVICE_NAME}.service" >/dev/null 2>&1 || true
  rm -f "$SERVICE_PATH" "$HELPER_PATH" "$APT_FORCE_IPV4_PATH"
  systemctl daemon-reload
  systemctl reset-failed "${VM_UPLINK_SERVICE_NAME}.service" >/dev/null 2>&1 || true
  echo "Removed ${VM_UPLINK_SERVICE_NAME}.service and helper files."
}

show_status() {
  local interface="$VM_UPLINK_INTERFACE"
  if [[ "$interface" == "auto" ]]; then
    interface="$(detect_interface)"
  fi
  systemctl status "${VM_UPLINK_SERVICE_NAME}.service" --no-pager -l || true
  echo
  ip route show default || true
  echo
  resolvectl dns "$interface" || true
}

case "$ACTION" in
  install)
    install_fix
    ;;
  remove)
    remove_fix
    ;;
  status)
    show_status
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
