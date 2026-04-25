# Workflow: Backup Remote to ZIP

`orthanc-backfill-export-by-date.py` combines remote backfill by date with final ZIP export. The local Orthanc instance acts only as a temporary staging store.

## Entry points

- canonical: `python3 -m orthanc_tools backup-remote-to-zip`
- installed package: `orthanc-tools backup-remote-to-zip`
- stable wrapper: `./scripts/workflows/orthanc-backfill-export-by-date.py`

`python3` must resolve to Python 3.10 or newer. If your system `python3` is older, use an explicit supported interpreter such as `python3.11`.

## Example

```bash
python3 -m orthanc_tools backup-remote-to-zip \
  --start-date 2021-07-16 \
  --remote-aet REMOTE \
  --remote-host 127.0.0.1 \
  --remote-port 4242 \
  --backup-dir /data/backup \
  --state-dir /data/backup/.orthanc-remote-zip-backup-state
```

## Dry Run

Preview the days and remote study counts without retrieving DICOM objects, writing ZIPs, deleting staged studies, or writing workflow state:

```bash
python3 -m orthanc_tools backup-remote-to-zip \
  --dry-run \
  --start-date 2021-07-16 \
  --end-date 2021-07-18 \
  --remote-aet REMOTE \
  --remote-host 127.0.0.1 \
  --remote-port 4242 \
  --backup-dir /data/backup \
  --state-dir /data/backup/.orthanc-remote-zip-backup-state \
  --base-url http://127.0.0.1:8042 \
  --user orthanc \
  --password orthanc \
  --calling-aet ORTHANC
```

Expected output includes the resolved Orthanc URL, remote modality, date range, backup directory, state directory, ZIP naming mode, ZIP mode, per-day remote study counts, total remote studies, and a final message confirming that no DICOM data, ZIP files, local staging studies, or workflow state were changed.

## When to use

- direct remote backup to disk with one ZIP per study
- minimal local retention in Orthanc
- validate PACS connectivity, output paths, and planned scope before a production backup
