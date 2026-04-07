# orthanc-tools

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

`orthanc-tools` reúne deploy, operação e workflows de sincronização e backup para Orthanc.

Orthanc deployment, sync, PACS mirroring, and ZIP backup toolkit for Docker and native Ubuntu.

**Keywords:** Orthanc, DICOM, DICOMweb, PACS, Docker, Ubuntu, PostgreSQL, backup, sync, ZIP export.

**Quick links:** [Docker quickstart](docs/quickstart/docker.md) · [Native Ubuntu quickstart](docs/quickstart/native-ubuntu.md) · [Workflow docs](docs/workflows) · [Operations docs](docs/operations)

O repositório é voltado a operadores e desenvolvedores que precisam:

- subir um Orthanc rapidamente em Docker para testes locais
- instalar Orthanc nativo em Ubuntu com PostgreSQL
- espelhar um PACS remoto em Orthanc local
- fazer backfill remoto por data
- gerar ZIPs por estudo a partir de um PACS remoto ou de um Orthanc já populado

## What Is Included

- `deploy/docker`: stack Docker com Orthanc, Orthanc Explorer 2, DICOMweb, OHIF e plugin Python
- `deploy/native`: instalador e purge para Ubuntu 24.04
- `scripts/orthanc`: helpers operacionais para start, stop, restart, diagnose e limpeza
- `scripts/workflows`: entrypoints nomeados para sync, backfill e export
- `src/orthanc_tools`: CLI unificada e lógica Python compartilhada
- `docs/`: quickstarts, workflows, operações e networking

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

## Related ThalesMMS Projects

- [`Dicom-Tools`](https://github.com/ThalesMMS/Dicom-Tools): unified UI for testing multiple DICOM libraries and toolchains.
- [`OsiriX-Backup-Plugin`](https://github.com/ThalesMMS/OsiriX-Backup-Plugin): OsiriX/Horos backup plugin with integrity checks and remote PACS delivery.
- [`DICOM-Decoder`](https://github.com/ThalesMMS/DICOM-Decoder): Swift DICOM decoding core for metadata and pixel buffers.

## Community

- Contributing: [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md)
- Code of Conduct: [`.github/CODE_OF_CONDUCT.md`](.github/CODE_OF_CONDUCT.md)
- Security: [`.github/SECURITY.md`](.github/SECURITY.md)
- Support: [`.github/SUPPORT.md`](.github/SUPPORT.md)
