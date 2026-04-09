# Workflows

| Script | Uses local Orthanc? | Source | Output | When to use |
|---|---:|---|---|---|
| `orthanc-sync-remote.py` | Yes | Remote PACS | Mirrored Orthanc | Faithful mirror |
| `orthanc-backfill-by-date.py` | Yes | Remote PACS | Filled Orthanc | Operational backfill |
| `orthanc-backfill-export-by-date.py` | Yes, as staging | Remote PACS | ZIP per study | Remote backup to disk |
| `orthanc-export-local-by-date.py` | Yes | Local Orthanc | ZIP per study | Export an already local archive |

Canonical entry point: `python3 -m orthanc_tools <subcommand>`.
