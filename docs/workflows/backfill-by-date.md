# Workflow: Backfill by Date

`orthanc-backfill-by-date.py` preenche o Orthanc local dia a dia a partir de um PACS remoto, com estado resumível.

## Entry points

- canônico: `python3 -m orthanc_tools backfill-by-date`
- wrapper estável: `./scripts/workflows/orthanc-backfill-by-date.py`

## Exemplo

```bash
python3 -m orthanc_tools backfill-by-date \
  --start-date 2021-07-16 \
  --remote-aet REMOTE \
  --remote-host 127.0.0.1 \
  --remote-port 4242
```

## Quando usar

- popular o Orthanc local como acervo operacional
- retomar uma carga longa a partir do ponto salvo
