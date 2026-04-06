# Native Ubuntu

Instalação nativa de Orthanc em Ubuntu com PostgreSQL local.

## Arquivos canônicos

- `deploy/native/install-orthanc-native.sh`
- `deploy/native/purge-orthanc-native.sh`
- `deploy/native/examples/orthanc-modalities.example.json`
- `scripts/orthanc/orthanc-*.sh`

## Instalar

```bash
sudo ./deploy/native/install-orthanc-native.sh
```

## Validar

```bash
sudo ./scripts/orthanc/orthanc-healthcheck.sh
```

## Remover

```bash
sudo ./deploy/native/purge-orthanc-native.sh
```

## Observações

- o instalador continua instalando helpers em `/usr/local/sbin`
- a configuração continua em `/etc/orthanc`
- a árvore do repositório mudou; o layout instalado no host não mudou
