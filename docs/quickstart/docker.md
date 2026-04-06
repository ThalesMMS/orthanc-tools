# Quickstart: Docker

## Pré-requisitos

- Docker com Compose v2 (`docker compose`)
- Portas `8042` e `4242` livres

## Subir a stack

```bash
docker compose -f deploy/docker/compose.yaml up -d
```

## URLs

- Orthanc Explorer 2: `http://localhost:8042/ui/app/`
- OHIF: `http://localhost:8042/ohif/`
- DICOMweb: `http://localhost:8042/dicom-web/studies`

## Arquivos principais

- `deploy/docker/compose.yaml`
- `deploy/docker/orthanc/config/orthanc.json`
- `deploy/docker/orthanc/python/startup.py`
- `deploy/docker/orthanc/plugins/ohif/`
