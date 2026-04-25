# Workflow: Sync Remote

`orthanc-sync-remote.py` mirrors a remote PACS into the local Orthanc instance and tries to correct drift between both sides.

## Entry points

- canonical: `python3 -m orthanc_tools sync-remote`
- installed package: `orthanc-tools sync-remote`
- stable wrapper: `./scripts/workflows/orthanc-sync-remote.py`

`python3` must resolve to Python 3.10 or newer. If your system `python3` is older, use an explicit supported interpreter such as `python3.11`.

## Example

```bash
python3 -m orthanc_tools sync-remote --remote OSIRIX-LAN --yes
```

## Dry Run

Preview the remote/local inventory comparison without retrieving or deleting studies:

```bash
python3 -m orthanc_tools sync-remote \
  --dry-run \
  --remote OSIRIX-LAN \
  --repair-mode replace
```

Expected output includes the resolved Orthanc URL, remote modality, repair mode, retrieve method, target AE title, remote study count, local study count, studies already present locally, studies missing locally, and extra local studies.

When `--repair-mode replace` is selected, dry-run prints a destructive-mode warning. A real replace-mode run can delete drifted local studies before refill and delete extra local studies that are not present on the remote modality.

```text
Dry-run plan: sync-remote
Orthanc REST: http://localhost:8042
Remote modality: OSIRIX-LAN
Repair mode: replace
Retrieve method: get
Target AET: (not used for C-GET)
Inventory:
  Remote studies: 42
  Local studies: 39
  Present locally: 37
  Missing locally: 5
  Extra local studies: 2
WARNING: A real replace-mode run can delete drifted local studies before refill and delete extra local studies not present on the remote modality.
```

## When to use

- faithful mirroring of a remote modality
- continuous synchronization or drift repair
- validate remote/local inventory and destructive repair scope before a production sync
