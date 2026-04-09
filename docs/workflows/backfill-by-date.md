# Workflow: Backfill by Date

`orthanc-backfill-by-date.py` fills the local Orthanc instance day by day from a remote PACS, with resumable state.

## Entry points

- canonical: `python3 -m orthanc_tools backfill-by-date`
- stable wrapper: `./scripts/workflows/orthanc-backfill-by-date.py`

## Example

```bash
python3 -m orthanc_tools backfill-by-date \
  --start-date 2021-07-16 \
  --remote-aet REMOTE \
  --remote-host 127.0.0.1 \
  --remote-port 4242
```

## When to use

- populate the local Orthanc instance as an operational archive
- resume a long import from the saved point
