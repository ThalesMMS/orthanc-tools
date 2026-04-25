# Workflow: Backfill by Date

`orthanc-backfill-by-date.py` fills the local Orthanc instance day by day from a remote PACS, with resumable state.

## Entry points

- canonical: `python3 -m orthanc_tools backfill-by-date`
- installed package: `orthanc-tools backfill-by-date`
- stable wrapper: `./scripts/workflows/orthanc-backfill-by-date.py`

`python3` must resolve to Python 3.10 or newer. If your system `python3` is older, use an explicit supported interpreter such as `python3.11`.

## Example

```bash
python3 -m orthanc_tools backfill-by-date \
  --start-date 2021-07-16 \
  --remote-aet REMOTE \
  --remote-host 127.0.0.1 \
  --remote-port 4242
```

## Dry Run

Preview the days and remote study counts without retrieving DICOM objects or writing workflow state:

```bash
python3 -m orthanc_tools backfill-by-date \
  --dry-run \
  --start-date 2021-07-16 \
  --end-date 2021-07-18 \
  --remote-name REMOTE \
  --remote-aet REMOTE \
  --remote-host 127.0.0.1 \
  --remote-port 4242 \
  --base-url http://127.0.0.1:8042 \
  --user orthanc \
  --password orthanc \
  --calling-aet ORTHANC
```

Expected output includes the resolved Orthanc URL, remote modality, date range, state directory, heuristic settings, per-day remote study counts, total remote studies, and a final message confirming that no data was retrieved and no state was written.

Dry-run is read-only: `--remote-name` must already exist in Orthanc `DicomModalities`; the workflow will not create or update a temporary modality in planning mode.

## When to use

- populate the local Orthanc instance as an operational archive
- resume a long import from the saved point
- validate PACS connectivity and planned scope before a production backfill
