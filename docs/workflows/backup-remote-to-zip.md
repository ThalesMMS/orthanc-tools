# Workflow: Backup Remote to ZIP

`orthanc-backfill-export-by-date.py` combina backfill remoto por data com exportação final em ZIP. O Orthanc local funciona só como staging store temporário.

## Entry points

- canônico: `python3 -m orthanc_tools backup-remote-to-zip`
- wrapper estável: `./scripts/workflows/orthanc-backfill-export-by-date.py`

## Exemplo

```bash
python3 -m orthanc_tools backup-remote-to-zip \
  --start-date 2021-07-16 \
  --remote-aet REMOTE \
  --remote-host 127.0.0.1 \
  --remote-port 4242 \
  --backup-dir /data/backup \
  --state-dir /data/backup/.orthanc-remote-zip-backup-state
```

## Quando usar

- backup remoto direto para disco em um ZIP por estudo
- retenção local mínima no Orthanc
