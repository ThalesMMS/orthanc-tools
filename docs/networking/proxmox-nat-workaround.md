# Networking: Proxmox NAT Workaround

The scripts in `scripts/networking/` are environment-specific workarounds, not part of the main product workflow.

## Guest VM

- `./scripts/networking/vm-network-guest-fix.sh`

## Proxmox host

- `./scripts/networking/proxmox-vm-nat-fix.sh`

Use them only when there is a real connectivity problem between the VM and the host, and remove them when the workaround is no longer needed.
