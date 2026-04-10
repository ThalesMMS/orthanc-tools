# orthanc-tools

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

`orthanc-tools` brings together deployment, operations, and sync/backup workflows for Orthanc.

Orthanc deployment, sync, PACS mirroring, and ZIP backup toolkit for Docker and native Ubuntu.

**Keywords:** Orthanc, DICOM, DICOMweb, PACS, Docker, Ubuntu, PostgreSQL, backup, sync, ZIP export.

**Quick links:** [Docker quickstart](docs/quickstart/docker.md) · [Native Ubuntu quickstart](docs/quickstart/native-ubuntu.md) · [Workflow docs](docs/workflows) · [Operations docs](docs/operations)

## Start Here in 30 Seconds

| If you want to... | Start with | What you get |
|---|---|---|
| Run a local Orthanc stack with a web UI | `docker compose -f deploy/docker/compose.yaml up -d` | Orthanc Explorer 2, OHIF, and DICOMweb on `localhost` |
| Install Orthanc natively on Ubuntu 24.04 | `sudo ./deploy/native/install-orthanc-native.sh` | Native Orthanc + PostgreSQL baseline |
| Run sync, backfill, or ZIP export workflows | `python3 -m orthanc_tools --help` | Unified CLI entry point for the repo workflows |

### Fastest path: local Docker stack

For local testing, the quickest way to see the project working is:

```bash
docker compose -f deploy/docker/compose.yaml up -d
```

Then open:

- Orthanc Explorer 2: `http://localhost:8042/ui/app/`
- OHIF: `http://localhost:8042/ohif/`
- DICOMweb studies endpoint: `http://localhost:8042/dicom-web/studies`

The repository is aimed at operators and developers who need to:

- bring up Orthanc quickly in Docker for local testing
- install native Orthanc on Ubuntu with PostgreSQL
- mirror a remote PACS into local Orthanc
- backfill remote studies by date
- generate one ZIP per study from a remote PACS or an already populated Orthanc instance

## What Is Included

- `deploy/docker`: Docker stack with Orthanc, Orthanc Explorer 2, DICOMweb, OHIF, and the Python plugin
- `deploy/native`: installer and purge scripts for Ubuntu 24.04
- `scripts/orthanc`: operational helpers for start, stop, restart, diagnose, and cleanup
- `scripts/workflows`: named entry points for sync, backfill, and export
- `src/orthanc_tools`: unified CLI and shared Python logic
- `docs/`: quickstarts, workflows, operations, and networking notes

## Getting Started

### Docker quickstart

```bash
docker compose -f deploy/docker/compose.yaml up -d
```

Docs: [`docs/quickstart/docker.md`](docs/quickstart/docker.md)

### Native Ubuntu

```bash
sudo ./deploy/native/install-orthanc-native.sh
```

Docs: [`docs/quickstart/native-ubuntu.md`](docs/quickstart/native-ubuntu.md)

### Unified CLI

```bash
python3 -m orthanc_tools <subcommand> ...
```

Available subcommands:

- `sync-remote`
- `backfill-by-date`
- `backup-remote-to-zip`
- `export-local-to-zip`

Stable named wrappers also exist in `scripts/workflows/`.

## Minimal Validation

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall src scripts orthanc_tools
find scripts deploy -type f \( -name '*.sh' -o -name '*.command' \) -print0 | xargs -0 -n1 bash -n
docker compose -f deploy/docker/compose.yaml config
```

## Support Matrix

| Area | Status |
|---|---|
| Native installer | validated for Ubuntu 24.04 |
| Docker stack | available through `deploy/docker/compose.yaml` |
| Python runtime | `>=3.10` |

## Stability Policy

Stable repository areas:

- `deploy/native`
- `deploy/docker`
- `scripts/orthanc`
- `scripts/workflows`
- `src/orthanc_tools`

Examples and automation helpers under `scripts/automation` are operational examples, not the main product surface.

## Security Note

The Docker configuration in [`deploy/docker/orthanc/config/orthanc.json`](deploy/docker/orthanc/config/orthanc.json) is tuned for fast local testing, not for long-term, shared, or exposed deployments.

At the moment it explicitly enables a permissive posture:

- `RemoteAccessAllowed: true`
- `AuthenticationEnabled: false`
- `RegisteredUsers: {"admin":"admin"}`
- `DicomAlwaysAllowEcho/Find/Move/Store: true`

Do not use these defaults unchanged for longer-lived environments. Review and harden them before any non-local use.

## Documentation Map

- Docker deployment: [`deploy/docker/README.md`](deploy/docker/README.md)
- Native deployment: [`deploy/native/README.md`](deploy/native/README.md)
- Workflow docs: [`docs/workflows`](docs/workflows)
- Operations: [`docs/operations`](docs/operations)
- Networking notes: [`docs/networking/proxmox-nat-workaround.md`](docs/networking/proxmox-nat-workaround.md)

## Related Projects

- [`Dicom-Tools`](https://github.com/ThalesMMS/Dicom-Tools): unified UI for testing multiple DICOM libraries and toolchains.
- [`OsiriX-Backup-Plugin`](https://github.com/ThalesMMS/OsiriX-Backup-Plugin): OsiriX/Horos backup plugin with integrity checks and remote PACS delivery.
- [`DICOM-Decoder`](https://github.com/ThalesMMS/DICOM-Decoder): Swift DICOM decoding core for metadata and pixel buffers.

## Community

- Contributing: [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md)
- Code of Conduct: [`.github/CODE_OF_CONDUCT.md`](.github/CODE_OF_CONDUCT.md)
- Security: [`.github/SECURITY.md`](.github/SECURITY.md)
- Support: [`.github/SUPPORT.md`](.github/SUPPORT.md)
