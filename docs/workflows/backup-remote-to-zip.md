# Workflow: Backup Remote to ZIP

`orthanc-backfill-export-by-date.py` combines remote backfill by date with final ZIP export. The local Orthanc instance acts only as a temporary staging store.

## Entry points

- canonical: `python3 -m orthanc_tools backup-remote-to-zip`
- stable wrapper: `./scripts/workflows/orthanc-backfill-export-by-date.py`

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

## When to use

- direct remote backup to disk with one ZIP per study
- minimal local retention in Orthanc
