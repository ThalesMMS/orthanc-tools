# Native Ubuntu

Native Orthanc installation on Ubuntu with local PostgreSQL.

## Canonical files

- `deploy/native/install-orthanc-native.sh`
- `deploy/native/purge-orthanc-native.sh`
- `deploy/native/examples/orthanc-modalities.example.json`
- `scripts/orthanc/orthanc-*.sh`

## Install

```bash
sudo ./deploy/native/install-orthanc-native.sh
```

## Validate

```bash
sudo ./scripts/orthanc/orthanc-healthcheck.sh
```

## Remove

```bash
sudo ./deploy/native/purge-orthanc-native.sh
```

## Notes

- the installer still places helpers in `/usr/local/sbin`
- the configuration still lives in `/etc/orthanc`
- the repository tree changed; the layout installed on the host did not change
