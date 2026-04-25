# orthanc-tools

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

`orthanc-tools` brings together deployment, operations, and sync/backup workflows for Orthanc.

Orthanc deployment, sync, PACS mirroring, and ZIP backup toolkit for Docker and native Ubuntu.

**Keywords:** Orthanc, DICOM, DICOMweb, PACS, Docker, Ubuntu, PostgreSQL, backup, sync, ZIP export.

**Quick links:** [Docker quickstart](docs/quickstart/docker.md) · [Native Ubuntu quickstart](docs/quickstart/native-ubuntu.md) · [Workflow docs](docs/workflows) · [Operations docs](docs/operations)

## Requirements

- Python `>=3.10` for the `orthanc_tools` package and workflow CLI.
- The CI matrix runs the Python validation suite on Python 3.10, 3.11, and 3.12.
- Command examples use `python3`, but that executable must resolve to Python 3.10 or newer. If your system `python3` is older, use an explicit supported interpreter such as `python3.11`.

The package metadata in [`pyproject.toml`](pyproject.toml) declares `requires-python = ">=3.10"`, so package installation enforces the same floor.

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

## Installation

There are two supported ways to consume the Python workflow surface from this repository.

### From a source checkout

Clone this repository, confirm that `python3` is Python 3.10 or newer, and run the module entry point directly:

```bash
python3 --version
python3 -m orthanc_tools --help
python3 -m orthanc_tools <subcommand> ...
```

This works from the repository root without installation because the top-level `orthanc_tools/` directory uses `__path__` to extend imports to `src/orthanc_tools/`, allowing development use from the repository root without installing the package.

### As an installed package

Install the local checkout with a supported interpreter:

```bash
python3 --version
python3 -m pip install .
orthanc-tools --help
orthanc-tools <subcommand> ...
```

The `orthanc-tools` command is provided by the `[project.scripts]` entry in [`pyproject.toml`](pyproject.toml).

Do not use `pip install orthanc-tools` from PyPI to consume this repository unless the release metadata explicitly says it was published from `ThalesMMS/orthanc-tools`. The `orthanc-tools` name exists on PyPI for a separate upstream project.

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

Use Python 3.10 or newer for validation. If `python3` is older on your machine, replace it with an explicit supported interpreter such as `python3.11`.

```bash
python3 --version
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

## Repository & Provenance

`ThalesMMS/orthanc-tools` is the development and staging repository for the current alpha package surface. The package metadata in [`pyproject.toml`](pyproject.toml) and [`CITATION.cff`](CITATION.cff) point to `https://github.com/ThalesMMS/orthanc-tools` as the intended public release and long-term citation target.

The current package version is `0.1.0`. This repository does not yet have Git tags or GitHub releases for that version; use [`CHANGELOG.md`](CHANGELOG.md) as the lightweight release-history surface until formal release artifacts are published.

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
- Release history: [`CHANGELOG.md`](CHANGELOG.md)

## Related Projects

- [`Dicom-Tools`](https://github.com/ThalesMMS/Dicom-Tools): unified UI for testing multiple DICOM libraries and toolchains.
- [`OsiriX-Backup-Plugin`](https://github.com/ThalesMMS/OsiriX-Backup-Plugin): OsiriX/Horos backup plugin with integrity checks and remote PACS delivery.
- [`DICOM-Decoder`](https://github.com/ThalesMMS/DICOM-Decoder): Swift DICOM decoding core for metadata and pixel buffers.

## Community

- Contributing: [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md)
- Code of Conduct: [`.github/CODE_OF_CONDUCT.md`](.github/CODE_OF_CONDUCT.md)
- Security: [`.github/SECURITY.md`](.github/SECURITY.md)
- Support: [`.github/SUPPORT.md`](.github/SUPPORT.md)
