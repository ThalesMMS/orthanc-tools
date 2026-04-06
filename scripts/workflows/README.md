# Workflows

| Script | Usa Orthanc local? | Origem | Saída | Quando usar |
|---|---:|---|---|---|
| `orthanc-sync-remote.py` | Sim | PACS remoto | Orthanc espelhado | Mirror fiel |
| `orthanc-backfill-by-date.py` | Sim | PACS remoto | Orthanc preenchido | Backfill operacional |
| `orthanc-backfill-export-by-date.py` | Sim, como staging | PACS remoto | ZIP por estudo | Backup remoto para disco |
| `orthanc-export-local-by-date.py` | Sim | Orthanc local | ZIP por estudo | Exportar acervo já local |

Entry point canônico: `python3 -m orthanc_tools <subcomando>`.
