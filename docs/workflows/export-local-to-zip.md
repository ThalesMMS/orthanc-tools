# Workflow: Export Local to ZIP

`orthanc-export-local-by-date.py` exports studies that already exist in the local Orthanc instance into one ZIP per study.

## Entry points

- canonical: `python3 -m orthanc_tools export-local-to-zip`
- installed package: `orthanc-tools export-local-to-zip`
- stable wrapper: `./scripts/workflows/orthanc-export-local-by-date.py`

`python3` must resolve to Python 3.10 or newer. If your system `python3` is older, use an explicit supported interpreter such as `python3.11`.

## Example

```bash
python3 -m orthanc_tools export-local-to-zip \
  --start-date 2024-01-01 \
  --end-date 2024-01-31 \
  --backup-dir ~/backup
```

## Dry Run

Preview the local day inventory without writing ZIPs or workflow state:

```bash
python3 -m orthanc_tools export-local-to-zip \
  --dry-run \
  --start-date 2024-01-01 \
  --end-date 2024-01-31 \
  --backup-dir ~/backup
```

Expected output includes the resolved Orthanc URL, Orthanc version, date range, backup directory, state directory, ZIP naming mode, ZIP mode, per-day local study counts, total local studies, and a final message confirming that no ZIPs or workflow state were written.

```text
Dry-run plan: export-local-to-zip
Orthanc REST: http://localhost:8042
Orthanc version: 1.12.9
Date range: 2026-04-01 to 2026-04-07
Backup directory: /backups/orthanc
State directory: /state/orthanc-export
ZIP naming mode: uid
ZIP mode: stored
Local inventory:
  2026-04-01: 12 studies
  2026-04-02: 9 studies
Total local studies: 21
Dry-run complete. No ZIPs were written and no state was written.
```

## When to use

- export an already local archive
- generate ZIPs without depending on a remote PACS
- validate local inventory, output paths, and planned scope before a production export
