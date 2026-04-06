# Networking: Proxmox NAT Workaround

Os scripts em `scripts/networking/` são workarounds de ambiente, não parte do fluxo central do produto.

## Guest VM

- `./scripts/networking/vm-network-guest-fix.sh`

## Host Proxmox

- `./scripts/networking/proxmox-vm-nat-fix.sh`

Use somente quando houver problema real de conectividade entre VM e host e remova quando o workaround deixar de ser necessário.
