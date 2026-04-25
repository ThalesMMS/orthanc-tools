# Changelog

This project follows a lightweight [Keep a Changelog](https://keepachangelog.com/) style until formal Git tags and GitHub releases are introduced.

## [0.1.0] - Unreleased alpha

Status: initial development release surface, not yet a formal GitHub release in `ThalesMMS/orthanc-tools`.

### Added

- Package name: `orthanc-tools`.
- Import package: `orthanc_tools`.
- Console script after local installation: `orthanc-tools`.
- Unified CLI subcommands:
  - `sync-remote`
  - `backfill-by-date`
  - `backup-remote-to-zip`
  - `export-local-to-zip`
- Supported deployment and consumption modes:
  - Docker stack through `deploy/docker/compose.yaml`.
  - Native Ubuntu deployment through `deploy/native`.
  - Python CLI workflows from a source checkout or local package installation.

### Requirements

- Supported Python floor: Python 3.10 or newer.
- CI validation matrix: Python 3.10, 3.11, and 3.12.

### Provenance

- Development/staging repository: `ThalesMMS/orthanc-tools`
- Intended public release and citation target: `ThalesMMS/orthanc-tools`
- Until a formal release tag exists, cite a source snapshot by recording the repository, commit SHA, package version, and this changelog entry.

### Reproducible consumer setup

From a source checkout:

```bash
python3 --version
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m orthanc_tools --help
```

As an installed local package:

```bash
python3 --version
python3 -m pip install .
orthanc-tools --help
```

In both cases, `python3` must resolve to Python 3.10 or newer. If it does not, use an explicit supported interpreter such as `python3.11`.

### Release checklist for the first formal release

- Confirm `pyproject.toml` version, Python classifiers, and `requires-python` agree.
- Run the validation commands in `README.md` on a supported interpreter.
- Create a signed or annotated Git tag for the released version.
- Publish GitHub release notes that include the commit SHA and artifact source.
- Confirm citation metadata points at the intended public release repository.
