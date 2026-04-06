# Operations: Troubleshooting

## REST não responde

- rode `./scripts/orthanc/orthanc-diagnose-rest.sh`
- valide credenciais em `/etc/orthanc/credentials.json`

## Orthanc sobe, mas ainda não está pronto

- use `./scripts/orthanc/orthanc-start.sh` ou `./scripts/orthanc/orthanc-restart.sh`
- esses helpers aguardam o healthcheck antes de retornar sucesso

## Sync/backfill falha em um estudo

- rode o workflow de novo; o estado é resumível
- verifique logs no diretório de estado configurado
