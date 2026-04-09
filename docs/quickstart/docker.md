# Quickstart: Docker

## Prerequisites

- Docker with Compose v2 (`docker compose`)
- Ports `8042` and `4242` available

## Start the stack

```bash
docker compose -f deploy/docker/compose.yaml up -d
```

## URLs

- Orthanc Explorer 2: `http://localhost:8042/ui/app/`
- OHIF: `http://localhost:8042/ohif/`
- DICOMweb: `http://localhost:8042/dicom-web/studies`

## Main files

- `deploy/docker/compose.yaml`
- `deploy/docker/orthanc/config/orthanc.json`
- `deploy/docker/orthanc/python/startup.py`
- `deploy/docker/orthanc/plugins/ohif/`
