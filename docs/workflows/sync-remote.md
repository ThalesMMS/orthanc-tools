# Workflow: Sync Remote

`orthanc-sync-remote.py` mirrors a remote PACS into the local Orthanc instance and tries to correct drift between both sides.

## Entry points

- canonical: `python3 -m orthanc_tools sync-remote`
- stable wrapper: `./scripts/workflows/orthanc-sync-remote.py`

## Example

```bash
python3 -m orthanc_tools sync-remote --remote OSIRIX-LAN --yes
```

## When to use

- faithful mirroring of a remote modality
- continuous synchronization or drift repair
