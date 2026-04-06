# Workflow: Export Local to ZIP

`orthanc-export-local-by-date.py` exporta estudos que já existem no Orthanc local em um ZIP por estudo.

## Entry points

- canônico: `python3 -m orthanc_tools export-local-to-zip`
- wrapper estável: `./scripts/workflows/orthanc-export-local-by-date.py`

## Exemplo

```bash
python3 -m orthanc_tools export-local-to-zip \
  --start-date 2024-01-01 \
  --end-date 2024-01-31 \
  --backup-dir ~/backup
```

## Quando usar

- exportar acervo já local
- gerar ZIPs sem depender de um PACS remoto
