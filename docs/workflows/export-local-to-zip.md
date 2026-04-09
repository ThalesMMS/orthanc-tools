# Workflow: Export Local to ZIP

`orthanc-export-local-by-date.py` exports studies that already exist in the local Orthanc instance into one ZIP per study.

## Entry points

- canonical: `python3 -m orthanc_tools export-local-to-zip`
- stable wrapper: `./scripts/workflows/orthanc-export-local-by-date.py`

## Example

```bash
python3 -m orthanc_tools export-local-to-zip \
  --start-date 2024-01-01 \
  --end-date 2024-01-31 \
  --backup-dir ~/backup
```

## When to use

- export an already local archive
- generate ZIPs without depending on a remote PACS
