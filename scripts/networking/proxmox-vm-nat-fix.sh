#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

trap 'echo "[ERROR] Failure at line ${LINENO}." >&2' ERR

usage() {
  cat <<'USAGE'
Usage:
  sudo ./proxmox-vm-nat-fix.sh [install|remove|status]

Optional variables:
  VM_NAT_IP=192.168.100.9
  VM_NAT_EGRESS_INTERFACE=vmbr0
  VM_NAT_SERVICE_NAME=vm-nat-fix
  VM_NAT_SYSCTL_FILE=/etc/sysctl.d/99-vm-nat.conf
USAGE
}

if [[ "${1:-}" =~ ^(-h|--help)$ ]]; then
  usage
  exit 0
fi

ACTION="${1:-install}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root (for example: sudo bash proxmox-vm-nat-fix.sh)." >&2
  exit 1
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

need_cmd systemctl
need_cmd install
need_cmd iptables
need_cmd sysctl
need_cmd rm

VM_NAT_IP="${VM_NAT_IP:-192.168.100.9}"
VM_NAT_EGRESS_INTERFACE="${VM_NAT_EGRESS_INTERFACE:-vmbr0}"
VM_NAT_SERVICE_NAME="${VM_NAT_SERVICE_NAME:-vm-nat-fix}"
VM_NAT_SYSCTL_FILE="${VM_NAT_SYSCTL_FILE:-/etc/sysctl.d/99-vm-nat.conf}"

HELPER_PATH="/usr/local/sbin/${VM_NAT_SERVICE_NAME}"
SERVICE_PATH="/etc/systemd/system/${VM_NAT_SERVICE_NAME}.service"

install_fix() {
  install -d -m 0755 /usr/local/sbin /etc/systemd/system /etc/sysctl.d

  cat >"$HELPER_PATH" <<EOF
#!/bin/sh
set -eu

case "\${1:-start}" in
  start)
    /usr/sbin/sysctl -w net.ipv4.ip_forward=1 >/dev/null
    /usr/sbin/iptables -C FORWARD -s ${VM_NAT_IP}/32 -j ACCEPT 2>/dev/null || /usr/sbin/iptables -I FORWARD 1 -s ${VM_NAT_IP}/32 -j ACCEPT
    /usr/sbin/iptables -C FORWARD -d ${VM_NAT_IP}/32 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || /usr/sbin/iptables -I FORWARD 1 -d ${VM_NAT_IP}/32 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    /usr/sbin/iptables -t nat -C POSTROUTING -s ${VM_NAT_IP}/32 -o ${VM_NAT_EGRESS_INTERFACE} -j MASQUERADE 2>/dev/null || /usr/sbin/iptables -t nat -I POSTROUTING 1 -s ${VM_NAT_IP}/32 -o ${VM_NAT_EGRESS_INTERFACE} -j MASQUERADE
    ;;
  stop)
    /usr/sbin/iptables -D FORWARD -s ${VM_NAT_IP}/32 -j ACCEPT 2>/dev/null || true
    /usr/sbin/iptables -D FORWARD -d ${VM_NAT_IP}/32 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
    /usr/sbin/iptables -t nat -D POSTROUTING -s ${VM_NAT_IP}/32 -o ${VM_NAT_EGRESS_INTERFACE} -j MASQUERADE 2>/dev/null || true
    ;;
  status)
    /usr/sbin/sysctl net.ipv4.ip_forward
    /usr/sbin/iptables -S FORWARD | grep ${VM_NAT_IP} || true
    /usr/sbin/iptables -t nat -S POSTROUTING | grep ${VM_NAT_IP} || true
    ;;
  *)
    exit 2
    ;;
esac
EOF
  chmod 0755 "$HELPER_PATH"

  cat >"$SERVICE_PATH" <<EOF
[Unit]
Description=NAT egress for ${VM_NAT_IP} via Proxmox
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${HELPER_PATH} start
ExecStop=${HELPER_PATH} stop

[Install]
WantedBy=multi-user.target
EOF

  cat >"$VM_NAT_SYSCTL_FILE" <<'EOF'
net.ipv4.ip_forward=1
EOF

  sysctl --system >/dev/null || true
  systemctl daemon-reload
  systemctl enable --now "${VM_NAT_SERVICE_NAME}.service"

  echo "Installed ${VM_NAT_SERVICE_NAME}.service for ${VM_NAT_IP} via ${VM_NAT_EGRESS_INTERFACE}."
  "$HELPER_PATH" status
}

remove_fix() {
  systemctl disable --now "${VM_NAT_SERVICE_NAME}.service" >/dev/null 2>&1 || true
  if [[ -x "$HELPER_PATH" ]]; then
    "$HELPER_PATH" stop || true
  fi
  rm -f "$SERVICE_PATH" "$HELPER_PATH" "$VM_NAT_SYSCTL_FILE"
  systemctl daemon-reload
  systemctl reset-failed "${VM_NAT_SERVICE_NAME}.service" >/dev/null 2>&1 || true
  echo "Removed ${VM_NAT_SERVICE_NAME}.service and NAT helper files."
}

show_status() {
  systemctl status "${VM_NAT_SERVICE_NAME}.service" --no-pager -l || true
  echo
  if [[ -x "$HELPER_PATH" ]]; then
    "$HELPER_PATH" status || true
  fi
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
