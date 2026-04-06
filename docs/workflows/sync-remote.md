# Workflow: Sync Remote

`orthanc-sync-remote.py` espelha um PACS remoto em Orthanc local e tenta corrigir deriva entre os lados.

## Entry points

- canônico: `python3 -m orthanc_tools sync-remote`
- wrapper estável: `./scripts/workflows/orthanc-sync-remote.py`

## Exemplo

```bash
python3 -m orthanc_tools sync-remote --remote OSIRIX-LAN --yes
```

## Quando usar

- espelhamento fiel de um modality remoto
- sincronização contínua ou reparo de drift
